/**
 * Tradier Live Market Data Updater — Options Trade Tracker
 *
 * Pulls current bid/ask, delta, and IV for every OPEN chain listed on the
 * "Market Data" tab (columns A-E there are auto-populated from Chain Summary).
 * Writes results into columns F (Bid), G (Ask), I (Delta), J (IV %), K (Last Updated).
 *
 * SETUP
 * 1. In the spreadsheet: Extensions > Apps Script.
 * 2. Delete the placeholder code (myFunction) and paste this whole file in
 *    (as e.g. Code.gs).
 * 3. Add a second file, Token.gs, containing only:
 *      const TRADIER_TOKEN = "your token from developer.tradier.com here";
 *    (Settings > API Access on developer.tradier.com for the token itself.
 *    A sandbox token works for delayed data.) Keeping the token in its own
 *    file makes it easy to avoid pasting it anywhere else, like into chat.
 * 4. If you only have a sandbox token, change TRADIER_BASE_URL below to
 *    "https://sandbox.tradier.com/v1".
 * 5. Run > updateMarketData once and approve the permissions prompt
 *    (it only calls Tradier and writes to this sheet).
 * 6. Click the clock icon (Triggers) > Add Trigger > updateMarketData >
 *    Time-driven > Minutes timer > Every 15 minutes.
 * 7. Reload the spreadsheet — you should also see a "Tradier" menu with a
 *    manual "Refresh Market Data Now" option.
 *
 * Note: the trigger above runs all day, but updateMarketData() exits
 * immediately outside 9:30am-5:00pm ET on weekdays (see isMarketOpen below),
 * so it won't waste API calls overnight or on weekends.
 */

// TRADIER_TOKEN is declared in Token.gs — don't redeclare it here, Apps Script
// shares one global scope across all files in the project.
const TRADIER_BASE_URL = "https://api.tradier.com/v1"; // sandbox: https://sandbox.tradier.com/v1
const SHEET_NAME = "Market Data";
const FIRST_DATA_ROW = 5;
const LAST_DATA_ROW = 101;

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Tradier")
    .addItem("Refresh Market Data Now", "updateMarketDataForce")
    .addItem("Update Strike Screener", "runStrikeScreener")
    .addItem("Clean Up Market Data", "cleanupMarketDataRows")
    .addToUi();
}

function isMarketOpen() {
  const now = new Date();
  const dayName = Utilities.formatDate(now, "America/New_York", "EEEE");
  const hhmm = Number(Utilities.formatDate(now, "America/New_York", "HHmm"));
  const isWeekday = dayName !== "Saturday" && dayName !== "Sunday";
  const isDuringHours = hhmm >= 930 && hhmm <= 1700; // 9:30am - 5:00pm ET
  return isWeekday && isDuringHours;
}

// Used by the time-driven trigger — respects market hours.
function updateMarketData() {
  if (!isMarketOpen()) return;
  runMarketDataUpdate();
}

// Used by the "Refresh Market Data Now" menu item — always runs, ignores market hours.
function updateMarketDataForce() {
  runMarketDataUpdate();
}

