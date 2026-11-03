/**
 * Helpers.gs — misc maintenance functions, kept separate from the main
 * Tradier script file so that file doesn't get cluttered. Apps Script
 * shares one global scope across every file in the project, so
 * SHEET_NAME (declared in the main file) resolves fine here too.
 */
 
// Clears leftover data on Market Data rows whose Chain ID (column A) is
// blank -- e.g. a position that was closed/removed from Chain Summary but
// still has stale Bid/Ask/Delta/IV/Theta/Last Updated values sitting
// around. Only clears specific columns (not the whole row), leaving A-F
// (Chain ID, Symbol, Spot, Type, Strike, Expiration) and Mid (I) untouched;
// it never deletes/shifts rows, so nothing below row 100 is affected and
// row positions stay stable.
const CLEANUP_FIRST_ROW = 5, CLEANUP_LAST_ROW = 100;
const CLEANUP_COLUMNS = [7, 8, 10, 11, 12, 13]; // G Bid, H Ask, J Delta, K IV, L Theta, M Last Updated
 
function cleanupMarketDataRows() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error('Sheet "' + SHEET_NAME + '" not found');
 
  const numRows = CLEANUP_LAST_ROW - CLEANUP_FIRST_ROW + 1;
  const chainIds = sheet.getRange(CLEANUP_FIRST_ROW, 1, numRows, 1).getValues();
 
  chainIds.forEach(function (r, i) {
    const chainId = r[0];
    if (chainId === "" || chainId === null) {
      const row = CLEANUP_FIRST_ROW + i;
      CLEANUP_COLUMNS.forEach(function (col) {
        sheet.getRange(row, col).clearContent();
      });
    }
  });
}

/**
 * Google Finance-compatible ticker, using the "Tickers" sheet (column A:
 * Ticker as used everywhere else in this workbook, column B: Google
 * Finance override, e.g. "BATS:DRAM") when a matching row exists,
 * otherwise the ticker unchanged. Only a couple of ETFs need an override
 * -- everything else just passes through.
 * Usage: =GOOGLEFINANCE(GFTICKER(A5), "price")
 */
function GFTICKER(ticker) {
  if (!ticker) return ticker;
  const sheet = SpreadsheetApp.getActive().getSheetByName("Tickers");
  if (!sheet) return ticker;
  const data = sheet.getDataRange().getValues();
  for (let i = 0; i < data.length; i++) {
    if (String(data[i][0]).trim().toUpperCase() === String(ticker).trim().toUpperCase() && data[i][1]) {
      return data[i][1];
    }
  }
  return ticker;
}
