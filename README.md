# LeadRadar — Lead Generation Bot v3
## Full-Stack B2B Lead Pipeline & Management System

> **Architecture:** Google Places API → Python Bot → HTTP POST → Google Apps Script → Google Sheet

A production-ready, **full-stack lead generation automation system** that discovers local businesses via Google Places API, deduplicates them at scale, and delivers qualified leads to Google Sheets through webhook integration. Features a **live React dashboard** for real-time monitoring, configuration management, and bot orchestration.

**Tech Stack:** Python (FastAPI/Bot) • React 18 (Dashboard) • JSON/CSV (Persistence)

---

## 🎯 What This Does

| Component | Function |
|-----------|----------|
| **Google Places Scraper** | Searches for local businesses across categories & locations using Places API (New) |
| **Deduplication Engine** | Maintains in-memory `place_id` set loaded from CSV; zero duplicate leads |
| **Rate Limiter** | Sliding 60-second window respects API quotas (configurable: 1–600 calls/min) |
| **Webhook Integration** | POSTs each new lead to Apps Script URL → appends to Google Sheet in real-time |
| **FastAPI Backend** | RESTful management API with 20+ endpoints for bot control & config management |
| **React Dashboard** | Live monitoring, lead search/pagination, category CRUD, template management |
| **Graceful Shutdown** | `stop()` completes current pair before exiting; state persists for resumable runs |
| **Background Tasks** | Bot runs in background thread; dashboard updates every 5–15 seconds |

---

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│         Frontend Dashboard (React SPA)              │
│  • Real-time bot monitoring & stats                │
│  • Lead table with search/pagination/export        │
│  • Category & template CRUD                        │
│  • Configuration UI (API keys, limits, cities)     │
│  • Per-pair progress matrix                        │
│  • Active city filter toggle                       │
└────────────────────┬────────────────────────────────┘
                     │ (HTTP REST API)
┌────────────────────▼────────────────────────────────┐
│    FastAPI Management Server (main.py)             │
│  ├─ Health & Metrics endpoints                     │
│  ├─ Config CRUD (thread-safe)                      │
│  ├─ Bot control (run/stop/reset/status)            │
│  ├─ Leads CSV export                               │
│  ├─ Categories & Templates management              │
│  └─ CORS enabled (browser requests)                │
└────────────────────┬────────────────────────────────┘
                     │
        ┌────────────┼────────────────┐
        │            │                │
    ┌───▼──┐  ┌─────▼────┐  ┌───────▼────┐
    │ Bot  │  │ Config   │  │   Store    │
    │Logic │  │Manager   │  │(JSON/CSV)  │
    │      │  │(Thread   │  │(Persistent)│
    │      │  │ Safe)    │  │            │
    └───┬──┘  └─────┬────┘  └────┬───────┘
        │           │            │
    ┌───▼───────────▼────────────▼──────────┐
    │   Data Layer (data/ directory)        │
    │                                       │
    │  • config.json       (bot settings)  │
    │  • leads.csv         (dedup + backup)│
    │  • status.json       (pair states)   │
    │  • metrics.json      (counters)      │
    │  • categories.json   (UI catalog)    │
    │  • templates.json    (DM templates)  │
    │  • api.log           (audit trail)   │
    └───┬───────────────────────────────────┘
        │
    ┌───▴──────────────────────────────────┐
    │   External Services                  │
    │                                      │
    │  Google Places API (New)             │
    │    ├─ searchText                     │
    │    └─ rate limit: 1000–2000 QPM     │
    │                                      │
    │  Google Apps Script                  │
    │    ├─ HTTP POST webhook URL         │
    │    └─ appends to Google Sheet        │
    └──────────────────────────────────────┘