function runMarketDataUpdate() {
  const ss = SpreadsheetApp.getActive();
  const sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error('Sheet "' + SHEET_NAME + '" not found');

  const numRows = LAST_DATA_ROW - FIRST_DATA_ROW + 1;
  const range = sheet.getRange(FIRST_DATA_ROW, 1, numRows, 12); // A:L (C = Stock Price, inserted after Symbol)
  const rows = range.getValues();

  const chainCache = {}; // "SYMBOL|YYYY-MM-DD" -> parsed chain, so each symbol/expiration is fetched once

  for (let i = 0; i < rows.length; i++) {
    const [chainId, symbol, spot, type, strike, expiration] = rows[i];
    const row = FIRST_DATA_ROW + i;
    if (!chainId || !symbol || !strike || !expiration || !type) continue;

    const expStr = formatExpiration(expiration);
    const cacheKey = symbol + "|" + expStr;

    try {
      if (!chainCache[cacheKey]) {
        chainCache[cacheKey] = fetchOptionChain(symbol, expStr);
        Utilities.sleep(200); // stay well under Tradier's rate limit
      }
      const chain = chainCache[cacheKey];
      const match = findOption(chain, strike, type);
      if (!match) {
        sheet.getRange(row, 13).setValue("No match found");
        continue;
      }
      sheet.getRange(row, 7).setValue(match.bid);                                 // G Bid
      sheet.getRange(row, 8).setValue(match.ask);                                 // H Ask
      sheet.getRange(row, 10).setValue(match.greeks ? match.greeks.delta : "");    // J Delta
      sheet.getRange(row, 11).setValue(match.greeks ? match.greeks.mid_iv : "");   // K IV
      sheet.getRange(row, 12).setValue(match.greeks ? match.greeks.theta : "");    // L theta
      sheet.getRange(row, 13).setValue(new Date());                               // L Last Updated
    } catch (err) {
      sheet.getRange(row, 13).setValue("Error: " + err.message);
    }
  }

  updateDashboardRefreshTime();
}

// cleanupMarketDataRows() lives in Helpers.gs now -- see that file.

function fetchOptionChain(symbol, expiration) {
  const url = TRADIER_BASE_URL + "/markets/options/chains?symbol=" + encodeURIComponent(symbol)
    + "&expiration=" + expiration + "&greeks=true";
  const response = UrlFetchApp.fetch(url, {
    method: "get",
    headers: {
      Authorization: "Bearer " + TRADIER_TOKEN,
      Accept: "application/json"
    },
    muteHttpExceptions: true
  });
  const json = JSON.parse(response.getContentText());
  if (!json.options || !json.options.option) {
    throw new Error("No option chain returned for " + symbol + " " + expiration);
  }
  // Tradier returns a single object (not an array) when there's only one contract
  return Array.isArray(json.options.option) ? json.options.option : [json.options.option];
}

// Mobile-friendly manual refresh: check a checkbox cell to trigger a refresh.
// The Sheets mobile app can't run custom menu items or drawing-assigned
// scripts, but editing a cell (including a checkbox) fires this trigger fine.
// Must be added as an INSTALLABLE "On edit" trigger (Triggers > Add Trigger),
// not left as a bare onEdit(e) — simple triggers can't call UrlFetchApp.
const REFRESH_SHEET = "Dashboard"; // sheet with the checkbox — matches the renamed tab
const REFRESH_CELL = "A6"; // cell holding the checkbox — adjust to match
const REFRESH_DATE = "B6"; // cell showing when the last refresh finished

function handleRefreshCheckbox(e) {
  if (!e || !e.range) return;
  if (e.range.getSheet().getName() !== REFRESH_SHEET) return;
  if (e.range.getA1Notation() !== REFRESH_CELL) return;
  if (e.value !== "TRUE") return; // only fire on check, not uncheck

  updateMarketDataForce();
  e.range.setValue(false); // reset so it can be tapped again next time
}

function updateDashboardRefreshTime() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(REFRESH_SHEET);
  if (!sheet) throw new Error('Sheet "' + REFRESH_SHEET + '" not found');
  sheet.getRange(REFRESH_DATE).setValue(new Date());
}

// Optional: force a refresh at ~6:31am (pre-market, before isMarketOpen() would
// normally allow it) regardless of the regular timer. Apps Script triggers are
// only accurate to about +/-15 minutes even with nearMinute() specified, so
// treat this as "sometime around 6:31am," not exact. Run createMorningRefreshTrigger
// once manually (Run menu) to install it; re-running it is safe, it de-dupes itself.
function createMorningRefreshTrigger() {
  deleteMorningRefreshTrigger();
  ScriptApp.newTrigger("updateMarketDataForce")
    .timeBased()
    .atHour(6)
    .nearMinute(31)
    .everyDays(1)
    .create();
}

function deleteMorningRefreshTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === "updateMarketDataForce" && t.getEventType() === ScriptApp.EventType.CLOCK) {
      ScriptApp.deleteTrigger(t);
    }
  });
}

