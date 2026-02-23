/**
 * apps_script_webhook.js
 * ──────────────────────
 * Google Apps Script Web App — receives lead data from LeadGenBot
 * via HTTP POST and appends a row to the "Leads" sheet.
 *
 * SETUP
 * ─────
 * 1. Open your Google Sheet → Extensions → Apps Script
 * 2. Paste this entire file, replacing any existing Code.gs content
 * 3. Save (Ctrl+S)
 * 4. Click Deploy → New deployment
 *    • Type:          Web app
 *    • Execute as:    Me
 *    • Who has access: Anyone  (so the Python bot can POST without OAuth)
 * 5. Click Deploy → copy the Web App URL
 * 6. Paste the URL into your bot's config:
 *      data/config.json → "google_sheets_webhook_url": "<URL>"
 *    or set the env var:
 *      SHEETS_WEBHOOK_URL=<URL>
 *
 * EXPECTED PAYLOAD (from LeadGenBot)
 * ───────────────────────────────────
 * POST body (JSON):
 * {
 *   "place_id":  "ChIJ...",
 *   "name":      "Ravi Kumar Tailors",
 *   "phone":     "+91 98765 43210",
 *   "rating":    "4.2",
 *   "category":  "Tailors",
 *   "location":  "Hyderabad, India"
 * }
 *
 * RESPONSE
 * ────────
 * HTTP 200 + JSON  {"status": "ok",      "row": <row number>}
 * HTTP 200 + JSON  {"status": "skipped", "reason": "duplicate"}
 * HTTP 400 + JSON  {"status": "error",   "message": "<detail>"}
 */

// ── Sheet name ────────────────────────────────────────────────
var SHEET_NAME = "Leads";

// ── Column order — must match HEADERS below ───────────────────
var HEADERS = [
  "place_id",
  "name",
  "phone",
  "rating",
  "category",
  "location",
  "discovered_at",
];

// ─────────────────────────────────────────────────────────────
// doPost(e)  — entry point for HTTP POST requests
// ─────────────────────────────────────────────────────────────
function doPost(e) {
  try {
    // Parse incoming JSON body
    var payload = JSON.parse(e.postData.contents);

    // Validate required fields
    if (!payload.place_id || !payload.name) {
      return jsonResponse(400, {
        status: "error",
        message: "Missing required fields: place_id, name",
      });
    }

    var sheet = getOrCreateSheet();

    // ── Duplicate check ──────────────────────────────────────
    // Scan column A (place_id) for an existing match.
    // This is a safety net; the Python bot also deduplicates in-memory.
    var lastRow  = sheet.getLastRow();
    if (lastRow > 1) {
      var existingIds = sheet
        .getRange(2, 1, lastRow - 1, 1)
        .getValues()
        .map(function (row) { return row[0]; });

      if (existingIds.indexOf(payload.place_id) !== -1) {
        return jsonResponse(200, {
          status: "skipped",
          reason: "duplicate",
          place_id: payload.place_id,
        });
      }
    }

    // ── Append row ───────────────────────────────────────────
    var now = new Date().toISOString().replace("T", " ").slice(0, 19);
    var row = [
      payload.place_id         || "",
      payload.name             || "",
      payload.phone            || "",
      String(payload.rating    || ""),
      payload.category         || "",
      payload.location         || "",
      now,
    ];

    sheet.appendRow(row);

    return jsonResponse(200, {
      status: "ok",
      row:    sheet.getLastRow(),
      name:   payload.name,
    });

  } catch (err) {
    return jsonResponse(400, {
      status:  "error",
      message: err.toString(),
    });
  }
}

// ─────────────────────────────────────────────────────────────
// doGet(e)  — simple health-check for browser / curl testing
// ─────────────────────────────────────────────────────────────
function doGet(e) {
  var sheet    = getOrCreateSheet();
  var lastRow  = Math.max(0, sheet.getLastRow() - 1); // exclude header
  return jsonResponse(200, {
    status:     "ok",
    service:    "LeadRadar Apps Script Webhook",
    sheet_name: SHEET_NAME,
    total_leads: lastRow,
  });
}

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────

/**
 * Return an existing "Leads" sheet or create it with headers.
 */
function getOrCreateSheet() {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow(HEADERS);

    // Freeze header row and bold it
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, HEADERS.length)
      .setFontWeight("bold")
      .setBackground("#E8F0FE");
  }

  return sheet;
}

/**
 * Build a JSON ContentService response with the given HTTP status code.
 * Apps Script always returns HTTP 200 at the transport level; the
 * status field inside the JSON body carries the semantic status.
 */
function jsonResponse(httpCode, payload) {
  // Note: Apps Script ignores httpCode for deployed web apps —
  // all responses arrive as 200 at the HTTP level. The caller
  // must check the "status" field in the JSON body.
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
