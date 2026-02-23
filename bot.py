"""
bot.py — LeadGenBot  (v3 — Webhook Edition)

Architecture
------------
  Google Places API  →  LeadGenBot  →  HTTP POST  →  Google Apps Script  →  Google Sheet
                                   ↘  CSV fallback (data/leads.csv)

What changed from v2
--------------------
  REMOVED:
    • gspread import
    • google.oauth2.service_account import
    • _connect_sheets()        — no more service-account handshake
    • _get_or_create_worksheet()
    • _load_ids_from_sheet()
    • _save_lead_to_sheet()

  ADDED:
    • ApiRateLimiter            — thread-safe sliding-window rate limiter
    • _save_lead_via_webhook()  — HTTP POST to Apps Script Web App
    • _load_ids_from_csv()      — seed in-memory dedup set from local CSV
    • _save_lead_to_csv()       — local CSV backup for every successfully
                                  posted lead (audit trail + offline dedup)

  KEPT INTACT:
    • Pagination logic
    • status.json  progress tracking
    • metrics.json daily / total counters
    • In-memory duplicate prevention  (_existing_ids)
    • Daily lead limit
    • Active city filtering
    • Retry logic (429 / network errors)
    • CSV read-back for /leads endpoint
"""

from __future__ import annotations

import collections
import csv
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from threading import Lock
from typing import Optional

import requests

from config import ConfigManager, AppConfig

log = logging.getLogger("bot")

# ─────────────────────────────────────────────────────────────
# MODULE-LEVEL CONSTANTS
# ─────────────────────────────────────────────────────────────

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Strict field mask — only request what we need to avoid higher billing tiers.
# places.id + places.displayName         → Essentials SKU
# places.formattedPhoneNumber            → Pro SKU
# places.websiteUri + places.rating      → Enterprise SKU
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedPhoneNumber,"
    "places.websiteUri,"
    "places.rating"
)

CSV_HEADERS = [
    "place_id", "name", "phone", "rating",
    "category", "location", "discovered_at",
]

# Retry back-off delays (seconds) for 429 / transient errors
RETRY_DELAYS = [5, 15, 30]

# File paths (all under data/ to keep working dir clean)
FALLBACK_CSV = Path("data/leads.csv")
STATUS_FILE  = Path("data/status.json")
METRICS_FILE = Path("data/metrics.json")


# ─────────────────────────────────────────────────────────────
# ApiRateLimiter — thread-safe sliding-window limiter
# ─────────────────────────────────────────────────────────────

class ApiRateLimiter:
    """
    Sliding-window rate limiter for Google Places API calls.

    Tracks the timestamps of recent calls in a deque.
    Before each call, removes timestamps older than 60 seconds,
    then blocks until the window has room for one more call.

    Thread-safe: all mutations are protected by a Lock.

    Usage
    -----
        limiter = ApiRateLimiter(max_calls_per_minute=30)
        limiter.acquire()          # blocks if window is full
        response = requests.post(...)
    """

    _WINDOW_SECONDS = 60

    def __init__(self, max_calls_per_minute: int) -> None:
        self._max    = max_calls_per_minute
        self._lock   = Lock()
        self._calls: collections.deque[float] = collections.deque()

    def update_limit(self, new_limit: int) -> None:
        """Allow live updates when config changes mid-run."""
        with self._lock:
            self._max = new_limit

    def acquire(self) -> None:
        """
        Block until a call slot is available within the current 60-second window.
        Logs a warning whenever it has to sleep.
        """
        while True:
            with self._lock:
                now     = time.monotonic()
                cutoff  = now - self._WINDOW_SECONDS

                # Evict timestamps that have fallen outside the window
                while self._calls and self._calls[0] < cutoff:
                    self._calls.popleft()

                if len(self._calls) < self._max:
                    self._calls.append(now)
                    return  # slot available — proceed immediately

                # Window is full: calculate how long to sleep
                oldest     = self._calls[0]
                sleep_secs = (oldest + self._WINDOW_SECONDS) - now + 0.05

            log.warning(
                "API rate limit reached (%d calls/min). "
                "Sleeping %.1fs before next call.",
                self._max, sleep_secs,
            )
            time.sleep(max(sleep_secs, 0))


# ─────────────────────────────────────────────────────────────
# LeadGenBot
# ─────────────────────────────────────────────────────────────

