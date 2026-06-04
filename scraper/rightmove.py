"""RightMove scraper.

Strategy: RightMove pages are Next.js apps that embed their full state as JSON in
a `window.PAGE_MODEL` block on detail pages, and a `__NEXT_DATA__` script tag on
search-results pages. We extract from those JSON blobs rather than walking the
DOM with obfuscated class selectors (the approach that broke the old R script).
We still need Playwright because the pages JS-render, and the static map image
URL (which carries the lat/lon) is only visible after the map widget hydrates.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, Page, BrowserContext, Browser

from .config import (
    SearchConfig,
    USER_AGENT,
    MIN_DELAY_SECONDS,
    MAX_DELAY_SECONDS,
)
from .flight import resolve_compact_dedup


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    """Minimal info from a search-results page."""
    prop_id: str
    price: int | None
    address: str | None
    bedrooms: int | None
    property_type: str | None
    added_on: str | None
    summary: str | None
    sold_status: str | None = None


async def collect_search_hits(cfg: SearchConfig) -> list[SearchHit]:
    """Walk all pages of search results and return per-property hits."""
    hits: dict[str, SearchHit] = {}
    async with async_playwright() as p:
        browser, context = await _launch(p)
        try:
            page = await context.new_page()
            index = 0
            page_num = 0
            while True:
                page_num += 1
                if cfg.max_pages and page_num > cfg.max_pages:
                    break
                page_hits = await _scrape_search_page(page, cfg, index)
                if not page_hits:
                    break
                new = 0
                for h in page_hits:
                    if h.prop_id not in hits:
                        hits[h.prop_id] = h
                        new += 1
                print(f"  page {page_num} (index={index}): {len(page_hits)} hits, {new} new")
                if cfg.max_properties and len(hits) >= cfg.max_properties:
                    break
                if len(page_hits) < 24:
                    break
                index += 24
                await _polite_sleep()
        finally:
            await context.close()
            await browser.close()
    return list(hits.values())


async def scrape_property(prop_id: str) -> dict[str, Any]:
    """Fetch a single property detail page and return its parsed payload."""
    async with async_playwright() as p:
        browser, context = await _launch(p)
        try:
            page = await context.new_page()
            return await _scrape_property_page(page, prop_id)
        finally:
            await context.close()
            await browser.close()


async def scrape_properties(prop_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Scrape many property detail pages in sequence (polite)."""
    results: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser, context = await _launch(p)
        try:
            page = await context.new_page()
            for i, pid in enumerate(prop_ids, start=1):
                try:
                    payload = await _scrape_property_page(page, pid)
                    results.append(payload)
                    print(f"  [{i}] {pid}: £{payload.get('price')} - {payload.get('address')!r}")
                except Exception as e:
                    print(f"  [{i}] {pid}: FAILED ({e!r})")
                await _polite_sleep()
        finally:
            await context.close()
            await browser.close()
    return results


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _launch(pw) -> tuple[Browser, BrowserContext]:
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-GB",
        timezone_id="Europe/London",
    )
    # Speed: block images, fonts, media. Map image URL is read from the DOM, not loaded.
    async def _route(route, request):
        if request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()
    await context.route("**/*", _route)
    return browser, context


async def _polite_sleep() -> None:
    await asyncio.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))


async def _dismiss_cookies(page: Page) -> None:
    for sel in ("#onetrust-reject-all-handler", "button:has-text(\"Reject All\")"):
        try:
            await page.locator(sel).first.click(timeout=1500)
            return
        except Exception:
            continue


