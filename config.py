"""
config.py — AppConfig dataclass + thread-safe ConfigManager.

Storage:  data/config.json  (plain JSON, no database)
Env vars: GOOGLE_PLACES_API_KEY and SHEETS_WEBHOOK_URL always override
          the JSON file at runtime but are NOT written back to disk.

Changes from v2:
  • Removed: google_sheet_id, service_account_file, use_google_sheets
  • Added:   google_sheets_webhook_url
  • Added:   max_api_calls_per_minute  (strict per-minute rate limit)
  • Added:   max_api_calls_per_run     (optional hard cap per run, 0 = unlimited)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from threading import Lock

CONFIG_FILE = Path("data/config.json")


# ─────────────────────────────────────────────────────────────
# AppConfig — single source of truth for all settings
# ─────────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    # ── Credentials ───────────────────────────────────────────
    google_places_api_key:     str = ""
    google_sheets_webhook_url: str = ""   # Apps Script Web App URL

    # ── Targeting matrix ─────────────────────────────────────
    business_categories: list[str] = field(default_factory=lambda: [
        "Salons", "Bakeries", "Clinics", "Restaurants", "Tailors",
        "Photographers", "Plumbers", "Electricians", "Tutors", "Gyms",
    ])
    locations: list[str] = field(default_factory=lambda: [
        "Hyderabad, India",
        "Chennai, India",
        "Pune, India",
        "Jaipur, India",
        "Lucknow, India",
    ])

    # ── Bot behaviour ─────────────────────────────────────────
    daily_lead_limit:         int   = 50
    max_results_per_query:    int   = 20   # Google Places max per page
    max_pages_per_query:      int   = 3    # pagination depth per pair
    rate_limit_delay:         float = 1.0  # baseline sleep between calls (s)

    # ── API rate limiting ─────────────────────────────────────
    max_api_calls_per_minute: int = 30     # hard cap: calls per 60-second window
    max_api_calls_per_run:    int = 0      # 0 = unlimited per run

    # ── Runtime control ───────────────────────────────────────
    active_city: str = ""   # empty = search all cities

    # ─────────────────────────────────────────────────────────
    def apply_env_overrides(self) -> "AppConfig":
        """
        Environment variables always take precedence over config.json.
        Applied at load time; never written back to disk.
        """
        if v := os.environ.get("GOOGLE_PLACES_API_KEY"):
            self.google_places_api_key = v
        if v := os.environ.get("SHEETS_WEBHOOK_URL"):
            self.google_sheets_webhook_url = v
        return self

    def to_safe_dict(self) -> dict:
        """Return config as a plain dict with credentials masked."""
        d = asdict(self)

        key = d.get("google_places_api_key", "")
        if key:
            d["google_places_api_key"] = (
                key[:6] + "..." + key[-4:] if len(key) > 10 else "***"
            )

        url = d.get("google_sheets_webhook_url", "")
        if url:
            d["google_sheets_webhook_url"] = (
                url[:40] + "..." if len(url) > 40 else url
            )

        return d


# ─────────────────────────────────────────────────────────────
# ConfigManager — load / save / update  (thread-safe)
# ─────────────────────────────────────────────────────────────

class ConfigManager:
    """
    Loads AppConfig from data/config.json.
    Creates the file with defaults if it does not exist.
    Applies env-var overrides on every load.
    Provides a thread-safe update() for the FastAPI /config endpoint.
    """

    _EDITABLE_FIELDS: frozenset = frozenset({
        "google_places_api_key",
        "google_sheets_webhook_url",
        "business_categories",
        "locations",
        "daily_lead_limit",
        "max_results_per_query",
        "max_pages_per_query",
        "rate_limit_delay",
        "max_api_calls_per_minute",
        "max_api_calls_per_run",
        "active_city",
    })

    def __init__(self) -> None:
        self._lock   = Lock()
        Path("data").mkdir(exist_ok=True)
        self._config = self._load()

    # ── Private helpers ────────────────────────────────────────

    def _load(self) -> AppConfig:
        """
        Read config.json → merge into AppConfig defaults → apply env vars.
        If the file does not exist, write defaults first.
        """
        cfg = AppConfig()

        if not CONFIG_FILE.exists():
            with open(CONFIG_FILE, "w") as fh:
                json.dump(asdict(cfg), fh, indent=2)
        else:
            with open(CONFIG_FILE) as fh:
                stored: dict = json.load(fh)
            for key, value in stored.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)

        cfg.apply_env_overrides()
        return cfg

    def _save(self) -> None:
        """Persist the in-memory config to disk."""
        with open(CONFIG_FILE, "w") as fh:
            json.dump(asdict(self._config), fh, indent=2)

    # ── Public interface ───────────────────────────────────────

    @property
    def config(self) -> AppConfig:
        return self._config

    def reload(self) -> None:
        """Re-read from disk (useful after external edits to config.json)."""
        with self._lock:
            self._config = self._load()

    def update(self, **kwargs) -> AppConfig:
        """
        Update one or more config fields atomically and persist.
        Only fields in _EDITABLE_FIELDS are accepted.
        Raises ValueError for unknown or read-only fields.
        Auto-coerces int, float, and bool.
        """
        with self._lock:
            for field_name, new_value in kwargs.items():
                if field_name not in self._EDITABLE_FIELDS:
                    raise ValueError(
                        f"Field '{field_name}' is not editable via the API."
                    )
                if not hasattr(self._config, field_name):
                    raise ValueError(f"Unknown config field: '{field_name}'")

                current = getattr(self._config, field_name)

                if isinstance(current, bool):
                    new_value = bool(new_value)
                elif isinstance(current, int):
                    new_value = int(new_value)
                elif isinstance(current, float):
                    new_value = float(new_value)

                setattr(self._config, field_name, new_value)

            self._save()

        return self._config