```

---

## 📊 Core Components

### **1. LeadGenBot (bot.py)**

The **main execution engine** that orchestrates the entire scraping workflow.

#### **Key Responsibilities:**
- **Pair Matrix Generation:** Cartesian product of categories × locations
  - Example: 10 categories × 5 locations = **50 pairs to process**
  - Each pair tracked independently in `status.json` (Pending/Done/Error)
  
- **Places API Integration:**
  - Calls `places.searchText()` (New API, supports pagination token)
  - Filters for businesses without `websiteUri` (targets local SMBs)
  - Respects `max_results_per_query` (1–20) and `max_pages_per_query` (1–10)
  
- **Deduplication:**
  ```python
  # On startup, load all existing place_ids from CSV
  existing_ids = set(row['place_id'] for row in leads.csv)
  
  # Per new lead
  if place_id not in existing_ids:
      save_to_csv()  # append atomically
      post_webhook()  # async HTTP POST
      existing_ids.add(place_id)  # memory set
  ```
  - **O(1) lookup** on every result
  - Safe to re-run pairs without duplicates
  
- **Rate Limiting:**
  - `ApiRateLimiter` class with sliding 60-second window
  - Tracks monotonic timestamps in `deque`
  - Before each call: evicts old timestamps, sleeps if full
  - Live-updateable mid-run via `update_limit()`
  
- **Daily Caps:**
  - Stops if `daily_lead_limit` reached (e.g., 50 leads/day)
  - Tracks in `metrics.json`
  
- **Graceful Shutdown:**
  ```python
  if stop_flag_set:
      # Finish current pair
      mark_complete()
      persist_state()
      exit()  # no abrupt kill
  ```

#### **API Flow:**
```python
def run(self):
    load_config()
    load_dedup_set_from_csv()
    
    for category, location in pairs:
        if status[key] == "Done":
            continue  # skip already completed
        
        try:
            results = search_places(f"{category} in {location}")
            for result in results:
                if not is_duplicate(result['place_id']):
                    save_lead(result)
                    post_webhook_async(result)
                    check_daily_limit()
            status[key] = "Done"
        except ApiError as e:
            status[key] = f"Error: {e}"
        
        if stop_requested:
            break
    
    persist_status()
    log_summary()
```

---

### **2. ConfigManager (config.py)**

**Thread-safe, persistent configuration system** with environment variable override.

#### **Core Features:**

| Feature | Behavior |
|---------|----------|
| **Single Source of Truth** | `AppConfig` dataclass, one instance per app |
| **Persistence Layer** | `data/config.json` (plain JSON, no database) |
| **Env Override** | `GOOGLE_PLACES_API_KEY` & `SHEETS_WEBHOOK_URL` override JSON at runtime |
| **Never Write Env Vars** | Credentials from env are **not** written back to disk |
| **Thread Safety** | Mutex lock on all reads/writes |
| **Type Coercion** | Auto-convert str → int/float/bool on update |
| **Credential Masking** | API responses show only first 6 + last 4 chars |

#### **Editable Fields:**

```json
{
  "google_places_api_key":     "AIzaSy...",                    // API key
  "google_sheets_webhook_url": "https://script.google.com/...", // Apps Script URL
  "business_categories":       ["Salons", "Bakeries", ...],     // N categories
  "locations":                 ["Hyderabad, India", ...],       // M locations
  "daily_lead_limit":          50,                              // 1–10,000
  "max_results_per_query":     20,                              // 1–20
  "max_pages_per_query":       3,                               // 1–10
  "rate_limit_delay":          1.0,                             // 0–30s (baseline sleep)
  "max_api_calls_per_minute":  30,                              // 1–600 (hard cap)
  "max_api_calls_per_run":     0,                               // 0 = unlimited
  "active_city":               ""                               // "" = ALL cities
}
```

#### **Usage Pattern:**
```python
# In main.py, single global instance
config_manager = ConfigManager()  # auto-creates data/config.json

# Reads
cfg = config_manager.config
print(cfg.daily_lead_limit)

# Atomic updates (persisted immediately)
config_manager.update(
    daily_lead_limit=100,
    active_city="Hyderabad, India"
)

