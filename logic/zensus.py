import os
import re
import hashlib
import urllib.request
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd


ZENSUS_SOURCE_LABEL = "Zensus 2022 (Destatis, 100 m grid)"
ZENSUS_INFO_URL = (
    "https://www.destatis.de/DE/Themen/Gesellschaft-Umwelt/"
    "Bevoelkerung/Zensus2022/_inhalt.html"
)

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_ZENSUS_CACHE_DIR = os.path.join(_ROOT_DIR, "cache", "zensus")

_DATASETS = {
    "age": {
        "url": (
            "https://www.destatis.de/static/DE/zensus/gitterdaten/"
            "Gebaeude_nach_Baujahr_in_Mikrozensus_Klassen.zip"
        ),
        "filename": "zensus_age_mikrozensus.zip",
        "csv_marker": "100m-Gitter.csv",
    },
    "type": {
        "url": (
            "https://www.destatis.de/static/DE/zensus/gitterdaten/"
            "Gebaeude_mit_Wohnraum_nach_Gebaeudetyp_Groesse.zip"
        ),
        "filename": "zensus_building_type_size.zip",
        "csv_marker": "100m-Gitter.csv",
    },
}

AGE_CLASSES = [
    {"col": "Vor1919", "label": "before 1919", "start": 1900, "end": 1918, "year": 1910},
    {"col": "a1919bis1948", "label": "1919-1948", "start": 1919, "end": 1948, "year": 1934},
    {"col": "a1949bis1978", "label": "1949-1978", "start": 1949, "end": 1978, "year": 1963},
    {"col": "a1979bis1990", "label": "1979-1990", "start": 1979, "end": 1990, "year": 1985},
    {"col": "a1991bis2000", "label": "1991-2000", "start": 1991, "end": 2000, "year": 1996},
    {"col": "a2001bis2010", "label": "2001-2010", "start": 2001, "end": 2010, "year": 2006},
    {"col": "a2011bis2019", "label": "2011-2019", "start": 2011, "end": 2019, "year": 2015},
    {"col": "a2020undspaeter", "label": "2020 or later", "start": 2020, "end": 2022, "year": 2021},
]

TYPE_CLASSES = [
    {"col": "FreiEFH", "label": "detached single-family", "building": "detached", "rank": 1},
    {"col": "EFH_DHH", "label": "semi-detached single-family", "building": "semidetached_house", "rank": 2},
    {"col": "EFH_Reihenhaus", "label": "row single-family", "building": "terrace", "rank": 3},
    {"col": "Freist_ZFH", "label": "detached two-family", "building": "house", "rank": 4},
    {"col": "ZFH_DHH", "label": "semi-detached two-family", "building": "semidetached_house", "rank": 5},
    {"col": "ZFH_Reihenhaus", "label": "row two-family", "building": "terrace", "rank": 6},
    {"col": "MFH_3bis6Wohnungen", "label": "multi-family 3-6 dwellings", "building": "apartments", "rank": 7},
    {"col": "MFH_7bis12Wohnungen", "label": "multi-family 7-12 dwellings", "building": "apartments", "rank": 8},
    {"col": "MFH_13undmehrWohnungen", "label": "multi-family 13+ dwellings", "building": "apartments", "rank": 9},
    {"col": "AndererGebaeudetyp", "label": "other residential building", "building": "residential", "rank": 5},
]

_TYPE_BY_COL = {item["col"]: item for item in TYPE_CLASSES}
_AGE_BY_COL = {item["col"]: item for item in AGE_CLASSES}
_TYPE_BY_LABEL = {item["label"]: item for item in TYPE_CLASSES}
_AGE_BY_LABEL = {item["label"]: item for item in AGE_CLASSES}

_OSM_TO_ZENSUS_TYPE = {
    "detached": "FreiEFH",
    "house": "FreiEFH",
    "bungalow": "FreiEFH",
    "static_caravan": "FreiEFH",
    "semidetached": "EFH_DHH",
    "semidetached_house": "EFH_DHH",
    "terrace": "EFH_Reihenhaus",
    "row_house": "EFH_Reihenhaus",
    "residential": "MFH_3bis6Wohnungen",
    "apartments": "MFH_7bis12Wohnungen",
    "dormitory": "MFH_7bis12Wohnungen",
}

