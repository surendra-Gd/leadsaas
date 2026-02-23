"""
bot.py — LeadGenBot class
Encapsulates all Google Places search logic, storage, and state management.
Consumed by main.py (FastAPI) and can also be run standalone.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from threading import Lock
from typing import Optional

import requests
import gspread
from google.oauth2.service_account import Credentials

from config import ConfigManager, AppConfig

log = logging.getLogger("bot")

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedPhoneNumber,"
    "places.websiteUri,"
    "places.rating"
)
SHEET_HEADERS = [
    "place_id", "name", "phone", "rating",
    "category", "location", "discovered_at",
]
RETRY_DELAYS = [5, 15, 30]

FALLBACK_CSV = Path("data/leads.csv")
STATUS_FILE  = Path("data/status.json")


# ─────────────────────────────────────────────────────────────
# LeadGenBot
# ─────────────────────────────────────────────────────────────

class LeadGenBot:
    """
    Single-class lead generation engine.

    All mutable runtime state is stored in memory and persisted to
    data/status.json so the FastAPI layer can read/write it safely.
    """

    def __init__(self, config_manager: ConfigManager):
        self._cfg_mgr   = config_manager
        self._lock       = Lock()           # protects metrics + status writes
        self._running    = False
        self._status: dict = {}             # category+location progress
        self._metrics: dict = self._blank_metrics()
        self._existing_ids: set[str] = set()
        self._ws = None                     # gspread worksheet handle

        # Ensure data dir exists
        Path("data").mkdir(exist_ok=True)
        self._load_status()
        self._load_metrics()

    # ── Public properties ──────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_city(self) -> str:
        return self._cfg_mgr.config.active_city

    # ── Config shortcuts ───────────────────────────────────────

    @property
    def cfg(self) -> AppConfig:
        return self._cfg_mgr.config

    # ── Metrics ────────────────────────────────────────────────

    def _blank_metrics(self) -> dict:
        return {
            "today": str(date.today()),
            "leads_today": 0,
            "leads_total": 0,
            "api_calls_today": 0,
            "api_calls_total": 0,
            "last_run_at": None,
            "last_run_new_leads": 0,
        }

    def _load_metrics(self):
        metrics_file = Path("data/metrics.json")
        if metrics_file.exists():
            with open(metrics_file) as f:
                saved = json.load(f)
            # Reset daily counters if date rolled over
            if saved.get("today") != str(date.today()):
                saved["leads_today"] = 0
                saved["api_calls_today"] = 0
                saved["today"] = str(date.today())
            self._metrics = saved
        else:
            self._metrics = self._blank_metrics()

    def _save_metrics(self):
        with open("data/metrics.json", "w") as f:
            json.dump(self._metrics, f, indent=2)

    def _increment_api_call(self):
        with self._lock:
            self._metrics["api_calls_today"] += 1
            self._metrics["api_calls_total"] += 1
            self._save_metrics()

    def _increment_lead(self):
        with self._lock:
            self._metrics["leads_today"] += 1
            self._metrics["leads_total"] += 1
            self._save_metrics()

    def get_metrics(self) -> dict:
        self._load_metrics()   # refresh from disk (handles multi-process)
        return dict(self._metrics)

    # ── Status / state management ──────────────────────────────

    def _load_status(self):
        if STATUS_FILE.exists():
            with open(STATUS_FILE) as f:
                self._status = json.load(f)
        else:
            self._status = {}

    def _save_status(self):
        with open(STATUS_FILE, "w") as f:
            json.dump(self._status, f, indent=2)

    def get_status(self) -> dict:
        self._load_status()
        return dict(self._status)

    def reset_status(self, key: Optional[str] = None):
        """Reset one pair or all pairs to allow re-scraping."""
        self._load_status()
        if key:
            self._status.pop(key, None)
        else:
            self._status = {}
        self._save_status()

    @staticmethod
    def _status_key(category: str, location: str) -> str:
        return (
            f"{category.replace(' ', '_')}_"
            f"{location.replace(', ', '_').replace(' ', '_')}"
        )

    # ── Google Sheets ──────────────────────────────────────────

    def _connect_sheets(self) -> bool:
        """Try to connect to Google Sheets. Returns True on success."""
        cfg = self.cfg
        if not cfg.use_google_sheets:
            return False
        try:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(
                cfg.service_account_file, scopes=scopes
            )
            gc = gspread.authorize(creds)
            spreadsheet = gc.open_by_key(cfg.google_sheet_id)
            self._ws = self._get_or_create_worksheet(spreadsheet, "Leads")
            self._existing_ids = self._load_ids_from_sheet()
            log.info(f"Sheets connected. Existing leads: {len(self._existing_ids)}")
            return True
        except Exception as e:
            log.error(f"Sheets connection failed: {e}. Using CSV fallback.")
            self._ws = None
            self._existing_ids = self._load_ids_from_csv()
            return False

    def _get_or_create_worksheet(self, spreadsheet, title: str):
        try:
            ws = spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=title, rows=5000, cols=20)
            ws.append_row(SHEET_HEADERS)
            log.info(f"Created worksheet: {title}")
        return ws

    def _load_ids_from_sheet(self) -> set[str]:
        if not self._ws:
            return set()
        records = self._ws.get_all_values()
        return {row[0] for row in records[1:] if row and row[0]}

    def _save_lead_to_sheet(self, lead: dict) -> bool:
        if lead["place_id"] in self._existing_ids:
            return False
        row = [
            lead["place_id"], lead["name"], lead.get("phone", ""),
            lead.get("rating", ""), lead["category"], lead["location"],
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        ]
        self._ws.append_row(row)
        self._existing_ids.add(lead["place_id"])
        return True

    # ── CSV fallback ───────────────────────────────────────────

    def _load_ids_from_csv(self) -> set[str]:
        if not FALLBACK_CSV.exists():
            return set()
        with open(FALLBACK_CSV, newline="", encoding="utf-8") as f:
            return {row["place_id"] for row in csv.DictReader(f) if row.get("place_id")}

    def _save_lead_to_csv(self, lead: dict) -> bool:
        if lead["place_id"] in self._existing_ids:
            return False
        FALLBACK_CSV.parent.mkdir(exist_ok=True)
        file_exists = FALLBACK_CSV.exists()
        with open(FALLBACK_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SHEET_HEADERS)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "place_id":      lead["place_id"],
                "name":          lead["name"],
                "phone":         lead.get("phone", ""),
                "rating":        lead.get("rating", ""),
                "category":      lead["category"],
                "location":      lead["location"],
                "discovered_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            })
        self._existing_ids.add(lead["place_id"])
        return True

    def _save_lead(self, lead: dict) -> bool:
        """Route lead to Sheets or CSV."""
        if self._ws:
            return self._save_lead_to_sheet(lead)
        return self._save_lead_to_csv(lead)

    # ── Google Places API ──────────────────────────────────────

    def _call_places_api(self, query: str, page_token: Optional[str] = None) -> Optional[dict]:
        """POST to places.searchText with strict field mask + retry logic."""
        headers = {
            "Content-Type":    "application/json",
            "X-Goog-Api-Key":  self.cfg.google_places_api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }
        body: dict = {
            "textQuery":      query,
            "maxResultCount": self.cfg.max_results_per_query,
            "languageCode":   "en",
        }
        if page_token:
            body["pageToken"] = page_token

        self._increment_api_call()

        for attempt, delay in enumerate([0] + RETRY_DELAYS):
            if delay:
                log.warning(f"Rate-limit hit. Retry in {delay}s (attempt {attempt})…")
                time.sleep(delay)
            try:
                resp = requests.post(
                    PLACES_SEARCH_URL, headers=headers, json=body, timeout=30
                )
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    continue
                if resp.status_code in (400, 401, 403):
                    log.error(f"Auth error {resp.status_code}: {resp.text[:200]}")
                    return None
                log.warning(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except requests.RequestException as e:
                log.error(f"Request exception: {e}")

        log.error(f"All retries exhausted for: {query}")
        return None

    @staticmethod
    def _extract_lead(place: dict, category: str, location: str) -> Optional[dict]:
        """Return a lead dict only if the place has no website."""
        if place.get("websiteUri"):
            return None
        place_id = place.get("id", "")
        if not place_id:
            return None
        display = place.get("displayName", {})
        name = display.get("text", "Unknown") if isinstance(display, dict) else str(display)
        return {
            "place_id": place_id,
            "name":     name,
            "phone":    place.get("formattedPhoneNumber", ""),
            "rating":   place.get("rating", ""),
            "category": category,
            "location": location,
        }

    def _fetch_leads_for_pair(self, category: str, location: str) -> list[dict]:
        """Paginate Places API for one category+location pair."""
        query      = f"{category} in {location}"
        leads: list[dict] = []
        page_token = None

        for page in range(1, self.cfg.max_pages_per_query + 1):
            time.sleep(self.cfg.rate_limit_delay)
            data = self._call_places_api(query, page_token)
            if not data:
                break
            places = data.get("places", [])
            if not places:
                log.info(f"  No results on page {page} for '{query}'")
                break
            for place in places:
                lead = self._extract_lead(place, category, location)
                if lead:
                    leads.append(lead)
            log.info(f"  [{category} / {location}] page {page}: "
                     f"{len(places)} results → {len(leads)} qualified leads")
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return leads

    # ── Read leads back (for /leads endpoint) ─────────────────

    def read_leads(self, limit: int = 200) -> list[dict]:
        """Return leads from Sheets or CSV."""
        if self._ws:
            try:
                rows = self._ws.get_all_values()
                if len(rows) <= 1:
                    return []
                headers = rows[0]
                return [dict(zip(headers, r)) for r in rows[1:limit + 1]]
            except Exception as e:
                log.error(f"Sheets read error: {e}")

        # CSV fallback
        if not FALLBACK_CSV.exists():
            return []
        with open(FALLBACK_CSV, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))[:limit]

    # ── City switch ────────────────────────────────────────────

    def set_active_city(self, city: str):
        """Update active_city in config and persist."""
        self._cfg_mgr.update(active_city=city)
        log.info(f"Active city switched to: {city}")

    # ── Main run ───────────────────────────────────────────────

    def run(self):
        """
        Full scrape run — iterates targeting matrix, respects daily_lead_limit,
        resumes from first non-Done pair, and persists progress.
        """
        if self._running:
            log.warning("Bot is already running. Ignoring duplicate run().")
            return

        self._running = True
        cfg = self.cfg
        log.info("=" * 60)
        log.info("LeadGenBot run started")
        log.info(f"Active city filter: {cfg.active_city or 'ALL'}")
        log.info(f"Daily lead limit: {cfg.daily_lead_limit}")
        log.info("=" * 60)

        self._load_status()
        self._load_metrics()
        self._connect_sheets()

        total_new      = 0
        leads_today    = self._metrics.get("leads_today", 0)
        daily_limit    = cfg.daily_lead_limit

        try:
            for category in cfg.business_categories:
                for location in cfg.locations:
                    # Honour active_city filter
                    if cfg.active_city and cfg.active_city.lower() not in location.lower():
                        continue

                    if leads_today >= daily_limit:
                        log.info(f"Daily lead limit ({daily_limit}) reached. Stopping.")
                        return

                    key = self._status_key(category, location)
                    if self._status.get(key) == "Done":
                        log.info(f"Skip (Done): {key}")
                        continue

                    try:
                        leads = self._fetch_leads_for_pair(category, location)
                        saved = 0
                        for lead in leads:
                            if leads_today >= daily_limit:
                                break
                            if self._save_lead(lead):
                                saved += 1
                                leads_today += 1
                                self._increment_lead()

                        log.info(f"  ✓ {key}: {len(leads)} leads found, {saved} new saved")
                        total_new += saved

                        with self._lock:
                            self._status[key] = "Done"
                            self._save_status()

                    except Exception as e:
                        log.error(f"Error on {key}: {e}")
                        with self._lock:
                            self._status[key] = f"Error: {str(e)[:120]}"
                            self._save_status()

        finally:
            self._running = False
            with self._lock:
                self._metrics["last_run_at"]        = datetime.utcnow().isoformat()
                self._metrics["last_run_new_leads"]  = total_new
                self._save_metrics()

            log.info("=" * 60)
            log.info(f"Run complete. New leads this run: {total_new}")
            log.info("=" * 60)


# ── Standalone entry point ─────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("data/lead_gen.log")],
    )
    from config import ConfigManager
    bot = LeadGenBot(ConfigManager())
    bot.run()