async def _scrape_search_page(page: Page, cfg: SearchConfig, index: int) -> list[SearchHit]:
    url = cfg.search_url(index)
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    if index == 0:
        await _dismiss_cookies(page)
    # __NEXT_DATA__ holds the full search payload
    try:
        next_data_raw = await page.locator("#__NEXT_DATA__").first.inner_text(timeout=10_000)
    except Exception:
        return []
    data = json.loads(next_data_raw)
    properties = (
        data.get("props", {})
        .get("pageProps", {})
        .get("searchResults", {})
        .get("properties", [])
    )
    out: list[SearchHit] = []
    for item in properties:
        pid = str(item.get("id") or "").strip()
        if not pid:
            continue
        price = (item.get("price") or {}).get("amount")
        address = item.get("displayAddress")
        beds = item.get("bedrooms")
        ptype = item.get("propertySubType") or item.get("propertyType")
        first_visible = (item.get("firstVisibleDate") or item.get("listingUpdate", {}).get("listingUpdateDate"))
        summary = item.get("summary")
        display_status = item.get("displayStatus") or None
        # "BUY" / "RENT" are channel names, not sold statuses — ignore them
        if display_status and display_status.upper() in ("BUY", "RENT", "COMMERCIAL"):
            display_status = None
        out.append(SearchHit(
            prop_id=pid,
            price=int(price) if price else None,
            address=address,
            bedrooms=int(beds) if beds else None,
            property_type=ptype,
            added_on=_iso_date(first_visible),
            summary=summary,
            sold_status=display_status,
        ))
    return out


async def _scrape_property_page(page: Page, prop_id: str) -> dict[str, Any]:
    url = SearchConfig.property_url(prop_id)
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await _dismiss_cookies(page)
    # Detail page exposes the full payload as window.__PAGE_MODEL (compact dedup array)
    model = None
    try:
        raw_model = await page.evaluate("() => window.__PAGE_MODEL ? window.__PAGE_MODEL : null")
        if raw_model:
            raw_data = raw_model.get("data")
            if isinstance(raw_data, str):
                arr = json.loads(raw_data)
            elif isinstance(raw_data, list):
                arr = raw_data
            else:
                arr = None
            if arr:
                model = resolve_compact_dedup(arr)
    except Exception:
        model = None
    if not model:
        html = await page.content()
        model = _extract_page_model_from_html(html)

    payload = _parse_page_model(model, prop_id, url) if model else {"prop_id": prop_id, "url": url}

    # Map URL holds the lat/lon. Scroll a touch to encourage map hydration.
    try:
        await page.mouse.wheel(0, 1500)
        await page.wait_for_selector("img[alt*='map' i], img[src*='staticmap']", timeout=8_000)
        map_src = await page.locator("img[alt*='map' i], img[src*='staticmap']").first.get_attribute("src")
    except Exception:
        map_src = None
    if map_src:
        payload["map_url"] = map_src
        lat, lon = _latlon_from_map_url(map_src)
        if lat and lon:
            # Only override if PAGE_MODEL didn't already supply them.
            payload.setdefault("latitude", lat)
            payload.setdefault("longitude", lon)

    return payload


def _extract_page_model_from_html(html: str) -> dict | None:
    m = re.search(r"window\.__PAGE_MODEL\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    raw_data = raw.get("data") if isinstance(raw, dict) else None
    if isinstance(raw_data, str):
        try:
            arr = json.loads(raw_data)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw_data, list):
        arr = raw_data
    else:
        return None
    return resolve_compact_dedup(arr) if arr else None