_GRID_CACHE: dict[tuple[str, str, str], pd.DataFrame] = {}
_POLYGON_STATS_CACHE: dict[tuple[str, str], tuple[dict[str, float], dict[str, float], pd.DataFrame]] = {}


def _clean_numeric_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if col.startswith("GITTER_ID"):
            continue
        out[col] = (
            out[col]
            .replace({"–": np.nan, "-": np.nan, "": np.nan})
            .pipe(pd.to_numeric, errors="coerce")
        )
    return out


def _download_dataset(kind: str, cache_dir: str) -> str:
    meta = _DATASETS[kind]
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, meta["filename"])
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return path

    request = urllib.request.Request(
        meta["url"],
        headers={"User-Agent": "Mozilla/5.0 (oemof-hri zensus importer)"},
    )
    tmp_path = f"{path}.tmp"
    with urllib.request.urlopen(request, timeout=60) as response:
        with open(tmp_path, "wb") as f:
            f.write(response.read())
    os.replace(tmp_path, path)
    return path


def _load_grid(kind: str, cache_dir: str, resolution: str = "100m") -> pd.DataFrame:
    key = (kind, cache_dir, resolution)
    if key in _GRID_CACHE:
        return _GRID_CACHE[key].copy()

    zip_path = _download_dataset(kind, cache_dir)
    marker = f"{resolution}-Gitter.csv"
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [
            name for name in zf.namelist()
            if name.lower().endswith(".csv") and marker.lower() in name.lower()
        ]
        if not csv_names:
            csv_names = [
                name for name in zf.namelist()
                if name.lower().endswith(".csv") and _DATASETS[kind]["csv_marker"].lower() in name.lower()
            ]
        if not csv_names:
            raise RuntimeError(f"No {resolution} CSV found in {zip_path}")
        with zf.open(csv_names[0]) as f:
            df = pd.read_csv(f, sep=";", encoding="utf-8-sig", low_memory=False)

    df = _clean_numeric_frame(df)
    _GRID_CACHE[key] = df
    return df.copy()


def _xy_columns(df: pd.DataFrame) -> tuple[str, str]:
    x_col = next((c for c in df.columns if c.startswith("x_mp_")), None)
    y_col = next((c for c in df.columns if c.startswith("y_mp_")), None)
    if not x_col or not y_col:
        raise RuntimeError("Zensus grid CSV has no x_mp/y_mp coordinate columns")
    return x_col, y_col


def _cells_for_polygon(df: pd.DataFrame, polygon_wgs84, buffer_m: float = 75.0) -> pd.DataFrame:
    if df.empty or polygon_wgs84 is None or getattr(polygon_wgs84, "is_empty", True):
        return df.iloc[0:0].copy()

    x_col, y_col = _xy_columns(df)
    poly_3035 = gpd.GeoSeries([polygon_wgs84], crs="EPSG:4326").to_crs(epsg=3035).iloc[0]
    minx, miny, maxx, maxy = poly_3035.bounds
    bbox = df.loc[
        df[x_col].between(minx - 150, maxx + 150)
        & df[y_col].between(miny - 150, maxy + 150)
    ].copy()
    if bbox.empty:
        return bbox

    points = gpd.GeoSeries(
        gpd.points_from_xy(bbox[x_col], bbox[y_col]),
        crs="EPSG:3035",
        index=bbox.index,
    )
    mask = points.within(poly_3035.buffer(buffer_m))
    selected = bbox.loc[mask].copy()
    if selected.empty:
        # For tiny selected areas, keep the surrounding cells rather than failing silently.
        selected = bbox.copy()
    return selected


def _counts_from_cells(cells: pd.DataFrame, class_defs: list[dict]) -> dict[str, float]:
    counts = {}
    for item in class_defs:
        col = item["col"]
        if col in cells.columns:
            value = pd.to_numeric(cells[col], errors="coerce").fillna(0).sum()
            if value > 0:
                counts[col] = float(value)
    return counts


