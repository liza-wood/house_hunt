"""Diagnose v3: parse the new structure and verify our extraction works.

Run this after running the v2 diagnose. It uses the same Playwright dance to
load a search page + property page, but this time it parses the data with our
new flight-format resolver and dumps the resolved JSON.

Outputs:
    data/_diagnose/search_results_shape.txt   — shape of pageProps.searchResults
    data/_diagnose/search_first_property.json — first hit from the listing
    data/_diagnose/property_resolved.json     — fully resolved propertyData
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from scraper.config import SearchConfig, USER_AGENT
from scraper.flight import resolve_compact_dedup


SEARCH_URL = SearchConfig().search_url(0)
OUT_DIR = Path("data/_diagnose")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def shape(obj, depth=3, max_list=2):
    if depth == 0:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: shape(v, depth - 1, max_list) for k, v in obj.items()}
    if isinstance(obj, list):
        return [f"list[{len(obj)}]"] + [shape(v, depth - 1, max_list) for v in obj[:max_list]]
    if isinstance(obj, str):
        return f"str[{len(obj)}]"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return type(obj).__name__
    return type(obj).__name__


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-GB",
            timezone_id="Europe/London",
        )
        page = await context.new_page()

        # =================== SEARCH ===================
        print(f"== SEARCH ==\n{SEARCH_URL}")
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2500)
        for sel in ("#onetrust-reject-all-handler", "button:has-text(\"Reject All\")"):
            try:
                await page.locator(sel).first.click(timeout=1500)
                break
            except Exception:
                pass

        next_raw = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent || ''"
        )
        nd = json.loads(next_raw) if next_raw else {}
        page_props = nd.get("props", {}).get("pageProps", {})
        search_results = page_props.get("searchResults") or {}
        print(f"  searchResults top-level keys: {list(search_results.keys())[:20]}")
        (OUT_DIR / "search_results_shape.txt").write_text(
            json.dumps(shape(search_results, depth=3), indent=2, default=str),
            encoding="utf-8",
        )
        print(f"  saved → {OUT_DIR / 'search_results_shape.txt'}")

        # Find the property list — try common paths
        list_candidates = []
        for k, v in search_results.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                list_candidates.append((f"searchResults.{k}", v))
        print(f"\n  candidate list keys inside searchResults:")
        for path, lst in list_candidates:
            keys = sorted(lst[0].keys())[:25]
            print(f"    {path}: list[{len(lst)}], first item keys: {keys}")

        first_props_list = None
        for path, lst in list_candidates:
            # property-ish if it has 'id' or 'price' or 'bedrooms' or 'displayAddress'
            keys = set(lst[0].keys())
            if keys & {"id", "displayAddress", "bedrooms", "price"}:
                first_props_list = (path, lst)
                break
        if first_props_list:
            path, lst = first_props_list
            print(f"\n  → picking '{path}' as the property list")
            (OUT_DIR / "search_first_property.json").write_text(
                json.dumps(lst[0], indent=2, default=str), encoding="utf-8"
            )
            print(f"  saved → {OUT_DIR / 'search_first_property.json'}")
            prop_id = str(lst[0].get("id") or "")
        else:
            prop_id = ""

        # If we couldn't get an id from the JSON, scrape one from HTML
        if not prop_id:
            html = await page.content()
            ids = re.findall(r"/properties/(\d{6,})", html)
            prop_id = ids[0] if ids else "151000000"

        # =================== PROPERTY ===================
        prop_url = f"https://www.rightmove.co.uk/properties/{prop_id}#/?channel=RES_BUY"
        print(f"\n== PROPERTY ==\n{prop_url}")
        await page.goto(prop_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2500)

        # Pull window.__PAGE_MODEL — note the TWO underscores
        page_model = await page.evaluate(
            "() => window.__PAGE_MODEL ? window.__PAGE_MODEL : null"
        )
        if not page_model:
            print("  ✗ window.__PAGE_MODEL is undefined — cannot continue")
            return

        # Save the raw so we can inspect format
        (OUT_DIR / "property_pagemodel_raw.json").write_text(
            json.dumps(page_model, indent=2, default=str)[:2_000_000],
            encoding="utf-8",
        )
        print(f"  raw __PAGE_MODEL keys: {list(page_model.keys())}")

        # The 'data' field is a JSON string
        raw_data = page_model.get("data")
        if isinstance(raw_data, str):
            try:
                arr = json.loads(raw_data)
            except json.JSONDecodeError as e:
                print(f"  ✗ data JSON parse err: {e!r}")
                return
        elif isinstance(raw_data, list):
            arr = raw_data
        else:
            print(f"  ✗ unexpected 'data' type: {type(raw_data).__name__}")
            return

        print(f"  data array length: {len(arr)}")
        print(f"  arr[0]: {json.dumps(arr[0])[:300]}")

        # Resolve
        try:
            resolved = resolve_compact_dedup(arr)
        except Exception as e:
            print(f"  ✗ resolve err: {e!r}")
            return

        (OUT_DIR / "property_resolved.json").write_text(
            json.dumps(resolved, indent=2, default=str)[:2_000_000],
            encoding="utf-8",
        )
        print(f"  saved fully-resolved → {OUT_DIR / 'property_resolved.json'}")

        prop_data = resolved.get("propertyData") if isinstance(resolved, dict) else None
        if isinstance(prop_data, dict):
            print(f"\n  propertyData keys: {sorted(prop_data.keys())[:30]}")
            print("  spot-checks:")
            print(f"    bedrooms          = {prop_data.get('bedrooms')!r}")
            print(f"    bathrooms         = {prop_data.get('bathrooms')!r}")
            print(f"    propertySubType   = {prop_data.get('propertySubType')!r}")
            print(f"    tenure (raw)      = {prop_data.get('tenure')!r}")
            print(f"    address (raw)     = {prop_data.get('address')!r}")
            print(f"    prices (raw)      = {prop_data.get('prices')!r}")
            print(f"    location (raw)    = {prop_data.get('location')!r}")
            text = prop_data.get('text') or {}
            print(f"    text keys         = {list(text.keys()) if isinstance(text, dict) else type(text).__name__}")
            print(f"    keyFeatures       = {prop_data.get('keyFeatures')!r}")
            print(f"    sizings           = {prop_data.get('sizings')!r}")
            print(f"    firstVisibleDate  = {prop_data.get('firstVisibleDate')!r}")
            print(f"    listingHistory    = {(prop_data.get('listingHistory') or {}).get('listingUpdateDate')!r}")
            print(f"    misInfo           = {prop_data.get('misInfo')!r}")
        else:
            print(f"  ✗ resolved.propertyData not a dict: {type(prop_data).__name__}")

        await context.close()
        await browser.close()

    print("\n✓ diagnose v3 done. Paste back:")
    print("  - this console output")
    print("  - data/_diagnose/property_resolved.json (first ~80 lines, or attach)")


if __name__ == "__main__":
    asyncio.run(main())