// ============================================================
// Strike Screener — ad-hoc covered call / CSP pricing scanner.
// Sheet "Strike Screener", row 5 (labels in row 4):
//   A5 Ticker (dropdown+override), B5 Current Price (=GOOGLEFINANCE(A5,"price")),
//   C5 Type (Call/Put), D5 Start Strike, E5 End Strike, F5 Interval,
//   G5 Clear Strike checkbox, H5 Run checkbox, I5 Last Run.
//   J4 "Dates" label, K4:R4 the next 8 Fridays (formulas, K4 =
//   TODAY()+MOD(6-WEEKDAY(TODAY()),7), each next cell = previous+7),
//   K5:R5 checkboxes under each date. S5 Extra/override dates
//   (comma-separated, additive with whatever's checked in K5:R5; a bare
//   number = weeks out snapped to the closest real expiration, or a
//   literal YYYY-MM-DD date -- for non-Friday weeklies or Friday holidays).
// Results populate from row 8 down, columns A-J: Type, DTE, Expiration,
// Strike, Bid, Mid, Ask, Delta, IV %, Premium ($/contract). Premium uses
// Bid (not Mid) since that's the realistic fill price when you're the
// one selling.
// Wire G5 and H5 as an INSTALLABLE "On edit" trigger -> handleScreenerCheckbox.
// ============================================================
const SCREENER_SHEET = "Strike Screener";
const SCR_TICKER = "A5", SCR_PRICE = "B5", SCR_TYPE = "C5",
      SCR_START = "D5", SCR_END = "E5", SCR_INTERVAL = "F5",
      SCR_CLEAR = "G5", SCR_RUN = "H5", SCR_LAST_RUN = "I5";
const SCR_DATE_CELLS = ["K4", "L4", "M4", "N4", "O4", "P4", "Q4", "R4"];
const SCR_CHECKBOX_CELLS = ["K5", "L5", "M5", "N5", "O5", "P5", "Q5", "R5"];
const SCR_EXTRA_DATES = "S5";
const SCR_TABLE_ROW = 8, SCR_TABLE_COL = 1, SCR_CLEAR_ROWS = 500;

function handleScreenerCheckbox(e) {
  if (!e || !e.range) return;
  if (e.range.getSheet().getName() !== SCREENER_SHEET) return;
  if (e.value !== "TRUE") return;
  const cell = e.range.getA1Notation();

  if (cell === SCR_CLEAR) {
    const sheet = e.range.getSheet();
    sheet.getRange(SCR_START).clearContent();
    sheet.getRange(SCR_END).clearContent();
    e.range.setValue(false);
    return;
  }
  if (cell === SCR_RUN) {
    runStrikeScreener();
    e.range.setValue(false);
    return;
  }
}