def _largest_remainder_targets(counts: dict[str, float], n: int) -> dict[str, int]:
    if n <= 0 or not counts:
        return {}
    total = float(sum(v for v in counts.values() if v and v > 0))
    if total <= 0:
        return {}

    raw = {k: (v / total) * n for k, v in counts.items() if v > 0}
    targets = {k: int(np.floor(v)) for k, v in raw.items()}
    remaining = n - sum(targets.values())
    fractions = sorted(raw, key=lambda k: (raw[k] - targets[k], raw[k]), reverse=True)
    for k in fractions[:remaining]:
        targets[k] += 1
    return targets


def _assignment_list(
    counts: dict[str, float],
    known_groups: pd.Series,
    missing_n: int,
    all_n: int,
    rank_lookup: dict[str, int] | None = None,
) -> list[str]:
    if missing_n <= 0 or not counts:
        return []

    targets = _largest_remainder_targets(counts, all_n)
    known_counts = known_groups.dropna().astype(str).value_counts().to_dict()

    assignments: list[str] = []
    for key, target in targets.items():
        deficit = max(int(target) - int(known_counts.get(key, 0)), 0)
        assignments.extend([key] * deficit)

    ranked_counts = sorted(
        counts,
        key=lambda k: (counts[k], rank_lookup.get(k, 0) if rank_lookup else 0),
        reverse=True,
    )
    while len(assignments) < missing_n and ranked_counts:
        for key in ranked_counts:
            assignments.append(key)
            if len(assignments) >= missing_n:
                break

    assignments = assignments[:missing_n]
    if rank_lookup:
        assignments.sort(key=lambda k: rank_lookup.get(k, 0), reverse=True)
    return assignments


def _spread_age_years(age_assignments: list[str]) -> list[int]:
    """Return representative years spread inside each assigned Zensus age class."""
    if not age_assignments:
        return []
    counters = {key: 0 for key in set(age_assignments)}
    totals = pd.Series(age_assignments).value_counts().to_dict()
    years = []
    for key in age_assignments:
        item = _AGE_BY_COL.get(key)
        if not item:
            years.append(2005)
            continue
        count = int(totals.get(key, 1))
        pos = counters[key]
        counters[key] += 1
        if count <= 1:
            years.append(int(item["year"]))
            continue
        values = np.linspace(int(item["start"]), int(item["end"]), count)
        years.append(int(round(values[pos])))
    return years


def defaults_from_zensus_summary(summary: pd.DataFrame | None) -> dict[str, object]:
    """Return simple fallback defaults for manual buildings from a Zensus summary table."""
    out = {"building": None, "year": np.nan, "type_label": None, "age_label": None}
    if summary is None or getattr(summary, "empty", True):
        return out
    s = summary.copy()
    if "count" not in s.columns or "topic" not in s.columns or "label" not in s.columns:
        return out
    s["count"] = pd.to_numeric(s["count"], errors="coerce")
    type_rows = s.loc[s["topic"].astype(str).str.lower().eq("building type")].dropna(subset=["count"])
    if not type_rows.empty:
        row = type_rows.sort_values("count", ascending=False).iloc[0]
        label = str(row.get("label"))
        item = _TYPE_BY_LABEL.get(label)
        if item:
            out["building"] = item["building"]
            out["type_label"] = item["label"]
    age_rows = s.loc[s["topic"].astype(str).str.lower().eq("building age")].dropna(subset=["count"])
    if not age_rows.empty:
        row = age_rows.sort_values("count", ascending=False).iloc[0]
        label = str(row.get("label"))
        item = _AGE_BY_LABEL.get(label)
        if item:
            out["year"] = int(item["year"])
            out["age_label"] = item["label"]
    return out


