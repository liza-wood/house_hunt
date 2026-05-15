"""End-to-end refresh: scrape RightMove → enrich → write SQLite."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx
from dotenv import load_dotenv

from . import rightmove, enrich, tfl, hmlr, db
from .config import SearchConfig


async def refresh() -> None:
    load_dotenv()
    cfg = SearchConfig()
    db.init_db()

    print(f"== House-hunt refresh ==")
    print(f"  search: minBeds={cfg.min_bedrooms} maxPrice=£{cfg.max_price} location={cfg.location_id}")
    if cfg.max_pages:
        print(f"  CAPPED to first {cfg.max_pages} pages")
    if cfg.max_properties:
        print(f"  CAPPED to first {cfg.max_properties} properties")

    # 1. Search results -> property IDs (+ a few summary fields as fallback)
    print("\n[1/3] collecting search hits…")
    hits = await rightmove.collect_search_hits(cfg)
    print(f"  → {len(hits)} unique listings")

    # 2. Scrape each property's detail page
    print("\n[2/3] scraping property detail pages…")
    pids = [h.prop_id for h in hits]
    if cfg.max_properties:
        pids = pids[: cfg.max_properties]
    payloads = await rightmove.scrape_properties(pids)
    # Backfill from search hit if a detail field is missing
    by_id = {h.prop_id: h for h in hits}
    for p in payloads:
        h = by_id.get(str(p["prop_id"]))
        if not h:
            continue
        p.setdefault("price", h.price)
        p.setdefault("address", h.address)
        p.setdefault("bedrooms", h.bedrooms)
        p.setdefault("property_type", h.property_type)
        if not p.get("added_on"):
            p["added_on"] = h.added_on

    # 3. Enrich (postcode, stations, flood, commutes, valuation)
    print("\n[3/3] enriching…")
    async with httpx.AsyncClient(headers={"User-Agent": "house-hunt/0.1 (personal)"}) as client:
        for i, p in enumerate(payloads, 1):
            lat, lon = p.get("latitude"), p.get("longitude")
            # postcode (only if missing)
            if not p.get("postcode") and lat and lon:
                try:
                    p["postcode"] = await enrich.reverse_geocode(client, lat, lon)
                except Exception as e:
                    print(f"  postcode {p['prop_id']}: {e!r}")

            # stations
            if lat and lon:
                try:
                    p.update(await enrich.nearest_stations(client, lat, lon))
                except Exception as e:
                    print(f"  stations {p['prop_id']}: {e!r}")

            # flood
            try:
                p["flood_risk_band"] = await enrich.flood_risk(client, p.get("postcode"), lat, lon)
            except Exception as e:
                print(f"  flood {p['prop_id']}: {e!r}")

            # commute times
            try:
                p.update(await tfl.commute_times(client, p.get("postcode")))
            except Exception as e:
                print(f"  commute {p['prop_id']}: {e!r}")

            # valuation
            growth = hmlr.area_growth_for(p.get("postcode"))
            p["implied_annual_pct"] = enrich.implied_annual_growth(
                p.get("price"), p.get("last_sold_price"), p.get("last_sold_date")
            )
            p["fair_value_gbp"] = enrich.fair_value_estimate(
                p.get("last_sold_price"), p.get("last_sold_date"), growth
            )

            if i % 10 == 0 or i == len(payloads):
                print(f"  enriched {i}/{len(payloads)}")

    # Write to DB
    with db.connect() as conn:
        for p in payloads:
            db.upsert_property(conn, p)
    print(f"\n✓ Wrote {len(payloads)} properties to {db.DB_PATH}")


if __name__ == "__main__":
    sys.exit(asyncio.run(refresh()) or 0)