# Reload from disk if external edit
config_manager.reload()
```

---

### **3. FastAPI Management API (main.py)**

**RESTful backend** on `localhost:8000` with 20+ endpoints for complete bot orchestration.

#### **Health & Monitoring:**
```http
GET /                    → {"status": "ok", "service": "LeadRadar", "version": "3.1.0", "bot_running": bool, "active_city": str}
GET /config              → {"config": {...masked...}, "note": "Use POST to update"}
GET /metrics             → {"leads_today": int, "leads_total": int, "api_calls_today": int, "webhook_errors": int, ...}
GET /leads?limit=200     → {"total_returned": int, "leads": [...], "source": "data/leads.csv"}
GET /bot/status          → {"bot_running": bool, "summary": {...}, "pairs": [{key, category, location, status}, ...]}
```

#### **Configuration Management:**
```http
POST /config
  Body: {
    "google_places_api_key": "AIzaSy...",
    "daily_lead_limit": 100,
    "active_city": "Mumbai, India",
    ...  // any editable fields
  }
  Response: {"message": "Config updated", "updated_fields": [...], "config": {...masked...}}
```

#### **Bot Control:**
```http
POST /bot/run            → {"message": "Bot run started", "monitor": [...]}
                            (runs in background task queue)

POST /bot/stop           → {"message": "Stop requested...", "bot_running": true/false}
                            (graceful halt after current pair)

POST /bot/reset?key=Salons_Mumbai_India
                         → {"message": "Pair reset to Pending"}
                            (replay individual pair)

POST /active-city
  Body: {"city": "Hyderabad, India"}
  Response: {"message": "Active city set...", "active_city": "Hyderabad, India"}
```

#### **Categories Management:**
```http
GET /categories          → {"categories": [...with live lead counts...], "total": int}

POST /categories
  Body: {
    "name": "Florists",
    "emoji": "🌸",
    "color": "#ec4899",
    "active": true
  }
  Response: {"message": "Category created", "category": {...}}

POST /categories/sync    → {"message": "Synced N active categories to config", "business_categories": [...]}
                            (push active categories from UI → config.json)

DELETE /categories/{id}  → {"message": "Category deleted", "id": "c1"}
```

#### **Templates Management:**
```http
GET /templates           → {"templates": [...], "total": int}

POST /templates
  Body: {
    "name": "Outreach Template",
    "tag": "outreach",
    "subject": "Let's connect",
    "body": "Hi {{name}}, we help {{category}} businesses...",
    "variables": ["name", "category"]
  }
  Response: {"message": "Template created", "template": {...}}

DELETE /templates/{id}   → {"message": "Template deleted", "id": "t_abc123"}
```

#### **Interactive Docs:**
- Swagger UI: `GET http://localhost:8000/docs`
- ReDoc: `GET http://localhost:8000/redoc`

---

### **4. React Dashboard (dashboard_preview.html)**

**Embedded SPA** (no build step, inline JSX via Babel) for real-time monitoring & control.

#### **Dashboard Tab:**
```
┌─────────────────────────────────────────────────────┐
│  Stats Row (4 cards)                                │
│  ┌─────────────────┬──────────────┬──────────────────┤
│  │ Daily Leads:    │ Total Leads: │ API Calls Today: │ Active City: │
│  │ 23 / 50 (46%)   │ 1,247 total  │ 89 calls         │ Hyderabad    │
│  └─────────────────┴──────────────┴──────────────────┘
│                                                      │
│  ┌─────────────────────────┐  ┌──────────────────┐  │
│  │ Today's Progress        │  │ Leads Table      │  │
│  │ ▓▓▓▓░░░░░░░░░░ 46%      │  │ (search, sort)   │  │
│  │ API calls: 89           │  │ Category │ Name  │  │
│  │                         │  │ Salons   │ Ravi  │  │
│  │ [Config Panel]          │  │ Bakery   │ Maya  │  │
│  │ [City Toggle]           │  │ ...             │  │
│  └─────────────────────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────┘
```

#### **Categories Tab:**
- List all categories with **live lead counts** (computed from CSV)
- Create new category (name, emoji picker, color picker)
- Edit existing (toggle active status)
- Delete category
- **Sync** active categories → `config.json` (for bot to use on next run)

