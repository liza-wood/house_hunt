"""Post-scrape enrichment: postcode, stations, flood risk, valuation."""
from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import POSTCODES_IO_BASE, FLOOD_RISK_BASE


# ---------------------------------------------------------------------------
# Postcode lookup
# ---------------------------------------------------------------------------


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
async def reverse_geocode(client: httpx.AsyncClient, lat: float, lon: float) -> str | None:
    """Approximate postcode from lat/lon using free postcodes.io."""
    r = await client.get(
        f"{POSTCODES_IO_BASE}/postcodes",
        params={"lon": lon, "lat": lat, "limit": 1, "radius": 1000},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    data = r.json()
    result = data.get("result") or []
    if not result:
        return None
    return result[0].get("postcode")


# ---------------------------------------------------------------------------
# Nearest station (Tube/Overground/National Rail)
# ---------------------------------------------------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
async def nearest_stations(client: httpx.AsyncClient, lat: float, lon: float) -> dict[str, Any]:
    """Find nearest Tube and nearest Rail (Overground/National Rail) within 1500m using TfL StopPoint."""
    from .config import TFL_BASE
    import os

    params = {
        "lat": lat,
        "lon": lon,
        "radius": 1500,
        "stopTypes": "NaptanMetroStation,NaptanRailStation",
        "modes": "tube,overground,national-rail,elizabeth-line,dlr",
    }
    if os.getenv("TFL_APP_KEY"):
        params["app_key"] = os.environ["TFL_APP_KEY"]
    r = await client.get(f"{TFL_BASE}/StopPoint", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    stops = data.get("stopPoints", [])
    tube = rail = None
    tube_d = rail_d = float("inf")
    for s in stops:
        s_lat, s_lon = s.get("lat"), s.get("lon")
        if s_lat is None or s_lon is None:
            continue
        d = haversine_m(lat, lon, s_lat, s_lon)
        modes = {m.lower() for m in (s.get("modes") or [])}
        if "tube" in modes or "dlr" in modes or "elizabeth-line" in modes:
            if d < tube_d:
                tube, tube_d = s.get("commonName"), d
        if "overground" in modes or "national-rail" in modes or "elizabeth-line" in modes:
            if d < rail_d:
                rail, rail_d = s.get("commonName"), d
    return {
        "nearest_tube_name": tube,
        "nearest_tube_dist_m": None if tube is None else round(tube_d),
        "nearest_rail_name": rail,
        "nearest_rail_dist_m": None if rail is None else round(rail_d),
    }


# ---------------------------------------------------------------------------
# Flood risk
# ---------------------------------------------------------------------------


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
async def flood_risk(client: httpx.AsyncClient, postcode: str | None, lat: float | None, lon: float | None) -> str | None:
    """
    Best-effort flood risk band. The official check-long-term-flood-risk service has
    no public API. We approximate via the EA's flood-monitoring/floods endpoint:
    presence of an active flood warning within ~5km bumps the band up.

    For a more accurate band, swap in a Flood Zone 2/3 polygon lookup later.
    Returns one of "Very Low" / "Low" / "Medium" / "High" / None.
    """
    if lat is None or lon is None:
        return None
    try:
        r = await client.get(
            f"{FLOOD_RISK_BASE}/id/floods",
            params={"lat": lat, "long": lon, "dist": 5},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("items", [])
        # severityLevel: 1=Severe, 2=Warning, 3=Alert, 4=Removed
        severities = [i.get("severityLevel") for i in items if i.get("severityLevel") is not None]
        if not severities:
            return "Very Low"
        worst = min(severities)
        return {1: "High", 2: "High", 3: "Medium", 4: "Low"}.get(worst, "Low")
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------------------
# Valuation (HMLR-based)
# ---------------------------------------------------------------------------


def implied_annual_growth(current_price: int | None, last_sold_price: int | None, last_sold_date: str | None) -> float | None:
    """((current/last)^(1/years)) - 1, or None if any input is missing."""
    if not (current_price and last_sold_price and last_sold_date):
        return None
    from datetime import date
    try:
        y, m, d = map(int, last_sold_date[:10].split("-"))
        sold = date(y, m, d)
    except (ValueError, AttributeError):
        return None
    days = (date.today() - sold).days
    if days < 30:
        return None  # too recent to be meaningful
    years = days / 365.25
    try:
        return (current_price / last_sold_price) ** (1 / years) - 1
    except (ZeroDivisionError, ValueError):
        return None


def fair_value_estimate(last_sold_price: int | None, last_sold_date: str | None, area_growth_pct: float = 0.04) -> int | None:
    """
    Simple HMLR-derived estimate: project last sold price forward at the area's
    average annual growth rate. Default 4%/yr (rough London average 2015-24).

    We pass `area_growth_pct` from a per-postcode lookup later (see hmlr.py — TODO).
    """
    if not (last_sold_price and last_sold_date):
        return None
    from datetime import date
    try:
        y, m, d = map(int, last_sold_date[:10].split("-"))
        sold = date(y, m, d)
    except (ValueError, AttributeError):
        return None
    years = (date.today() - sold).days / 365.25
    return int(last_sold_price * (1 + area_growth_pct) ** years)
