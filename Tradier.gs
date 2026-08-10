/**
 * Tradier Live Market Data Updater — Options Trade Tracker
 *
 * Pulls current bid/ask, delta, and IV for every OPEN chain listed on the
 * "Market Data" tab (columns A-E there are auto-populated from Chain Summary).
 * Writes results into columns G (Bid), H (Ask), J (Delta), K (IV %), L (Last Updated).
 *
 * SETUP
 * 1. In the spreadsheet: Extensions > Apps Script.
 * 2. Delete the placeholder code (myFunction) and paste this whole file in.
 * 3. Replace TRADIER_TOKEN below with your token from developer.tradier.com
 *    (Settings > API Access). A sandbox token works for delayed data.
 * 4. If you only have a sandbox token, change TRADIER_BASE_URL to
 *    "https://sandbox.tradier.com/v1".
 * 5. Run > updateMarketData once and approve the permissions prompt
 *    (it only calls Tradier and writes to this sheet).
 * 6. Click the clock icon (Triggers) > Add Trigger > updateMarketData >
 *    Time-driven > Minutes timer > Every 15 minutes.
 * 7. Reload the spreadsheet — you should also see a "Tradier" menu with a
 *    manual "Refresh Market Data Now" option.
 */

// Store another file that has const TRADIER_TOKEN = ""; for the actual token.
const TRADIER_BASE_URL = "https://api.tradier.com/v1"; // sandbox: https://sandbox.tradier.com/v1
const SHEET_NAME = "Market Data";
const FIRST_DATA_ROW = 5;
const LAST_DATA_ROW = 101;
 
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Tradier")
    .addItem("Refresh Market Data Now", "updateMarketDataForce")
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
  const range = sheet.getRange(FIRST_DATA_ROW, 1, numRows, 12); // A:L
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
        sheet.getRange(row, 12).setValue("No match found");
        continue;
      }
      sheet.getRange(row, 7).setValue(match.bid);                                 // G Bid
      sheet.getRange(row, 8).setValue(match.ask);                                 // H Ask
      sheet.getRange(row, 10).setValue(match.greeks ? match.greeks.delta : "");     // J Delta
      sheet.getRange(row, 11).setValue(match.greeks ? match.greeks.mid_iv : "");   // K IV
      sheet.getRange(row, 12).setValue(new Date());                               // L Last Updated
    } catch (err) {
      sheet.getRange(row, 12).setValue("Error: " + err.message);
    }
  }
}
 
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