#### **Templates Tab:**
- Create/edit DM templates (outreach, followup, etc.)
- Track `sentCount` & `replyRate` metrics
- Variables placeholder detection (`{{variable_name}}`)
- Delete templates

#### **Bot Control:**
```
Start Bot    [● running]  Stop Bot
├─ Pair matrix displayed below
├─ Salons_Hyderabad       : Done ✓
├─ Salons_Chennai         : Done ✓
├─ Bakeries_Hyderabad     : Pending
├─ Bakeries_Chennai       : Error 403 ✗
└─ Summary: 2 Done, 1 Pending, 1 Error (4 total)
```

#### **Tech Details:**
- **Framework:** React 18 (CDN) + Babel (no build)
- **State:** `useState`, `useCallback`, `useEffect`
- **API:** Fetch-based with error handling
- **Styling:** Inline CSS with CSS variables (dark theme)
- **Icons:** Custom SVG icons (17 icons pre-defined)

---

## 🔄 Complete Workflow: Example Run

### **Scenario:**
- Config: 2 categories (Salons, Bakeries), 2 cities (Hyderabad, Chennai)
- Daily limit: 10 leads
- Rate limit: 30 calls/min

### **Step 1: User Clicks "Start Bot"**
```
GET /bot/status
Response:
  "pairs": [
    {"key": "Salons_Hyderabad_India", "status": "Pending"},
    {"key": "Salons_Chennai_India", "status": "Pending"},
    {"key": "Bakeries_Hyderabad_India", "status": "Pending"},
    {"key": "Bakeries_Chennai_India", "status": "Pending"}
  ]
```

### **Step 2: POST /bot/run (runs in background)**
```
Background Task:
  1. Load config (2 categories, 2 cities, 10-lead limit)
  2. Load dedup set from leads.csv (e.g., 1,200 existing place_ids)
  3. Start loop:
     
     Pair 1: Salons in Hyderabad
     ├─ Search: "Salons in Hyderabad, India"
     ├─ Get results (up to 20/page × 3 pages = 60 max)
     ├─ For each result:
     │  ├─ Check: place_id in dedup set?
     │  │  ├─ YES → skip (already saved)
     │  │  └─ NO → NEW LEAD
     │  ├─ Append to leads.csv
     │  ├─ POST to webhook (async)
     │  ├─ Add place_id to dedup set
     │  ├─ Check: daily_limit reached? (stop if yes)
     │  └─ Respect rate_limit_delay (1.0s) before next call
     ├─ ApiRateLimiter.acquire() → blocks if ≥30 calls in last 60s
     └─ Mark pair: "Done", save status.json
     
     Pair 2: Salons in Chennai
     └─ (repeat same flow)
     
     Pair 3, 4: Bakeries × cities
     └─ (repeat)
  
  4. Log summary: "New leads: 5 | API calls this run: 8"
  5. Exit gracefully
```

### **Step 3: Dashboard Auto-Refreshes (every 5s while running)**
```
Dashboard GET /metrics:
  "leads_today": 5
  "api_calls_today": 8
  "bot_running": true

Dashboard GET /bot/status:
  "pairs": [
    {"key": "Salons_Hyderabad_India", "status": "Done"},
    {"key": "Salons_Chennai_India", "status": "Done"},
    {"key": "Bakeries_Hyderabad_India", "status": "Pending"},
    {"key": "Bakeries_Chennai_India", "status": "Pending"}
  ]

Dashboard GET /leads?limit=500:
  "total_returned": 505
  "leads": [
    {"name": "Ravi Kumar Salons", "category": "Salons", "location": "Hyderabad, India", ...}
    ...
  ]
```

### **Step 4: User Clicks "Stop" Mid-Run**
```
POST /bot/stop
Response: {"message": "Stop requested...", "bot_running": true}

Bot Behavior:
  1. Set stop_flag = True
  2. Finish current pair (Bakeries_Hyderabad)
  3. Persist status.json (Pair 4 remains "Pending")
  4. Log: "Stop flag detected — exiting run loop"
  5. Exit gracefully
```