function runStrikeScreener() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(SCREENER_SHEET);
  if (!sheet) throw new Error('Sheet "' + SCREENER_SHEET + '" not found');

  const symbol = sheet.getRange(SCR_TICKER).getValue().toString().trim().toUpperCase();
  let intervalRaw = sheet.getRange(SCR_INTERVAL).getValue();
  let startStrike = Number(sheet.getRange(SCR_START).getValue());
  let endStrike = Number(sheet.getRange(SCR_END).getValue());
  const typeRaw = sheet.getRange(SCR_TYPE).getValue().toString().trim().toLowerCase();
  const optionType = typeRaw.indexOf("p") === 0 ? "put" : "call";

  if (!symbol) {
    sheet.getRange(SCR_LAST_RUN).setValue("Missing ticker (A5)");
    return;
  }

  // Default strike interval to 10 when left blank -- and write it back to
  // F5 so it's visible/adjustable, rather than silently using 10 under
  // the hood. A present-but-invalid value (0, negative, non-numeric)
  // still blocks the run instead of guessing.
  if (intervalRaw === "" || intervalRaw === null) {
    intervalRaw = 10;
    sheet.getRange(SCR_INTERVAL).setValue(intervalRaw);
  }
  const interval = Number(intervalRaw);
  if (!interval || interval <= 0) {
    sheet.getRange(SCR_LAST_RUN).setValue("Invalid strike interval (F5)");
    return;
  }

  sheet.getRange(SCR_TABLE_ROW, SCR_TABLE_COL, SCR_CLEAR_ROWS, 10).clearContent();

  // Default strike range when left blank. Puts (CSPs) look below spot;
  // calls (CCs) look above spot -- ranges are asymmetric on purpose.
  // Put:  start = 25% below spot (floored to nearest 10), end = 5% below spot (ceiled to nearest 10).
  // Call: start = 5% above spot (floored to nearest 10), end = 30% above spot (ceiled to nearest 10).
  // Spot comes straight from B5's live GOOGLEFINANCE price -- no separate
  // Tradier quote call needed just to compute a default range.
  if (!startStrike || !endStrike) {
    const spot = Number(sheet.getRange(SCR_PRICE).getValue());
    if (!spot) {
      sheet.getRange(SCR_LAST_RUN).setValue("No price in " + SCR_PRICE);
      return;
    }
    const lowPct = optionType === "call" ? 1.05 : 0.75;
    const highPct = optionType === "call" ? 1.30 : 0.95;
    if (!startStrike) {
      startStrike = Math.floor((spot * lowPct) / 10) * 10;
      sheet.getRange(SCR_START).setValue(startStrike);
    }
    if (!endStrike) {
      endStrike = Math.ceil((spot * highPct) / 10) * 10;
      sheet.getRange(SCR_END).setValue(endStrike);
    }
  }

  let available;
  try {
    available = fetchExpirations(symbol);
  } catch (err) {
    sheet.getRange(SCR_LAST_RUN).setValue("Error fetching expirations: " + err.message);
    return;
  }

  const targetExpirations = collectSelectedExpirations(sheet, available);
  const strikes = [];
  for (let s = startStrike; s <= endStrike; s += interval) strikes.push(s);

  let outRow = SCR_TABLE_ROW;
  const today = new Date();

  targetExpirations.forEach(function (exp) {
    let chain;
    try {
      chain = fetchOptionChain(symbol, exp);
    } catch (err) {
      sheet.getRange(outRow, SCR_TABLE_COL).setValue("Error for " + exp + ": " + err.message);
      outRow++;
      return;
    }
    strikes.forEach(function (strike) {
      // Nearest-strike match, not exact -- actual listed strikes won't always
      // land on a clean multiple of the interval. The actual matched strike
      // (which may differ slightly from the requested one) is what gets shown.
      const match = findClosestOption(chain, strike, optionType);
      const actualStrike = match ? Number(match.strike) : strike;
      const dte = Math.round((new Date(exp) - today) / 86400000);
      const mid = (match && match.bid != null && match.ask != null) ? (match.bid + match.ask) / 2 : "";
      const premium = (match && match.bid != null) ? match.bid * 100 : "";
      const typeLabel = optionType === "call" ? "Call" : "Put";
      const row = [
        typeLabel, dte, exp, actualStrike,
        match ? match.bid : "", mid, match ? match.ask : "",
        match && match.greeks ? match.greeks.delta : "",
        match && match.greeks ? match.greeks.mid_iv : "",
        match && match.greeks ? match.greeks.theta : "",
        premium
      ];
      sheet.getRange(outRow, SCR_TABLE_COL, 1, row.length).setValues([row]);
      outRow++;
    });
  });

  sheet.getRange(SCR_LAST_RUN).setValue(new Date());
}

