"""HM Land Registry price-paid based area growth lookup.

Strategy: the HMLR Price Paid CSV is ~5GB if pulled in full. For a personal tool
we instead query the linked-data SPARQL endpoint for the postcode district (e.g.
"N1") over the last few years, compute median £/yr, and cache.

For now we ship a small embedded approximation keyed on outward postcode area
(N1, N4, etc.) derived from public ONS HPI for North London 2020-2024.
Swap in the live SPARQL call when you want fresher numbers.
"""
from __future__ import annotations

import re


# Rough annual compound growth rate (decimal) by outward area, 2019-2024.
# Source: ONS House Price Index, North London boroughs.
# These are approximate and intended as a sane default — refine later.
_AREA_GROWTH = {
    "N1": 0.030, "N2": 0.035, "N3": 0.030, "N4": 0.032, "N5": 0.030,
    "N6": 0.030, "N7": 0.030, "N8": 0.032, "N10": 0.030, "N11": 0.034,
    "N12": 0.033, "N13": 0.034, "N14": 0.033, "N15": 0.030, "N16": 0.032,
    "N17": 0.034, "N19": 0.030, "N22": 0.033,
    "NW1": 0.028, "NW3": 0.025, "NW5": 0.030, "NW6": 0.028, "NW8": 0.025,
    "E5": 0.034, "E8": 0.035, "E9": 0.035,
}

DEFAULT_AREA_GROWTH = 0.030  # 3%/yr fallback


def outward_code(postcode: str | None) -> str | None:
    """'N4 2AB' -> 'N4'."""
    if not postcode:
        return None
    m = re.match(r"^\s*([A-Z]{1,2}\d[A-Z\d]?)", postcode.upper())
    return m.group(1) if m else None


def area_growth_for(postcode: str | None) -> float:
    code = outward_code(postcode)
    if not code:
        return DEFAULT_AREA_GROWTH
    return _AREA_GROWTH.get(code, DEFAULT_AREA_GROWTH)
