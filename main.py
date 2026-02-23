"""
main.py — FastAPI Management API  (v3 — Webhook Edition)

Endpoints
─────────
GET  /                   Health check + version info
GET  /config             View config (credentials masked)
POST /config             Update config fields
GET  /leads              Read leads from data/leads.csv
GET  /metrics            Daily counters, API call totals, webhook errors
POST /active-city        Switch active search city filter
POST /bot/run            Trigger a scrape run in the background
POST /bot/stop           Request graceful stop after current pair
GET  /bot/status         Running state + per-pair progress matrix
POST /bot/reset          Reset one or all pairs back to Pending

What changed from v2
────────────────────
  • Removed:  service_account_file / use_google_sheets / google_sheet_id fields
              from ConfigUpdateRequest
  • Added:    google_sheets_webhook_url field to ConfigUpdateRequest
  • Added:    max_api_calls_per_minute / max_api_calls_per_run fields
  • Added:    POST /bot/stop endpoint  (calls bot.request_stop())
  • Updated:  /metrics response includes webhook_errors counter
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import ConfigManager
from bot import LeadGenBot

# ─────────────────────────────────────────────────────────────
# Logging — console + file
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/api.log"),
    ],
)
log = logging.getLogger("api")


# ─────────────────────────────────────────────────────────────
# Singletons — created once, shared for the process lifetime
# ─────────────────────────────────────────────────────────────

config_manager = ConfigManager()
bot            = LeadGenBot(config_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("LeadRadar API v3 starting up.")
    yield
    log.info("LeadRadar API v3 shutting down.")


app = FastAPI(
    title="LeadRadar — Lead Generation Bot API",
    description=(
        "Manage and monitor the local business lead generation bot.\n\n"
        "Architecture: **Google Places API → LeadGenBot → Apps Script Webhook → Google Sheet**"
    ),
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# Pydantic request / response schemas
# ─────────────────────────────────────────────────────────────

class ConfigUpdateRequest(BaseModel):
    """
    All fields are optional — send only the ones you want to change.
    Fields omitted from the request are left unchanged.
    """
    # Credentials
    google_places_api_key:     Optional[str]   = Field(
        None, description="Google Places API (New) key"
    )
    google_sheets_webhook_url: Optional[str]   = Field(
        None, description="Google Apps Script Web App URL (webhook receiver)"
    )

    # Targeting
    business_categories:       Optional[list[str]] = Field(
        None, description='e.g. ["Salons", "Plumbers"]'
    )
    locations:                 Optional[list[str]]  = Field(
        None, description='e.g. ["Hyderabad, India", "Chennai, India"]'
    )

    # Behaviour
    daily_lead_limit:          Optional[int]   = Field(None, ge=1,   le=10_000)
    max_results_per_query:     Optional[int]   = Field(None, ge=1,   le=20)
    max_pages_per_query:       Optional[int]   = Field(None, ge=1,   le=10)
    rate_limit_delay:          Optional[float] = Field(None, ge=0.0, le=30.0)

    # Rate limiting
    max_api_calls_per_minute:  Optional[int]   = Field(None, ge=1,   le=600)
    max_api_calls_per_run:     Optional[int]   = Field(
        None, ge=0, le=100_000,
        description="Hard cap on API calls per run. 0 = unlimited."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "google_places_api_key":     "AIzaSy...",
                "google_sheets_webhook_url": "https://script.google.com/macros/s/XXXX/exec",
                "daily_lead_limit":          50,
                "max_api_calls_per_minute":  30,
            }
        }
    }


class ActiveCityRequest(BaseModel):
    city: str = Field(
        ...,
        description='City name, e.g. "Hyderabad". Empty string removes the filter.',
    )

    model_config = {
        "json_schema_extra": {"example": {"city": "Hyderabad"}}
    }


class ResetStatusRequest(BaseModel):
    key: Optional[str] = Field(
        None,
        description=(
            'Specific pair key to reset, e.g. "Salons_Hyderabad_India". '
            "Leave null / omit to reset ALL pairs."
        ),
    )


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

# ── Health ────────────────────────────────────────────────────

@app.get("/", tags=["Health"], summary="Health check")
def health_check():
    """Returns service name, version, and bot running state."""
    return {
        "status":      "ok",
        "service":     "LeadRadar Lead Generation Bot",
        "version":     "3.0.0",
        "bot_running": bot.is_running,
        "active_city": bot.active_city or "ALL",
    }


# ── Config ────────────────────────────────────────────────────

@app.get("/config", tags=["Config"], summary="View current configuration")
def get_config():
    """
    Returns the active bot configuration.
    Credentials are partially masked for security.
    """
    config_manager.reload()
    return {
        "config": config_manager.config.to_safe_dict(),
        "note":   "Credentials are masked. Use POST /config to update values.",
    }


@app.post("/config", tags=["Config"], summary="Update configuration")
def update_config(body: ConfigUpdateRequest):
    """
    Update one or more configuration fields.
    Changes are persisted to `data/config.json` immediately.
    Only supplied (non-null) fields are updated.
    """
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    try:
        updated = config_manager.update(**updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "message":        f"Config updated — {len(updates)} field(s) changed.",
        "updated_fields": list(updates.keys()),
        "config":         updated.to_safe_dict(),
    }


# ── Leads ─────────────────────────────────────────────────────

@app.get("/leads", tags=["Leads"], summary="Fetch stored leads from CSV")
def get_leads(limit: int = 200):
    """
    Returns leads from `data/leads.csv`.

    Leads are read in insertion order (oldest first).
    Use `limit` to cap the number of rows returned (default 200, max 5000).
    """
    if not (1 <= limit <= 5000):
        raise HTTPException(status_code=400, detail="`limit` must be between 1 and 5000.")

    leads = bot.read_leads(limit=limit)
    return {
        "total_returned": len(leads),
        "source":         "data/leads.csv",
        "leads":          leads,
    }


# ── Metrics ───────────────────────────────────────────────────

@app.get("/metrics", tags=["Metrics"], summary="Runtime metrics and counters")
def get_metrics():
    """
    Returns live counters including:

    | Field                | Description                             |
    |----------------------|-----------------------------------------|
    | leads_today          | New leads saved since midnight UTC      |
    | leads_total          | All-time leads saved                    |
    | api_calls_today      | Places API calls today                  |
    | api_calls_total      | All-time Places API calls               |
    | webhook_errors       | Failed webhook POSTs (all time)         |
    | last_run_at          | UTC timestamp of last completed run     |
    | last_run_new_leads   | Leads saved in the last run             |
    | bot_running          | Whether the bot is currently active     |
    | active_city          | Current city filter (ALL if none)       |
    | daily_lead_limit     | Configured daily cap                    |
    | max_api_calls_per_min| Configured per-minute API call limit    |
    """
    metrics = bot.get_metrics()
    cfg     = config_manager.config
    metrics["bot_running"]            = bot.is_running
    metrics["active_city"]            = bot.active_city or "ALL"
    metrics["daily_lead_limit"]       = cfg.daily_lead_limit
    metrics["max_api_calls_per_min"]  = cfg.max_api_calls_per_minute
    return metrics


# ── Active city ───────────────────────────────────────────────

@app.post("/active-city", tags=["Control"], summary="Switch active search city")
def set_active_city(body: ActiveCityRequest):
    """
    Immediately changes which city the bot targets.

    Pass an empty string (`""`) to remove the filter and search all cities.
    Takes effect on the next pair processed (even mid-run).
    """
    bot.set_active_city(body.city)
    if body.city:
        msg = f"Active city set to '{body.city}'."
    else:
        msg = "City filter removed — bot will search all configured locations."
    return {"message": msg, "active_city": body.city or "ALL"}


# ── Bot control ───────────────────────────────────────────────

def _run_bot_task() -> None:
    """Background wrapper — ensures exceptions are logged, not swallowed."""
    try:
        bot.run()
    except Exception as exc:
        log.error("Bot run crashed unexpectedly: %s", exc, exc_info=True)


@app.post("/bot/run", tags=["Bot Control"], summary="Trigger a scrape run")
def trigger_bot_run(background_tasks: BackgroundTasks):
    """
    Starts a full scrape run in the background.

    Returns immediately. Monitor progress with:
    - `GET /metrics`   — lead and API call counters
    - `GET /bot/status` — per-pair Done/Pending/Error breakdown
    """
    if bot.is_running:
        raise HTTPException(status_code=409, detail="Bot is already running.")
    background_tasks.add_task(_run_bot_task)
    return {
        "message": "Bot run started in background.",
        "monitor": ["GET /metrics", "GET /bot/status"],
    }


@app.post("/bot/stop", tags=["Bot Control"], summary="Request graceful stop")
def stop_bot():
    """
    Signals the bot to stop after it finishes the **current** category+location pair.

    The bot will not abort mid-pair — it completes the ongoing scrape,
    marks the pair Done, then exits cleanly.
    Has no effect if the bot is not running.
    """
    if not bot.is_running:
        return {"message": "Bot is not currently running.", "bot_running": False}
    bot.request_stop()
    return {
        "message":     "Stop requested — bot will halt after the current pair.",
        "bot_running": True,
    }


@app.get("/bot/status", tags=["Bot Control"], summary="Bot state + pair progress matrix")
def bot_status():
    """
    Returns:
    - Whether the bot is running
    - Active city filter
    - Summary counts (Done / Pending / Error / Total)
    - Full matrix of every (category × location) pair with its current status
    """
    progress = bot.get_status()
    cfg      = config_manager.config

    matrix = [
        {
            "key":      LeadGenBot._status_key(cat, loc),
            "category": cat,
            "location": loc,
            "status":   progress.get(LeadGenBot._status_key(cat, loc), "Pending"),
        }
        for cat in cfg.business_categories
        for loc in cfg.locations
        if not cfg.active_city or cfg.active_city.lower() in loc.lower()
    ]

    done    = sum(1 for p in matrix if p["status"] == "Done")
    pending = sum(1 for p in matrix if p["status"] == "Pending")
    errors  = sum(1 for p in matrix if p["status"].startswith("Error"))

    return {
        "bot_running": bot.is_running,
        "active_city": bot.active_city or "ALL",
        "summary":     {
            "done":    done,
            "pending": pending,
            "errors":  errors,
            "total":   len(matrix),
        },
        "pairs": matrix,
    }


@app.post("/bot/reset", tags=["Bot Control"], summary="Reset pair(s) to Pending")
def reset_bot_status(body: ResetStatusRequest):
    """
    Resets one specific pair (or all pairs) back to **Pending**,
    allowing the bot to re-scrape them on the next run.

    Provide `key` to reset a single pair, or omit it to reset everything.
    Key format: `{Category}_{City}_{Country}`, e.g. `Salons_Hyderabad_India`.
    """
    bot.reset_status(key=body.key)
    if body.key:
        return {"message": f"Pair '{body.key}' reset to Pending."}
    return {"message": "All pairs reset to Pending — full re-scrape will run next."}


# ─────────────────────────────────────────────────────────────
# Dev entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
