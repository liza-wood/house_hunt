# Setup walkthrough

About 15 minutes total. Free for everything.

## 1. Local install + first scrape (~10 min)

```bash
cd ~/Documents/Claude/Projects/house_hunt
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Get a free **TfL API key** at https://api-portal.tfl.gov.uk/profile (sign up → "Add subscription" → free tier). Put it in `.env`:

```bash
cp .env.example .env
echo 'TFL_APP_KEY=your-key-here' >> .env
```

Run a tiny test scrape (1 page, 5 properties — fast, lets us check everything works):

```bash
RM_MAX_PAGES=1 RM_MAX_PROPERTIES=5 python -m scraper.main
streamlit run app/app.py
```

If listings show up with prices/addresses, great — kill Streamlit and run the full scrape:

```bash
python -m scraper.main
```

Expect ~10–15 min for ~200 listings.

## 2. GitHub for the daily cloud refresh (~5 min)

1. Go to https://github.com/new → create a **private** repo called `house_hunt`.
2. From the project folder:

   ```bash
   git init
   git add .
   git commit -m "initial commit"
   git branch -M main
   git remote add origin git@github.com:<you>/house_hunt.git
   git push -u origin main
   ```

3. In the repo → Settings → Secrets and variables → Actions → "New repository secret":
   - Name: `TFL_APP_KEY`
   - Value: your TfL key.

4. Go to the Actions tab → run "Daily RightMove refresh" → "Run workflow" once manually to verify it works.

If the manual run succeeds, the cron will fire every day at 17:00 UTC (= 18:00 UK in summer).

## 3. Streamlit Cloud (~3 min)

1. Go to https://share.streamlit.io/ → "New app" → pick your `house_hunt` repo.
2. Set **Main file path** to `app/app.py`.
3. Deploy. Streamlit Cloud will redeploy automatically each time GitHub Actions commits new data.

You'll get a permanent URL like `https://your-name-house-hunt.streamlit.app/`.

## Troubleshooting

**GitHub Actions run fails with "Access denied / 403" from RightMove:**
Their bot detection caught the runner IP. Switch to running the scraper from your Mac (launchd) instead — easiest fix; full instructions in `LOCAL_CRON.md` (TODO if we hit this).

**No data in the app:**
Check `data/houses.db` exists locally. If empty, run `python -m scraper.main` directly and watch the output.

**Stale Streamlit Cloud data:**
Streamlit Cloud caches the SQLite file. The app uses a 5-minute cache TTL; reload after that. If it's still stale, hit "Reboot" in the Streamlit Cloud dashboard.
