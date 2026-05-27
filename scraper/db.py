"""SQLite schema + helpers. One row per property; price history is separate."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    prop_id            TEXT PRIMARY KEY,
    url                TEXT NOT NULL,
    address            TEXT,
    postcode           TEXT,             -- enriched, may be NULL
    latitude           REAL,
    longitude          REAL,

    price              INTEGER,
    price_qualifier    TEXT,             -- e.g. "Guide Price", "Offers Over"
    sold_status        TEXT,             -- e.g. NULL, "Under Offer", "Sold STC"

    property_type      TEXT,             -- "Flat", "Terraced", etc.
    bedrooms           INTEGER,
    bathrooms          INTEGER,
    size_sqm           REAL,             -- floor area
    size_source        TEXT,             -- "listing" | "floorplan" | NULL
    is_flat            INTEGER,          -- 1 if flat-like
    ground_floor       INTEGER,          -- 1/0/NULL, applies to flats
    tenure             TEXT,             -- "Freehold", "Leasehold", "Share of Freehold"
    service_charge_gbp REAL,             -- annual £, flats only
    ground_rent_gbp    REAL,
    council_tax_band   TEXT,

    description        TEXT,
    key_features       TEXT,             -- "; " joined
    blurb              TEXT,             -- meta og:description

    added_on           TEXT,             -- ISO date when first listed
    first_seen         TEXT,             -- ISO datetime first time we scraped it
    last_seen          TEXT,             -- ISO datetime most recent scrape

    last_sold_price    INTEGER,
    last_sold_date     TEXT,             -- ISO date
    implied_annual_pct REAL,             -- (current_price / last_sold) ^ (1/years) - 1

    -- enrichment
    nearest_tube_name      TEXT,
    nearest_tube_dist_m    REAL,
    nearest_rail_name      TEXT,         -- Overground or National Rail
    nearest_rail_dist_m    REAL,
    flood_risk_band        TEXT,         -- "Very Low" / "Low" / "Medium" / "High" / NULL
    fair_value_gbp         INTEGER,      -- HMLR-derived estimate
    commute_wc2b_minutes   INTEGER,
    commute_sw1p_minutes   INTEGER,

    map_url            TEXT,             -- the RightMove static map (contains lat/lon)
    raw_json           TEXT              -- full scraped payload for debugging
);

CREATE TABLE IF NOT EXISTS price_history (
    prop_id   TEXT NOT NULL,
    seen_at   TEXT NOT NULL,
    price     INTEGER,
    sold_status TEXT,
    PRIMARY KEY (prop_id, seen_at)
);

CREATE INDEX IF NOT EXISTS idx_props_added_on ON properties(added_on);
CREATE INDEX IF NOT EXISTS idx_props_price ON properties(price);
"""


@contextmanager
def connect(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def upsert_property(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Insert or update a property row. Records price history if price changed."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    prop_id = str(payload["prop_id"])

    # Fetch existing for change detection / first_seen preservation
    cur = conn.execute("SELECT price, sold_status, first_seen FROM properties WHERE prop_id = ?", (prop_id,))
    existing = cur.fetchone()

    payload = dict(payload)  # don't mutate caller
    payload["prop_id"] = prop_id
    payload["last_seen"] = now
    payload["first_seen"] = existing["first_seen"] if existing else now
    payload.setdefault("raw_json", None)
    if payload["raw_json"] is not None and not isinstance(payload["raw_json"], str):
        payload["raw_json"] = json.dumps(payload["raw_json"], default=str)

    cols = [c for c in _ALL_COLUMNS if c in payload]
    placeholders = ", ".join(f":{c}" for c in cols)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "first_seen")
    sql = (
        f"INSERT INTO properties ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(prop_id) DO UPDATE SET {set_clause}"
    )
    conn.execute(sql, payload)

    # Price history
    new_price = payload.get("price")
    new_status = payload.get("sold_status")
    if existing is None or existing["price"] != new_price or existing["sold_status"] != new_status:
        conn.execute(
            "INSERT OR REPLACE INTO price_history (prop_id, seen_at, price, sold_status) "
            "VALUES (?, ?, ?, ?)",
            (prop_id, now, new_price, new_status),
        )


def known_prop_ids(db_path: Path = DB_PATH) -> set[str]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT prop_id FROM properties").fetchall()
    return {r["prop_id"] for r in rows}


def touch_property(conn: sqlite3.Connection, prop_id: str, price: int | None, sold_status: str | None) -> None:
    """Lightweight update for already-known properties: price, sold_status, last_seen only."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    cur = conn.execute("SELECT price, sold_status FROM properties WHERE prop_id = ?", (prop_id,))
    existing = cur.fetchone()
    conn.execute(
        "UPDATE properties SET price=?, sold_status=?, last_seen=? WHERE prop_id=?",
        (price, sold_status, now, prop_id),
    )
    if existing and (existing["price"] != price or existing["sold_status"] != sold_status):
        conn.execute(
            "INSERT OR REPLACE INTO price_history (prop_id, seen_at, price, sold_status) VALUES (?, ?, ?, ?)",
            (prop_id, now, price, sold_status),
        )


def all_properties(db_path: Path = DB_PATH) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return list(conn.execute("SELECT * FROM properties ORDER BY added_on DESC NULLS LAST"))


def get_property(prop_id: str, db_path: Path = DB_PATH) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM properties WHERE prop_id = ?", (prop_id,))
        return cur.fetchone()


_ALL_COLUMNS: tuple[str, ...] = (
    "prop_id", "url", "address", "postcode", "latitude", "longitude",
    "price", "price_qualifier", "sold_status",
    "property_type", "bedrooms", "bathrooms", "size_sqm", "size_source",
    "is_flat", "ground_floor", "tenure", "service_charge_gbp", "ground_rent_gbp",
    "council_tax_band",
    "description", "key_features", "blurb",
    "added_on", "first_seen", "last_seen",
    "last_sold_price", "last_sold_date", "implied_annual_pct",
    "nearest_tube_name", "nearest_tube_dist_m",
    "nearest_rail_name", "nearest_rail_dist_m",
    "flood_risk_band", "fair_value_gbp",
    "commute_wc2b_minutes", "commute_sw1p_minutes",
    "map_url", "raw_json",
)
