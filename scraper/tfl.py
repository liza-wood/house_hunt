"""TfL Journey Planner client. Computes commute times from a postcode to two work locations."""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time, timedelta

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import TFL_BASE, WORK_LOCATIONS


def _next_weekday_at_9am() -> datetime:
    """Next Tuesday at 09:00 local. Tuesday avoids Mondays-after-bank-holidays weirdness."""
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7  # Mon=0, Tue=1
    if days_ahead == 0:
        days_ahead = 7
    target = today + timedelta(days=days_ahead)
    return datetime.combine(target, time(9, 0))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
async def journey_minutes(client: httpx.AsyncClient, from_postcode: str, to_postcode: str) -> int | None:
    """
    Returns the fastest itinerary duration in minutes, arriving by 09:00 next Tuesday.
    None on failure or no route.
    """
    target = _next_weekday_at_9am()
    params = {
        "date": target.strftime("%Y%m%d"),
        "time": target.strftime("%H%M"),
        "timeIs": "Arriving",
        "journeyPreference": "LeastTime",
        "mode": "tube,overground,national-rail,elizabeth-line,dlr,bus,walking",
    }
    if os.getenv("TFL_APP_KEY"):
        params["app_key"] = os.environ["TFL_APP_KEY"]
    # TfL accepts free-form locations including postcodes.
    url = f"{TFL_BASE}/Journey/JourneyResults/{from_postcode}/to/{to_postcode}"
    r = await client.get(url, params=params, timeout=20)
    if r.status_code == 300:
        # Disambiguation needed — give up cleanly for now
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    journeys = data.get("journeys", [])
    if not journeys:
        return None
    return min(int(j.get("duration") or 9999) for j in journeys)


async def commute_times(client: httpx.AsyncClient, from_postcode: str | None) -> dict[str, int | None]:
    """Returns {commute_wc2b_minutes: int|None, commute_sw1p_minutes: int|None}."""
    if not from_postcode:
        return {"commute_wc2b_minutes": None, "commute_sw1p_minutes": None}
    results = await asyncio.gather(
        *[journey_minutes(client, from_postcode, dest) for _, dest in WORK_LOCATIONS],
        return_exceptions=True,
    )
    def _safe(v):
        return v if isinstance(v, int) else None
    return {
        "commute_wc2b_minutes": _safe(results[0]),
        "commute_sw1p_minutes": _safe(results[1]),
    }