### **Step 5: User Clicks "Start" Again**
```
POST /bot/run
Bot reloads config + status.json:
  "Salons_Hyderabad_India": "Done" → skip
  "Salons_Chennai_India": "Done" → skip
  "Bakeries_Hyderabad_India": "Done" → skip
  "Bakeries_Chennai_India": "Pending" → RESUME HERE

Resume from Pair 4, continue where it left off.
```

---

## 🔐 Rate Limiting Strategy

### **Sliding 60-Second Window (ApiRateLimiter)**

```python
class ApiRateLimiter:
    def __init__(self, max_per_minute=30):
        self.max = max_per_minute
        self.timestamps = deque()  # monotonic timestamps of calls
        self.lock = Lock()
    
    def acquire(self):
        """Block until safe to make API call."""
        with self.lock:
            now = time.monotonic()
            
            # Remove old timestamps (older than 60s)
            while self.timestamps and (now - self.timestamps[0]) >= 60:
                self.timestamps.popleft()
            
            # Check if window is full
            if len(self.timestamps) >= self.max:
                # Sleep until oldest call expires
                sleep_time = 60 - (now - self.timestamps[0])
                sleep(sleep_time)
                now = time.monotonic()
            
            # Record this call
            self.timestamps.append(now)
```

### **Two-Level Rate Control:**

| Level | Setting | Purpose |
|-------|---------|---------|
| **Per-Minute** | `max_api_calls_per_minute` (1–600) | Hard cap; prevents throttling |
| **Per-Run** | `max_api_calls_per_run` (0 = unlimited) | Soft cap; protects first runs |
| **Baseline** | `rate_limit_delay` (0–30s) | Sleep between calls |

**Example:**
```
Scenario: max_calls_per_minute=30, rate_limit_delay=1.0
├─ Call 1: acquired immediately
├─ Call 2: wait 1.0s
├─ Call 3: wait 1.0s
├─ ...
├─ Call 30: wait 1.0s (all 30 within 60s window)
└─ Call 31: wait ~61s total (oldest call expires, window slides)
```

---

## 📁 File Structure & Persistence

```
leadsaas/
├── bot.py                    # LeadGenBot engine + ApiRateLimiter
├── config.py                 # ConfigManager + AppConfig dataclass
├── main.py                   # FastAPI app (20+ endpoints)
├── dashboard_preview.html    # React SPA (embedded, no build)
├── apps_script_webhook.js    # (Reference: paste into Apps Script)
├── requirements.txt          # requests, fastapi, uvicorn, pydantic
├── README.md                 # (this file)
└── data/                     # Runtime files (GITIGNORE THIS)
    ├── config.json           # Bot settings (user-editable)
    ├── status.json           # Per-pair states (Pending/Done/Error)
    ├── metrics.json          # Daily + all-time counters
    ├── leads.csv             # Dedup set + local backup (grows)
    ├── categories.json       # UI category list + metadata
    ├── templates.json        # DM templates (for future use)
    └── api.log               # Structured logs (append-only)
```

### **Data File Formats:**

**config.json:**
```json
{
  "google_places_api_key": "AIzaSy...",
  "google_sheets_webhook_url": "https://script.google.com/...",
  "business_categories": ["Salons", "Bakeries", ...],
  "locations": ["Hyderabad, India", ...],
  "daily_lead_limit": 50,
  "max_api_calls_per_minute": 30,
  ...
}
```

**status.json:**
```json
{
  "Salons_Hyderabad_India": "Done",
  "Salons_Chennai_India": "Done",
  "Bakeries_Hyderabad_India": "Error: 403 Unauthorized",
  "Bakeries_Chennai_India": "Pending"
}
```

**leads.csv:**
```csv
place_id,name,category,location,phone,rating,website,discovered_at
ChIJ1...,Ravi Salons,Salons,Hyderabad India,+91-xxx,4.2,,2026-02-23T21:13:37
ChIJ2...,Maya Bakery,Bakeries,Chennai India,+91-yyy,4.5,,2026-02-23T21:15:12
```