def _parse_page_model(model: dict, prop_id: str, url: str) -> dict[str, Any]:
    """Map RightMove's PAGE_MODEL → our flat schema. Be defensive: fields move."""
    prop = model.get("propertyData") or model.get("analyticsInfo", {}).get("propertyData") or {}
    address = prop.get("address") or {}
    if isinstance(address, str):
        address = {"displayAddress": address}
    prices = prop.get("prices") or {}
    if not isinstance(prices, dict):
        prices = {}
    location = prop.get("location") or {}
    if not isinstance(location, dict):
        location = {}
    text = prop.get("text") or {}
    if not isinstance(text, dict):
        text = {}
    industry_affiliations = prop.get("industryAffiliations") or []
    keys = prop.get("keyFeatures") or []
    rooms = prop.get("rooms") or []
    customer = prop.get("customer") or {}
    listing_history = prop.get("listingHistory") or {}
    sizings = prop.get("sizings") or []
    floorplans = prop.get("floorplans") or []
    misinfo = prop.get("misInfo") or {}
    living_costs = prop.get("livingCosts") or {}

    # Bedrooms / bathrooms / property type
    bedrooms = prop.get("bedrooms")
    bathrooms = prop.get("bathrooms")
    ptype = prop.get("propertySubType") or prop.get("propertyType")
    is_flat = bool(ptype and "flat" in ptype.lower())

    # Price. Across page variants this is sometimes a string ("£500,000"),
    # sometimes an int, sometimes a {amount, frequency} dict.
    price_raw = (
        prices.get("primaryPrice")
        or prices.get("displayPrice")
        or prices.get("amount")
    )
    if isinstance(price_raw, dict):
        price_raw = price_raw.get("amount") or price_raw.get("displayPrice")
    if isinstance(price_raw, str):
        price_int = _money_to_int(price_raw)
    elif isinstance(price_raw, (int, float)):
        price_int = int(price_raw)
    else:
        price_int = None
    price_qualifier = prices.get("displayPriceQualifier") or prices.get("primaryPriceQualifier")

    # Sold/under-offer status
    sold_status = prop.get("status", {}).get("publishedStatus") if isinstance(prop.get("status"), dict) else None
    if not sold_status:
        sold_status = prop.get("transactionType") if not is_flat else None

    # Sq m from sizings array (RightMove gives both sqft and sqm)
    size_sqm, size_source = _extract_size(sizings, text.get("description") or "")

    # Lat/lon
    lat = location.get("latitude")
    lon = location.get("longitude")

    # Service charge / ground rent / council tax (in livingCosts in the new __PAGE_MODEL shape)
    service_charge = (
        living_costs.get("annualServiceCharge")
        or misinfo.get("annualServiceCharge")
        or misinfo.get("serviceCharge")
    )
    ground_rent = (
        living_costs.get("annualGroundRent")
        or misinfo.get("annualGroundRent")
        or misinfo.get("groundRent")
    )
    tenure = (prop.get("tenure") or {}).get("tenureType") if isinstance(prop.get("tenure"), dict) else prop.get("tenure")
    council_tax_band = living_costs.get("councilTaxBand") or misinfo.get("councilTaxBand")

    # Ground floor flag
    ground_floor = _detect_ground_floor(
        is_flat=is_flat,
        title=address.get("displayAddress") or "",
        description=text.get("description") or "",
        key_features=keys,
        floorplans=floorplans,
    ) if is_flat else None

    # Last sold (HMLR-linked in PAGE_MODEL)
    last_sold_price, last_sold_date = _extract_last_sold(prop)

    # Added on / first listed
    added_on = _iso_date(
        prop.get("firstVisibleDate")
        or listing_history.get("listingUpdateDate")
        or _date_from_listing_reason(listing_history.get("listingUpdateReason"))
    )

    description = text.get("description") or text.get("propertyPhrase")
    blurb = text.get("shareText") or text.get("pageTitle")

    return {
        "prop_id": str(prop_id),
        "url": url,
        "address": address.get("displayAddress"),
        "postcode": " ".join(filter(None, [address.get("outcode"), address.get("incode")])) or None,
        "latitude": lat,
        "longitude": lon,
        "price": price_int,
        "price_qualifier": price_qualifier,
        "sold_status": sold_status,
        "property_type": ptype,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "size_sqm": size_sqm,
        "size_source": size_source,
        "is_flat": 1 if is_flat else 0,
        "ground_floor": (1 if ground_floor else 0) if ground_floor is not None else None,
        "tenure": tenure,
        "service_charge_gbp": _money_to_float(service_charge),
        "ground_rent_gbp": _money_to_float(ground_rent),
        "council_tax_band": council_tax_band,
        "description": description,
        "key_features": "; ".join(k for k in keys if k),
        "blurb": blurb,
        "added_on": added_on,
        "last_sold_price": last_sold_price,
        "last_sold_date": last_sold_date,
        "raw_json": json.dumps(prop)[:200_000],  # cap to keep DB sane
    }


# ---------------------------------------------------------------------------
# Small extractors
# ---------------------------------------------------------------------------


