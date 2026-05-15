"""Smoke test: scrape 2 properties end-to-end and dump what we got.

Usage:
    python -m scripts.smoke_test
or:
    python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running as `python scripts/smoke_test.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import rightmove
from scraper.config import SearchConfig


async def main() -> None:
    cfg = SearchConfig()
    # Force a small run
    os.environ.setdefault("RM_MAX_PAGES", "1")
    os.environ.setdefault("RM_MAX_PROPERTIES", "2")
    cfg = SearchConfig()  # re-read with overrides

    print("Step 1: collect search hits (first page only)…")
    hits = await rightmove.collect_search_hits(cfg)
    print(f"  got {len(hits)} hits")
    for h in hits[:5]:
        print(f"    {h.prop_id}  £{h.price}  {h.bedrooms}bd  {h.address!r}  added={h.added_on}")

    if not hits:
        print("FAIL: no search results — check the URL/selector/__NEXT_DATA__ parsing.")
        return

    print("\nStep 2: scrape first 2 property detail pages…")
    payloads = await rightmove.scrape_properties([h.prop_id for h in hits[:2]])

    for p in payloads:
        print("\n---")
        # Drop raw_json + description for readability
        slim = {k: v for k, v in p.items() if k not in ("raw_json", "description")}
        print(json.dumps(slim, indent=2, default=str))

    print("\n✓ smoke test done. If the JSON above looks sensible, run the full scrape:")
    print("  python -m scraper.main")


if __name__ == "__main__":
    asyncio.run(main())