class LeadGenBot:
    """
    Webhook-based lead generation engine.

    Flow per run():
        1. Load status.json  (resume from last non-Done pair)
        2. Load metrics.json (daily counters; reset if date rolled over)
        3. Seed _existing_ids from leads.csv  (in-memory dedup)
        4. For each (category × location) pair not yet Done:
             a. Paginate Places API  →  filter leads with no website
             b. For each new lead:
                  • POST JSON to Apps Script webhook
                  • If webhook succeeds: append row to leads.csv
                  • Increment counters; respect daily_lead_limit
             c. Mark pair Done in status.json
        5. Write final metrics
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self._cfg_mgr = config_manager

        # Guards metrics/status writes and the rate-limiter's deque
        self._lock    = Lock()

        self._running = False
        self._stop_requested = False        # soft stop flag from API

        self._status: dict[str, str] = {}
        self._metrics: dict           = self._blank_metrics()
        self._existing_ids: set[str] = set()

        # Rate limiter — seeded from current config; updated before each run
        cfg = self._cfg_mgr.config
        self._rate_limiter = ApiRateLimiter(cfg.max_api_calls_per_minute)

        # Run-level call counter (reset each run() invocation)
        self._run_api_calls = 0

        Path("data").mkdir(exist_ok=True)
        self._load_status()
        self._load_metrics()

    # ─────────────────────────────────────────────────────────
    # Public properties
    # ─────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_city(self) -> str:
        return self._cfg_mgr.config.active_city

    @property
    def cfg(self) -> AppConfig:
        return self._cfg_mgr.config

    # ─────────────────────────────────────────────────────────
    # Metrics
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _blank_metrics() -> dict:
        return {
            "today":               str(date.today()),
            "leads_today":         0,
            "leads_total":         0,
            "api_calls_today":     0,
            "api_calls_total":     0,
            "webhook_errors":      0,
            "last_run_at":         None,
            "last_run_new_leads":  0,
        }

    def _load_metrics(self) -> None:
        if METRICS_FILE.exists():
            with open(METRICS_FILE) as fh:
                saved = json.load(fh)
            # Reset daily counters if the date has rolled over
            if saved.get("today") != str(date.today()):
                saved["leads_today"]     = 0
                saved["api_calls_today"] = 0
                saved["today"]           = str(date.today())
            self._metrics = saved
        else:
            self._metrics = self._blank_metrics()

    def _save_metrics(self) -> None:
        """Write metrics to disk. Must be called while holding self._lock."""
        with open(METRICS_FILE, "w") as fh:
            json.dump(self._metrics, fh, indent=2)

    def _increment_api_call(self) -> None:
        with self._lock:
            self._metrics["api_calls_today"] += 1
            self._metrics["api_calls_total"] += 1
            self._save_metrics()
        self._run_api_calls += 1

    def _increment_lead(self) -> None:
        with self._lock:
            self._metrics["leads_today"] += 1
            self._metrics["leads_total"] += 1
            self._save_metrics()

    def _increment_webhook_error(self) -> None:
        with self._lock:
            self._metrics["webhook_errors"] = self._metrics.get("webhook_errors", 0) + 1
            self._save_metrics()

    def get_metrics(self) -> dict:
        """Return a copy of current metrics (re-reads from disk first)."""
        self._load_metrics()
        return dict(self._metrics)

    # ─────────────────────────────────────────────────────────
    # Status / progress tracking
    # ─────────────────────────────────────────────────────────

    def _load_status(self) -> None:
        if STATUS_FILE.exists():
            with open(STATUS_FILE) as fh:
                self._status = json.load(fh)
        else:
            self._status = {}

    def _save_status(self) -> None:
        """Write status to disk. Call while holding self._lock."""
        with open(STATUS_FILE, "w") as fh:
            json.dump(self._status, fh, indent=2)

    def get_status(self) -> dict:
        self._load_status()
        return dict(self._status)

    def reset_status(self, key: Optional[str] = None) -> None:
        """Reset one pair (or all pairs) back to Pending."""
        self._load_status()
        if key:
            self._status.pop(key, None)
        else:
            self._status = {}
        self._save_status()

    def request_stop(self) -> None:
        """Signal a graceful stop after the current pair finishes."""
        self._stop_requested = True
        log.info("Stop requested — bot will halt after the current pair.")

    @staticmethod
    def _status_key(category: str, location: str) -> str:
        return (
            f"{category.replace(' ', '_')}_"
            f"{location.replace(', ', '_').replace(' ', '_')}"
        )

    # ─────────────────────────────────────────────────────────
    # CSV (local backup + dedup seed + /leads read-back)
    # ─────────────────────────────────────────────────────────

    def _load_ids_from_csv(self) -> set[str]:
        """
        Seed the in-memory dedup set from leads.csv.
        Called once at the start of each run().
        """
        if not FALLBACK_CSV.exists():
            return set()
        try:
            with open(FALLBACK_CSV, newline="", encoding="utf-8") as fh:
                return {
                    row["place_id"]
                    for row in csv.DictReader(fh)
                    if row.get("place_id")
                }
        except Exception as exc:
            log.error("Failed to read leads.csv for dedup seeding: %s", exc)
            return set()

    def _append_lead_to_csv(self, lead: dict) -> None:
        """
        Append one lead row to leads.csv.
        Creates the file with headers if it doesn't exist yet.
        """
        FALLBACK_CSV.parent.mkdir(exist_ok=True)
        file_exists = FALLBACK_CSV.exists()
        try:
            with open(FALLBACK_CSV, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
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
        except OSError as exc:
            log.error("Could not write to leads.csv: %s", exc)

    def read_leads(self, limit: int = 200) -> list[dict]:
        """Return leads from leads.csv for the /leads API endpoint."""
        if not FALLBACK_CSV.exists():
            return []
        try:
            with open(FALLBACK_CSV, newline="", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))[:limit]
        except Exception as exc:
            log.error("Error reading leads.csv: %s", exc)
            return []

    # ─────────────────────────────────────────────────────────
    # Webhook storage
    # ─────────────────────────────────────────────────────────

    def _save_lead_via_webhook(self, lead: dict) -> bool:
        """
        POST a single lead to the Google Apps Script Web App webhook.

        The Apps Script receives:
            {
              "place_id": "...",
              "name":     "...",
              "phone":    "...",
              "rating":   "...",
              "category": "...",
              "location": "..."
            }

        Returns True if the webhook accepted the lead (HTTP 200),
        False on any failure (logged but not raised).

        On success the lead is also written to leads.csv as a local
        audit trail and offline dedup seed.
        """
        webhook_url = self.cfg.google_sheets_webhook_url
        if not webhook_url:
            log.warning(
                "No webhook URL configured — lead '%s' saved to CSV only.",
                lead.get("name"),
            )
            self._append_lead_to_csv(lead)
            return True   # treat as success; CSV is the fallback

        payload = {
            "place_id": lead["place_id"],
            "name":     lead["name"],
            "phone":    lead.get("phone", ""),
            "rating":   str(lead.get("rating", "")),
            "category": lead["category"],
            "location": lead["location"],
        }

        for attempt, delay in enumerate([0] + RETRY_DELAYS):
            if delay:
                log.warning(
                    "Webhook retry %d in %ds for lead '%s'.",
                    attempt, delay, lead.get("name"),
                )
                time.sleep(delay)
            try:
                resp = requests.post(
                    webhook_url,
                    json=payload,
                    timeout=15,
                    # Apps Script redirects; follow them
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    log.debug("Webhook OK for '%s'.", lead.get("name"))
                    self._append_lead_to_csv(lead)
                    return True

                log.warning(
                    "Webhook HTTP %d for '%s': %s",
                    resp.status_code, lead.get("name"), resp.text[:120],
                )
                if resp.status_code in (400, 401, 403):
                    # Auth or bad-request errors are not retryable
                    break

            except requests.RequestException as exc:
                log.error("Webhook request error for '%s': %s", lead.get("name"), exc)

        self._increment_webhook_error()
        log.error(
            "Webhook failed after all retries for lead '%s' (place_id=%s). "
            "Saved to CSV only.",
            lead.get("name"), lead.get("place_id"),
        )
        self._append_lead_to_csv(lead)
        return False   # indicate the webhook itself failed

    def _save_lead(self, lead: dict) -> bool:
        """
        Main save entry point.

        Checks in-memory dedup first, then delegates to the webhook.
        Returns True if the lead was newly saved (not a duplicate).
        """
        if lead["place_id"] in self._existing_ids:
            log.debug("Duplicate skipped: %s", lead["place_id"])
            return False

        saved = self._save_lead_via_webhook(lead)
        if saved:
            # Register in dedup set regardless of webhook success
            # (CSV was written either way, so we don't want double CSV rows)
            self._existing_ids.add(lead["place_id"])
        return saved

    # ─────────────────────────────────────────────────────────
    # Google Places API
    # ─────────────────────────────────────────────────────────

    def _call_places_api(
        self,
        query: str,
        page_token: Optional[str] = None,
    ) -> Optional[dict]:
        """
        POST to places.searchText (New) with:
          • Strict field mask   (cost control)
          • Per-minute rate limiting  (ApiRateLimiter.acquire blocks if needed)
          • Per-run call cap    (max_api_calls_per_run, 0 = unlimited)
          • Exponential back-off retries on 429 / transient errors

        Returns the parsed JSON response, or None on unrecoverable error.
        """
        # ── Per-run call cap ──────────────────────────────────
        max_per_run = self.cfg.max_api_calls_per_run
        if max_per_run > 0 and self._run_api_calls >= max_per_run:
            log.warning(
                "Per-run API call limit reached (%d). Halting scrape.",
                max_per_run,
            )
            return None

        # ── Sliding-window rate limiter ───────────────────────
        # This call blocks if the per-minute budget is exhausted.
        self._rate_limiter.acquire()

        # ── Record the call BEFORE making the HTTP request ────
        self._increment_api_call()

        headers = {
            "Content-Type":     "application/json",
            "X-Goog-Api-Key":   self.cfg.google_places_api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }
        body: dict = {
            "textQuery":      query,
            "maxResultCount": self.cfg.max_results_per_query,
            "languageCode":   "en",
        }
        if page_token:
            body["pageToken"] = page_token

        for attempt, delay in enumerate([0] + RETRY_DELAYS):
            if delay:
                log.warning(
                    "Places API retry %d in %ds for query '%s'.",
                    attempt, delay, query,
                )
                time.sleep(delay)
            try:
                resp = requests.post(
                    PLACES_SEARCH_URL,
                    headers=headers,
                    json=body,
                    timeout=30,
                )
                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 429:
                    log.warning("Places API 429 — quota hit for '%s'.", query)
                    continue   # back-off and retry

                if resp.status_code in (400, 401, 403):
                    log.error(
                        "Places API auth/config error %d for '%s': %s",
                        resp.status_code, query, resp.text[:200],
                    )
                    return None   # not retryable

                log.warning(
                    "Places API HTTP %d for '%s': %s",
                    resp.status_code, query, resp.text[:200],
                )
                # 5xx and unknown codes → retry

            except requests.RequestException as exc:
                log.error("Places API request exception for '%s': %s", query, exc)

        log.error("All retries exhausted for query: '%s'", query)
        return None

    @staticmethod
    def _extract_lead(
        place: dict,
        category: str,
        location: str,
    ) -> Optional[dict]:
        """
        Parse a raw place dict from the API response.
        Returns a lead dict only if the place has NO websiteUri.
        """
        if place.get("websiteUri"):
            return None   # has a website — discard

        place_id = place.get("id", "")
        if not place_id:
            return None   # malformed record

        display = place.get("displayName", {})
        name    = (
            display.get("text", "Unknown")
            if isinstance(display, dict)
            else str(display)
        )

        return {
            "place_id": place_id,
            "name":     name,
            "phone":    place.get("formattedPhoneNumber", ""),
            "rating":   place.get("rating", ""),
            "category": category,
            "location": location,
        }

    def _fetch_leads_for_pair(
        self,
        category: str,
        location: str,
    ) -> list[dict]:
        """
        Paginate through Places API results for one (category, location) pair.

        Respects:
          • max_pages_per_query  — limits pagination depth
          • rate_limit_delay     — baseline sleep between pages
          • _call_places_api()   — handles per-minute cap + retries
        """
        query      = f"{category} in {location}"
        leads:  list[dict]  = []
        page_token: Optional[str] = None

        log.info("Searching: '%s'", query)

        for page_num in range(1, self.cfg.max_pages_per_query + 1):
            # Baseline sleep between pages (in addition to rate limiter)
            if page_num > 1:
                time.sleep(self.cfg.rate_limit_delay)

            data = self._call_places_api(query, page_token)
            if data is None:
                # Unrecoverable error or per-run cap hit
                break

            places = data.get("places", [])
            if not places:
                log.info("  No results on page %d for '%s'.", page_num, query)
                break

            for place in places:
                lead = self._extract_lead(place, category, location)
                if lead:
                    leads.append(lead)

            log.info(
                "  [%s / %s] page %d: %d results, %d qualified leads so far.",
                category, location, page_num, len(places), len(leads),
            )

            page_token = data.get("nextPageToken")
            if not page_token:
                break   # no further pages

        return leads

    # ─────────────────────────────────────────────────────────
    # City control (called from FastAPI endpoint)
    # ─────────────────────────────────────────────────────────

    def set_active_city(self, city: str) -> None:
        self._cfg_mgr.update(active_city=city)
        log.info("Active city switched to: '%s'", city or "ALL")

    # ─────────────────────────────────────────────────────────
    # Main run loop
    # ─────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Execute a full scrape run.

        Steps:
          1. Guard against concurrent runs.
          2. Refresh config, status, metrics, and dedup set.
          3. Sync rate-limiter limit with current config.
          4. Iterate (category × location) matrix:
               • Skip Done pairs and city-filtered pairs.
               • Respect daily_lead_limit and stop-request flag.
               • Save each new lead via webhook (+ CSV backup).
               • Mark pair Done after all its leads are saved.
          5. Write final metrics regardless of how the run ends.
        """
        if self._running:
            log.warning("LeadGenBot.run() called while already running — ignoring.")
            return

        self._running         = True
        self._stop_requested  = False
        self._run_api_calls   = 0

        cfg = self.cfg
        log.info("=" * 60)
        log.info("LeadGenBot run started")
        log.info("Active city filter : %s", cfg.active_city or "ALL")
        log.info("Daily lead limit   : %d", cfg.daily_lead_limit)
        log.info("Max calls/min      : %d", cfg.max_api_calls_per_minute)
        log.info("Max calls/run      : %d (0=unlimited)", cfg.max_api_calls_per_run)
        log.info("Webhook URL        : %s", cfg.google_sheets_webhook_url[:40] + "..." if len(cfg.google_sheets_webhook_url) > 40 else cfg.google_sheets_webhook_url or "(not set)")
        log.info("=" * 60)

        # Refresh state from disk
        self._load_status()
        self._load_metrics()

        # Seed dedup set from CSV (prior leads from previous runs)
        self._existing_ids = self._load_ids_from_csv()
        log.info("Dedup seed: %d existing place_ids loaded from CSV.", len(self._existing_ids))

        # Sync rate limiter with (possibly updated) config
        self._rate_limiter.update_limit(cfg.max_api_calls_per_minute)

        total_new   = 0
        leads_today = self._metrics.get("leads_today", 0)
        daily_limit = cfg.daily_lead_limit

        try:
            for category in cfg.business_categories:
                for location in cfg.locations:

                    # ── Soft stop ─────────────────────────────
                    if self._stop_requested:
                        log.info("Stop flag detected — exiting run loop.")
                        return

                    # ── Active city filter ────────────────────
                    if cfg.active_city and cfg.active_city.lower() not in location.lower():
                        continue

                    # ── Daily lead cap ────────────────────────
                    if leads_today >= daily_limit:
                        log.info(
                            "Daily lead limit (%d) reached. Stopping run.",
                            daily_limit,
                        )
                        return

                    key = self._status_key(category, location)

                    # ── Resume: skip already-done pairs ───────
                    if self._status.get(key) == "Done":
                        log.info("Skip (Done): %s", key)
                        continue

                    log.info("Processing pair: %s", key)

                    try:
                        leads = self._fetch_leads_for_pair(category, location)
                        saved = 0

                        for lead in leads:
                            if leads_today >= daily_limit:
                                log.info("Daily limit hit mid-pair — breaking.")
                                break
                            if self._save_lead(lead):
                                saved      += 1
                                leads_today += 1
                                self._increment_lead()

                        total_new += saved
                        log.info(
                            "  Pair %s: %d leads found, %d new saved.",
                            key, len(leads), saved,
                        )

                        with self._lock:
                            self._status[key] = "Done"
                            self._save_status()

                    except Exception as exc:
                        log.error("Error processing pair %s: %s", key, exc, exc_info=True)
                        with self._lock:
                            self._status[key] = f"Error: {str(exc)[:120]}"
                            self._save_status()

        finally:
            self._running = False
            with self._lock:
                self._metrics["last_run_at"]       = datetime.utcnow().isoformat()
                self._metrics["last_run_new_leads"] = total_new
                self._save_metrics()

            log.info("=" * 60)
            log.info(
                "Run complete. New leads: %d | API calls this run: %d",
                total_new, self._run_api_calls,
            )
            log.info("=" * 60)


# ─────────────────────────────────────────────────────────────
# Standalone entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/lead_gen.log"),
        ],
    )
    bot = LeadGenBot(ConfigManager())
    bot.run()
