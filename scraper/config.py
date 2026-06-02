"""Centralised configuration. Override anything via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "houses.db"


@dataclass(frozen=True)
class SearchConfig:
    max_price: int = int(os.getenv("RM_MAX_PRICE", "750000"))
    min_bedrooms: int = int(os.getenv("RM_MIN_BEDROOMS", "2"))
    location_id: int = int(os.getenv("RM_LOCATION_ID", "10139343"))
    include_sstc: bool = True
    # Soft caps for testing; set in .env or env vars.
    max_pages: int | None = (
        int(os.environ["RM_MAX_PAGES"]) if os.getenv("RM_MAX_PAGES") else None
    )
    max_properties: int | None = (
        int(os.environ["RM_MAX_PROPERTIES"]) if os.getenv("RM_MAX_PROPERTIES") else None
    )

    def search_url(self, index: int = 0) -> str:
        return (
            "https://www.rightmove.co.uk/property-for-sale/find.html?"
            f"minBedrooms={self.min_bedrooms}"
            "&keywords=&sortType=2&viewType=LIST&channel=BUY"
            f"&includeSSTC={'true' if self.include_sstc else 'false'}"
            f"&index={index}"
            f"&maxPrice={self.max_price}"
            "&radius=0.0"
            f"&locationIdentifier=USERDEFINEDAREA%5E%7B%22id%22%3A{self.location_id}%7D"
        )

    @staticmethod
    def property_url(prop_id: str | int) -> str:
        return f"https://www.rightmove.co.uk/properties/{prop_id}#/?channel=RES_BUY"


# Commute destinations
WORK_LOCATIONS = [
    ("Holborn (WC2B 4BG)", "WC2B 4BG"),
    ("Westminster (SW1P 4DF)", "SW1P 4DF"),
]

# Politeness
MIN_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 7.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

# Free APIs
TFL_BASE = "https://api.tfl.gov.uk"
POSTCODES_IO_BASE = "https://api.postcodes.io"
FLOOD_RISK_BASE = "https://environment.data.gov.uk/flood-monitoring"
# UK gov long-term flood risk has no public API; we use the EA flood-zone WFS as a proxy.
EA_FLOODZONE_WFS = (
    "https://environment.data.gov.uk/spatialdata/flood-map-for-planning-rivers-and-sea-flood-zone-3/wfs"
)
