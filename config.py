"""
config.py — App configuration model + persistent JSON manager.

Config is stored in data/config.json and hot-reloaded on each API request.
Sensitive keys (API keys, service account path) can also be set via env vars,
which always take precedence over the JSON file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from threading import Lock
from typing import Optional

CONFIG_FILE = Path("data/config.json")

# ─────────────────────────────────────────────────────────────
# AppConfig — single source of truth
# ─────────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    # ── Credentials (env vars take precedence) ────────────────
    google_places_api_key: str = ""
    google_sheet_id:       str = ""
    service_account_file:  str = "service_account.json"

    # ── Storage ───────────────────────────────────────────────
    use_google_sheets: bool = True

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
    daily_lead_limit:      int   = 30
    max_results_per_query: int   = 20
    max_pages_per_query:   int   = 3
    rate_limit_delay:      float = 1.0

    # ── Runtime control ───────────────────────────────────────
    active_city: str = ""   # empty = search all cities

    def apply_env_overrides(self):
        """Environment variables always win over the JSON file."""
        if v := os.environ.get("GOOGLE_PLACES_API_KEY"):
            self.google_places_api_key = v
        if v := os.environ.get("GOOGLE_SHEET_ID"):
            self.google_sheet_id = v
        if v := os.environ.get("SERVICE_ACCOUNT_FILE"):
            self.service_account_file = v
        if v := os.environ.get("USE_GOOGLE_SHEETS"):
            self.use_google_sheets = v.lower() == "true"
        return self

    def to_safe_dict(self) -> dict:
        """Return config as dict, masking the API key."""
        d = asdict(self)
        if d.get("google_places_api_key"):
            key = d["google_places_api_key"]
            d["google_places_api_key"] = key[:6] + "…" + key[-4:] if len(key) > 10 else "***"
        return d


# ─────────────────────────────────────────────────────────────
# ConfigManager — thread-safe JSON persistence
# ─────────────────────────────────────────────────────────────

class ConfigManager:
    """
    Load config from data/config.json, overlay env vars, and provide
    a thread-safe update() method used by the FastAPI /config endpoint.
    """

    _EDITABLE_FIELDS = {
        "google_places_api_key", "google_sheet_id", "service_account_file",
        "use_google_sheets", "business_categories", "locations",
        "daily_lead_limit", "max_results_per_query", "max_pages_per_query",
        "rate_limit_delay", "active_city",
    }

    def __init__(self):
        self._lock = Lock()
        Path("data").mkdir(exist_ok=True)
        self._config = self._load()

    def _load(self) -> AppConfig:
        cfg = AppConfig()
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            # Merge stored values into default dataclass
            for k, v in stored.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        cfg.apply_env_overrides()
        return cfg

    def _save(self):
        """Persist current config to disk (env-override values are NOT written)."""
        with open(CONFIG_FILE, "w") as f:
            json.dump(asdict(self._config), f, indent=2)

    @property
    def config(self) -> AppConfig:
        return self._config

    def reload(self):
        """Re-read from disk (useful after external edits)."""
        with self._lock:
            self._config = self._load()

    def update(self, **kwargs) -> AppConfig:
        """
        Update one or more config fields and persist.
        Only keys listed in _EDITABLE_FIELDS are accepted.
        Returns the updated AppConfig.
        """
        with self._lock:
            for k, v in kwargs.items():
                if k not in self._EDITABLE_FIELDS:
                    raise ValueError(f"Field '{k}' is not editable via API.")
                if not hasattr(self._config, k):
                    raise ValueError(f"Unknown config field: '{k}'")
                # Type coercion for int / float / bool
                current = getattr(self._config, k)
                if isinstance(current, bool):
                    v = bool(v)
                elif isinstance(current, int):
                    v = int(v)
                elif isinstance(current, float):
                    v = float(v)
                setattr(self._config, k, v)
            self._save()
        return self._config