**metrics.json:**
```json
{
  "leads_today": 5,
  "leads_total": 1247,
  "api_calls_today": 89,
  "api_calls_total": 12340,
  "webhook_errors": 2,
  "last_run": "2026-02-23T21:15:00"
}
```

---

## 🚀 Quick Start

### **1. Clone & Install**
```bash
git clone https://github.com/surendra-Gd/leadsaas.git
cd leadsaas
pip install -r requirements.txt
```

### **2. Get API Credentials**

**Google Places API (New):**
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project or select existing
3. Enable **Places API** (New)
4. Create **API Key** (Application restrictions: HTTP referrer or None)
5. Copy key (e.g., `AIzaSy...`)

**Google Apps Script Webhook:**
1. Create or open a Google Sheet
2. **Extensions → Apps Script**
3. Paste code from `apps_script_webhook.js` into `Code.gs`
4. **Deploy → New deployment → Web app**
   - Execute as: **Me** (your account)
   - Who has access: **Anyone** (allow unauthenticated)
5. Copy the Web App URL (e.g., `https://script.google.com/macros/s/XXXXX/exec`)

### **3. Configure**

**Option A: Edit JSON**
```bash
# Edit data/config.json (created on first run)
{
  "google_places_api_key": "AIzaSy...",
  "google_sheets_webhook_url": "https://script.google.com/macros/s/XXXXX/exec",
  "business_categories": ["Salons", "Bakeries", "Clinics"],
  "locations": ["Hyderabad, India", "Chennai, India"],
  "daily_lead_limit": 50,
  "max_api_calls_per_minute": 30
}
```

**Option B: Environment Variables (takes precedence)**
```bash
export GOOGLE_PLACES_API_KEY="AIzaSy..."
export SHEETS_WEBHOOK_URL="https://script.google.com/..."
```

### **4. Run**

**Standalone (CLI):**
```bash
python bot.py
# Scrapes, saves to data/leads.csv, exits
```

**With Dashboard (Full-Featured):**
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# Open http://localhost:8000/docs (API docs)
# Open http://localhost:8000 (dashboard - check HTML file)
```

### **5. Monitor**

**Dashboard:**
- Browse to `http://localhost:8000` (or serve `dashboard_preview.html`)
- See live stats, leads, bot status
- Start/stop bot, configure, manage categories

**Logs:**
```bash
tail -f data/api.log
```

---

## 🔌 Webhook Payload Reference

Every new lead is POSTed to your Apps Script URL:

```json
{
  "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
  "name": "Ravi Kumar Tailors",
  "phone": "+91 98765 43210",
  "rating": "4.2",
  "category": "Tailors",
  "location": "Hyderabad, India"
}
```

**Apps Script Response Codes (at HTTP 200 transport level):**
```json
{ "status": "ok", "row": 42 }              // saved to sheet
{ "status": "skipped", "reason": "duplicate" }  // duplicate
{ "status": "error", "message": "..." }   // failure
```

---

## 🎯 Key Features & Specificity

### **✅ Deduplication at Scale**
- In-memory `place_id` set loaded once at startup
- **O(1)** lookup per lead
- Safe to re-run pairs without duplicates
- CSV-backed persistent state

### **✅ Smart Rate Limiting**
- Sliding 60-second window (adjustable 1–600 calls/min)
- Blocks automatically if quota full
- Wakes up exactly when oldest call expires
- Thread-safe, live-updateable

### **✅ Matrix-Based Processing**
- Generates N × M pairs automatically (categories × locations)
- Each pair tracked independently
- Replay individual failed pairs or pause mid-run
- Resume from last known state

### **✅ Graceful Shutdown**
- `stop()` completes current pair before exiting
- No abrupt termination mid-search
- State persisted for resumable runs

