// Clears leftover data on Market Data rows whose Chain ID (column A) is
// blank -- e.g. a position that was closed/removed from Chain Summary but
// still has stale Bid/Ask/Delta/IV/Last Updated values sitting in G:L.
// Only clears content (values + any formulas) across A:L for those rows;
// it never deletes/shifts rows, so nothing below row 100 is affected and
// row positions stay stable. If column A holds a formula that should
// survive being "blank" (e.g. an ARRAYFORMULA spill), let me know and
// this can be narrowed to only clear columns G:L instead.
const CLEANUP_FIRST_ROW = 5, CLEANUP_LAST_ROW = 100;

function cleanupMarketDataRows() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error('Sheet "' + SHEET_NAME + '" not found');

  const numRows = CLEANUP_LAST_ROW - CLEANUP_FIRST_ROW + 1;
  const range = sheet.getRange(CLEANUP_FIRST_ROW, 1, numRows, 12); // A:L
  const values = range.getValues();

  values.forEach(function (row, i) {
    const chainId = row[0];
    if (chainId === "" || chainId === null) {
      sheet.getRange(CLEANUP_FIRST_ROW + i, 1, 1, 12).clearContent();
    }
  });
}