// Combines the checked K5:O5 Fridays with whatever's typed in P5 (additive,
// not either/or). Checked Fridays snap to the closest real listed
// expiration (handles holiday closures automatically); P5 entries accept
// either a bare "weeks out" number or a literal YYYY-MM-DD date, for
// non-Friday weeklies or anything the 5-Friday grid doesn't cover.
function collectSelectedExpirations(sheet, available) {
  const result = [];

  for (let i = 0; i < SCR_DATE_CELLS.length; i++) {
    if (sheet.getRange(SCR_CHECKBOX_CELLS[i]).getValue() !== true) continue;
    const dateVal = sheet.getRange(SCR_DATE_CELLS[i]).getValue();
    const closest = closestExpiration(new Date(dateVal), available);
    if (closest) result.push(closest);
  }

  const extraRaw = sheet.getRange(SCR_EXTRA_DATES).getValue().toString();
  extraRaw.split(",").map(function (p) { return p.trim(); }).filter(function (p) { return p; })
    .forEach(function (p) {
      const target = /^\d+$/.test(p) ? nthFridayOut(Number(p)) : new Date(p);
      const closest = closestExpiration(target, available);
      if (closest) result.push(closest);
    });

  return result.filter(function (v, i) { return result.indexOf(v) === i; }); // de-dupe
}

function nthFridayOut(weeksOut) {
  const today = new Date();
  const dayOfWeek = today.getDay(); // 0=Sun ... 5=Fri ... 6=Sat
  const daysUntilFriday = (5 - dayOfWeek + 7) % 7; // 0 if today is already Friday
  const firstFriday = new Date(today.getTime() + daysUntilFriday * 86400000);
  return new Date(firstFriday.getTime() + (weeksOut - 1) * 7 * 86400000);
}

// Nearest-strike match for the Screener only. Deliberately separate from
// findOption() above, which does exact matching and is used for your real
// open positions in runMarketDataUpdate -- that one should never "snap" to
// a different strike than what you actually hold.
function findClosestOption(chain, targetStrike, type) {
  const candidates = chain.filter(function (o) { return o.option_type === type; });
  if (candidates.length === 0) return null;
  let best = candidates[0];
  let bestDiff = Math.abs(Number(best.strike) - targetStrike);
  candidates.forEach(function (o) {
    const diff = Math.abs(Number(o.strike) - targetStrike);
    if (diff < bestDiff) { best = o; bestDiff = diff; }
  });
  return best;
}

function fetchQuote(symbol) {
  const url = TRADIER_BASE_URL + "/markets/quotes?symbols=" + encodeURIComponent(symbol);
  const response = UrlFetchApp.fetch(url, {
    method: "get",
    headers: { Authorization: "Bearer " + TRADIER_TOKEN, Accept: "application/json" },
    muteHttpExceptions: true
  });
  const json = JSON.parse(response.getContentText());
  const quote = json.quotes && json.quotes.quote;
  if (!quote) throw new Error("No quote returned for " + symbol);
  return quote.last != null ? quote.last : (quote.bid + quote.ask) / 2;
}

function closestExpiration(targetDate, available) {
  let best = null, bestDiff = Infinity;
  available.forEach(function (a) {
    const diff = Math.abs(new Date(a).getTime() - targetDate.getTime());
    if (diff < bestDiff) { bestDiff = diff; best = a; }
  });
  return best;
}

function fetchExpirations(symbol) {
  const url = TRADIER_BASE_URL + "/markets/options/expirations?symbol=" + encodeURIComponent(symbol) + "&includeAllRoots=true";
  const response = UrlFetchApp.fetch(url, {
    method: "get",
    headers: { Authorization: "Bearer " + TRADIER_TOKEN, Accept: "application/json" },
    muteHttpExceptions: true
  });
  const json = JSON.parse(response.getContentText());
  if (!json.expirations || !json.expirations.date) return [];
  const dates = json.expirations.date;
  return Array.isArray(dates) ? dates : [dates];
}

function findOption(chain, strike, type) {
  const wantType = String(type).toLowerCase().indexOf("c") === 0 ? "call" : "put";
  return chain.find(function (o) {
    return Number(o.strike) === Number(strike) && o.option_type === wantType;
  });
}

function formatExpiration(value) {
  // Accepts a JS Date (Sheets date cell) or a date-like string; returns YYYY-MM-DD
  const d = value instanceof Date ? value : new Date(value);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return yyyy + "-" + mm + "-" + dd;
}