### **✅ Webhook-Driven Delivery**
- No service account JSON or OAuth complexity
- Simple HTTP POST to Apps Script URL
- Async delivery (doesn't block bot)
- Real-time Google Sheet updates

### **✅ Environment Variable Overrides**
- Credentials injected via CI/CD without touching JSON
- Never written back to disk
- Secure for GitHub Actions, cloud deployments

### **✅ Live Dashboard**
- React SPA with real-time stats
- Search/paginate 5000+ leads
- Category management with CRUD & sync
- Template management for future outreach

### **✅ Production-Ready**
- Structured logging to `data/api.log`
- Atomic CSV writes
- Thread-safe config manager
- Background task orchestration
- Full error recovery & retry logic

---

## 📊 Data Flow

```
User Browser (React Dashboard)
  │
  ├─ GET /config              → ConfigManager.config (masked)
  ├─ POST /config             → ConfigManager.update() → saves to JSON
  ├─ POST /bot/run            → LeadGenBot.run() (background)
  ├─ GET /bot/status          → progress matrix
  ├─ GET /leads?limit=500     → CSV data
  ├─ GET /metrics             → counters
  └─ POST /active-city        → filters search
              │
              ▼
┌─────────────────────────────────────────┐
│   LeadGenBot.run() (background thread)  │
│                                         │
│   for each (category, location) pair:  │
│     ├─ ApiRateLimiter.acquire()        │
│     ├─ GET places.searchText()         │
│     │   └─ Google Places API           │
│     └─ for each new lead:              │
│         ├─ dedup check (place_id)      │
│         ├─ append leads.csv            │
│         ├─ POST webhook                │
│         │   └─ Apps Script → Sheet     │
│         └─ update metrics              │
└─────────────────────────────────────────┘
```

---

## ⚙️ Configuration Guide

### **Tuning for Performance**

| Parameter | Recommendations | Trade-offs |
|-----------|------------------|-----------|
| `max_api_calls_per_minute` | 30–50 (default: 30) | Higher = faster scraping, but risk 429 throttling |
| `rate_limit_delay` | 0.5–1.5s (default: 1.0) | Lower = faster, Higher = safer rate limiting |
| `max_results_per_query` | 20 (max for Places API) | Always use max for efficiency |
| `max_pages_per_query` | 1–5 (default: 3) | More pages = more results, but more calls |
| `daily_lead_limit` | 50–200 (default: 50) | Limits cost, limits results |
| `active_city` | "" (all) or single city | Narrow for testing, wide for production |

### **Cost Estimation**

**Google Places API (New):** ~$0.017 per call (varies by region)

```
Example: 10 categories × 5 cities × 3 pages = 150 pairs × 20 results = 3,000 leads
Cost ≈ 150 API calls × $0.017 = $2.55 per run
Monthly (daily): ~$77 per month
```

---

## 🐛 Troubleshooting

### **Error: "403 Method doesn't allow unregistered callers"**
- **Cause:** Invalid or missing API key
- **Fix:** Check `GOOGLE_PLACES_API_KEY` env var or `config.json`
- **Verify:** Use `curl -H "X-Goog-Api-Key: YOUR_KEY" "https://places.googleapis.com/v1/places:searchText"`

### **Error: "Webhook POST failed"**
- **Cause:** Invalid Apps Script URL or server error
- **Fix:** Test URL: `curl -X POST -H "Content-Type: application/json" -d '{"test": true}' "<your-webhook-url>"`
- **Check:** Apps Script deployment still active?

### **Leads not increasing**
- **Cause:** All results already in CSV (dedup) or daily limit hit
- **Fix:** Run `POST /bot/reset` to clear status, rescan
- **Check:** `GET /metrics` for `leads_today` vs `daily_lead_limit`

### **Dashboard blank**
- **Cause:** API server not running or CORS issue
- **Fix:** Start server: `uvicorn main:app --host 0.0.0.0 --port 8000`
- **Check:** Browser console for fetch errors

---

## 📚 GitHub Actions (Automated Daily Runs)

### **Create `.github/workflows/lead_gen.yml`:**
```yaml
name: LeadGen Daily

on:
  schedule:
    - cron: '0 3 * * *'  # 03:00 UTC daily

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - uses: actions/cache@v3
        with:
          path: data/
          key: leadgen-${{ github.run_id }}
          restore-keys: leadgen-
      
      - run: pip install -r requirements.txt
      
      - env:
          GOOGLE_PLACES_API_KEY: ${{ secrets.GOOGLE_PLACES_API_KEY }}
          SHEETS_WEBHOOK_URL: ${{ secrets.SHEETS_WEBHOOK_URL }}
        run: python bot.py
      
      - uses: actions/upload-artifact@v3
        with:
          name: data
          path: data/
```

### **Required Repository Secrets:**
1. `GOOGLE_PLACES_API_KEY` → Your Places API key
2. `SHEETS_WEBHOOK_URL` → Your Apps Script Web App URL

---

## 📈 Monitoring & Metrics

### **Key Metrics (in `/metrics` endpoint):**

```json
{
  "leads_today": 5,                    // New leads scraped today
  "leads_total": 1247,                 // Cumulative across all runs
  "api_calls_today": 89,               // Calls to Places API today
  "api_calls_total": 12340,            // Cumulative calls
  "webhook_errors": 2,                 // Failed POSTs to Apps Script
  "bot_running": true,                 // Current run status
  "active_city": "Hyderabad",          // Current filter
  "daily_lead_limit": 50,              // Config limit
  "max_api_calls_per_min": 30          // Config rate limit
}
```

### **Health Check:**
```bash
curl http://localhost:8000/
# {"status": "ok", "service": "LeadRadar", "version": "3.1.0", "bot_running": false, ...}
```

---

## 🛡️ Security Best Practices

1. **Always use environment variables for secrets:**
   ```bash
   export GOOGLE_PLACES_API_KEY="..." # Never commit to git
   export SHEETS_WEBHOOK_URL="..."
   ```

2. **Add `data/` to `.gitignore`:**
   ```
   data/
   *.log
   ```

3. **Restrict API Key in Google Cloud Console:**
   - Set HTTP referrer restrictions if using from web
   - Or restrict to IP ranges if server-only

4. **Make Apps Script Web App private:**
   - Only allow known callers if possible
   - Log all webhook requests in Apps Script

5. **Use HTTPS for webhook URLs:**
   - Never transmit leads over plain HTTP

---

## 📝 Example: Complete Setup from Scratch

```bash
# 1. Clone repo
git clone https://github.com/surendra-Gd/leadsaas.git
cd leadsaas

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up Google Cloud APIs (see section above)
# → Copy API key to env var or config.json

# 4. Set up Apps Script (see section above)
# → Copy webhook URL to env var or config.json

# 5. Run standalone test
export GOOGLE_PLACES_API_KEY="AIzaSy..."
export SHEETS_WEBHOOK_URL="https://script.google.com/..."
python bot.py

# 6. Start dashboard server
uvicorn main:app --host 0.0.0.0 --port 8000

# 7. Open dashboard in browser
# http://localhost:8000

# 8. Configure: click "Config" tab, set limits, save

# 9. Start bot: click "Start Bot" button

# 10. Monitor: watch dashboard update in real-time

# 11. Stop: click "Stop Bot" or let it finish naturally

# 12. Review results: "Leads" tab shows all scraped leads
```

---

## 📋 Requirements

```
fastapi==0.104.1
uvicorn==0.24.0
pydantic==2.4.2
requests==2.31.0
python-dateutil==2.8.2
```

> **Python 3.9+** required

---

## 📄 License

[Add your license here]

---

## 🙌 Support

For issues or questions:
1. Check `data/api.log` for detailed error messages
2. Review `/metrics` endpoint for bot state
3. Verify API keys and webhook URL are correct
4. Test Places API directly: `curl -H "X-Goog-Api-Key: KEY" "https://places.googleapis.com/v1/places:searchText"`

---

**Last Updated:** Feb 23, 2026 | **Version:** 3.1.0