_money_re = re.compile(r"([0-9][0-9,]*\.?\d*)")


def _money_to_int(s: str | None) -> int | None:
    if not s:
        return None
    m = _money_re.search(s)
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None


def _money_to_float(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = _money_re.search(str(s))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _iso_date(s: str | None) -> str | None:
    if not s:
        return None
    # RightMove uses "2025-04-10T11:22:33Z" or "2025-04-10"
    return s[:10]


_dmy_re = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _date_from_listing_reason(s: str | None) -> str | None:
    """Parse 'Added on 21/03/2026' → '2026-03-21'."""
    if not s:
        return None
    m = _dmy_re.search(s)
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def _extract_size(sizings: list[dict], description: str) -> tuple[float | None, str | None]:
    """Prefer sizings.sqm; fall back to parsing description."""
    for s in sizings or []:
        unit = (s.get("unit") or "").lower()
        if unit in ("sq m", "sqm", "m²", "m2"):
            try:
                return float(s.get("minimumSize") or s.get("amount") or s.get("value")), "listing"
            except (TypeError, ValueError):
                pass
        if unit in ("sq ft", "sqft"):
            try:
                sqft = float(s.get("minimumSize") or s.get("amount") or s.get("value"))
                return round(sqft * 0.092903, 1), "listing"
            except (TypeError, ValueError):
                pass
    # Regex on description: "1,234 sq ft" or "120 sq m"
    if description:
        m = re.search(r"([\d,]+)\s*(sq\s*m|sqm|m²)", description, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", "")), "description"
            except ValueError:
                pass
        m = re.search(r"([\d,]+)\s*(sq\s*ft|sqft)", description, re.IGNORECASE)
        if m:
            try:
                return round(float(m.group(1).replace(",", "")) * 0.092903, 1), "description"
            except ValueError:
                pass
    return None, None


_ground_floor_keywords = (
    "ground floor", "ground-floor", "garden flat", "garden apartment",
    "lower ground", "raised ground",
)
_not_ground_floor = re.compile(r"(?:first|second|third|fourth|fifth|top)\s+floor", re.IGNORECASE)


def _detect_ground_floor(*, is_flat: bool, title: str, description: str, key_features: list, floorplans: list) -> bool | None:
    if not is_flat:
        return None
    haystack = " | ".join(filter(None, [title, description, " ".join(key_features or [])])).lower()
    if any(kw in haystack for kw in _ground_floor_keywords):
        return True
    if _not_ground_floor.search(haystack):
        return False
    # We don't OCR floor plans here. Leave NULL if unclear.
    return None


def _extract_last_sold(prop: dict) -> tuple[int | None, str | None]:
    """RightMove embeds HMLR sold history under 'soldPropertyPriceHistory' or 'priceHistory'."""
    hist = (
        prop.get("soldPropertyPriceHistory")
        or prop.get("priceHistory")
        or prop.get("listingHistory", {}).get("priceHistory")
        or []
    )
    if isinstance(hist, dict):
        hist = hist.get("entries") or hist.get("items") or []
    if not hist:
        return None, None
    # Most recent entry first or last — sort by date desc
    def _key(e):
        return e.get("dateSold") or e.get("date") or ""
    hist = sorted([e for e in hist if isinstance(e, dict)], key=_key, reverse=True)
    if not hist:
        return None, None
    top = hist[0]
    price = _money_to_int(top.get("displayPrice") or str(top.get("price") or ""))
    date = _iso_date(top.get("dateSold") or top.get("date"))
    return price, date


def _latlon_from_map_url(url: str) -> tuple[float | None, float | None]:
    """RightMove static map URLs include `latitude=...&longitude=...` query params."""
    if not url:
        return None, None
    try:
        q = parse_qs(urlparse(url).query)
        lat = float(q.get("latitude", [None])[0]) if q.get("latitude") else None
        lon = float(q.get("longitude", [None])[0]) if q.get("longitude") else None
        return lat, lon
    except (ValueError, TypeError):
        return None, None
