"""
main.py — FastAPI Management API for the Lead Generation Bot.

Endpoints
─────────
GET  /                  Health check
GET  /config            View current config (API key masked)
POST /config            Update config fields
GET  /leads             Pull leads from Google Sheet / CSV
GET  /metrics           Today's lead count + API call totals
POST /active-city       Switch the city the bot is currently targeting
POST /bot/run           Trigger a bot run in the background
POST /bot/stop          Request the bot to stop after current pair
GET  /bot/status        Is the bot running? What pairs are Done?
POST /bot/reset         Reset Done status for all or one pair
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import ConfigManager
from bot import LeadGenBot

# ─────────────────────────────────────────────────────────────
# Logging
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
# App + singletons
# ─────────────────────────────────────────────────────────────

config_manager = ConfigManager()
bot            = LeadGenBot(config_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Lead Gen API starting up.")
    yield
    log.info("Lead Gen API shutting down.")


app = FastAPI(
    title="Lead Generation Bot API",
    description="Manage and monitor the local business lead generation bot.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────

class ConfigUpdateRequest(BaseModel):
    google_places_api_key: Optional[str]  = Field(None, description="Google Places API key")
    google_sheet_id:       Optional[str]  = Field(None, description="Google Sheet ID")
    service_account_file:  Optional[str]  = Field(None, description="Path to service account JSON")
    use_google_sheets:     Optional[bool] = Field(None, description="Use Sheets (True) or CSV (False)")
    daily_lead_limit:      Optional[int]  = Field(None, ge=1, le=10_000, description="Max new leads per day")
    max_results_per_query: Optional[int]  = Field(None, ge=1, le=20)
    max_pages_per_query:   Optional[int]  = Field(None, ge=1, le=10)
    rate_limit_delay:      Optional[float]= Field(None, ge=0.0, le=10.0)
    business_categories:   Optional[list[str]] = None
    locations:             Optional[list[str]]  = None

    class Config:
        json_schema_extra = {
            "example": {
                "google_places_api_key": "AIzaSy...",
                "google_sheet_id": "1BxiM...",
                "daily_lead_limit": 50,
            }
        }


class ActiveCityRequest(BaseModel):
    city: str = Field(..., description='City name, e.g. "Hyderabad". Empty string = search all cities.')

    class Config:
        json_schema_extra = {"example": {"city": "Hyderabad"}}


class ResetStatusRequest(BaseModel):
    key: Optional[str] = Field(
        None,
        description='Specific pair key to reset, e.g. "Salons_Hyderabad_India". '
                    "Leave empty to reset ALL pairs.",
    )


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def health_check():
    return {"status": "ok", "service": "Lead Generation Bot API v2"}


# ── Config ────────────────────────────────────────────────────

@app.get("/config", tags=["Config"], summary="View current configuration")
def get_config():
    """
    Returns the current bot configuration.
    The Google Places API key is partially masked for security.
    """
    config_manager.reload()
    return {
        "config": config_manager.config.to_safe_dict(),
        "note": "API key is masked. POST /config to update values.",
    }


@app.post("/config", tags=["Config"], summary="Update configuration")
def update_config(body: ConfigUpdateRequest):
    """
    Update one or more configuration fields.
    Changes are persisted to `data/config.json` immediately.
    """
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")
    try:
        updated = config_manager.update(**updates)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "message": f"Config updated ({len(updates)} field(s)).",
        "updated_fields": list(updates.keys()),
        "config": updated.to_safe_dict(),
    }


# ── Leads ─────────────────────────────────────────────────────

@app.get("/leads", tags=["Leads"], summary="Fetch stored leads")
def get_leads(limit: int = 200):
    """
    Pull leads from Google Sheets (or the CSV fallback).
    Sorted newest-first; capped at `limit` rows (default 200).
    """
    if limit < 1 or limit > 5000:
        raise HTTPException(status_code=400, detail="`limit` must be between 1 and 5000.")
    leads = bot.read_leads(limit=limit)
    return {
        "total_returned": len(leads),
        "leads": leads,
    }


# ── Metrics ───────────────────────────────────────────────────

@app.get("/metrics", tags=["Metrics"], summary="Runtime metrics")
def get_metrics():
    """
    Returns:
    - leads_today       — new leads saved since midnight UTC
    - leads_total       — all-time leads saved
    - api_calls_today   — Places API calls today
    - api_calls_total   — all-time API calls
    - last_run_at       — UTC timestamp of last completed run
    - last_run_new_leads— leads saved in the last run
    - bot_running       — whether the bot is currently active
    """
    metrics = bot.get_metrics()
    metrics["bot_running"] = bot.is_running
    return metrics


# ── Active city ───────────────────────────────────────────────

@app.post("/active-city", tags=["Control"], summary="Switch active search city")
def set_active_city(body: ActiveCityRequest):
    """
    Immediately changes which city the bot targets on its next (or current) run.
    Pass an empty string to remove the city filter and search all locations.
    """
    bot.set_active_city(body.city)
    return {
        "message": f"Active city set to: '{body.city}'" if body.city else "City filter removed (all cities).",
        "active_city": body.city,
    }


# ── Bot control ───────────────────────────────────────────────

def _run_bot_task():
    """Wrapper so exceptions don't swallow silently in background."""
    try:
        bot.run()
    except Exception as e:
        log.error(f"Bot run crashed: {e}", exc_info=True)


@app.post("/bot/run", tags=["Bot Control"], summary="Trigger a bot run")
def trigger_bot_run(background_tasks: BackgroundTasks):
    """
    Starts a full scrape run in the background.
    Returns immediately — check GET /bot/status or GET /metrics to monitor.
    """
    if bot.is_running:
        raise HTTPException(status_code=409, detail="Bot is already running.")
    background_tasks.add_task(_run_bot_task)
    return {"message": "Bot run started in background.", "check": "GET /metrics or GET /bot/status"}


@app.get("/bot/status", tags=["Bot Control"], summary="Bot running state + pair progress")
def bot_status():
    """Returns whether the bot is running and the Done/Error/pending status of every pair."""
    progress = bot.get_status()
    cfg = config_manager.config
    matrix = [
        {
            "key":      f"{cat.replace(' ', '_')}_{loc.replace(', ', '_').replace(' ', '_')}",
            "category": cat,
            "location": loc,
            "status":   progress.get(
                            f"{cat.replace(' ', '_')}_{loc.replace(', ', '_').replace(' ', '_')}",
                            "Pending"
                        ),
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
        "summary":     {"done": done, "pending": pending, "errors": errors, "total": len(matrix)},
        "pairs":       matrix,
    }


@app.post("/bot/reset", tags=["Bot Control"], summary="Reset pair(s) to Pending")
def reset_bot_status(body: ResetStatusRequest):
    """
    Reset a specific category+location pair (or all pairs) back to Pending,
    allowing the bot to re-scrape them on the next run.
    """
    bot.reset_status(key=body.key)
    msg = f"Reset key '{body.key}' to Pending." if body.key else "All pairs reset to Pending."
    return {"message": msg}


# ─────────────────────────────────────────────────────────────
# Dev entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
