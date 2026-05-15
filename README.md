# house_hunt

Personal North-London house-hunting tool that scrapes RightMove daily, enriches each listing with location and commute data, and serves a filterable/mappable view via a Streamlit app.

## What it does

- **Scrapes** RightMove search results matching `minBedrooms=2`, `maxPrice=£750k`, custom polygon area `8910875`.
- **Enriches** each property with:
  - Approximate postcode (reverse-geocoded from RightMove's map pin lat/lon)
  - Distance to nearest Tube / Overground / National Rail station
  - Flood risk (UK Environment Agency)
  - Commute time to two work locations (TfL Journey Planner, arriving 09:00 weekday)
  - Square metres (from listing text or floor plan)
  - For flats: ground-floor flag, service charge
  - Last-sold price + implied annual price growth
  - HMLR-based fair-value estimate
- **Stores** everything in a SQLite database, version-controlled in this repo.
- **Refreshes** daily at 18:00 UK time via GitHub Actions.
- **Serves** a Streamlit app with a left-side filter panel, sortable list view, and map view. NA values pass through filters.

## Repo layout

```
house_hunt/
├── scraper/         # scraping + enrichment Python code
├── app/             # Streamlit app
├── data/            # SQLite DB (committed; small enough that git handles fine)
├── .github/workflows/scrape.yml  # daily cron
├── requirements.txt
└── README.md
```

## Local dev

```bash
# one-time setup
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# scrape (slow first time, ~5-15 min)
python -m scraper.main

# run the app
streamlit run app/app.py
```

## Required secrets

- `TFL_APP_KEY` — free from https://api-portal.tfl.gov.uk/

## Cloud deploy

- GitHub Actions runs the scraper daily at 17:00 UTC (= 18:00 UK during BST, 17:00 UK during GMT — TODO: adjust for winter).
- Streamlit Community Cloud auto-deploys from `main` and reads the committed SQLite DB.
