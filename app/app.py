"""Streamlit app: filter, browse, map RightMove listings.

NA-friendly filters: any property with a NULL value in a filtered column
passes the filter (so we don't accidentally hide listings where we couldn't
compute an enrichment field).
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

# Allow running as both `streamlit run app/app.py` and `python -m app.app`
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.config import DB_PATH


st.set_page_config(page_title="North London house hunt", layout="wide")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300)
def load_properties() -> pd.DataFrame:
    if not Path(DB_PATH).exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT prop_id, url, address, postcode, latitude, longitude, "
            "price, price_qualifier, sold_status, property_type, bedrooms, bathrooms, "
            "size_sqm, is_flat, ground_floor, tenure, service_charge_gbp, "
            "council_tax_band, added_on, last_seen, last_sold_price, last_sold_date, "
            "implied_annual_pct, nearest_tube_name, nearest_tube_dist_m, "
            "nearest_rail_name, nearest_rail_dist_m, flood_risk_band, fair_value_gbp, "
            "commute_wc2b_minutes, commute_sw1p_minutes "
            "FROM properties",
            conn,
        )
    if df.empty:
        return df
    # Sort by added_on desc, NaNs last
    df["added_on_dt"] = pd.to_datetime(df["added_on"], errors="coerce")
    df = df.sort_values("added_on_dt", ascending=False, na_position="last").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# NA-friendly filter helpers
# ---------------------------------------------------------------------------


def f_range(df: pd.DataFrame, col: str, lo, hi) -> pd.Series:
    """Inclusive range; NaN passes."""
    s = df[col]
    mask = s.isna()
    if lo is not None:
        mask = mask | (s >= lo)
    if hi is not None:
        mask = mask & (s.isna() | (s <= hi))
    return mask


def f_max(df: pd.DataFrame, col: str, hi) -> pd.Series:
    if hi is None:
        return pd.Series([True] * len(df), index=df.index)
    s = df[col]
    return s.isna() | (s <= hi)


def f_min(df: pd.DataFrame, col: str, lo) -> pd.Series:
    if lo is None:
        return pd.Series([True] * len(df), index=df.index)
    s = df[col]
    return s.isna() | (s >= lo)


def f_in(df: pd.DataFrame, col: str, allowed: list[str]) -> pd.Series:
    if not allowed:
        return pd.Series([True] * len(df), index=df.index)
    s = df[col]
    return s.isna() | s.isin(allowed)


def f_bool(df: pd.DataFrame, col: str, want: str) -> pd.Series:
    """want in {'Any', 'Yes', 'No'}; NaN passes for Any only."""
    if want == "Any":
        return pd.Series([True] * len(df), index=df.index)
    s = df[col]
    if want == "Yes":
        return s == 1
    return s == 0


# ---------------------------------------------------------------------------
# Sidebar filters
# ---------------------------------------------------------------------------


df = load_properties()

with st.sidebar:
    st.header("Filters")
    if df.empty:
        st.info("No data yet. Run `python -m scraper.main` to populate.")
        st.stop()

    price_max = int(df["price"].max(skipna=True) or 750_000)
    price_lo, price_hi = st.slider(
        "Price (£)", 0, max(price_max, 750_000), (0, 750_000), step=10_000, format="£%d"
    )

    bed_lo, bed_hi = st.slider("Bedrooms", 0, 6, (2, 6))

    sqm_lo, sqm_hi = st.slider("Floor area (sqm)", 0, 250, (0, 250))

    types = sorted(df["property_type"].dropna().unique().tolist())
    chosen_types = st.multiselect("Property type", types, default=types)

    flat_floor = st.radio("Ground-floor flat", ["Any", "Yes", "No"], horizontal=True)

    service_cap = st.number_input(
        "Max annual service charge (£)", min_value=0, value=5000, step=250
    )

    commute_max = st.slider("Max commute to either work (min)", 5, 90, 60)

    flood_allowed = st.multiselect(
        "Flood risk band (allow these)",
        ["Very Low", "Low", "Medium", "High"],
        default=["Very Low", "Low", "Medium"],
    )

    max_walk_tube = st.slider("Max walk to Tube/DLR (m)", 0, 2500, 1500, step=100)
    max_walk_rail = st.slider("Max walk to Overground/Rail (m)", 0, 2500, 2000, step=100)

    show_sold = st.checkbox("Include Sold STC / Under Offer", value=True)


# Apply
mask = (
    f_range(df, "price", price_lo, price_hi)
    & f_range(df, "bedrooms", bed_lo, bed_hi)
    & f_range(df, "size_sqm", sqm_lo, sqm_hi)
    & f_in(df, "property_type", chosen_types)
    & f_bool(df, "ground_floor", flat_floor)
    & f_max(df, "service_charge_gbp", service_cap)
    & f_in(df, "flood_risk_band", flood_allowed)
    & f_max(df, "nearest_tube_dist_m", max_walk_tube)
    & f_max(df, "nearest_rail_dist_m", max_walk_rail)
)

# Commute: either work location under cap → pass; NaN passes
commute_pass = (
    df["commute_wc2b_minutes"].isna() | (df["commute_wc2b_minutes"] <= commute_max)
) | (
    df["commute_sw1p_minutes"].isna() | (df["commute_sw1p_minutes"] <= commute_max)
)
mask = mask & commute_pass

if not show_sold:
    mask = mask & ~df["sold_status"].fillna("").str.contains("Sold|Under Offer", case=False, regex=True)

filtered = df[mask].reset_index(drop=True)

st.title("North London house hunt")
st.caption(
    f"{len(filtered)} of {len(df)} properties match. "
    f"Last refresh: {df['last_seen'].max() if 'last_seen' in df else 'n/a'}"
)

view = st.radio("View", ["List", "Map"], horizontal=True)

# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


def _fmt_price(v) -> str:
    if pd.isna(v):
        return "£?"
    return f"£{int(v):,}"


def _fmt(v, suffix="", unknown="—") -> str:
    if pd.isna(v) or v is None:
        return unknown
    if isinstance(v, float):
        return f"{v:.0f}{suffix}"
    return f"{v}{suffix}"


def _card(row: pd.Series) -> None:
    cols = st.columns([3, 2])
    with cols[0]:
        st.markdown(f"### [{row['address'] or 'Unknown address'}]({row['url']})")
        bits = [
            _fmt_price(row.get("price")),
            f"{_fmt(row.get('bedrooms'))} bed",
            f"{_fmt(row.get('size_sqm'), ' sqm')}",
            row.get("property_type") or "—",
        ]
        st.write(" · ".join(bits))
        sub = []
        if pd.notna(row.get("commute_wc2b_minutes")):
            sub.append(f"WC2B: {int(row['commute_wc2b_minutes'])} min")
        if pd.notna(row.get("commute_sw1p_minutes")):
            sub.append(f"SW1P: {int(row['commute_sw1p_minutes'])} min")
        if pd.notna(row.get("nearest_tube_name")):
            sub.append(f"Tube: {row['nearest_tube_name']} ({int(row['nearest_tube_dist_m'])}m)")
        if pd.notna(row.get("flood_risk_band")):
            sub.append(f"Flood: {row['flood_risk_band']}")
        if sub:
            st.caption(" · ".join(sub))
    with cols[1]:
        st.write(f"**Added:** {row.get('added_on') or '—'}")
        if pd.notna(row.get("service_charge_gbp")):
            st.write(f"**Service charge:** £{row['service_charge_gbp']:.0f}/yr")
        if pd.notna(row.get("fair_value_gbp")):
            diff = (row.get("price") or 0) - row["fair_value_gbp"]
            st.write(f"**HMLR fair value:** £{int(row['fair_value_gbp']):,} ({'+' if diff>=0 else '−'}£{abs(int(diff)):,})")
        if pd.notna(row.get("implied_annual_pct")):
            st.write(f"**Implied growth since last sale:** {row['implied_annual_pct']*100:.1f}%/yr")
        if row.get("sold_status"):
            st.write(f"**Status:** {row['sold_status']}")
    st.divider()


PAGE_SIZE = 25

if view == "List":
    if filtered.empty:
        st.warning("No matches. Loosen a filter.")
    else:
        total_pages = max(1, math.ceil(len(filtered) / PAGE_SIZE))
        page = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1)
        start = (page - 1) * PAGE_SIZE
        st.caption(f"Showing {start + 1}–{min(start + PAGE_SIZE, len(filtered))} of {len(filtered)}")
        for _, row in filtered.iloc[start : start + PAGE_SIZE].iterrows():
            _card(row)

# ---------------------------------------------------------------------------
# Map view
# ---------------------------------------------------------------------------

else:
    pts = filtered.dropna(subset=["latitude", "longitude"])
    if pts.empty:
        st.warning("No mappable matches.")
    else:
        center_lat = pts["latitude"].mean()
        center_lon = pts["longitude"].mean()
        m = folium.Map(location=[center_lat, center_lon], zoom_start=13, tiles="cartodbpositron")
        cluster = MarkerCluster().add_to(m)
        for _, row in pts.iterrows():
            popup_html = (
                f"<b>{row.get('address') or 'Unknown'}</b><br>"
                f"{_fmt_price(row.get('price'))} · "
                f"{_fmt(row.get('bedrooms'))} bed · {_fmt(row.get('size_sqm'), ' sqm')}<br>"
                f"<a href='{row['url']}' target='_blank'>Open on RightMove</a>"
            )
            folium.Marker(
                location=[row["latitude"], row["longitude"]],
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=_fmt_price(row.get("price")),
            ).add_to(cluster)
        st_folium(m, width=None, height=720, returned_objects=[])