def _norm_default(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().lower()
    if text in {"", "yes", "true", "1", "building", "none", "nan"}:
        return None
    return text


def _year_from_value(value):
    if value is None or pd.isna(value):
        return np.nan
    match = re.search(r"(18|19|20)\d{2}", str(value))
    if not match:
        return np.nan
    year = int(match.group(0))
    if year < 1800 or year > 2030:
        return np.nan
    return max(year, 1900)


def extract_osm_year(row: pd.Series) -> float:
    for col in [
        "year_built",
        "building:year_built",
        "building:year",
        "construction:year",
        "start_date",
        "building:start_date",
    ]:
        if col in row.index:
            year = _year_from_value(row.get(col))
            if pd.notna(year):
                return float(year)
    return np.nan


def age_class_for_year(year) -> str | None:
    try:
        y = int(float(year))
    except Exception:
        return None
    if y < 1919:
        return "Vor1919"
    if y <= 1948:
        return "a1919bis1948"
    if y <= 1978:
        return "a1949bis1978"
    if y <= 1990:
        return "a1979bis1990"
    if y <= 2000:
        return "a1991bis2000"
    if y <= 2010:
        return "a2001bis2010"
    if y <= 2019:
        return "a2011bis2019"
    return "a2020undspaeter"


def _summary_rows(kind: str, counts: dict[str, float], cells: pd.DataFrame) -> list[dict]:
    rows = []
    defs = AGE_CLASSES if kind == "age" else TYPE_CLASSES
    lookup = _AGE_BY_COL if kind == "age" else _TYPE_BY_COL
    total = sum(counts.values())
    for item in defs:
        col = item["col"]
        value = float(counts.get(col, 0))
        if value <= 0:
            continue
        rows.append({
            "topic": "building age" if kind == "age" else "building type",
            "zensus_class": col,
            "label": lookup[col]["label"],
            "count": value,
            "share": value / total if total else np.nan,
            "grid_cells": len(cells),
            "source": ZENSUS_SOURCE_LABEL,
        })
    return rows


def apply_zensus_defaults(
    buildings_gdf,
    info_df: pd.DataFrame,
    selected_polygon_wgs84,
    residential_types: set[str] | None = None,
    zero_demand_types: set[str] | None = None,
    norm_btype=None,
    cache_dir: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    """
    Add Zensus-derived defaults without changing stable building IDs.

    OSM/user values remain authoritative. Zensus fills missing residential
    building type and missing year_built values, and marks every filled value
    with a source column so it is visible in the UI and saved project data.
    """
    warnings: list[str] = []
    if buildings_gdf is None or getattr(buildings_gdf, "empty", True) or info_df is None or info_df.empty:
        return buildings_gdf, info_df, pd.DataFrame(), warnings

    norm = norm_btype or _norm_default
    residential_types = residential_types or set()
    zero_demand_types = zero_demand_types or set()
    cache_dir = cache_dir or DEFAULT_ZENSUS_CACHE_DIR

    buildings = buildings_gdf.copy()
    info = info_df.copy()
    buildings["bidx"] = buildings.get("bidx", buildings.index.astype(str)).astype(str)
    info["bidx"] = info["bidx"].astype(str)

    if "building type" not in info.columns:
        info["building type"] = np.nan
    if "building" not in buildings.columns:
        buildings["building"] = np.nan

    current_type = info["building type"].map(norm)
    info["building type"] = current_type
    buildings["building"] = buildings["building"].map(norm)

    if "building_type_source" not in buildings.columns:
        buildings["building_type_source"] = np.where(buildings["building"].notna(), "osm", None)
    if "building type source" not in info.columns:
        type_source_map = buildings.set_index("bidx")["building_type_source"].to_dict()
        info["building type source"] = info["bidx"].map(type_source_map)
        info.loc[current_type.notna() & info["building type source"].isna(), "building type source"] = "osm"

    osm_years = buildings.apply(extract_osm_year, axis=1)
    if "year_built" not in buildings.columns:
        buildings["year_built"] = np.nan
    buildings["year_built"] = pd.to_numeric(buildings["year_built"], errors="coerce")
    fill_osm_year = buildings["year_built"].isna() & pd.to_numeric(osm_years, errors="coerce").notna()
    if fill_osm_year.any():
        buildings.loc[fill_osm_year, "year_built"] = pd.to_numeric(osm_years, errors="coerce").loc[fill_osm_year]
    buildings["year_built"] = pd.to_numeric(buildings["year_built"], errors="coerce")

    if "year_built_source" not in buildings.columns:
        buildings["year_built_source"] = None
    buildings.loc[fill_osm_year & buildings["year_built_source"].isna(), "year_built_source"] = "osm"
    buildings.loc[buildings["year_built"].notna() & buildings["year_built_source"].isna(), "year_built_source"] = "osm"
    if "year built" not in info.columns:
        year_map = buildings.set_index("bidx")["year_built"].to_dict()
        info["year built"] = info["bidx"].map(year_map)
    info["year built"] = pd.to_numeric(info["year built"], errors="coerce")
    if "year source" not in info.columns:
        year_source_map = buildings.set_index("bidx")["year_built_source"].to_dict()
        info["year source"] = info["bidx"].map(year_source_map)
        info.loc[info["year built"].notna() & info["year source"].isna(), "year source"] = "osm"

    for col in ["zensus age class", "zensus type class", "zensus source", "zensus type note", "zensus year note"]:
        if col not in info.columns:
            info[col] = None
    for col in ["zensus_age_class", "zensus_type_class", "zensus_source", "zensus_type_note", "zensus_year_note"]:
        if col not in buildings.columns:
            buildings[col] = None

    try:
        poly_key = hashlib.sha1(selected_polygon_wgs84.wkb).hexdigest()
        stats_key = (cache_dir, poly_key)
        if stats_key in _POLYGON_STATS_CACHE:
            age_counts, type_counts, summary = _POLYGON_STATS_CACHE[stats_key]
            summary = summary.copy()
        else:
            age_grid = _load_grid("age", cache_dir)
            type_grid = _load_grid("type", cache_dir)
            age_cells = _cells_for_polygon(age_grid, selected_polygon_wgs84)
            type_cells = _cells_for_polygon(type_grid, selected_polygon_wgs84)
            age_counts = _counts_from_cells(age_cells, AGE_CLASSES)
            type_counts = _counts_from_cells(type_cells, TYPE_CLASSES)
            summary = pd.DataFrame(
                _summary_rows("age", age_counts, age_cells)
                + _summary_rows("type", type_counts, type_cells)
            )
            _POLYGON_STATS_CACHE[stats_key] = (age_counts, type_counts, summary.copy())
    except Exception as exc:
        warnings.append(f"Zensus data could not be loaded: {exc}")
        return buildings, info, pd.DataFrame(), warnings

    if not age_counts and not type_counts:
        warnings.append("No usable Zensus age/type counts were found for the selected area.")
        return buildings, info, summary, warnings

    type_norm = info["building type"].map(norm)
    is_zero_or_non_res = type_norm.isin(zero_demand_types) | (
        type_norm.notna() & ~type_norm.isin(residential_types)
    )
    residential_candidate = ~is_zero_or_non_res
    info.loc[type_norm.notna() & residential_candidate, "zensus type note"] = "Existing building type kept"
    info.loc[is_zero_or_non_res, "zensus type note"] = "Not adjusted: OSM type is outside the residential Zensus type dataset"

    # Building type imputation.
    missing_type = residential_candidate & type_norm.isna()
    known_type_groups = type_norm.loc[residential_candidate & type_norm.notna()].map(
        lambda t: _OSM_TO_ZENSUS_TYPE.get(t)
    )
    type_rank = {item["col"]: item["rank"] for item in TYPE_CLASSES}
    type_assignments = _assignment_list(
        type_counts,
        known_type_groups,
        int(missing_type.sum()),
        int(residential_candidate.sum()),
        rank_lookup=type_rank,
    )
    if missing_type.any() and not type_counts:
        info.loc[missing_type, "zensus type note"] = "Not adjusted: no usable Zensus type counts in the selected grid cells"
    if type_assignments:
        size = pd.to_numeric(info.get("area (m²)", pd.Series(np.nan, index=info.index)), errors="coerce")
        levels = pd.to_numeric(info.get("levels (#)", pd.Series(np.nan, index=info.index)), errors="coerce").fillna(1)
        order = (size.fillna(size.median() if size.notna().any() else 0) * levels).sort_values(ascending=False)
        missing_idx = [idx for idx in order.index if bool(missing_type.loc[idx])]
        for idx, zclass in zip(missing_idx, type_assignments):
            item = _TYPE_BY_COL[zclass]
            bidx = str(info.at[idx, "bidx"])
            info.at[idx, "building type"] = item["building"]
            info.at[idx, "building type source"] = "zensus"
            info.at[idx, "zensus type class"] = item["label"]
            info.at[idx, "zensus source"] = ZENSUS_SOURCE_LABEL
            info.at[idx, "zensus type note"] = "Filled from Zensus type distribution"
            bpos = buildings.index[buildings["bidx"].astype(str) == bidx]
            if len(bpos):
                bidx0 = bpos[0]
                buildings.at[bidx0, "building"] = item["building"]
                buildings.at[bidx0, "building_type_source"] = "zensus"
                buildings.at[bidx0, "zensus_type_class"] = item["label"]
                buildings.at[bidx0, "zensus_source"] = ZENSUS_SOURCE_LABEL
                buildings.at[bidx0, "zensus_type_note"] = "Filled from Zensus type distribution"
    if missing_type.any():
        still_missing = missing_type & info["building type"].map(norm).isna() & info["zensus type note"].isna()
        info.loc[still_missing, "zensus type note"] = "Not adjusted: Zensus type distribution could not assign this row"

    # Age imputation. Use all residential candidates after type imputation.
    type_after = info["building type"].map(norm)
    residential_after = ~(type_after.isin(zero_demand_types) | (type_after.notna() & ~type_after.isin(residential_types)))
    info.loc[info["year built"].notna(), "zensus year note"] = "Existing construction year kept"
    info.loc[~residential_after & info["year built"].isna(), "zensus year note"] = (
        "Not adjusted: building type is outside the residential Zensus age dataset"
    )
    known_age_groups = info.loc[residential_after & info["year built"].notna(), "year built"].map(age_class_for_year)
    missing_age = residential_after & info["year built"].isna()
    age_assignments = _assignment_list(
        age_counts,
        known_age_groups,
        int(missing_age.sum()),
        int(residential_after.sum()),
        rank_lookup=None,
    )
    if missing_age.any() and not age_counts:
        info.loc[missing_age, "zensus year note"] = "Not adjusted: no usable Zensus age counts in the selected grid cells"
    if age_assignments:
        assigned_years = _spread_age_years(age_assignments)
        missing_idx = list(info.loc[missing_age].sort_values("bidx", key=lambda s: pd.to_numeric(s, errors="coerce")).index)
        for idx, zclass, assigned_year in zip(missing_idx, age_assignments, assigned_years):
            item = _AGE_BY_COL[zclass]
            bidx = str(info.at[idx, "bidx"])
            info.at[idx, "year built"] = assigned_year
            info.at[idx, "year source"] = "zensus"
            info.at[idx, "zensus age class"] = item["label"]
            info.at[idx, "zensus source"] = ZENSUS_SOURCE_LABEL
            info.at[idx, "zensus year note"] = "Filled from Zensus age distribution"
            bpos = buildings.index[buildings["bidx"].astype(str) == bidx]
            if len(bpos):
                bidx0 = bpos[0]
                buildings.at[bidx0, "year_built"] = assigned_year
                buildings.at[bidx0, "year_built_source"] = "zensus"
                buildings.at[bidx0, "zensus_age_class"] = item["label"]
                buildings.at[bidx0, "zensus_source"] = ZENSUS_SOURCE_LABEL
                buildings.at[bidx0, "zensus_year_note"] = "Filled from Zensus age distribution"
    if missing_age.any():
        still_missing = missing_age & info["year built"].isna() & info["zensus year note"].isna()
        info.loc[still_missing, "zensus year note"] = "Not adjusted: Zensus age distribution could not assign this row"

    note_map = info.set_index("bidx")[["zensus type note", "zensus year note"]].to_dict()
    for idx, row in buildings.iterrows():
        bidx = str(row["bidx"])
        buildings.at[idx, "zensus_type_note"] = note_map["zensus type note"].get(bidx)
        buildings.at[idx, "zensus_year_note"] = note_map["zensus year note"].get(bidx)

    # Keep Zensus class columns visible for rows already having OSM values where possible.
    for idx, row in info.iterrows():
        bidx = str(row["bidx"])
        bpos = buildings.index[buildings["bidx"].astype(str) == bidx]
        if not len(bpos):
            continue
        bidx0 = bpos[0]
        if pd.notna(row.get("year built")) and not row.get("zensus age class"):
            zclass = age_class_for_year(row.get("year built"))
            if zclass in _AGE_BY_COL:
                label = _AGE_BY_COL[zclass]["label"]
                info.at[idx, "zensus age class"] = label if row.get("year source") == "zensus" else None
                buildings.at[bidx0, "zensus_age_class"] = buildings.at[bidx0, "zensus_age_class"] or None

    return buildings, info, summary, warnings
