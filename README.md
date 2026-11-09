# Forever AH tracker

GitHub runs `forever_ah.py` every hour and commits the price history
(`forever_ah.sqlite`) and the report back to this repo. With GitHub Pages
switched on, the latest report is live at:

    https://<your-username>.github.io/<repo-name>/

Settings (region / ruleset / faction) are in `.github/workflows/scan.yml`.
The Blizzard API keys live in the repo's Actions secrets, never in the code.
