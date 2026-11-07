# stocks: notes for Claude Code

Personal options-income tooling (cash-secured puts / covered calls). Two independent projects:

- `options-tracker-sheet/`: Google Apps Script (`Tradier.gs`, `Helpers.gs`) behind the owner's Google Sheets position
  tracker. Pulls Tradier quotes/greeks into sheet tabs and runs a per-ticker strike screener. No local runtime;
  the files are pasted into the sheet's Apps Script editor. `Token.gs` (holds the Tradier token) is gitignored and
  must never be committed or echoed.
- `screener/`: Python script that screens a watchlist for ≤0.20Δ puts and renders a static page with IV history.
  Read `screener/CLAUDE.md` first; it has the architecture, gotchas, status and next steps.

Rules for both:
- Secrets live in gitignored files (`.env`, `Token.gs`, `token.txt`). Never commit, print or paste them.
- Don't place trades or call any trading endpoint; these tools are read-only market-data consumers.
- Commit directly to the repo when asked; keep commits small and descriptive.
