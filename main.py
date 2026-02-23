"""
main.py — FastAPI Management API  (v3.1 — Live Dashboard Edition)

Endpoints
─────────
GET  /                        Health check + version info
GET  /config                  View config (credentials masked)
POST /config                  Update config fields
GET  /leads                   Read leads from data/leads.csv
GET  /metrics                 Daily counters, API call totals, webhook errors
POST /active-city             Switch active search city filter
POST /bot/run                 Trigger a scrape run in the background
POST /bot/stop                Request graceful stop after current pair
GET  /bot/status              Running state + per-pair progress matrix
POST /bot/reset               Reset one or all pairs back to Pending

GET  /categories              List all categories (with lead counts)
POST /categories              Create or update a category
DELETE /categories/{cat_id}   Delete a category
POST /categories/sync         Sync categories → config.json business_categories

GET  /templates               List all DM templates
POST /templates               Create or update a template
DELETE /templates/{tpl_id}    Delete a template
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
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
# JSON store helpers — categories.json & templates.json
# ─────────────────────────────────────────────────────────────

CATS_FILE = Path("data/categories.json")
TPLS_FILE = Path("data/templates.json")
_store_lock = Lock()

DEFAULT_CATS = [
    {"id": "c1",  "name": "Salons",        "emoji": "✂️",  "color": "#8b5cf6", "count": 0, "active": True},
    {"id": "c2",  "name": "Bakeries",      "emoji": "🥐",  "color": "#f59e0b", "count": 0, "active": True},
    {"id": "c3",  "name": "Clinics",       "emoji": "🏥",  "color": "#10b981", "count": 0, "active": True},
    {"id": "c4",  "name": "Restaurants",   "emoji": "🍽️", "color": "#ef4444", "count": 0, "active": True},
    {"id": "c5",  "name": "Tailors",       "emoji": "🧵",  "color": "#3b82f6", "count": 0, "active": True},
    {"id": "c6",  "name": "Photographers", "emoji": "📷",  "color": "#ec4899", "count": 0, "active": False},
    {"id": "c7",  "name": "Plumbers",      "emoji": "🔧",  "color": "#0ea5e9", "count": 0, "active": True},
    {"id": "c8",  "name": "Electricians",  "emoji": "⚡",  "color": "#f97316", "count": 0, "active": True},
    {"id": "c9",  "name": "Tutors",        "emoji": "📚",  "color": "#06b6d4", "count": 0, "active": False},
    {"id": "c10", "name": "Gyms",          "emoji": "🏋️", "color": "#84cc16", "count": 0, "active": True},
]

DEFAULT_TPLS: list[dict] = []


def _read_json(path: Path, default: list) -> list:
    if not path.exists():
        _write_json(path, default)
        return list(default)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, data: list) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _enrich_cats(cats: list, leads: list) -> list:
    """Attach live lead counts to each category from leads CSV."""
    counts: dict[str, int] = {}
    for lead in leads:
        cat = lead.get("category", "")
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    return [{**c, "count": counts.get(c["name"], c.get("count", 0))} for c in cats]


# ─────────────────────────────────────────────────────────────
# Singletons
# ─────────────────────────────────────────────────────────────

config_manager = ConfigManager()
bot            = LeadGenBot(config_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("LeadRadar API v3.1 starting up.")
    # Ensure store files exist
    _read_json(CATS_FILE, DEFAULT_CATS)
    _read_json(TPLS_FILE, DEFAULT_TPLS)
    yield
    log.info("LeadRadar API v3.1 shutting down.")


app = FastAPI(
    title="LeadRadar — Lead Generation Bot API",
    description=(
        "Manage and monitor the local business lead generation bot.\n\n"
        "Architecture: **Google Places API → LeadGenBot → Apps Script Webhook → Google Sheet**"
    ),
    version="3.1.0",
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
    google_places_api_key:     Optional[str]       = Field(None)
    google_sheets_webhook_url: Optional[str]       = Field(None)
    business_categories:       Optional[list[str]] = Field(None)
    locations:                 Optional[list[str]] = Field(None)
    daily_lead_limit:          Optional[int]       = Field(None, ge=1, le=10_000)
    max_results_per_query:     Optional[int]       = Field(None, ge=1, le=20)
    max_pages_per_query:       Optional[int]       = Field(None, ge=1, le=10)
    rate_limit_delay:          Optional[float]     = Field(None, ge=0.0, le=30.0)
    max_api_calls_per_minute:  Optional[int]       = Field(None, ge=1, le=600)
    max_api_calls_per_run:     Optional[int]       = Field(None, ge=0, le=100_000)

    model_config = {
        "json_schema_extra": {
            "example": {
                "google_places_api_key": "AIzaSy...",
                "google_sheets_webhook_url": "https://script.google.com/macros/s/XXXX/exec",
                "daily_lead_limit": 50,
                "max_api_calls_per_minute": 30,
            }
        }
    }


class ActiveCityRequest(BaseModel):
    city: str = Field(..., description='City name or empty string to remove filter.')
    model_config = {"json_schema_extra": {"example": {"city": "Hyderabad"}}}


class ResetStatusRequest(BaseModel):
    key: Optional[str] = Field(None)


class CategoryModel(BaseModel):
    id:     Optional[str] = Field(None, description="Auto-generated if omitted (create mode)")
    name:   str           = Field(..., min_length=1, max_length=80)
    emoji:  str           = Field("🏢")
    color:  str           = Field("#6366f1")
    active: bool          = Field(True)


class TemplateModel(BaseModel):
    id:        Optional[str] = Field(None)
    name:      str           = Field(..., min_length=1, max_length=120)
    tag:       str           = Field("outreach")
    subject:   str           = Field("")
    body:      str           = Field(..., min_length=1)
    variables: list[str]     = Field(default_factory=list)
    sentCount: int           = Field(0, ge=0)
    replyRate: float         = Field(0.0, ge=0.0, le=100.0)
    createdAt: Optional[str] = Field(None)


# ─────────────────────────────────────────────────────────────
# Routes — Health
# ─────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def health_check():
    return {
        "status":      "ok",
        "service":     "LeadRadar Lead Generation Bot",
        "version":     "3.1.0",
        "bot_running": bot.is_running,
        "active_city": bot.active_city or "ALL",
    }


# ─────────────────────────────────────────────────────────────
# Routes — Config
# ─────────────────────────────────────────────────────────────

@app.get("/config", tags=["Config"])
def get_config():
    config_manager.reload()
    return {
        "config": config_manager.config.to_safe_dict(),
        "note":   "Credentials are masked. Use POST /config to update.",
    }


@app.post("/config", tags=["Config"])
def update_config(body: ConfigUpdateRequest):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields provided.")
    try:
        updated = config_manager.update(**updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {
        "message":        f"Config updated — {len(updates)} field(s) changed.",
        "updated_fields": list(updates.keys()),
        "config":         updated.to_safe_dict(),
    }


# ─────────────────────────────────────────────────────────────
# Routes — Leads
# ─────────────────────────────────────────────────────────────

@app.get("/leads", tags=["Leads"])
def get_leads(limit: int = 200):
    if not (1 <= limit <= 5000):
        raise HTTPException(400, "`limit` must be 1–5000.")
    leads = bot.read_leads(limit=limit)
    return {"total_returned": len(leads), "source": "data/leads.csv", "leads": leads}


# ─────────────────────────────────────────────────────────────
# Routes — Metrics
# ─────────────────────────────────────────────────────────────

@app.get("/metrics", tags=["Metrics"])
def get_metrics():
    metrics = bot.get_metrics()
    cfg     = config_manager.config
    metrics["bot_running"]           = bot.is_running
    metrics["active_city"]           = bot.active_city or "ALL"
    metrics["daily_lead_limit"]      = cfg.daily_lead_limit
    metrics["max_api_calls_per_min"] = cfg.max_api_calls_per_minute
    return metrics


# ─────────────────────────────────────────────────────────────
# Routes — Active city
# ─────────────────────────────────────────────────────────────

@app.post("/active-city", tags=["Control"])
def set_active_city(body: ActiveCityRequest):
    bot.set_active_city(body.city)
    msg = f"Active city set to '{body.city}'." if body.city else "City filter removed."
    return {"message": msg, "active_city": body.city or "ALL"}


# ─────────────────────────────────────────────────────────────
# Routes — Bot control
# ─────────────────────────────────────────────────────────────

def _run_bot_task() -> None:
    try:
        bot.run()
    except Exception as exc:
        log.error("Bot run crashed: %s", exc, exc_info=True)


@app.post("/bot/run", tags=["Bot Control"])
def trigger_bot_run(background_tasks: BackgroundTasks):
    if bot.is_running:
        raise HTTPException(409, "Bot is already running.")
    background_tasks.add_task(_run_bot_task)
    return {"message": "Bot run started.", "monitor": ["GET /metrics", "GET /bot/status"]}


@app.post("/bot/stop", tags=["Bot Control"])
def stop_bot():
    if not bot.is_running:
        return {"message": "Bot is not running.", "bot_running": False}
    bot.request_stop()
    return {"message": "Stop requested — bot will halt after the current pair.", "bot_running": True}


@app.get("/bot/status", tags=["Bot Control"])
def bot_status():
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
        "summary":     {"done": done, "pending": pending, "errors": errors, "total": len(matrix)},
        "pairs":       matrix,
    }


@app.post("/bot/reset", tags=["Bot Control"])
def reset_bot_status(body: ResetStatusRequest):
    bot.reset_status(key=body.key)
    if body.key:
        return {"message": f"Pair '{body.key}' reset to Pending."}
    return {"message": "All pairs reset to Pending."}


# ─────────────────────────────────────────────────────────────
# Routes — Categories
# ─────────────────────────────────────────────────────────────

@app.get("/categories", tags=["Categories"])
def get_categories():
    """
    Return all categories with live lead counts derived from leads.csv.
    """
    with _store_lock:
        cats = _read_json(CATS_FILE, DEFAULT_CATS)
    leads = bot.read_leads(limit=5000)
    enriched = _enrich_cats(cats, leads)
    return {"categories": enriched, "total": len(enriched)}


@app.post("/categories", tags=["Categories"])
def upsert_category(body: CategoryModel):
    """
    Create a new category (omit `id`) or update an existing one (provide `id`).
    """
    with _store_lock:
        cats = _read_json(CATS_FILE, DEFAULT_CATS)
        now_id = body.id
        payload = {
            "id":     now_id or f"c_{uuid.uuid4().hex[:8]}",
            "name":   body.name,
            "emoji":  body.emoji,
            "color":  body.color,
            "active": body.active,
            "count":  0,
        }
        existing = next((i for i, c in enumerate(cats) if c["id"] == payload["id"]), None)
        if existing is not None:
            payload["count"] = cats[existing].get("count", 0)
            cats[existing] = payload
            action = "updated"
        else:
            cats.append(payload)
            action = "created"
        _write_json(CATS_FILE, cats)
    log.info("Category %s: %s", action, payload["name"])
    return {"message": f"Category {action}.", "category": payload}


@app.delete("/categories/{cat_id}", tags=["Categories"])
def delete_category(cat_id: str):
    with _store_lock:
        cats = _read_json(CATS_FILE, DEFAULT_CATS)
        before = len(cats)
        cats = [c for c in cats if c["id"] != cat_id]
        if len(cats) == before:
            raise HTTPException(404, f"Category '{cat_id}' not found.")
        _write_json(CATS_FILE, cats)
    log.info("Category deleted: %s", cat_id)
    return {"message": "Category deleted.", "id": cat_id}


@app.post("/categories/sync", tags=["Categories"])
def sync_categories_to_config():
    """
    Push active category names from categories.json → config.json
    (business_categories field). The bot will use the updated list on next run.
    """
    with _store_lock:
        cats = _read_json(CATS_FILE, DEFAULT_CATS)
    active_names = [c["name"] for c in cats if c.get("active", True)]
    config_manager.update(business_categories=active_names)
    log.info("Synced %d active categories to config.", len(active_names))
    return {
        "message":             f"Synced {len(active_names)} active categories to config.",
        "business_categories": active_names,
    }


# ─────────────────────────────────────────────────────────────
# Routes — DM Templates
# ─────────────────────────────────────────────────────────────

@app.get("/templates", tags=["Templates"])
def get_templates():
    with _store_lock:
        tpls = _read_json(TPLS_FILE, DEFAULT_TPLS)
    return {"templates": tpls, "total": len(tpls)}


@app.post("/templates", tags=["Templates"])
def upsert_template(body: TemplateModel):
    """
    Create a new template (omit `id`) or update an existing one (provide `id`).
    """
    from datetime import date
    with _store_lock:
        tpls = _read_json(TPLS_FILE, DEFAULT_TPLS)
        payload = {
            "id":        body.id or f"t_{uuid.uuid4().hex[:8]}",
            "name":      body.name,
            "tag":       body.tag,
            "subject":   body.subject,
            "body":      body.body,
            "variables": body.variables,
            "sentCount": body.sentCount,
            "replyRate": body.replyRate,
            "createdAt": body.createdAt or str(date.today()),
        }
        existing = next((i for i, t in enumerate(tpls) if t["id"] == payload["id"]), None)
        if existing is not None:
            # Preserve sentCount / replyRate if caller didn't provide them
            if body.sentCount == 0 and tpls[existing].get("sentCount", 0) > 0:
                payload["sentCount"] = tpls[existing]["sentCount"]
            if body.replyRate == 0.0 and tpls[existing].get("replyRate", 0) > 0:
                payload["replyRate"] = tpls[existing]["replyRate"]
            payload["createdAt"] = tpls[existing].get("createdAt", payload["createdAt"])
            tpls[existing] = payload
            action = "updated"
        else:
            tpls.append(payload)
            action = "created"
        _write_json(TPLS_FILE, tpls)
    log.info("Template %s: %s", action, payload["name"])
    return {"message": f"Template {action}.", "template": payload}


@app.delete("/templates/{tpl_id}", tags=["Templates"])
def delete_template(tpl_id: str):
    with _store_lock:
        tpls = _read_json(TPLS_FILE, DEFAULT_TPLS)
        before = len(tpls)
        tpls = [t for t in tpls if t["id"] != tpl_id]
        if len(tpls) == before:
            raise HTTPException(404, f"Template '{tpl_id}' not found.")
        _write_json(TPLS_FILE, tpls)
    log.info("Template deleted: %s", tpl_id)
    return {"message": "Template deleted.", "id": tpl_id}


# ─────────────────────────────────────────────────────────────
# Dev entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)