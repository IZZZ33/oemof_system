import streamlit as st
import streamlit_folium
import folium
from folium.plugins import Draw
import osmnx as ox
import geopandas as gpd
from shapely.geometry import shape, mapping, Polygon, box, Point
from shapely import wkt
from shapely.ops import unary_union
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderUnavailable, GeocoderTimedOut
import pandas as pd
import numpy as np
import teaser
from teaser.project import Project
import re
import unicodedata
import hashlib
import random
from openpyxl import load_workbook
import shutil, time, os, tempfile
from filelock import FileLock, Timeout
import json
from pathlib import Path

from logic.project_paths import get_project_dir, get_scenario_dir_for_project
from logic.dwd_weather import get_location_weather
from logic.zensus import ZENSUS_SOURCE_LABEL, apply_zensus_defaults, defaults_from_zensus_summary

__all__ = ["load_project_dataset_v2"]

DHW_ESTIMATE_COLUMNS = [
    "bidx",
    "building index",
    "building type",
    "area_m2",
    "levels",
    "net_floor_area_m2",
    "opendhw_type",
    "estimated_occupants",
    "water_l_per_person_day",
    "mean_drawoff_l_per_day",
    "annual_dhw_demand_kWh",
    "basis",
    "status",
]

DHW_PROFILE_COLUMNS = [
    "timestamp",
    "dhw_heat_kWh",
    "dhw_heat_kW",
    "water_L",
    "water_LperH",
]

RETROFIT_SITUATION_OPTIONS = ["standard", "retrofit", "advanced retrofit"]
RETROFIT_SITUATION_DEFAULT = "standard"
RETROFIT_CONSTRUCTION_DATA = {
    "standard": "tabula_de_standard",
    "retrofit": "tabula_de_retrofit",
    "advanced retrofit": "tabula_de_adv_retrofit",
}
RETROFIT_NONRES_DEMAND_FACTORS = {
    "standard": 1.0,
    "retrofit": 0.72,
    "advanced retrofit": 0.48,
}

SPACE_HEATING_DEMAND_LABEL = "Last_SH"
DHW_DEMAND_LABEL = "Last_DHW"
DHW_PROFILE_COLUMN = "Last_DHW.fix"


def _scenario_demand_mismatches(project_name, space_heat_kwh, dhw_kwh):
    """Return scenarios whose imported project-demand rows differ from current totals."""
    scenario_dir = get_scenario_dir_for_project(project_name)
    mismatches = []
    if not os.path.isdir(scenario_dir):
        return mismatches
    expected = {
        SPACE_HEATING_DEMAND_LABEL: float(space_heat_kwh),
        DHW_DEMAND_LABEL: float(dhw_kwh),
    }
    for path in sorted(Path(scenario_dir).glob("*.xlsx")):
        try:
            demand = pd.read_excel(path, sheet_name="demand")
            if "label" not in demand.columns or "nominal value" not in demand.columns:
                continue
            found_imported_row = False
            differs = False
            for label, value in expected.items():
                mask = demand["label"].astype(str).str.strip().eq(label)
                if mask.any():
                    found_imported_row = True
                    current = pd.to_numeric(demand.loc[mask, "nominal value"], errors="coerce").iloc[0]
                    differs = differs or pd.isna(current) or not np.isclose(float(current), value)
            if found_imported_row and differs:
                mismatches.append(path)
        except Exception:
            continue
    return mismatches


def _update_scenario_demands(paths, space_heat_kwh, dhw_kwh, dhw_profile_df=None):
    """Update the automatically imported demand rows in existing scenarios."""
    expected = {
        SPACE_HEATING_DEMAND_LABEL: float(space_heat_kwh),
        DHW_DEMAND_LABEL: float(dhw_kwh),
    }
    updated = []
    for path in paths:
        demand = pd.read_excel(path, sheet_name="demand")
        for label, value in expected.items():
            mask = demand["label"].astype(str).str.strip().eq(label)
            if not mask.any():
                continue
            demand.loc[mask, "nominal value"] = value
            if "Demand in MWh/a" in demand.columns:
                demand.loc[mask, "Demand in MWh/a"] = value / 1_000
            if "Demand in GWh/a" in demand.columns:
                demand.loc[mask, "Demand in GWh/a"] = value / 1_000_000

        loss_mask = demand["label"].astype(str).str.strip().isin(["Loss", "Network_Heat_Loss"])
        if loss_mask.any():
            thermal_mask = (
                demand.get("from", pd.Series("", index=demand.index)).astype(str).str.match(r"^b_th_", na=False)
                & ~loss_mask
            )
            heat_total = pd.to_numeric(demand.loc[thermal_mask, "nominal value"], errors="coerce").sum()
            loss_kwh = float(heat_total) * 0.05
            demand.loc[loss_mask, "nominal value"] = loss_kwh
            if "Demand in MWh/a" in demand.columns:
                demand.loc[loss_mask, "Demand in MWh/a"] = loss_kwh / 1_000
            if "Demand in GWh/a" in demand.columns:
                demand.loc[loss_mask, "Demand in GWh/a"] = loss_kwh / 1_000_000

        with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            demand.to_excel(writer, sheet_name="demand", index=False)

        if isinstance(dhw_profile_df, pd.DataFrame) and not dhw_profile_df.empty:
            profile_col = "dhw_heat_kWh" if "dhw_heat_kWh" in dhw_profile_df.columns else None
            if profile_col:
                values = pd.to_numeric(dhw_profile_df[profile_col], errors="coerce").fillna(0).to_numpy(dtype=float)
                wb = load_workbook(path)
                if "time_series" in wb.sheetnames:
                    ws = wb["time_series"]
                    headers = {str(ws.cell(1, col).value): col for col in range(1, ws.max_column + 1)}
                    timestamp_col = headers.get("timestamp")
                    timestamp_rows = (
                        [row for row in range(2, ws.max_row + 1) if ws.cell(row, timestamp_col).value is not None]
                        if timestamp_col else []
                    )
                    target_len = len(timestamp_rows)
                    if target_len:
                        if len(values) < target_len:
                            values = np.pad(values, (0, target_len - len(values)), mode="constant")
                        values = values[:target_len]
                        profile_total = float(values.sum())
                        values = values / profile_total if profile_total > 0 else np.ones(target_len) / target_len
                    target_col = headers.get(DHW_PROFILE_COLUMN, ws.max_column + 1)
                    ws.cell(1, target_col, DHW_PROFILE_COLUMN)
                    for row_idx, value in enumerate(values, start=2):
                        ws.cell(row_idx, target_col, float(value))
                    wb.save(path)
        updated.append(path.name)
    return updated

def _std_estimates_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df is False or df.empty:
        return pd.DataFrame(columns=["input_index", "heat_demand_kWh"])
    d = df.copy()

    if "bidx" in d.columns and "input_index" not in d.columns:
        d = d.rename(columns={"bidx": "input_index"})
    if "heat_demand_kWh" not in d.columns:
        for alt in ["demand_kwh", "demand_kWh", "heat_kwh", "value", "nominal value"]:
            if alt in d.columns:
                d = d.rename(columns={alt: "heat_demand_kWh"})
                break

    if "input_index" not in d.columns or "heat_demand_kWh" not in d.columns:
        return pd.DataFrame(columns=["input_index", "heat_demand_kWh"])

    d["input_index"] = d["input_index"].astype(str)
    d["heat_demand_kWh"] = pd.to_numeric(d["heat_demand_kWh"], errors="coerce")

    d = d.reset_index(drop=True)
    d = d.drop_duplicates(subset=["input_index"], keep="last")
    return d[["input_index", "heat_demand_kWh"]]

def _std_dhw_estimates_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df is False or getattr(df, "empty", True):
        return pd.DataFrame(columns=DHW_ESTIMATE_COLUMNS)

    d = df.copy()
    if "input_index" in d.columns and "bidx" not in d.columns:
        d = d.rename(columns={"input_index": "bidx"})
    if "annual_dhw_demand_kWh" not in d.columns:
        for alt in ["dhw_demand_kwh", "dhw_demand_kWh", "annual_dhw_kwh", "value"]:
            if alt in d.columns:
                d = d.rename(columns={alt: "annual_dhw_demand_kWh"})
                break

    if "bidx" not in d.columns:
        d["bidx"] = d.index.astype(str)

    for c in DHW_ESTIMATE_COLUMNS:
        if c not in d.columns:
            d[c] = np.nan

    d["bidx"] = d["bidx"].astype(str)
    for c in [
        "area_m2", "levels", "net_floor_area_m2", "estimated_occupants",
        "water_l_per_person_day", "mean_drawoff_l_per_day", "annual_dhw_demand_kWh",
    ]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    for c in ["building index", "building type", "opendhw_type", "basis", "status"]:
        d[c] = d[c].apply(_nz)

    d = d.drop_duplicates(subset=["bidx"], keep="last").reset_index(drop=True)
    return d[DHW_ESTIMATE_COLUMNS]

def _std_dhw_profile_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df is False or getattr(df, "empty", True):
        return pd.DataFrame(columns=DHW_PROFILE_COLUMNS)

    d = df.copy()
    if "timestamp" not in d.columns:
        first = d.columns[0] if len(d.columns) else None
        if first is not None:
            d = d.rename(columns={first: "timestamp"})
        else:
            d["timestamp"] = pd.Series(dtype="datetime64[ns]")

    for c in DHW_PROFILE_COLUMNS:
        if c not in d.columns:
            d[c] = np.nan

    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    for c in ["dhw_heat_kWh", "dhw_heat_kW", "water_L", "water_LperH"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    d = d.dropna(subset=["timestamp"]).reset_index(drop=True)
    return d[DHW_PROFILE_COLUMNS]

def _mk_est_from_ds(ds: pd.DataFrame, col_src: str) -> pd.DataFrame:
    if ("bidx" in ds.columns) and (col_src in ds.columns):
        peak_col = "peak_kw_auto" if "auto" in col_src else "peak_kw_final"
        extra = [c for c in [peak_col, "heat_estimation_method"] if c in ds.columns]
        d = ds[["bidx", col_src] + extra].rename(
            columns={"bidx": "input_index", col_src: "heat_demand_kWh"}
        ).copy()
        d.rename(columns={
            peak_col: "peak_load_kW", "heat_estimation_method": "estimation_method"
        }, inplace=True)
        d["input_index"] = d["input_index"].astype(str)
        d["heat_demand_kWh"] = pd.to_numeric(d["heat_demand_kWh"], errors="coerce")
        if "peak_load_kW" not in d.columns:
            d["peak_load_kW"] = np.nan
        if "estimation_method" not in d.columns:
            d["estimation_method"] = "legacy annual-demand estimate"
        d = d.drop_duplicates(subset=["input_index"], keep="last")
        return d[["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]]
    return pd.DataFrame(
        columns=["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]
    )

def _nz(v):
    if v is None or (isinstance(v, float) and np.isnan(v)) or (pd.isna(v) if hasattr(pd, "isna") else False):
        return None
    s = str(v).strip()
    if s.lower() in {"nan", "none", "null"}:
        return None
    return s or None

def _normalize_retrofit_situation(v):
    s = _nz(v)
    if s is None:
        return None
    key = re.sub(r"[\s\-_]+", " ", str(s).strip().lower())
    aliases = {
        "standard": "standard",
        "current": "standard",
        "original": "standard",
        "not renovated": "standard",
        "not retrofitted": "standard",
        "unrenovated": "standard",
        "no retrofit": "standard",
        "retrofit": "retrofit",
        "retrofitted": "retrofit",
        "renovated": "retrofit",
        "partly renovated": "retrofit",
        "partially renovated": "retrofit",
        "typical retrofit": "retrofit",
        "advanced retrofit": "advanced retrofit",
        "advanced renovated": "advanced retrofit",
        "deep retrofit": "advanced retrofit",
        "deep renovated": "advanced retrofit",
        "full retrofit": "advanced retrofit",
    }
    return aliases.get(key)

def _to_bool_series(s: pd.Series) -> pd.Series:
    """
    Convert a column that might contain True/False, 1/0, 'WAHR'/'FALSCH',
    'TRUE'/'FALSE', 'yes'/'no', etc. into a clean boolean Series.

    Unknown / empty values become False.
    """
    if s is None:
        return pd.Series([], dtype=bool)

    v = s.astype(str).str.strip().str.lower()

    truthy = {"1", "true", "wahr", "yes", "y", "ja"}
    falsy  = {"0", "false", "falsch", "no", "n", "nein", "", "nan", "none"}

    out = v.map(
        lambda x: True if x in truthy
        else False if x in falsy
        else False  # unknown values -> False
    )
    return out.astype(bool)

def _has_building_rows(df) -> bool:
    return df is not None and hasattr(df, "empty") and not df.empty

def _sanitize_building_attrs_df(df) -> pd.DataFrame:
    cols = [
        "bidx", "building type", "area (m²)", "levels", "height (m)",
        "address", "year built", "retrofit situation",
    ]
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame(columns=cols)

    out = df.copy()
    if "year built" not in out.columns and "year_built" in out.columns:
        out = out.rename(columns={"year_built": "year built"})
    for c in cols:
        if c not in out.columns:
            out[c] = np.nan
    out = out[cols]
    out["bidx"] = out["bidx"].astype(str)

    for c in ["building type", "address"]:
        out[c] = out[c].apply(_nz)
    out["retrofit situation"] = out["retrofit situation"].apply(_normalize_retrofit_situation)
    out["area (m²)"] = pd.to_numeric(out["area (m²)"], errors="coerce")
    out["year built"] = pd.to_numeric(out["year built"], errors="coerce")
    out["levels"] = pd.to_numeric(out["levels"], errors="coerce")
    out["height (m)"] = pd.to_numeric(out["height (m)"], errors="coerce")

    has_override = (
        out["building type"].notna()
        | out["area (m²)"].notna()
        | out["address"].notna()
        | out["year built"].notna()
        | out["retrofit situation"].notna()
    )
    has_override = has_override | out["levels"].notna() | out["height (m)"].notna()
    return out.loc[has_override].drop_duplicates(subset=["bidx"], keep="last").reset_index(drop=True)

def _normalize_manual_buildings_df(df) -> pd.DataFrame:
    cols = [
        "bidx", "name", "building type", "area (m²)", "levels", "height (m)", "year built",
        "retrofit situation",
        "building type source", "year source", "zensus age class", "zensus type class",
        "zensus type note", "zensus year note", "zensus source",
    ]
    out = pd.DataFrame(columns=cols)
    if df is None or getattr(df, "empty", True):
        return out

    src = df.copy()
    if "bidx" not in src.columns:
        return out

    out["bidx"] = src["bidx"].astype(str)
    out["name"] = src.get("name", pd.Series([None] * len(src), index=src.index))

    def _first_existing(*names, default=None):
        for name in names:
            if name in src.columns:
                return src[name]
        return pd.Series([default] * len(src), index=src.index)

    out["building type"] = _first_existing("building type", "building_type_final", "building_type_base", "building")
    area_col = next(
        (c for c in src.columns if str(c).strip().lower().startswith("area")),
        None,
    )
    out["area (m²)"] = pd.to_numeric(
        src[area_col] if area_col is not None else _first_existing("area_final_m2", "area_base_m2", "area_m2"),
        errors="coerce",
    )
    out["year built"] = pd.to_numeric(
        _first_existing("year built", "year_built_final", "year_built_base", "year_built"),
        errors="coerce",
    )
    out["levels"] = pd.to_numeric(_first_existing("levels"), errors="coerce")
    out["height (m)"] = pd.to_numeric(
        _first_existing("height (m)", "height_m", "height"), errors="coerce"
    )
    out["retrofit situation"] = _first_existing(
        "retrofit situation", "retrofit_situation_final", "retrofit_situation"
    )
    out["building type source"] = _first_existing("building type source", "building_type_source")
    out["year source"] = _first_existing("year source", "year_built_source")
    out["zensus age class"] = _first_existing("zensus age class", "zensus_age_class")
    out["zensus type class"] = _first_existing("zensus type class", "zensus_type_class")
    out["zensus type note"] = _first_existing("zensus type note", "zensus_type_note")
    out["zensus year note"] = _first_existing("zensus year note", "zensus_year_note")
    out["zensus source"] = _first_existing("zensus source", "zensus_source")

    for col in ["name", "building type", "building type source", "year source",
                "zensus age class", "zensus type class", "zensus type note",
                "zensus year note", "zensus source"]:
        out[col] = out[col].apply(_nz)
    out["building type"] = out["building type"].map(norm_btype)
    out["retrofit situation"] = out["retrofit situation"].apply(_normalize_retrofit_situation)
    out = out.loc[out["bidx"].astype(str).str.strip().ne("")]
    return out.drop_duplicates(subset=["bidx"], keep="last").reset_index(drop=True)

def load_project_dataset_v2(project_name: str):
    """
    Loader-first design:
    - Prefer persisted json/csv/geojson in <project>/persist
    - Fall back to <project>/project_data.xlsx (sheets: polygons, buildings_dataset, meta)
    Returns a dict ready for hydrate_saved_selection to adopt.
    """
    out = {
        "polygons_wkt": [],
        "buildings_gdf": None,
        "manual_buildings_df": pd.DataFrame(columns=[
            "bidx", "name", "building type", "area (m²)", "year built", "retrofit situation",
            "building type source", "year source", "zensus age class", "zensus type class",
            "zensus type note", "zensus year note", "zensus source",
        ]),
        "building_attrs_df": pd.DataFrame(columns=["bidx","building type","area (m²)","address","year built","retrofit situation"]),
        # "info_overridden_df": pd.DataFrame(columns=["bidx","building index","address","name","building type","area (m²)","source"]),
        "excluded_bidx": set(),
        "estimates_df_final": pd.DataFrame(columns=["input_index","heat_demand_kWh", "peak_load_kW", "estimation_method"]),
        "estimates_df_original": pd.DataFrame(columns=["input_index","heat_demand_kWh", "peak_load_kW", "estimation_method"]),
        "dhw_estimates_df": pd.DataFrame(columns=DHW_ESTIMATE_COLUMNS),
        "dhw_profile_df": pd.DataFrame(columns=DHW_PROFILE_COLUMNS),
        "total_heat_demand_kwh": None,
        "total_dhw_demand_kwh": None,
        "route_length_m": None,
        "zensus_summary_df": pd.DataFrame(),
    }

    proj_dir   = get_project_dir(project_name)
    persist_dir = os.path.join(proj_dir, "persist")
    xlsx_path   = os.path.join(proj_dir, "project_data.xlsx")

    try:
        df_base = pd.read_excel(xlsx_path, sheet_name="base_attrs")
        if not df_base.empty and "bidx" in df_base.columns:
            df_base["bidx"] = df_base["bidx"].astype(str)
            st.session_state["_osm_base_attrs"] = df_base
    except Exception:
        pass

    # 1) POLYGONS (persist → xlsx)
    p_polys = os.path.join(persist_dir, "polygons.json")
    if os.path.exists(p_polys):
        try:
            with open(p_polys, "r", encoding="utf-8") as f:
                vals = json.load(f) or []
            if isinstance(vals, list):
                out["polygons_wkt"] = [str(x) for x in vals]
        except Exception:
            pass
    if not out["polygons_wkt"] and os.path.exists(xlsx_path):
        try:
            df_polys = pd.read_excel(xlsx_path, sheet_name="polygons")
            if "wkt" in df_polys.columns:
                out["polygons_wkt"] = df_polys["wkt"].dropna().astype(str).tolist()
        except Exception:
            pass

    # 2) BUILDINGS (persist → xlsx)
    # 2A) GeoJSON persisted
    p_bld = os.path.join(persist_dir, "buildings.geojson")
    if os.path.exists(p_bld) and out["buildings_gdf"] is None:
        try:
            gdf = gpd.read_file(p_bld)
            if gdf is not None and not gdf.empty:
                if "bidx" not in gdf.columns:
                    gdf["bidx"] = gdf.index.astype(str)
                gdf["bidx"] = gdf["bidx"].astype(str)
                if getattr(gdf, "crs", None) is None:
                    try: gdf = gdf.set_crs(epsg=4326)
                    except Exception: pass
                out["buildings_gdf"] = gdf
        except Exception:
            pass

    # 2B) Fall back to xlsx: buildings_dataset → (Geo)DataFrame
    ds = pd.DataFrame()
    if os.path.exists(xlsx_path):
        try:
            ds = pd.read_excel(xlsx_path, sheet_name="buildings_dataset")
        except Exception:
            ds = pd.DataFrame()

    if not _has_building_rows(out["buildings_gdf"]) and not ds.empty:
        ds_buildings = ds.copy()
        if "source" in ds_buildings.columns:
            ds_buildings = ds_buildings.loc[
                ds_buildings["source"].astype(str).str.lower().ne("manual")
            ].copy()

        # geometry from WKT if available
        geom = None
        if "geom_wkt" in ds_buildings.columns:
            try:
                geom = gpd.GeoSeries.from_wkt(ds_buildings["geom_wkt"], crs="EPSG:4326")
            except Exception:
                geom = None

        # choose useful columns, normalize names expected by the UI
        keep = [c for c in [
            "bidx", "building_type_final", "building_type_base",
            "area_final_m2", "area_base_m2",
            "building_type_source", "year_built_final", "year_built_base", "year_built_source",
            "retrofit_situation_final", "retrofit_situation_base",
            "retrofit_situation", "retrofit_probability",
            "zensus_age_class", "zensus_type_class", "zensus_source",
            "zensus_type_note", "zensus_year_note",
            "levels", "height_m", "name", "address",
            "osm_id", "osmid"
        ] if c in ds_buildings.columns]
        base = ds_buildings[keep].copy() if keep else pd.DataFrame()

        # normalize
        if "building_type_final" in base.columns:
            base = base.rename(columns={"building_type_final": "building"})
        elif "building_type_base" in base.columns and "building" not in base.columns:
            base = base.rename(columns={"building_type_base": "building"})
        if "area_final_m2" in base.columns:
            base = base.rename(columns={"area_final_m2": "area_m2"})
        elif "area_base_m2" in base.columns and "area_m2" not in base.columns:
            base = base.rename(columns={"area_base_m2": "area_m2"})
        if "year_built_final" in base.columns:
            base = base.rename(columns={"year_built_final": "year_built"})
        elif "year_built_base" in base.columns and "year_built" not in base.columns:
            base = base.rename(columns={"year_built_base": "year_built"})
        if "retrofit_situation_final" in base.columns:
            base = base.rename(columns={"retrofit_situation_final": "retrofit_situation"})
        elif "retrofit_situation_base" in base.columns and "retrofit_situation" not in base.columns:
            base = base.rename(columns={"retrofit_situation_base": "retrofit_situation"})

        if "bidx" not in base.columns:
            base["bidx"] = ds_buildings.index.astype(str)
        base["bidx"] = base["bidx"].astype(str)

        if geom is not None and not base.empty:
            try:
                out["buildings_gdf"] = gpd.GeoDataFrame(base, geometry=geom, crs="EPSG:4326")
            except Exception:
                out["buildings_gdf"] = base
        else:
            out["buildings_gdf"] = base

    # 3) EXCLUSIONS (persist → xlsx via Included flag)
    p_ex = os.path.join(persist_dir, "excluded.json")
    loaded_exclusions_from_persist = False
    if os.path.exists(p_ex):
        try:
            with open(p_ex, "r", encoding="utf-8") as f:
                raw = json.load(f) or []
            out["excluded_bidx"] = set(str(x) for x in raw)
            loaded_exclusions_from_persist = True
        except Exception:
            pass
    if not loaded_exclusions_from_persist and not ds.empty and "included" in ds.columns and "bidx" in ds.columns:
        try:
            # ✅ robustly handle WAHR/FALSCH, 1/0, True/False, etc.
            inc = _to_bool_series(ds["included"])
            excl = ds.loc[~inc, "bidx"].astype(str).tolist()
            out["excluded_bidx"] = set(excl)
        except Exception:
            pass

    # 4) MANUAL BUILDINGS (persist → xlsx via source=='manual')
    p_manual = os.path.join(persist_dir, "manual_buildings.csv")
    if os.path.exists(p_manual):
        try:
            df = pd.read_csv(p_manual)
            out["manual_buildings_df"] = _normalize_manual_buildings_df(df)
        except Exception:
            pass
    if out["manual_buildings_df"].empty and not ds.empty and "source" in ds.columns:
        try:
            man = ds.loc[ds["source"].astype(str).str.lower() == "manual"].copy()
            if not man.empty:
                mb = pd.DataFrame({
                    "bidx": man["bidx"].astype(str),
                    "name": man.get("name", pd.Series([None] * len(man))),
                    "building type": man.get("building_type_final", man.get("building_type_base", None)),
                    "area (m²)": pd.to_numeric(man.get("area_final_m2", man.get("area_base_m2", np.nan)), errors="coerce"),
                    "year built": pd.to_numeric(man.get("year_built_final", man.get("year_built_base", np.nan)), errors="coerce"),
                    "retrofit situation": man.get("retrofit_situation_final", man.get("retrofit_situation", None)),
                    "building type source": man.get("building_type_source", pd.Series([None] * len(man))),
                    "year source": man.get("year_built_source", pd.Series([None] * len(man))),
                    "zensus age class": man.get("zensus_age_class", pd.Series([None] * len(man))),
                    "zensus type class": man.get("zensus_type_class", pd.Series([None] * len(man))),
                    "zensus type note": man.get("zensus_type_note", pd.Series([None] * len(man))),
                    "zensus year note": man.get("zensus_year_note", pd.Series([None] * len(man))),
                    "zensus source": man.get("zensus_source", pd.Series([None] * len(man))),
                })
                out["manual_buildings_df"] = _normalize_manual_buildings_df(mb)
        except Exception:
            pass

    # 5) BUILDING ATTR OVERRIDES – derive from xlsx per field
    out["building_attrs_df"] = pd.DataFrame(
        columns=["bidx", "building type", "area (m²)", "address", "year built", "retrofit situation"]
    )

    if not ds.empty and "bidx" in ds.columns:
        try:
            import numpy as np

            src = ds["source"].astype(str) if "source" in ds.columns else pd.Series(["osm"] * len(ds), index=ds.index)
            is_osm = src.str.lower().eq("osm")

            has_flags = all(c in ds.columns for c in ["edited_type", "edited_area", "edited_address"])
            has_year_flag = "edited_year" in ds.columns
            has_retrofit_flag = "edited_retrofit" in ds.columns

            rows = ds.loc[is_osm].copy()
            bidx = rows["bidx"].astype(str)

            sub = pd.DataFrame({"bidx": bidx})

            # --- TYPE overrides ---
            if "building_type_final" in rows.columns:
                if has_flags:
                    # ✅ use robust boolean parsing
                    m_type = _to_bool_series(rows["edited_type"])
                    sub.loc[m_type, "building type"] = rows.loc[m_type, "building_type_final"]
                else:
                    if "building_type_base" in rows.columns:
                        bt_base = rows["building_type_base"].fillna("__na__")
                        bt_final = rows["building_type_final"].fillna("__na__")
                        m_type = bt_base != bt_final
                        sub.loc[m_type, "building type"] = rows.loc[m_type, "building_type_final"]

            # --- AREA overrides ---
            if "area_final_m2" in rows.columns:
                af = pd.to_numeric(rows["area_final_m2"], errors="coerce")
                if has_flags:
                    # ✅ use robust boolean parsing
                    m_area = _to_bool_series(rows["edited_area"])
                    sub.loc[m_area, "area (m²)"] = af[m_area]
                else:
                    if "area_base_m2" in rows.columns:
                        ab = pd.to_numeric(rows["area_base_m2"], errors="coerce")
                        m_area = ~np.isclose(af, ab, equal_nan=True)
                        sub.loc[m_area, "area (m²)"] = af[m_area]

            # --- ADDRESS overrides ---
            if "address" in rows.columns:
                addr = rows["address"].astype(str).str.strip()
                if has_flags:
                    # ✅ use robust boolean parsing
                    m_addr = _to_bool_series(rows["edited_address"])
                    sub.loc[m_addr, "address"] = addr[m_addr]
                else:
                    m_addr = addr.ne("") & addr.notna()
                    sub.loc[m_addr, "address"] = addr[m_addr]

            # --- YEAR overrides ---
            if "year_built_final" in rows.columns:
                yf = pd.to_numeric(rows["year_built_final"], errors="coerce")
                if has_year_flag:
                    m_year = _to_bool_series(rows["edited_year"])
                    sub.loc[m_year, "year built"] = yf[m_year]
                elif "year_built_source" in rows.columns:
                    m_year = rows["year_built_source"].astype(str).str.lower().eq("user")
                    sub.loc[m_year, "year built"] = yf[m_year]
                elif "year_built_base" in rows.columns:
                    yb = pd.to_numeric(rows["year_built_base"], errors="coerce")
                    m_year = ~np.isclose(yf, yb, equal_nan=True)
                    sub.loc[m_year, "year built"] = yf[m_year]

            # --- RETROFIT overrides ---
            if "retrofit_situation_final" in rows.columns:
                rf = rows["retrofit_situation_final"].apply(_normalize_retrofit_situation)
                if has_retrofit_flag:
                    m_retrofit = _to_bool_series(rows["edited_retrofit"])
                    sub.loc[m_retrofit, "retrofit situation"] = rf[m_retrofit]
                elif "retrofit_situation_base" in rows.columns:
                    rb = rows["retrofit_situation_base"].apply(_normalize_retrofit_situation)
                    m_retrofit = rf.fillna("__na__") != rb.fillna("__na__")
                    sub.loc[m_retrofit, "retrofit situation"] = rf[m_retrofit]

            # keep only rows where at least one override exists
            override_cols = [
                c for c in ["building type", "area (m²)", "address", "year built", "retrofit situation"]
                if c in sub.columns
            ]
            if override_cols:
                mask_any = ~sub[override_cols].isna().all(axis=1)
                sub = sub.loc[mask_any]
            else:
                sub = sub.iloc[0:0]

            # ensure one row per bidx, and discard empty "nan" address edits
            out["building_attrs_df"] = _sanitize_building_attrs_df(sub)

        except Exception:
            # fall back to empty overrides on any error
            out["building_attrs_df"] = pd.DataFrame(
                columns=["bidx", "building type", "area (m²)", "address", "year built", "retrofit situation"]
            )

    # 6) ESTIMATES (persist → xlsx)
    p_est_f = os.path.join(persist_dir, "estimates_final.csv")
    if os.path.exists(p_est_f):
        try:
            out["estimates_df_final"] = _std_estimates_df(pd.read_csv(p_est_f))
        except Exception:
            pass
    p_est_o = os.path.join(persist_dir, "estimates_original.csv")
    if os.path.exists(p_est_o):
        try:
            out["estimates_df_original"] = _std_estimates_df(pd.read_csv(p_est_o))
        except Exception:
            pass

    p_dhw_est = os.path.join(persist_dir, "dhw_estimates.csv")
    if os.path.exists(p_dhw_est):
        try:
            out["dhw_estimates_df"] = _std_dhw_estimates_df(pd.read_csv(p_dhw_est))
        except Exception:
            pass

    p_dhw_profile = os.path.join(persist_dir, "dhw_profile.csv")
    if os.path.exists(p_dhw_profile):
        try:
            out["dhw_profile_df"] = _std_dhw_profile_df(pd.read_csv(p_dhw_profile))
        except Exception:
            pass

    p_zensus = os.path.join(persist_dir, "zensus_summary.csv")
    if os.path.exists(p_zensus):
        try:
            out["zensus_summary_df"] = pd.read_csv(p_zensus)
        except Exception:
            pass

    if out["estimates_df_final"].empty and out["estimates_df_original"].empty and not ds.empty:
        try:
            cand_final = ["demand_kwh_final","heat_kwh_final","heat_demand_kWh_final"]
            cand_auto  = ["demand_kwh_auto","heat_kwh_auto","heat_demand_kWh_auto"]
            fc = next((c for c in cand_final if c in ds.columns), None)
            ac = next((c for c in cand_auto  if c in ds.columns), None)
            if fc: out["estimates_df_final"]    = _mk_est_from_ds(ds, fc)
            if ac: out["estimates_df_original"] = _mk_est_from_ds(ds, ac)
        except Exception:
            pass

    if out["zensus_summary_df"].empty and os.path.exists(xlsx_path):
        try:
            out["zensus_summary_df"] = pd.read_excel(xlsx_path, sheet_name="zensus_summary")
        except Exception:
            pass

    # 7) META (persist → xlsx; fallback compute total)
    if out["dhw_estimates_df"].empty and os.path.exists(xlsx_path):
        try:
            out["dhw_estimates_df"] = _std_dhw_estimates_df(pd.read_excel(xlsx_path, sheet_name="dhw_estimates"))
        except Exception:
            pass

    if out["dhw_profile_df"].empty and os.path.exists(xlsx_path):
        try:
            out["dhw_profile_df"] = _std_dhw_profile_df(pd.read_excel(xlsx_path, sheet_name="dhw_profile"))
        except Exception:
            pass

    p_meta = os.path.join(persist_dir, "meta.json")
    if os.path.exists(p_meta):
        try:
            with open(p_meta, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
            if "total_heat_demand_kwh" in meta and meta["total_heat_demand_kwh"] is not None:
                out["total_heat_demand_kwh"] = float(meta["total_heat_demand_kwh"])
            if "route_length_m" in meta and meta["route_length_m"] is not None:
                out["route_length_m"] = float(meta["route_length_m"])
            if "total_dhw_demand_kwh" in meta and meta["total_dhw_demand_kwh"] is not None:
                out["total_dhw_demand_kwh"] = float(meta["total_dhw_demand_kwh"])
        except Exception:
            pass

    if out["total_heat_demand_kwh"] is None and os.path.exists(xlsx_path):
        try:
            df_meta = pd.read_excel(xlsx_path, sheet_name="meta")
            if {"key","value"} <= set(df_meta.columns):
                kv = dict(zip(df_meta["key"].astype(str), df_meta["value"]))
                if "total_heat_demand_kwh" in kv and pd.notna(kv["total_heat_demand_kwh"]):
                    out["total_heat_demand_kwh"] = float(kv["total_heat_demand_kwh"])
                if "route_length_m" in kv and pd.notna(kv["route_length_m"]):
                    out["route_length_m"] = float(kv["route_length_m"])
                if "total_dhw_demand_kwh" in kv and pd.notna(kv["total_dhw_demand_kwh"]):
                    out["total_dhw_demand_kwh"] = float(kv["total_dhw_demand_kwh"])
        except Exception:
            pass

    if out["total_heat_demand_kwh"] is None and not out["estimates_df_final"].empty:
        out["total_heat_demand_kwh"] = float(
            pd.to_numeric(out["estimates_df_final"]["heat_demand_kWh"], errors="coerce").fillna(0).sum()
        )

    if out["total_dhw_demand_kwh"] is None and not out["dhw_estimates_df"].empty:
        out["total_dhw_demand_kwh"] = float(
            pd.to_numeric(out["dhw_estimates_df"]["annual_dhw_demand_kWh"], errors="coerce").fillna(0).sum()
        )

    # 8) info_overridden_df: single table the UI can show directly

    try:
        gsrc = out["buildings_gdf"]
        if gsrc is not None and hasattr(gsrc, "empty") and not gsrc.empty:
            g = gsrc.copy()
            if "bidx" not in g.columns:
                g["bidx"] = g.index.astype(str)
            g["bidx"] = g["bidx"].astype(str)

            # base info (OSM or from ds)
            info = pd.DataFrame({
                "bidx": g["bidx"].astype(str),
                "building index": g["bidx"].astype(str).map(lambda x: f"Building_{x}"),
                "address": g.get("address", pd.Series([None] * len(g))),
                "name": g.get("name", pd.Series([None] * len(g))),
                "building type": g.get("building", pd.Series([None] * len(g))),
                "building type source": g.get("building_type_source", pd.Series([None] * len(g))),
                "area (m²)": pd.to_numeric(g.get("area_m2", pd.Series([np.nan]*len(g))), errors="coerce"),
                "year built": pd.to_numeric(g.get("year_built", pd.Series([np.nan]*len(g))), errors="coerce"),
                "retrofit situation": g.get("retrofit_situation", pd.Series([None] * len(g))),
                "renovation probability (%)": pd.to_numeric(
                    g.get("retrofit_probability", pd.Series([np.nan] * len(g))), errors="coerce"
                ) * 100.0,
                "year source": g.get("year_built_source", pd.Series([None] * len(g))),
                "zensus age class": g.get("zensus_age_class", pd.Series([None] * len(g))),
                "zensus type class": g.get("zensus_type_class", pd.Series([None] * len(g))),
                "zensus type note": g.get("zensus_type_note", pd.Series([None] * len(g))),
                "zensus year note": g.get("zensus_year_note", pd.Series([None] * len(g))),
                "zensus source": g.get("zensus_source", pd.Series([None] * len(g))),
                "source": "osm",
            })

            # manual buildings (treated as separate rows or to be merged)
            mb = out.get("manual_buildings_df", pd.DataFrame())
            if isinstance(mb, pd.DataFrame) and not mb.empty:
                m = mb.copy()
                m["bidx"] = m["bidx"].astype(str)
                man_info = pd.DataFrame({
                    "bidx": m["bidx"],
                    "building index": m["bidx"].map(lambda x: f"Building_{x}"),
                    "address": None,
                    "name": m.get("name", pd.Series([None] * len(m))),
                    "building type": m.get("building type", pd.Series([None] * len(m))),
                    "building type source": m.get("building type source", pd.Series(["manual"] * len(m))),
                    "area (m²)": pd.to_numeric(m.get("area (m²)", pd.Series([np.nan]*len(m))), errors="coerce"),
                    "year built": pd.to_numeric(m.get("year built", pd.Series([np.nan]*len(m))), errors="coerce"),
                    "retrofit situation": m.get("retrofit situation", pd.Series([None] * len(m))),
                    "renovation probability (%)": np.nan,
                    "year source": m.get(
                        "year source",
                        pd.Series(
                            np.where(
                                pd.to_numeric(m.get("year built", pd.Series([np.nan]*len(m))), errors="coerce").notna(),
                                "manual",
                                None,
                            )
                        ),
                    ),
                    "zensus age class": m.get("zensus age class", pd.Series([None] * len(m))),
                    "zensus type class": m.get("zensus type class", pd.Series([None] * len(m))),
                    "zensus type note": m.get("zensus type note", pd.Series([None] * len(m))),
                    "zensus year note": m.get("zensus year note", pd.Series([None] * len(m))),
                    "zensus source": m.get("zensus source", pd.Series([None] * len(m))),
                    "source": "manual",
                })
                info = pd.concat([info, man_info], ignore_index=True)

            # ensure one row per bidx
            info = info.drop_duplicates(subset=["bidx"], keep="last").reset_index(drop=True)
            out["info_overridden"] = info
    except Exception:
        pass

    try:
        ss = st.session_state
        buildings_gdf = out.get("buildings_gdf")
        polygons_wkt = out.get("polygons_wkt") or []

        if buildings_gdf is not None and not getattr(buildings_gdf, "empty", True) and polygons_wkt:
            # canonical source for this project
            ss["buildings_gdf"] = buildings_gdf
            ss["_preloaded_buildings_gdf"] = buildings_gdf.copy()
            ss["_preloaded_drawn_polygons"] = list(polygons_wkt)
            ss["_osm_preloaded"] = True
    except Exception:
        pass

    return out

def norm_btype(val):
    """Normalize OSM building type strings to a canonical form or None."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip().lower()
    if s in ("", "none", "yes", "building", "true", "1"):
        return None
    return s

# ---- osmid normalized to Int64 ----
def _norm_osmid(v):
    if isinstance(v, (list, tuple, set)):
        try:
            return sorted(v)[0]
        except Exception:
            return None
    return v


def _clean_str(x):
    if x is None or pd.isna(x):
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    return s


def _clean_int(x):
    if x is None or pd.isna(x):
        return None
    try:
        return int(float(x))  # handles Excel: 154498382.0 -> 154498382
    except Exception:
        return None


def _stable_key(row):
    osm_id = _clean_str(row.get("osm_id"))
    if osm_id:
        return f"osm_id:{osm_id.lower()}"

    osmid = _clean_int(row.get("osmid"))
    if osmid is not None:
        return f"osmid:{osmid}"

    try:
        geom = getattr(row, "geometry", None)
        return "geom:" + hashlib.sha1(geom.wkb).hexdigest() if geom is not None else None
    except Exception:
        return None

def _rehydrate_project_state(loaded):

    # -----------------------------------------
    # 0. Start restoring UI state
    # -----------------------------------------
    if "info_overridden" in loaded and isinstance(loaded["info_overridden"], pd.DataFrame):
        st.session_state["info_overridden"] = loaded["info_overridden"].copy()

    ss = st.session_state

    ss["drawn_polygons"] = list(loaded.get("polygons_wkt", []))
    ss["excluded_bidx"] = set(map(str, loaded.get("excluded_bidx", set())))
    if isinstance(loaded.get("zensus_summary_df"), pd.DataFrame):
        ss["zensus_summary"] = loaded.get("zensus_summary_df").copy()

    # -----------------------------------------
    # 1. Restore buildings_gdf
    # -----------------------------------------
    gdf = loaded.get("buildings_gdf")
    if gdf is not None and not getattr(gdf, "empty", True):
        gdf = gdf.copy()
        if "bidx" in gdf.columns:
            gdf["bidx"] = gdf["bidx"].astype(str)
        gdf = gdf.sort_values(by="bidx", key=lambda s: pd.to_numeric(s, errors="coerce")).reset_index(drop=True)

        ss["buildings_gdf"] = gdf
        ss["_preloaded_buildings_gdf"] = gdf
        ss["_last_buildings_gdf"] = gdf

        # -----------------------------------------
        # 1A. CREATE STABLE BIDX MAP
        # -----------------------------------------
        bmap, next_id = {}, 0
        for _, r in gdf.iterrows():
            k = _stable_key(r)

            # If no stable key, create a deterministic fallback tied to bidx,
            # NOT to iteration order.
            if not k:
                k = f"bidx:{str(r.get('bidx'))}"

            if k not in bmap:
                bmap[k] = str(r["bidx"])

            try:
                next_id = max(next_id, int(r["bidx"]) + 1)
            except Exception:
                next_id += 1

        ss["bidx_map"] = bmap
        ss["next_bidx"] = next_id

        # -----------------------------------------
        # 1B. ENSURE BASE SNAPSHOT (only if missing)
        # -----------------------------------------
        if "_osm_base_attrs" not in ss or not isinstance(ss["_osm_base_attrs"], pd.DataFrame) or ss[
            "_osm_base_attrs"].empty:
            # No persisted base_attrs present (legacy project) → freeze current OSM as baseline
            base_snapshot = pd.DataFrame({
                "bidx": gdf["bidx"].astype(str),
                "building_type_base": gdf.get("building", pd.Series([None] * len(gdf))).map(norm_btype),
                "area_base_m2": pd.to_numeric(gdf.get("area_m2", None), errors="coerce"),
                "year_built_base": pd.to_numeric(gdf.get("year_built", pd.Series([np.nan] * len(gdf))), errors="coerce"),
                "retrofit_situation_base": gdf.get("retrofit_situation", pd.Series([None] * len(gdf))).apply(
                    _normalize_retrofit_situation
                ),
                "address": gdf.get("address", pd.Series([None] * len(gdf))),
                "osm_id": gdf.get("osm_id", None),
                "osmid": gdf.get("osmid", None),
            })

            ss["_osm_base_attrs"] = base_snapshot
        else:
            # We already loaded a base snapshot from base_attrs; keep it.
            # Optionally, append rows for any *new* bidx not in the snapshot.
            base_snapshot = ss["_osm_base_attrs"].copy()
            base_snapshot["bidx"] = base_snapshot["bidx"].astype(str)
            seen = set(base_snapshot["bidx"])

            new_rows = gdf[~gdf["bidx"].astype(str).isin(seen)]
            if not new_rows.empty:
                extra = pd.DataFrame({
                    "bidx": new_rows["bidx"].astype(str),
                    "building_type_base": new_rows.get("building", pd.Series([None] * len(new_rows))).map(norm_btype),
                    "area_base_m2": pd.to_numeric(new_rows.get("area_m2", None), errors="coerce"),
                    "year_built_base": pd.to_numeric(new_rows.get("year_built", pd.Series([np.nan] * len(new_rows))), errors="coerce"),
                    "retrofit_situation_base": new_rows.get(
                        "retrofit_situation", pd.Series([None] * len(new_rows))
                    ).apply(_normalize_retrofit_situation),
                    "address": new_rows.get("address", pd.Series([None] * len(new_rows))),
                    "osm_id": new_rows.get("osm_id", None),
                    "osmid": new_rows.get("osmid", None),
                })
                base_snapshot = pd.concat([base_snapshot, extra], ignore_index=True)

            ss["_osm_base_attrs"] = base_snapshot

    # -----------------------------------------
    # 2. Restore manual buildings
    # -----------------------------------------
    mb = loaded.get("manual_buildings_df", pd.DataFrame(columns=[
        "bidx", "name", "building type", "area (m²)", "year built", "retrofit situation",
        "building type source", "year source", "zensus age class", "zensus type class",
        "zensus type note", "zensus year note", "zensus source",
    ])).copy()
    if not mb.empty and "bidx" in mb.columns:
        mb["bidx"] = mb["bidx"].astype(str)
    ss["manual_buildings"] = mb
    ss["manual_buildings"] = _normalize_manual_buildings_df(ss["manual_buildings"])
    mb = ss["manual_buildings"]
    if not mb.empty and "bidx" in mb.columns:
        numeric_manual = pd.to_numeric(mb["bidx"], errors="coerce")
        if numeric_manual.notna().any():
            ss["next_bidx"] = max(int(ss.get("next_bidx", 0) or 0), int(numeric_manual.max()) + 1)

    # -----------------------------------------
    # 3. Restore override attributes
    # -----------------------------------------
    ov = _sanitize_building_attrs_df(
        loaded.get("building_attrs_df", pd.DataFrame(
            columns=["bidx","building type","area (m²)","address","year built","retrofit situation"]
        ))
    )
    ss["building_attrs"] = ov

    # Apply overrides to buildings_gdf
    gdf = ss.get("buildings_gdf")
    attrs = ss.get("building_attrs", pd.DataFrame())
    if gdf is not None and not gdf.empty and isinstance(attrs, pd.DataFrame) and not attrs.empty:

        g = gdf.copy()
        g["bidx"] = g["bidx"].astype(str)

        a = attrs.copy()
        a["bidx"] = a["bidx"].astype(str)

        # building type override
        if "building type" in a.columns:
            t = a[["bidx", "building type"]].dropna(subset=["building type"]).copy()
            t["building type"] = t["building type"].map(norm_btype)
            t = t.rename(columns={"building type": "building_type_ovr"})
            g = g.merge(t, on="bidx", how="left")
            g["building"] = g["building_type_ovr"].combine_first(g.get("building"))
            g.drop(columns=["building_type_ovr"], inplace=True, errors="ignore")

        # area override
        if "area (m²)" in a.columns:
            u = a[["bidx", "area (m²)"]].copy()
            u["area (m²)"] = pd.to_numeric(u["area (m²)"], errors="coerce")
            u = u.dropna(subset=["area (m²)"])
            if not u.empty:
                u = u.rename(columns={"area (m²)": "area_m2_ovr"})
                g = g.merge(u, on="bidx", how="left")
                g["area_m2"] = g["area_m2_ovr"].combine_first(g.get("area_m2"))
                g.drop(columns=["area_m2_ovr"], inplace=True, errors="ignore")

        # number-of-levels override
        if "levels" in a.columns:
            lev = a[["bidx", "levels"]].copy()
            lev["levels"] = pd.to_numeric(lev["levels"], errors="coerce")
            lev = lev.dropna(subset=["levels"])
            if not lev.empty:
                lev = lev.rename(columns={"levels": "levels_ovr"})
                g = g.merge(lev, on="bidx", how="left")
                if "levels" not in g.columns:
                    g["levels"] = np.nan
                g["levels"] = g["levels_ovr"].combine_first(pd.to_numeric(g["levels"], errors="coerce"))
                g.drop(columns=["levels_ovr"], inplace=True, errors="ignore")

        # address override
        if "address" in a.columns:
            v = a[["bidx", "address"]].copy()
            v["address"] = v["address"].apply(lambda s: None if pd.isna(s) or str(s).strip()=="" else str(s).strip())
            v = v.dropna(subset=["address"])
            if not v.empty:
                g = g.merge(v, on="bidx", how="left", suffixes=("", "_ovr"))
                g["address"] = g["address_ovr"].combine_first(g["address"])
                g.drop(columns=["address_ovr"], inplace=True, errors="ignore")

        # year-built override
        if "year built" in a.columns:
            y = a[["bidx", "year built"]].copy()
            y["year built"] = pd.to_numeric(y["year built"], errors="coerce")
            y = y.dropna(subset=["year built"])
            if not y.empty:
                y = y.rename(columns={"year built": "year_built_ovr"})
                g = g.merge(y, on="bidx", how="left")
                if "year_built" not in g.columns:
                    g["year_built"] = np.nan
                if "year_built_source" not in g.columns:
                    g["year_built_source"] = None
                g["year_built"] = g["year_built_ovr"].combine_first(g["year_built"])
                g["year_built_source"] = np.where(g["year_built_ovr"].notna(), "user", g["year_built_source"])
                g.drop(columns=["year_built_ovr"], inplace=True, errors="ignore")

        # retrofit-situation override
        if "retrofit situation" in a.columns:
            r = a[["bidx", "retrofit situation"]].copy()
            r["retrofit situation"] = r["retrofit situation"].apply(_normalize_retrofit_situation)
            r = r.dropna(subset=["retrofit situation"])
            if not r.empty:
                r = r.rename(columns={"retrofit situation": "retrofit_situation_ovr"})
                g = g.merge(r, on="bidx", how="left")
                if "retrofit_situation" not in g.columns:
                    g["retrofit_situation"] = None
                g["retrofit_situation"] = g["retrofit_situation_ovr"].combine_first(g["retrofit_situation"])
                g.drop(columns=["retrofit_situation_ovr"], inplace=True, errors="ignore")

        ss["buildings_gdf"] = g
        ss["_preloaded_buildings_gdf"] = g
        ss["_last_buildings_gdf"] = g

    # -----------------------------------------
    # 4. Demand estimates
    # -----------------------------------------
    def _std(df):
        if df is None or getattr(df, "empty", True):
            return pd.DataFrame(columns=["input_index","heat_demand_kWh"])
        d = df.copy()
        if "bidx" in d.columns and "input_index" not in d.columns:
            d = d.rename(columns={"bidx":"input_index"})
        if "input_index" not in d.columns or "heat_demand_kWh" not in d.columns:
            return pd.DataFrame(columns=["input_index","heat_demand_kWh"])
        d["input_index"] = d["input_index"].astype(str)
        return d[["input_index","heat_demand_kWh"]]

    ss["building_demand_estimates"] = _std(loaded.get("estimates_df_final"))
    ss["building_demand_estimates_original"] = _std(loaded.get("estimates_df_original"))

    # totals
    tot = loaded.get("total_heat_demand_kwh")
    if tot is None:
        fin = ss["building_demand_estimates"]
        tot = float(pd.to_numeric(fin["heat_demand_kWh"], errors="coerce").fillna(0).sum())

    ss["total_heat_demand"] = float(tot)
    ss["route_length"] = loaded.get("route_length_m")

    dhw_est = _std_dhw_estimates_df(loaded.get("dhw_estimates_df"))
    dhw_profile = _std_dhw_profile_df(loaded.get("dhw_profile_df"))
    ss["dhw_demand_estimates"] = dhw_est
    ss["dhw_load_profile"] = dhw_profile

    total_dhw = loaded.get("total_dhw_demand_kwh")
    if total_dhw is None and not dhw_est.empty:
        total_dhw = float(
            pd.to_numeric(dhw_est["annual_dhw_demand_kWh"], errors="coerce").fillna(0).sum()
        )
    ss["total_dhw_demand"] = float(total_dhw) if total_dhw is not None else None

    # state flags
    ss["results_stale"] = False
    ss["dhw_results_stale"] = False
    ss["show_teaser_results"] = not ss["building_demand_estimates"].empty
    ss["show_dhw_results"] = not dhw_est.empty
    ss["_osm_preloaded"] = True

def get_project_data_file(project_name: str):
    """Per-project workbook to store OSM selection state."""
    proj_dir = get_project_dir(project_name)
    os.makedirs(proj_dir, exist_ok=True)
    return os.path.join(proj_dir, "project_data.xlsx")

def load_osm_selection_from_excel_v2(project_name: str):
    """
    Read the v2 workbook written by save_project_dataset_v2:
      - Sheet 'polygons'               -> drawn_polygons
      - Sheet 'buildings_dataset'      -> buildings_gdf (+ excluded, manual_buildings, building_attrs)
                                         and estimates_df_final / estimates_df_original
      - Sheet 'meta' (optional)        -> total_heat_demand_kwh, route_length_m

    Returns a dict with keys:
      drawn_polygons: list[str]
      buildings_gdf: (Geo)DataFrame or None
      excluded_bidx: set[str]
      manual_buildings: DataFrame[bidx,name,building type,area (m²)]
      building_attrs:  DataFrame[bidx,(building type)?,(area (m²))?,(address)?]  # only edited fields included
      estimates_df_final:    DataFrame[input_index,heat_demand_kWh,peak_load_kW,estimation_method]
      estimates_df_original: DataFrame[input_index,heat_demand_kWh,peak_load_kW,estimation_method]
      total_heat_demand_kwh: float | None
      route_length_m:        float | None
    """
    out = {
        "drawn_polygons": [],
        "buildings_gdf": None,
        "excluded_bidx": set(),
        "manual_buildings": pd.DataFrame(columns=[
            "bidx", "name", "building type", "area (m²)", "year built", "retrofit situation",
            "building type source", "year source", "zensus age class", "zensus type class",
            "zensus type note", "zensus year note", "zensus source",
        ]),
        "building_attrs": pd.DataFrame(columns=["bidx", "building type", "area (m²)", "address", "year built", "retrofit situation"]),
        "estimates_df_final": pd.DataFrame(columns=["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]),
        "estimates_df_original": pd.DataFrame(columns=["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]),
        "total_heat_demand_kwh": None,
        "route_length_m": None,
        "zensus_summary_df": pd.DataFrame(),
    }

    if not project_name:
        return out

    path = get_project_data_file(project_name)
    if not os.path.exists(path):
        return out

    # --- polygons
    try:
        df_polys = pd.read_excel(path, sheet_name="polygons")
        if "wkt" in df_polys.columns:
            out["drawn_polygons"] = df_polys["wkt"].dropna().astype(str).tolist()
    except Exception:
        pass

    # --- buildings, overrides, excluded, estimates
    try:
        ds = pd.read_excel(path, sheet_name="buildings_dataset")
    except Exception:
        ds = pd.DataFrame()

    if not ds.empty:
        # Restore geometry if present
        geom = None
        if "geom_wkt" in ds.columns:
            try:
                geom = gpd.GeoSeries.from_wkt(ds["geom_wkt"], crs="EPSG:4326")
            except Exception:
                geom = None

        # Build a GDF/DF with the most useful columns (renamed to what your UI expects)
        cols_keep = []
        for c in [
            "bidx", "building_type_final", "building_type_base",
            "area_final_m2", "area_base_m2",
            "building_type_source", "year_built_final", "year_built_base", "year_built_source",
            "retrofit_situation_final", "retrofit_situation_base",
            "retrofit_situation", "retrofit_probability",
            "zensus_age_class", "zensus_type_class", "zensus_source",
            "zensus_type_note", "zensus_year_note",
            "levels", "height_m", "name", "address",
            "osm_id", "osmid"
        ]:
            if c in ds.columns:
                cols_keep.append(c)

        gdf_like = ds[cols_keep].copy() if cols_keep else pd.DataFrame()

        # Normalize to expected names
        if "building_type_final" in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"building_type_final": "building"})
        elif "building_type_base" in gdf_like.columns and "building" not in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"building_type_base": "building"})

        if "area_final_m2" in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"area_final_m2": "area_m2"})
        elif "area_base_m2" in gdf_like.columns and "area_m2" not in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"area_base_m2": "area_m2"})
        if "year_built_final" in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"year_built_final": "year_built"})
        elif "year_built_base" in gdf_like.columns and "year_built" not in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"year_built_base": "year_built"})
        if "retrofit_situation_final" in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"retrofit_situation_final": "retrofit_situation"})
        elif "retrofit_situation_base" in gdf_like.columns and "retrofit_situation" not in gdf_like.columns:
            gdf_like = gdf_like.rename(columns={"retrofit_situation_base": "retrofit_situation"})

        if "bidx" not in gdf_like.columns:
            gdf_like["bidx"] = ds.index.astype(str)
        gdf_like["bidx"] = gdf_like["bidx"].astype(str)

        if geom is not None:
            try:
                gdf = gpd.GeoDataFrame(gdf_like, geometry=geom, crs="EPSG:4326")
            except Exception:
                gdf = gdf_like
        else:
            gdf = gdf_like

        out["buildings_gdf"] = gdf

        # Exclusions (from included flag)
        if "included" in ds.columns and "bidx" in ds.columns:
            try:
                inc = _to_bool_series(ds["included"])
                excl = ds.loc[~inc, "bidx"].astype(str).tolist()
                out["excluded_bidx"] = set(excl)
            except Exception:
                pass

        # Manual buildings (source == 'manual')
        if "source" in ds.columns:
            try:
                man = ds.loc[ds["source"].astype(str).str.lower() == "manual"].copy()
                if not man.empty:
                    mb = pd.DataFrame({
                        "bidx": man["bidx"].astype(str),
                        "name": man.get("name", pd.Series([None] * len(man))),
                        "building type": man.get("building_type_final", man.get("building_type_base", None)),
                        "area (m²)": pd.to_numeric(
                            man.get("area_final_m2", man.get("area_base_m2", np.nan)), errors="coerce"
                        ),
                        "levels": pd.to_numeric(man.get("levels", np.nan), errors="coerce"),
                        "year built": pd.to_numeric(
                            man.get("year_built_final", man.get("year_built_base", np.nan)), errors="coerce"
                        ),
                        "retrofit situation": man.get("retrofit_situation_final", man.get("retrofit_situation", None)),
                        "building type source": man.get("building_type_source", pd.Series([None] * len(man))),
                        "year source": man.get("year_built_source", pd.Series([None] * len(man))),
                        "zensus age class": man.get("zensus_age_class", pd.Series([None] * len(man))),
                        "zensus type class": man.get("zensus_type_class", pd.Series([None] * len(man))),
                        "zensus type note": man.get("zensus_type_note", pd.Series([None] * len(man))),
                        "zensus year note": man.get("zensus_year_note", pd.Series([None] * len(man))),
                        "zensus source": man.get("zensus_source", pd.Series([None] * len(man))),
                    })
                    out["manual_buildings"] = _normalize_manual_buildings_df(mb)
            except Exception:
                pass

        # Overrides (only where OSM rows were edited)
        try:
            # Prefer explicit edited_* flags if present
            has_flags = all(c in ds.columns for c in ["edited_type", "edited_area", "edited_address"])
            has_year_flag = "edited_year" in ds.columns
            if "source" in ds.columns and "bidx" in ds.columns:
                is_osm = ds["source"].astype(str).str.lower().eq("osm")

                if has_flags:
                    edited_type = _to_bool_series(ds["edited_type"])
                    edited_area = _to_bool_series(ds["edited_area"])
                    edited_address = _to_bool_series(ds["edited_address"])
                    edited_year = _to_bool_series(ds["edited_year"]) if has_year_flag else pd.Series(False, index=ds.index)
                    if "retrofit_situation_source" in ds.columns:
                        # This also repairs workbooks saved by older versions,
                        # where automatic assumptions were incorrectly flagged
                        # as edited even though their source remained estimated.
                        edited_retrofit = ds["retrofit_situation_source"].astype(str).str.lower().eq("user")
                    else:
                        edited_retrofit = (
                            _to_bool_series(ds["edited_retrofit"])
                            if has_retrofit_flag else pd.Series(False, index=ds.index)
                        )
                    edited_mask = is_osm & (
                            edited_type |
                            edited_area |
                            edited_address |
                            edited_year |
                            edited_retrofit
                    )
                    sub = ds.loc[edited_mask, ["bidx"]].copy()
                    if "building_type_final" in ds.columns:
                        sub.loc[edited_type.loc[edited_mask], "building type"] = ds.loc[
                            edited_mask & edited_type, "building_type_final"
                        ]
                    if "area_final_m2" in ds.columns:
                        sub.loc[edited_area.loc[edited_mask], "area (m²)"] = ds.loc[
                            edited_mask & edited_area, "area_final_m2"
                        ]
                    if "address" in ds.columns:
                        sub.loc[edited_address.loc[edited_mask], "address"] = ds.loc[
                            edited_mask & edited_address, "address"
                        ]
                    if "year_built_final" in ds.columns:
                        sub.loc[edited_year.loc[edited_mask], "year built"] = ds.loc[
                            edited_mask & edited_year, "year_built_final"
                        ]
                    if "retrofit_situation_final" in ds.columns:
                        sub.loc[edited_retrofit.loc[edited_mask], "retrofit situation"] = ds.loc[
                            edited_mask & edited_retrofit, "retrofit_situation_final"
                        ]
                else:
                    # Fallback: infer edits by comparing base vs final where both exist
                    compare_rows = ds.loc[is_osm].copy()
                    sub = pd.DataFrame({"bidx": compare_rows["bidx"].astype(str)})
                    # type
                    if "building_type_final" in compare_rows.columns and "building_type_base" in compare_rows.columns:
                        m_type = (compare_rows["building_type_final"].fillna("__na__") !=
                                  compare_rows["building_type_base"].fillna("__na__"))
                        sub.loc[m_type, "building type"] = compare_rows.loc[m_type, "building_type_final"]
                    # area
                    if "area_final_m2" in compare_rows.columns and "area_base_m2" in compare_rows.columns:
                        af = pd.to_numeric(compare_rows["area_final_m2"], errors="coerce")
                        ab = pd.to_numeric(compare_rows["area_base_m2"], errors="coerce")
                        m_area = ~np.isclose(af, ab, equal_nan=True)
                        sub.loc[m_area, "area (m²)"] = af[m_area]
                    # address (carry final/address if present)
                    if "address" in compare_rows.columns:
                        # keep only non-empty string addresses
                        addr = compare_rows["address"].astype(str).str.strip()
                        m_addr = addr.ne("") & addr.notna()
                        sub.loc[m_addr, "address"] = compare_rows.loc[m_addr, "address"]
                    # year
                    if "year_built_final" in compare_rows.columns:
                        yf = pd.to_numeric(compare_rows["year_built_final"], errors="coerce")
                        if "year_built_source" in compare_rows.columns:
                            m_year = compare_rows["year_built_source"].astype(str).str.lower().eq("user")
                        elif "year_built_base" in compare_rows.columns:
                            yb = pd.to_numeric(compare_rows["year_built_base"], errors="coerce")
                            m_year = ~np.isclose(yf, yb, equal_nan=True)
                        else:
                            m_year = pd.Series(False, index=compare_rows.index)
                        sub.loc[m_year, "year built"] = yf[m_year]
                    # retrofit situation
                    if "retrofit_situation_final" in compare_rows.columns:
                        rf = compare_rows["retrofit_situation_final"].apply(_normalize_retrofit_situation)
                        if "retrofit_situation_base" in compare_rows.columns:
                            rb = compare_rows["retrofit_situation_base"].apply(_normalize_retrofit_situation)
                            m_retrofit = rf.fillna("__na__") != rb.fillna("__na__")
                        else:
                            m_retrofit = pd.Series(False, index=compare_rows.index)
                        sub.loc[m_retrofit, "retrofit situation"] = rf[m_retrofit]

                    # Keep only rows where we actually have any override
                    keep_cols = ["building type", "area (m²)", "address", "year built", "retrofit situation"]
                    existing = [c for c in keep_cols if c in sub.columns]
                    if existing:
                        non_empty = ~sub[existing].isna().all(axis=1)
                        sub = sub.loc[non_empty]
                    else:
                        sub = sub.iloc[0:0]

                if not sub.empty:
                    sub["bidx"] = sub["bidx"].astype(str)
                    out["building_attrs"] = _sanitize_building_attrs_df(sub)
        except Exception:
            pass

        # Estimates (final + original) from dataset columns
        try:
            cand_final = ["demand_kwh_final", "heat_kwh_final", "heat_demand_kWh_final"]
            cand_auto = ["demand_kwh_auto", "heat_kwh_auto", "heat_demand_kWh_auto"]

            final_col = next((c for c in cand_final if c in ds.columns), None)
            auto_col = next((c for c in cand_auto if c in ds.columns), None)

            if final_col:
                out["estimates_df_final"] = _mk_est_from_ds(ds, final_col)
            if auto_col:
                out["estimates_df_original"] = _mk_est_from_ds(ds, auto_col)
        except Exception:
            pass

    # --- meta (optional)
    try:
        df_meta = pd.read_excel(path, sheet_name="meta")
        if {"key", "value"} <= set(df_meta.columns):
            kv = dict(zip(df_meta["key"].astype(str), df_meta["value"]))
            if "total_heat_demand_kwh" in kv and pd.notna(kv["total_heat_demand_kwh"]):
                try:
                    out["total_heat_demand_kwh"] = float(kv["total_heat_demand_kwh"])
                except Exception:
                    pass
            if "route_length_m" in kv and pd.notna(kv["route_length_m"]):
                try:
                    out["route_length_m"] = float(kv["route_length_m"])
                except Exception:
                    pass
    except Exception:
        # If meta is missing, compute a fallback total from final estimates if available
        if out["total_heat_demand_kwh"] is None and not out["estimates_df_final"].empty:
            try:
                out["total_heat_demand_kwh"] = float(
                    pd.to_numeric(out["estimates_df_final"]["heat_demand_kWh"], errors="coerce").fillna(0).sum()
                )
            except Exception:
                pass

    try:
        out["zensus_summary_df"] = pd.read_excel(path, sheet_name="zensus_summary")
    except Exception:
        pass

    return out

def _has_useful(d):
    return bool(d.get("polygons_wkt")) \
        or (d.get("buildings_gdf") is not None and not getattr(d["buildings_gdf"], "empty", True)) \
        or (not d.get("estimates_df_final", pd.DataFrame()).empty) \
        or (not d.get("estimates_df_original", pd.DataFrame()).empty)

def _has_letters_safe(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    try:
        return any(c.isalpha() for c in str(v))
    except Exception:
        return False

def _build_unified_dataset(
        buildings_gdf,
        manual_buildings_df,
        excluded_bidx,
        estimates_df_final,
        estimates_df_original,
        prev_dataset=None,
):
    # ========== 1. Load stable base snapshot ==========
    prev_base = st.session_state.get("_osm_base_attrs")
    if isinstance(prev_base, pd.DataFrame) and not prev_base.empty:
        prev_base = prev_base.copy()
        prev_base["bidx"] = prev_base["bidx"].astype(str)
        prev_base = prev_base.set_index("bidx", drop=False)
    else:
        prev_base = None

    rows = []

    # ========== 2. OSM buildings ==========
    if buildings_gdf is not None and not buildings_gdf.empty:
        g = buildings_gdf.copy()
        g["bidx"] = g.get("bidx", g.index.astype(str)).astype(str)

        # base values ALWAYS from stable snapshot
        n = len(g)
        if prev_base is not None:
            bt_base = g["bidx"].map(prev_base["building_type_base"])
            area_base = g["bidx"].map(prev_base["area_base_m2"])
            year_base = g["bidx"].map(prev_base["year_built_base"]) if "year_built_base" in prev_base.columns else pd.Series([np.nan] * n, index=g.index)
            addr_base = g["bidx"].map(prev_base["address"])
            retrofit_base = (
                g["bidx"].map(prev_base["retrofit_situation_base"])
                if "retrofit_situation_base" in prev_base.columns
                else pd.Series([None] * n, index=g.index)
            )
        else:
            bt_base = g.get("building", pd.Series([None] * n)).map(norm_btype)
            area_base = pd.to_numeric(g.get("area_m2", pd.Series([np.nan] * n)), errors="coerce")
            year_base = pd.to_numeric(g.get("year_built", pd.Series([np.nan] * n)), errors="coerce")
            addr_base = g.get("address", pd.Series([None] * n))
            retrofit_base = g.get("retrofit_situation", pd.Series([None] * n, index=g.index))

        bt_final = g.get("building").map(norm_btype)
        area_final = pd.to_numeric(g.get("area_m2"), errors="coerce")
        year_final = pd.to_numeric(g.get("year_built", pd.Series([np.nan] * n)), errors="coerce")
        addr_final = g.get("address")
        retrofit_final = g.get("retrofit_situation", pd.Series([None] * n, index=g.index)).apply(_normalize_retrofit_situation)
        retrofit_base = retrofit_base.apply(_normalize_retrofit_situation)
        retrofit_probability = pd.to_numeric(
            g.get("retrofit_probability", pd.Series([np.nan] * n, index=g.index)),
            errors="coerce",
        )
        bt_source = g.get("building_type_source", pd.Series([None] * n, index=g.index)).fillna("")
        year_source = g.get("year_built_source", pd.Series([None] * n, index=g.index)).fillna("")
        retrofit_source = g.get("retrofit_situation_source", pd.Series([None] * n, index=g.index)).fillna("")

        edited_type = (bt_source.astype(str).str.lower().eq("user")) | (
            (bt_base.fillna("__") != bt_final.fillna("__"))
            & ~bt_source.astype(str).str.lower().eq("zensus")
        )
        edited_area = ~np.isclose(area_base.fillna(np.nan), area_final.fillna(np.nan), equal_nan=True)
        edited_year = (year_source.astype(str).str.lower().eq("user")) | (
            ~np.isclose(year_base.fillna(np.nan), year_final.fillna(np.nan), equal_nan=True)
            & ~year_source.astype(str).str.lower().eq("zensus")
        )
        # Automatic assumptions can differ from raw OSM data. Only an explicit
        # user source represents an edit; comparing final and base values made
        # every automatically assigned retrofit status reopen as user-edited.
        edited_retrofit = retrofit_source.astype(str).str.lower().eq("user")

        saved_attrs = _sanitize_building_attrs_df(st.session_state.get("building_attrs"))
        if not saved_attrs.empty:
            saved_attrs = saved_attrs.copy()
            saved_attrs["bidx"] = saved_attrs["bidx"].astype(str)
            levels_override = pd.to_numeric(
                saved_attrs.set_index("bidx")["levels"], errors="coerce"
            ).to_dict()
            height_override = pd.to_numeric(
                saved_attrs.set_index("bidx")["height (m)"], errors="coerce"
            ).to_dict()
        else:
            levels_override = {}
            height_override = {}
        edited_levels = g["bidx"].map(lambda b: pd.notna(levels_override.get(str(b))))
        edited_height = g["bidx"].map(lambda b: pd.notna(height_override.get(str(b))))

        def _canon(v):
            if v is None or pd.isna(v): return None
            s = str(v).strip().lower()
            return s if s else None

        edited_address = addr_base.map(_canon) != addr_final.map(_canon)

        rows.append(pd.DataFrame({
            "bidx": g["bidx"],
            "source": "osm",
            "included": ~g["bidx"].isin(set(map(str, excluded_bidx))),
            "edited_attrs": (
                edited_type | edited_area | edited_levels | edited_height
                | edited_address | edited_year | edited_retrofit
            ),
            "edited_type": edited_type,
            "edited_area": edited_area,
            "edited_levels": edited_levels,
            "edited_height": edited_height,
            "edited_address": edited_address,
            "edited_year": edited_year,
            "edited_retrofit": edited_retrofit,
            "name": g.get("name"),
            "address": addr_final,
            "building_type_base": bt_base,
            "building_type_final": bt_final,
            "building_type_source": bt_source.replace("", np.nan),
            "area_base_m2": area_base,
            "area_final_m2": area_final,
            "year_built_base": year_base,
            "year_built_final": year_final,
            "year_built_source": year_source.replace("", np.nan),
            "retrofit_situation_base": retrofit_base,
            "retrofit_situation_final": retrofit_final,
            "retrofit_situation_source": retrofit_source.replace("", np.nan),
            "retrofit_probability": retrofit_probability,
            "zensus_age_class": g.get("zensus_age_class", pd.Series([None] * n, index=g.index)),
            "zensus_type_class": g.get("zensus_type_class", pd.Series([None] * n, index=g.index)),
            "zensus_type_note": g.get("zensus_type_note", pd.Series([None] * n, index=g.index)),
            "zensus_year_note": g.get("zensus_year_note", pd.Series([None] * n, index=g.index)),
            "zensus_source": g.get("zensus_source", pd.Series([None] * n, index=g.index)),
            "levels": g.get("levels"),
            "height_m": g.get("height_m"),
            "osm_id": g.get("osm_id"),
            "osmid": g.get("osmid"),
            "geom_wkt": g.geometry.apply(lambda x: x.wkt if x is not None else None),
        }))

    # ========== 3. Manual buildings ==========
    if manual_buildings_df is not None and not manual_buildings_df.empty:
        m = manual_buildings_df.copy()
        m["bidx"] = m["bidx"].astype(str)

        bt = m["building type"].map(norm_btype)
        area = pd.to_numeric(m["area (m²)"], errors="coerce")
        year = pd.to_numeric(m.get("year built", pd.Series([np.nan] * len(m))), errors="coerce")
        retrofit = m.get("retrofit situation", pd.Series([None] * len(m), index=m.index)).apply(_normalize_retrofit_situation)
        name_series = m.get("name")
        bt_source = m.get("building type source", pd.Series([None] * len(m), index=m.index))
        bt_source = bt_source.where(
            bt_source.notna() & bt_source.astype(str).str.strip().ne(""),
            np.where(bt.notna(), "manual", None),
        )
        year_source = m.get("year source", pd.Series([None] * len(m), index=m.index))
        year_source = year_source.where(
            year_source.notna() & year_source.astype(str).str.strip().ne(""),
            np.where(year.notna(), "manual", None),
        )

        rows.append(pd.DataFrame({
            "bidx": m["bidx"],
            "source": "manual",
            "included": ~m["bidx"].isin(set(map(str, excluded_bidx))),
            "edited_attrs": False,
            "edited_type": False,
            "edited_area": False,
            "edited_levels": False,
            "edited_height": False,
            "edited_address": False,
            "edited_year": False,
            "edited_retrofit": False,
            "name": name_series,
            "address": None,
            "building_type_base": bt,
            "building_type_final": bt,
            "building_type_source": bt_source,
            "area_base_m2": area,
            "area_final_m2": area,
            "year_built_base": year,
            "year_built_final": year,
            "year_built_source": year_source,
            "retrofit_situation_base": retrofit,
            "retrofit_situation_final": retrofit,
            "retrofit_situation_source": "manual",
            "retrofit_probability": np.nan,
            "zensus_age_class": m.get("zensus age class", pd.Series([None] * len(m), index=m.index)),
            "zensus_type_class": m.get("zensus type class", pd.Series([None] * len(m), index=m.index)),
            "zensus_type_note": m.get("zensus type note", pd.Series([None] * len(m), index=m.index)),
            "zensus_year_note": m.get("zensus year note", pd.Series([None] * len(m), index=m.index)),
            "zensus_source": m.get("zensus source", pd.Series([None] * len(m), index=m.index)),
            "levels": pd.to_numeric(m.get("levels", pd.Series([np.nan] * len(m), index=m.index)), errors="coerce"),
            "height_m": pd.to_numeric(
                m.get("height (m)", pd.Series([np.nan] * len(m), index=m.index)), errors="coerce"
            ),
            "osm_id": None,
            "osmid": None,
            "geom_wkt": None,
        }))

    unified = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()

    if unified.empty:
        unified = pd.DataFrame({"bidx": pd.Series(dtype=str)})

    # ========== 4. Merge demand ==========
    def _prep(est):
        if est is None or est.empty:
            return pd.DataFrame(columns=["bidx", "heat_kwh", "peak_kw", "method"])
        e = est.rename(columns={"input_index": "bidx", "heat_demand_kWh": "heat_kwh"}).copy()
        e["bidx"] = e["bidx"].astype(str)
        e["peak_kw"] = pd.to_numeric(e.get("peak_load_kW"), errors="coerce")
        e["method"] = e.get("estimation_method", "legacy annual-demand estimate")
        return e[["bidx", "heat_kwh", "peak_kw", "method"]]

    efinal = _prep(estimates_df_final)
    eauto = _prep(estimates_df_original)

    if not eauto.empty:
        unified = unified.merge(eauto, on="bidx", how="left").rename(columns={
            "heat_kwh": "demand_kwh_auto",
            "peak_kw": "peak_kw_auto",
            "method": "heat_estimation_method",
        })
    else:
        unified["demand_kwh_auto"] = np.nan
        unified["peak_kw_auto"] = np.nan
        unified["heat_estimation_method"] = None

    if not efinal.empty:
        unified = unified.merge(
            efinal[["bidx", "heat_kwh", "peak_kw"]], on="bidx", how="left"
        ).rename(columns={"heat_kwh": "demand_kwh_final", "peak_kw": "peak_kw_final"})
    else:
        unified["demand_kwh_final"] = np.nan
        unified["peak_kw_final"] = np.nan

    unified["demand_edited"] = ~np.isclose(
        pd.to_numeric(unified["demand_kwh_auto"]),
        pd.to_numeric(unified["demand_kwh_final"]),
        equal_nan=True
    )
    unified["peak_edited"] = ~np.isclose(
        pd.to_numeric(unified["peak_kw_auto"]),
        pd.to_numeric(unified["peak_kw_final"]),
        equal_nan=True,
    )

    # ========== 5. stable ordering ==========
    # Handle "no buildings" or partially constructed datasets gracefully
    if unified.empty or "bidx" not in unified.columns or "source" not in unified.columns:
        # Nothing to sort, or missing keys – just return as-is
        return unified

    unified["bidx_num"] = pd.to_numeric(unified["bidx"], errors="coerce")
    unified = unified.sort_values(["bidx_num", "source"]).drop(columns=["bidx_num"])
    return unified

def _leaflet_to_lonlat_geojson(geom):
    def swap_xy(coord):
        return [coord[1], coord[0]]

    if not isinstance(geom, dict) or "type" not in geom: return geom
    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [[swap_xy(c) for c in ring] for ring in geom["coordinates"]]}
    if geom["type"] == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[[swap_xy(c) for c in ring] for ring in poly] for poly in geom["coordinates"]]}
    if geom["type"] == "LineString":
        return {"type": "LineString", "coordinates": [swap_xy(c) for c in geom["coordinates"]]}
    if geom["type"] == "Point":
        return {"type": "Point", "coordinates": swap_xy(geom["coordinates"])}
    if geom["type"] == "Feature":
        g = geom.get("geometry")
        return {"type": "Feature", "geometry": _leaflet_to_lonlat_geojson(g),
                "properties": geom.get("properties", {})} if g else geom
    if geom.get("type") == "FeatureCollection":
        return {
            "type": "FeatureCollection",
            "features": [_leaflet_to_lonlat_geojson(f) for f in geom.get("features", [])]
        }
    return geom

PROJECT_STATE_KEYS = [
    "drawn_polygons",
    "polygons",
    "selected_polygons",
    "polygons_geojson",
    "polygons_wkt",
    "buildings_gdf",
    "osm_buildings_gdf",
    "buildings_osm_gdf",
    "selected_buildings_gdf",
    "gdf_osm",
    "gdf_selected",
    "gdf_buildings",
    "osm_gdf",
    "bldg_gdf",
    "bidx_map",
    "next_bidx",
    "excluded_bidx",
    "excluded",
    "excluded_list",
    "building_attrs",
    "attrs_df",
    "building_info",
    "total_heat_demand",
    "building_demand_estimates",
    "building_demand_estimates_original",
    "total_dhw_demand",
    "dhw_demand_estimates",
    "dhw_load_profile",
    "info_overridden",
    "route_length",
    "manual_buildings",
    "manual_gdf",
    "manual_buildings_df",
    "_osm_preloaded",
    "_preloaded_buildings_gdf",
    "_last_buildings_gdf",
    "_buildings_utm",
    "_preloaded_drawn_polygons",
    "results_stale",
    "show_teaser_results",
    "show_dhw_results",
    "dhw_results_stale",
    "attr_demand_conflicts",
    "demand_conflict_policy",
    "demand_conflict_snapshot",
    "last_attr_changed_set",
    "attr_change_fingerprint",
    "estimates_original_df",
    "estimates_final_df",
    "pending_center",
    "pending_zoom",
    "_osm_base_attrs",
    "zensus_summary",
    "zensus_warnings",
    "selected_drawn_area_idx",
    "reset_confirm_inline",
    "_ignored_draw_fps",
    "_skip_next_draw_capture",
    "_draw_map_rev",
    "_draft_manual_bidx",
    "_manual_missing_area_pending",
    "_manual_missing_area_keep",
    "_manual_save_notice",
]

def reset_project_state():
    for k in PROJECT_STATE_KEYS:
        st.session_state.pop(k, None)

def run_building_heat_demand_page():
    ss = st.session_state
    if ss.get("weather_data_message"):
        st.info(f"Weather data: {ss['weather_data_message']}")
    if ss.get("weather_data_warning"):
        st.warning(
            "DWD weather could not be loaded; weather-dependent calculations will use available fallbacks. "
            f"Details: {ss['weather_data_warning']}"
        )
    if "_osm_preloaded" not in ss:
        ss["_osm_preloaded"] = False
    if "_last_project" not in ss:
        ss["_last_project"] = None

    curr_project = ss.get("current_project")
    if curr_project != ss["_last_project"]:
        reset_project_state()
        ss["_last_project"] = curr_project
        ss["_osm_preloaded"] = False

    if curr_project and not ss.get("_osm_preloaded", False):
        persisted = load_project_dataset_v2(curr_project)
        _rehydrate_project_state(persisted)

        if not _has_useful(persisted):
            v2 = load_osm_selection_from_excel_v2(curr_project)
            _rehydrate_project_state({
                "polygons_wkt": v2.get("drawn_polygons", []),
                "buildings_gdf": v2.get("buildings_gdf"),
                "excluded_bidx": v2.get("excluded_bidx", set()),
                "manual_buildings_df": v2.get(
                    "manual_buildings",
                    pd.DataFrame(columns=[
                        "bidx", "name", "building type", "area (m²)", "year built", "retrofit situation",
                        "building type source", "year source", "zensus age class", "zensus type class",
                        "zensus type note", "zensus year note", "zensus source",
                    ])
                ),
                "building_attrs_df": v2.get(
                    "building_attrs",
                    pd.DataFrame(columns=["bidx", "building type", "area (m²)", "address", "year built", "retrofit situation"])
                ),
                "estimates_df_final": v2.get(
                    "estimates_df_final",
                    pd.DataFrame(columns=["input_index", "heat_demand_kWh"])
                ),
                "estimates_df_original": v2.get(
                    "estimates_df_original",
                    pd.DataFrame(columns=["input_index", "heat_demand_kWh"])
                ),
                "total_heat_demand_kwh": v2.get("total_heat_demand_kwh"),
                "route_length_m": v2.get("route_length_m"),
                "zensus_summary_df": v2.get("zensus_summary_df", pd.DataFrame()),
            })
        # mark as done so we don't reload every rerun
        ss["_osm_preloaded"] = True
        st.rerun()
        return
        # -------------------- session-scoped ids & selections --------------------
    if "bidx_map" not in st.session_state:
        st.session_state["bidx_map"] = {}
    if "next_bidx" not in st.session_state:
        st.session_state["next_bidx"] = 0
    if "manual_buildings" not in st.session_state:
        st.session_state["manual_buildings"] = pd.DataFrame(
            columns=[
                "bidx", "name", "building type", "area (m²)", "year built", "retrofit situation",
                "building type source", "year source", "zensus age class", "zensus type class",
                "zensus type note", "zensus year note", "zensus source",
            ]
        )

    st.session_state.setdefault("hl_show", False)
    st.session_state.setdefault("drawn_polygons", [])
    st.session_state.setdefault("excluded_bidx", set())
    st.session_state.setdefault(
        "building_attrs",
        pd.DataFrame(columns=["bidx", "building type", "area (m²)", "address", "year built", "retrofit situation"])
    )
    st.session_state.setdefault("_osm_preloaded", False)
    st.session_state.setdefault("map_center", [52.52, 13.405])  # Berlin fallback
    st.session_state.setdefault("map_zoom", 14)
    if "pending_center" in st.session_state and "pending_zoom" in st.session_state:
        st.session_state["map_center"] = st.session_state.pop("pending_center")
        st.session_state["map_zoom"] = st.session_state.pop("pending_zoom")

    st.session_state.setdefault("edit_heat_toggle", False)
    if st.session_state.get("_reset_edit_heat_toggle", False):
        st.session_state["_reset_edit_heat_toggle"] = False
        st.session_state["edit_heat_toggle"] = False
    st.session_state.setdefault("demand_conflict_policy", {})
    st.session_state.setdefault("_persist_dirty", False)
    st.session_state.setdefault("show_teaser_results", False)
    st.session_state.setdefault("show_dhw_results", False)
    st.session_state.setdefault("dhw_results_stale", False)

    ss = st.session_state

    # 1) polygons: accept legacy "polygons" and map to "drawn_polygons"
    if not ss.get("drawn_polygons") and ss.get("polygons"):
        try:
            ss["drawn_polygons"] = list(ss["polygons"])
        except Exception:
            pass  # keep defaults

    # 2) buildings_gdf vs osm_buildings_gdf
    if ("buildings_gdf" not in ss or getattr(ss.get("buildings_gdf"), "empty", True)) and \
            ("osm_buildings_gdf" in ss and getattr(ss["osm_buildings_gdf"], "empty", True) is False):
        ss["buildings_gdf"] = ss["osm_buildings_gdf"]

    def _ensure_osm_base_snapshot():
        gdf = st.session_state.get("buildings_gdf")
        if gdf is None or getattr(gdf, "empty", True):
            return

        base_key = "_osm_base_attrs"
        base_snapshot = st.session_state.get(base_key)
        g = gdf.copy()
        g["bidx"] = g.get("bidx", g.index.astype(str)).astype(str)

        if base_snapshot is None or base_snapshot.empty:
            # First snapshot: freeze OSM attributes *as they are now*
            n = len(g)
            base_snapshot = pd.DataFrame({
                "bidx": g["bidx"].astype(str),
                "building_type_base": g.get("building", pd.Series([None] * n)).map(norm_btype),
                "area_base_m2": pd.to_numeric(g.get("area_m2", pd.Series([np.nan] * n)), errors="coerce"),
                "year_built_base": pd.to_numeric(g.get("year_built", pd.Series([np.nan] * n)), errors="coerce"),
                "address": g.get("address", pd.Series([None] * n)),
                "osm_id": g.get("osm_id", pd.Series([None] * n)),
                "osmid": g.get("osmid", pd.Series([None] * n)),
            })
        else:
            base_snapshot = base_snapshot.copy()
            base_snapshot["bidx"] = base_snapshot["bidx"].astype(str)
            seen = set(base_snapshot["bidx"])
            new = g[~g["bidx"].isin(seen)]
            if not new.empty:
                extra = pd.DataFrame({
                    "bidx": new["bidx"].astype(str),
                    "building_type_base": new.get("building", pd.Series([None] * len(new))).map(norm_btype),
                    "area_base_m2": pd.to_numeric(new.get("area_m2", None), errors="coerce"),
                    "year_built_base": pd.to_numeric(new.get("year_built", pd.Series([np.nan] * len(new))), errors="coerce"),
                    "address": new.get("address", pd.Series([None] * len(new))),
                    "osm_id": new.get("osm_id", None),
                    "osmid": new.get("osmid", None),
                })
                base_snapshot = pd.concat([base_snapshot, extra], ignore_index=True)

        st.session_state[base_key] = base_snapshot

    _ensure_osm_base_snapshot()

    # 3) estimates (final) – accept multiple schemas
    if "estimates_final_df" in ss and "building_demand_estimates" not in ss:
        ef = ss["estimates_final_df"]
        if {"bidx", "heat_kwh"} <= set(ef.columns):
            ss["building_demand_estimates"] = ef.rename(columns={"bidx": "input_index", "heat_kwh": "heat_demand_kWh"})
        else:
            ss["building_demand_estimates"] = ef.copy()

    if "estimates_original_df" in ss and "building_demand_estimates_original" not in ss:
        eo = ss["estimates_original_df"]
        if {"bidx", "heat_kwh"} <= set(eo.columns):
            ss["building_demand_estimates_original"] = eo.rename(
                columns={"bidx": "input_index", "heat_kwh": "heat_demand_kWh"})
        else:
            ss["building_demand_estimates_original"] = eo.copy()

    # 4) totals / route length (normalize names)
    if "total_heat_demand_kwh" in ss and "total_heat_demand" not in ss:
        ss["total_heat_demand"] = ss["total_heat_demand_kwh"]
    if "route_length_m" in ss and "route_length" not in ss:
        ss["route_length"] = ss["route_length_m"]

    _REPLACE_MISSING = {"": None, "unknown": None, "none": None, "nan": None, "NaN": None}

    def _clean_addr(v):
        if v is None or pd.isna(v):
            return None
        s = str(v).strip()
        return s if s else None

    def _canon_addr_lower(v):
        v = _clean_addr(v)
        return v.lower() if isinstance(v, str) else v

    def _extract_numeric_height(v):
        try:
            return float(str(v).strip().replace(" m", "").replace("~", ""))
        except Exception:
            return None

    @st.cache_data(show_spinner=False)
    def _fetch_osm_buildings(union_wkt: str):
        geom = wkt.loads(union_wkt)
        if geom.geom_type == "Polygon":
            return ox.geometries_from_polygon(geom, tags={"building": True})
        if geom.geom_type == "MultiPolygon":
            frames = []
            for poly in geom.geoms:
                try:
                    frames.append(ox.geometries_from_polygon(poly, tags={"building": True}))
                except Exception:
                    pass
            return pd.concat(frames, ignore_index=False) if frames else gpd.GeoDataFrame()
        # Fallback: try convex hull
        return ox.geometries_from_polygon(geom.convex_hull, tags={"building": True})

    # --- status helpers (safe, self-contained) ---
    def _get_status_sets():
        excluded = set(map(str, st.session_state.get("excluded_bidx", set())))
        attrs = st.session_state.get("building_attrs", pd.DataFrame())
        edited = (
            set(attrs["bidx"].astype(str))
            if isinstance(attrs, pd.DataFrame) and not attrs.empty and "bidx" in attrs.columns
            else set()
        )
        return excluded, edited

    def add_status_col(df, bidx_col="bidx", extra_labels: dict[str, set[str]] | None = None):
        out = df.copy()
        out["status"] = ""
        excluded = set(map(str, st.session_state.get("excluded_bidx", set())))
        attrs = st.session_state.get("building_attrs", pd.DataFrame())

        edited_type = set()
        edited_area = set()
        edited_addr = set()
        edited_year = set()
        edited_retrofit = set()

        if isinstance(attrs, pd.DataFrame) and not attrs.empty and "bidx" in attrs.columns:
            a = attrs.copy()
            a["bidx"] = a["bidx"].astype(str)

            if "building type" in a.columns:
                edited_type = set(a.dropna(subset=["building type"])["bidx"].astype(str))
            if "area (m²)" in a.columns:
                edited_area = set(
                    a.loc[~pd.to_numeric(a["area (m²)"], errors="coerce").isna(), "bidx"].astype(str)
                )
            if "address" in a.columns:
                edited_addr = set(
                    a.loc[a["address"].notna() & (a["address"].astype(str).str.strip() != ""), "bidx"].astype(str)
                )
            if "year built" in a.columns:
                edited_year = set(
                    a.loc[~pd.to_numeric(a["year built"], errors="coerce").isna(), "bidx"].astype(str)
                )
            if "retrofit situation" in a.columns:
                retrofit_values = a["retrofit situation"].apply(_normalize_retrofit_situation)
                edited_retrofit = set(a.loc[retrofit_values.notna(), "bidx"].astype(str))

        extra_labels = extra_labels or {}
        have_any = bool(
            excluded
            or edited_type
            or edited_area
            or edited_addr
            or edited_year
            or edited_retrofit
            or any(bool(s) for s in extra_labels.values())
        )
        if not have_any:
            return out, False

        # excluded
        if excluded:
            m = out[bidx_col].astype(str).isin(excluded)
            out.loc[m & (out["status"] == ""), "status"] = "excluded"
            out.loc[m & (out["status"] != ""), "status"] = out["status"] + "; " + "excluded"
        if edited_type:
            m = out[bidx_col].astype(str).isin(edited_type)
            out.loc[m & (out["status"] == ""), "status"] = "edited (type)"
            out.loc[m & (out["status"] != ""), "status"] = out["status"] + "; edited (type)"
        if edited_area:
            m = out[bidx_col].astype(str).isin(edited_area)
            out.loc[m & (out["status"] == ""), "status"] = "edited (area)"
            out.loc[m & (out["status"] != ""), "status"] = out["status"] + "; edited (area)"
        if edited_addr:
            m = out[bidx_col].astype(str).isin(edited_addr)
            out.loc[m & (out["status"] == ""), "status"] = "edited (address)"
            out.loc[m & (out["status"] != ""), "status"] = out["status"] + "; edited (address)"
        if edited_year:
            m = out[bidx_col].astype(str).isin(edited_year)
            out.loc[m & (out["status"] == ""), "status"] = "edited (year)"
            out.loc[m & (out["status"] != ""), "status"] = out["status"] + "; edited (year)"
        if edited_retrofit:
            m = out[bidx_col].astype(str).isin(edited_retrofit)
            out.loc[m & (out["status"] == ""), "status"] = "edited (retrofit)"
            out.loc[m & (out["status"] != ""), "status"] = out["status"] + "; edited (retrofit)"

        # extra labels (e.g., edited (demand))
        for label, s in extra_labels.items():
            if not s:
                continue
            m = out[bidx_col].astype(str).isin(s)
            out.loc[m & (out["status"] == ""), "status"] = label
            out.loc[m & (out["status"] != ""), "status"] = out["status"] + "; " + label

        # dedup tokens
        def _dedup(tokens_str: str) -> str:
            if not tokens_str:
                return ""
            seen = set()
            ordered = []
            for tok in [t.strip() for t in tokens_str.split(";") if t.strip()]:
                if tok not in seen:
                    seen.add(tok)
                    ordered.append(tok)
            return "; ".join(ordered)

        out["status"] = out["status"].astype(str).map(_dedup)
        return out, True

    def status_note(prefix="The status column shows the"):
        excluded, edited = _get_status_sets()
        bits = []
        if excluded: bits.append("excluded")
        if edited:   bits.append("edited")
        return f"{prefix} {' and '.join(bits)} building(s)." if bits else ""

    def is_valid_xlsx(path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
            wb.close()
            return True
        except Exception:
            return False

    def atomic_write_excel(path: str, write_fn):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        dir_ = os.path.dirname(path) or "."
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, dir=dir_)
        tmp_name = tmp.name
        tmp.close()
        try:
            with pd.ExcelWriter(tmp_name, engine="openpyxl", mode="w") as w:
                write_fn(w)
            shutil.move(tmp_name, path)  # atomic replace
        except Exception:
            try:
                os.remove(tmp_name)
            except Exception:
                pass
            raise

    LOCK_TIMEOUT_SEC = 30

    def atomic_write_excel_locked(path: str, write_fn):
        lock = FileLock(path + ".lock")
        try:
            lock.acquire(timeout=LOCK_TIMEOUT_SEC)
            atomic_write_excel(path, write_fn)
        except Timeout:
            raise RuntimeError(f"Could not acquire lock to write {path} within {LOCK_TIMEOUT_SEC}s")
        finally:
            if lock.is_locked:
                lock.release()

    def _safe_replace_csv(df: pd.DataFrame | None, path: str):
        """Overwrite or remove a CSV so we don't keep stale data."""
        try:
            if df is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                df.to_csv(path, index=False)
        except Exception:
            pass

    def save_project_dataset_v2(
            project_name: str,
            polygons_wkt: list[str],
            buildings_gdf: gpd.GeoDataFrame | None,
            excluded_bidx: set[str],
            manual_buildings_df: pd.DataFrame,
            building_attrs_df: pd.DataFrame,
            estimates_df_final: pd.DataFrame | None,
            estimates_df_original: pd.DataFrame | None,
            total_heat_demand_kwh: float | None,
            route_length_m: float | None,
            dhw_estimates_df: pd.DataFrame | None = None,
            dhw_profile_df: pd.DataFrame | None = None,
            total_dhw_demand_kwh: float | None = None,
    ):
        if not project_name:
            return
        building_attrs_df = _sanitize_building_attrs_df(building_attrs_df)
        dhw_estimates_clean = _std_dhw_estimates_df(dhw_estimates_df)
        dhw_profile_clean = _std_dhw_profile_df(dhw_profile_df)
        if dhw_estimates_clean.empty:
            dhw_estimates_clean = None
        if dhw_profile_clean.empty:
            dhw_profile_clean = None
        path = get_project_data_file(project_name)
        prev_ds = None
        if os.path.exists(path) and is_valid_xlsx(path):
            try:
                prev_ds = pd.read_excel(path, sheet_name="buildings_dataset")
            except Exception:
                prev_ds = None

        dataset = _build_unified_dataset(
            buildings_gdf=buildings_gdf,
            manual_buildings_df=manual_buildings_df,
            excluded_bidx=excluded_bidx,
            # attrs_overrides_df=building_attrs_df,
            estimates_df_final=estimates_df_final,
            estimates_df_original=estimates_df_original,
            prev_dataset=prev_ds,
        )

        df_polys = pd.DataFrame({"id": range(len(polygons_wkt)), "wkt": polygons_wkt})
        df_meta = pd.DataFrame([
            # {"key": "version", "value": "v2"},
            {"key": "total_heat_demand_kwh", "value": total_heat_demand_kwh},
            {"key": "total_dhw_demand_kwh", "value": total_dhw_demand_kwh},
            {"key": "route_length_m", "value": route_length_m},
            {"key": "design_outdoor_temperature_c", "value": st.session_state.get("design_outdoor_temperature_c")},
            {"key": "ground_temperature_c", "value": st.session_state.get("ground_temperature_c")},
            {"key": "dwd_weather_station", "value": (st.session_state.get("dwd_weather") or {}).get("station_name")},
            {"key": "dwd_weather_station_id", "value": (st.session_state.get("dwd_weather") or {}).get("station_id")},
            {"key": "dwd_weather_year", "value": (st.session_state.get("dwd_weather") or {}).get("year")},
            {"key": "dwd_profile_years_used", "value": ", ".join(map(str, (st.session_state.get("dwd_weather") or {}).get("profile_years_used", [])))},
            {"key": "dwd_available_complete_year_count", "value": (st.session_state.get("dwd_weather") or {}).get("available_complete_year_count")},
        ])

        base_snapshot = st.session_state.get("_osm_base_attrs")
        zensus_summary = st.session_state.get("zensus_summary")

        def _write(w):
            dataset.to_excel(w, "buildings_dataset", index=False)
            df_polys.to_excel(w, "polygons", index=False)
            df_meta.to_excel(w, "meta", index=False)
            if isinstance(base_snapshot, pd.DataFrame) and not base_snapshot.empty:
                base_snapshot.to_excel(w, "base_attrs", index=False)
            if isinstance(zensus_summary, pd.DataFrame) and not zensus_summary.empty:
                zensus_summary.to_excel(w, "zensus_summary", index=False)
            if dhw_estimates_clean is not None:
                dhw_estimates_clean.to_excel(w, "dhw_estimates", index=False)
            if dhw_profile_clean is not None:
                dhw_profile_clean.to_excel(w, "dhw_profile", index=False)

        # same atomic + lock
        if os.path.exists(path) and not is_valid_xlsx(path):
            shutil.move(path, f"{path}.corrupt_{int(time.time())}")
        atomic_write_excel_locked(path, _write)

        def _safe_to_geojson(gdf, path):
            try:
                if (
                    gdf is None
                    or getattr(gdf, "empty", True)
                    or not hasattr(gdf, "geometry")
                    or gdf.geometry is None
                ):
                    if os.path.exists(path):
                        os.remove(path)
                    return

                out_gdf = gdf.copy()
                out_gdf = out_gdf.loc[out_gdf.geometry.notna()].copy()
                if out_gdf.empty:
                    if os.path.exists(path):
                        os.remove(path)
                    return

                if "bidx" not in out_gdf.columns:
                    out_gdf["bidx"] = out_gdf.index.astype(str)
                out_gdf["bidx"] = out_gdf["bidx"].astype(str)

                keep = [
                    c for c in [
                        "bidx", "name", "address", "building", "area_m2",
                        "building_type_source", "year_built", "year_built_source",
                        "retrofit_situation", "retrofit_situation_source", "retrofit_probability",
                        "zensus_age_class", "zensus_type_class", "zensus_source",
                        "zensus_type_note", "zensus_year_note",
                        "levels", "height_m", "osm_id", "osmid", "geometry"
                    ]
                    if c in out_gdf.columns
                ]
                out_gdf = out_gdf[keep].reset_index(drop=True)
                out_gdf.to_file(path, driver="GeoJSON")
            except Exception:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass

        persist_dir = os.path.join(get_project_dir(project_name), "persist")
        os.makedirs(persist_dir, exist_ok=True)

        # 1) polygons
        try:
            with open(os.path.join(persist_dir, "polygons.json"), "w", encoding="utf-8") as f:
                json.dump(polygons_wkt, f)  # you’re already passing geojson-ish WKT list; store as-is
        except Exception:
            pass

        # 2) buildings (GeoJSON cache; workbook remains the durable source)
        _safe_to_geojson(buildings_gdf, os.path.join(persist_dir, "buildings.geojson"))

        # 3) exclusions
        try:
            with open(os.path.join(persist_dir, "excluded.json"), "w", encoding="utf-8") as f:
                json.dump(sorted([str(x) for x in (excluded_bidx or [])]), f)
        except Exception:
            pass

        # 4) manual / overrides / estimates
        _safe_replace_csv(
            manual_buildings_df,
            os.path.join(persist_dir, "manual_buildings.csv"),
        )

        _safe_replace_csv(
            building_attrs_df,
            os.path.join(persist_dir, "building_attrs.csv"),
        )

        _safe_replace_csv(
            estimates_df_final,
            os.path.join(persist_dir, "estimates_final.csv"),
        )

        _safe_replace_csv(
            estimates_df_original,
            os.path.join(persist_dir, "estimates_original.csv"),
        )

        _safe_replace_csv(
            dhw_estimates_clean,
            os.path.join(persist_dir, "dhw_estimates.csv"),
        )

        _safe_replace_csv(
            dhw_profile_clean,
            os.path.join(persist_dir, "dhw_profile.csv"),
        )

        _safe_replace_csv(
            zensus_summary if isinstance(zensus_summary, pd.DataFrame) and not zensus_summary.empty else None,
            os.path.join(persist_dir, "zensus_summary.csv"),
        )

        # 5) small meta
        meta_out = {
            "total_heat_demand_kwh": float(total_heat_demand_kwh) if total_heat_demand_kwh is not None else None,
            "total_dhw_demand_kwh": float(total_dhw_demand_kwh) if total_dhw_demand_kwh is not None else None,
            "route_length_m": float(route_length_m) if route_length_m is not None else None,
        }
        with open(os.path.join(persist_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta_out, f, ensure_ascii=False, indent=2)

    def normalize_name(name):
        """Normalize building names for consistent use as keys."""
        if pd.isna(name) or name == '':
            return ''
        name = str(name)
        name = name.strip()
        name = re.sub(r'\s+', '', name)
        name = re.sub(r'[^\w]', '', name)
        name = unicodedata.normalize('NFKD', name)
        name = ''.join(c for c in name if not unicodedata.combining(c))
        return name.lower()

    def _is_osm_id_like(x: object) -> bool:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return False
        s = str(x).strip()
        return bool(re.fullmatch(r'(node|way|relation)/\d+', s))

    def _edited_demand_set(final=None, auto=None) -> set[str]:
        """Return set of bidx where demand was manually edited."""

        if final is None:
            final = st.session_state.get("building_demand_estimates")
        if auto is None:
            auto = st.session_state.get("building_demand_estimates_original")

        if final is None or auto is None:
            return set()
        if getattr(final, "empty", True) or getattr(auto, "empty", True):
            return set()

        f = final.copy()
        a = auto.copy()
        f["bidx"] = f["input_index"].astype(str)
        a["bidx"] = a["input_index"].astype(str)

        merged = (
            f[["bidx", "heat_demand_kWh"]].rename(columns={"heat_demand_kWh": "final"})
            .merge(
                a[["bidx", "heat_demand_kWh"]].rename(columns={"heat_demand_kWh": "auto"}),
                on="bidx",
                how="left",
            )
        )

        auto_v = pd.to_numeric(merged["auto"], errors="coerce")
        final_v = pd.to_numeric(merged["final"], errors="coerce")

        edited_mask = final_v.notna() & ~np.isclose(auto_v, final_v, equal_nan=False)

        return set(merged.loc[edited_mask, "bidx"].astype(str))

    def _edited_peak_set(final=None, auto=None) -> set[str]:
        """Return bidx values whose building peak was manually edited."""
        final = st.session_state.get("building_demand_estimates") if final is None else final
        auto = st.session_state.get("building_demand_estimates_original") if auto is None else auto
        if final is None or auto is None or getattr(final, "empty", True) or getattr(auto, "empty", True):
            return set()
        if "peak_load_kW" not in final.columns or "peak_load_kW" not in auto.columns:
            return set()
        f = final[["input_index", "peak_load_kW"]].copy()
        a = auto[["input_index", "peak_load_kW"]].copy()
        f["input_index"] = f["input_index"].astype(str)
        a["input_index"] = a["input_index"].astype(str)
        merged = f.merge(a, on="input_index", how="left", suffixes=("_final", "_auto"))
        final_v = pd.to_numeric(merged["peak_load_kW_final"], errors="coerce")
        auto_v = pd.to_numeric(merged["peak_load_kW_auto"], errors="coerce")
        changed = final_v.notna() & ~np.isclose(auto_v, final_v, equal_nan=False)
        return set(merged.loc[changed, "input_index"].astype(str))

    def _standardize_est(df):
        df = df.copy()
        # accept either schema
        if "bidx" in df.columns and "heat_kwh" in df.columns:
            df = df.rename(columns={"bidx": "input_index", "heat_kwh": "heat_demand_kWh"})
        if "input_index" not in df.columns or "heat_demand_kWh" not in df.columns:
            return pd.DataFrame(
                columns=["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]
            )
        df["input_index"] = df["input_index"].astype(str)
        if "peak_load_kW" not in df.columns:
            df["peak_load_kW"] = np.nan
        if "estimation_method" not in df.columns:
            df["estimation_method"] = "legacy annual-demand estimate"
        return df[["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]]

    def _merge_new_auto_into_final(
            new_auto_df: pd.DataFrame,
            prev_final_df: pd.DataFrame | None,
            keep: set[str] | None = None,
            keep_peak: set[str] | None = None,
    ) -> pd.DataFrame:
        """
        Keep previous final values for bidx in `keep`; otherwise take the new auto values.
        """
        new_auto = _standardize_est(new_auto_df)

        if prev_final_df is None or prev_final_df.empty or (not keep and not keep_peak):
            return new_auto

        prev_final = _standardize_est(prev_final_df)
        keep = set(keep or set())
        keep_peak = set(keep_peak or set())

        a = new_auto.set_index("input_index")
        f = prev_final.set_index("input_index")

        inter = [k for k in keep if k in a.index and k in f.index]
        if inter:
            a.loc[inter, "heat_demand_kWh"] = f.loc[inter, "heat_demand_kWh"]

        peak_inter = [k for k in keep_peak if k in a.index and k in f.index]
        if peak_inter:
            a.loc[peak_inter, "peak_load_kW"] = f.loc[peak_inter, "peak_load_kW"]

        a = a.reset_index()
        return a[["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]]

    def _ensure_info_overridden(force: bool = False):
        if not force and "info_overridden" in st.session_state and isinstance(st.session_state["info_overridden"], pd.DataFrame):
            if not st.session_state["info_overridden"].empty:
                return

        gsrc = st.session_state.get("buildings_gdf")
        if gsrc is None or getattr(gsrc, "empty", True):
            gsrc = st.session_state.get("_preloaded_buildings_gdf")
            st.session_state.setdefault("_preloaded_drawn_polygons", list(st.session_state.get("drawn_polygons", [])))

        info_osm = pd.DataFrame()
        if gsrc is not None and not getattr(gsrc, "empty", True):
            g = gsrc.copy()
            g["bidx"] = g.get("bidx", g.index.astype(str)).astype(str)
            info_osm = pd.DataFrame({
                "building index": g["bidx"].astype(str).map(lambda x: f"Building_{x}"),
                "address": g.get("address", pd.Series([None] * len(g))),
                "name": g.get("name", pd.Series([None] * len(g))),
                "building type": g.get("building", pd.Series([None] * len(g))),
                "building type source": g.get("building_type_source", pd.Series([None] * len(g))),
                "area (m²)": g.get("area_m2", pd.Series([np.nan] * len(g))),
                "levels": pd.to_numeric(
                    g.get("levels", pd.Series([np.nan] * len(g))), errors="coerce"
                ),
                "height (m)": pd.to_numeric(
                    g.get("height_m", pd.Series([np.nan] * len(g))), errors="coerce"
                ),
                "year built": pd.to_numeric(g.get("year_built", pd.Series([np.nan] * len(g))), errors="coerce"),
                "year source": g.get("year_built_source", pd.Series([None] * len(g))),
                "retrofit situation": g.get("retrofit_situation", pd.Series([None] * len(g))),
                "retrofit situation source": g.get("retrofit_situation_source", pd.Series([None] * len(g))),
                "renovation probability (%)": pd.to_numeric(
                    g.get("retrofit_probability", pd.Series([np.nan] * len(g))), errors="coerce"
                ) * 100.0,
                "zensus age class": g.get("zensus_age_class", pd.Series([None] * len(g))),
                "zensus type class": g.get("zensus_type_class", pd.Series([None] * len(g))),
                "zensus type note": g.get("zensus_type_note", pd.Series([None] * len(g))),
                "zensus year note": g.get("zensus_year_note", pd.Series([None] * len(g))),
                "zensus source": g.get("zensus_source", pd.Series([None] * len(g))),
                "bidx": g["bidx"].astype(str),
                "source": "osm",
            })

        # ---- APPLY OVERRIDES ONLY IF WE HAVE OSM ROWS WITH BIDX ----
        attrs = st.session_state.get("building_attrs", pd.DataFrame())
        if (
                isinstance(attrs, pd.DataFrame)
                and not attrs.empty
                and "bidx" in attrs.columns
                and not info_osm.empty
                and "bidx" in info_osm.columns
        ):
            a = attrs.copy()
            a["bidx"] = a["bidx"].astype(str)

            # building type override
            if "building type" in a.columns:
                t = a[["bidx", "building type"]].dropna(subset=["building type"]).copy()
                t["building type"] = t["building type"].map(norm_btype)
                info_osm = info_osm.merge(t, on="bidx", how="left", suffixes=("", "_ovr"))
                info_osm["building type"] = info_osm["building type_ovr"].combine_first(info_osm["building type"])
                info_osm["building type source"] = np.where(
                    info_osm["building type_ovr"].notna(),
                    "user",
                    info_osm.get("building type source"),
                )
                info_osm.drop(columns=["building type_ovr"], inplace=True, errors="ignore")

            # area override
            if "area (m²)" in a.columns:
                u = a[["bidx", "area (m²)"]].copy()
                u["area (m²)"] = pd.to_numeric(u["area (m²)"], errors="coerce")
                u = u.dropna(subset=["area (m²)"])
                if not u.empty:
                    info_osm = info_osm.merge(u, on="bidx", how="left", suffixes=("", "_ovr"))
                    info_osm["area (m²)"] = info_osm["area (m²)_ovr"].combine_first(info_osm["area (m²)"])
                    info_osm.drop(columns=["area (m²)_ovr"], inplace=True, errors="ignore")

            for geometry_column in ("levels", "height (m)"):
                if geometry_column in a.columns:
                    geometry_override = a[["bidx", geometry_column]].copy()
                    geometry_override[geometry_column] = pd.to_numeric(
                        geometry_override[geometry_column], errors="coerce"
                    )
                    geometry_override = geometry_override.dropna(subset=[geometry_column])
                    if not geometry_override.empty:
                        info_osm = info_osm.merge(
                            geometry_override, on="bidx", how="left", suffixes=("", "_ovr")
                        )
                        info_osm[geometry_column] = info_osm[f"{geometry_column}_ovr"].combine_first(
                            pd.to_numeric(info_osm[geometry_column], errors="coerce")
                        )
                        info_osm.drop(columns=[f"{geometry_column}_ovr"], inplace=True, errors="ignore")

            # address override
            if "address" in a.columns:
                v = a[["bidx", "address"]].copy()

                def _clean_addr(v_):
                    if v_ is None or pd.isna(v_):
                        return None
                    s = str(v_).strip()
                    return s if s else None

                v["address"] = v["address"].apply(_clean_addr)
                v = v.dropna(subset=["address"])
                if not v.empty:
                    info_osm = info_osm.merge(v, on="bidx", how="left", suffixes=("", "_ovr"))
                    info_osm["address"] = info_osm["address_ovr"].combine_first(info_osm["address"])
                    info_osm.drop(columns=["address_ovr"], inplace=True, errors="ignore")

            # year-built override
            if "year built" in a.columns:
                y = a[["bidx", "year built"]].copy()
                y["year built"] = pd.to_numeric(y["year built"], errors="coerce")
                y = y.dropna(subset=["year built"])
                if not y.empty:
                    info_osm = info_osm.merge(y, on="bidx", how="left", suffixes=("", "_ovr"))
                    info_osm["year built"] = pd.to_numeric(info_osm["year built_ovr"], errors="coerce").combine_first(
                        pd.to_numeric(info_osm["year built"], errors="coerce")
                    )
                    info_osm["year source"] = np.where(info_osm["year built_ovr"].notna(), "user", info_osm.get("year source"))
                    info_osm.drop(columns=["year built_ovr"], inplace=True, errors="ignore")

            # retrofit-situation override
            if "retrofit situation" in a.columns:
                r = a[["bidx", "retrofit situation"]].copy()
                r["retrofit situation"] = r["retrofit situation"].apply(_normalize_retrofit_situation)
                r = r.dropna(subset=["retrofit situation"])
                if not r.empty:
                    info_osm = info_osm.merge(r, on="bidx", how="left", suffixes=("", "_ovr"))
                    info_osm["retrofit situation"] = info_osm["retrofit situation_ovr"].combine_first(
                        info_osm["retrofit situation"]
                    )
                    info_osm["retrofit situation source"] = np.where(
                        info_osm["retrofit situation_ovr"].notna(),
                        "user",
                        info_osm.get("retrofit situation source"),
                    )
                    info_osm.drop(columns=["retrofit situation_ovr"], inplace=True, errors="ignore")

        if not info_osm.empty:
            auto_retrofit_mask = ~info_osm.get(
                "retrofit situation source", pd.Series([None] * len(info_osm), index=info_osm.index)
            ).astype(str).str.lower().eq("user")
            info_osm.loc[auto_retrofit_mask, "retrofit situation"] = None
            info_osm = _add_retrofit_defaults(info_osm)
            info_osm["retrofit situation source"] = info_osm["retrofit situation source"].where(
                info_osm["retrofit situation source"].astype(str).str.lower().eq("user"),
                "estimated",
            )

        # manual buildings as before
        info_manual = pd.DataFrame()
        mb = st.session_state.get("manual_buildings", pd.DataFrame())
        if isinstance(mb, pd.DataFrame) and not mb.empty:
            m = mb.copy()
            m["bidx"] = m["bidx"].astype(str)
            info_manual = pd.DataFrame({
                "building index": m["bidx"].map(lambda x: f"Building_{x}"),
                "address": None,
                "name": m.get("name", pd.Series([None] * len(m))),
                "building type": m.get("building type", pd.Series([None] * len(m))),
                "building type source": m.get("building type source", pd.Series(["manual"] * len(m))),
                "area (m²)": m.get("area (m²)", pd.Series([np.nan] * len(m))),
                "levels": pd.to_numeric(m.get("levels", pd.Series([np.nan] * len(m))), errors="coerce"),
                "height (m)": pd.to_numeric(
                    m.get("height (m)", pd.Series([np.nan] * len(m))), errors="coerce"
                ),
                "year built": pd.to_numeric(m.get("year built", pd.Series([np.nan] * len(m))), errors="coerce"),
                "year source": m.get(
                    "year source",
                    pd.Series(
                        np.where(
                            pd.to_numeric(m.get("year built", pd.Series([np.nan] * len(m))), errors="coerce").notna(),
                            "manual",
                            None,
                        )
                    ),
                ),
                "retrofit situation": m.get("retrofit situation", pd.Series([None] * len(m))),
                "retrofit situation source": "manual",
                "renovation probability (%)": np.nan,
                "zensus age class": m.get("zensus age class", pd.Series([None] * len(m))),
                "zensus type class": m.get("zensus type class", pd.Series([None] * len(m))),
                "zensus type note": m.get("zensus type note", pd.Series([None] * len(m))),
                "zensus year note": m.get("zensus year note", pd.Series([None] * len(m))),
                "zensus source": m.get("zensus source", pd.Series([None] * len(m))),
                "bidx": m["bidx"].astype(str),
                "source": "manual",
            })

        frames = []
        if not info_osm.empty:
            frames.append(info_osm)
        if not info_manual.empty:
            frames.append(_add_retrofit_defaults(info_manual))

        if frames:
            st.session_state["info_overridden"] = pd.concat(frames, ignore_index=True)

    ########################################################################################################################

    DEFAULT_LEVELS = 3
    DEFAULT_HEIGHT = 3

    FLOOR_HEIGHT_BY_TYPE = {
        'residential': 2.8, 'house': 2.8, 'bungalow': 2.8, 'detached': 2.8,
        'apartments': 3.0, 'office': 3.2, 'school': 3.5, 'university': 3.5,
        'kindergarten': 3.2, 'college': 3.5, 'church': 6.0, 'semidetached': 2.8,
        # 'warehouse': 5.0, 'industrial': 4.5,
        'religious': 6.0, 'hospital': 3.2, 'retail': 3.2, 'commercial': 3.2,
        'supermarket': 4.0, 'stadium': 8.0, 'government': 3.5,
    }

    DEFAULT_LEVELS_BY_TYPE = {
        'house': 2, 'bungalow': 1, 'detached': 2, 'residential': 3,
        'apartments': 4, 'semidetached_house': 2, 'terrace': 2, 'dormitory': 3,
        'school': 2, 'university': 3, 'college': 3, 'kindergarten': 2,
        'hospital': 5, 'office': 3, 'government': 4, 'commercial': 2,
        'retail': 2, 'supermarket': 1, 'semidetached': 2,
        # 'warehouse': 1, 'industrial': 2,
        'church': 1, 'religious': 1, 'stadium': 2, 'static_caravan': 1,
    }

    NET_AREA_FACTORS = {
        'residential': 0.75, 'house': 0.70, 'bungalow': 0.70, 'detached': 0.70,
        'semidetached_house': 0.70, 'terrace': 0.70, 'apartments': 0.75, 'dormitory': 0.75,
        'office': 0.65, 'school': 0.55, 'university': 0.55, 'college': 0.55, 'kindergarten': 0.55,
        'retail': 0.60, 'supermarket': 0.65, 'commercial': 0.60, 'hospital': 0.65,
        # 'warehouse': 0.40, 'industrial': 0.45,
        'government': 0.55, 'religious': 0.50, 'stadium': 0.30, 'semidetached': 0.70,
    }

    FULL_LOAD_HOURS_BY_TYPE = {
        'residential': 1000, 'house': 1000, 'detached': 1000, 'semidetached_house': 1000,
        'bungalow': 1000, 'static_caravan': 800, 'apartments': 900, 'terrace': 1000, 'dormitory': 900,
        'school': 600, 'university': 600, 'college': 600, 'kindergarten': 900, 'hospital': 1200,
        'care_home': 1200, 'office': 900, 'government': 900, 'commercial': 900, 'retail': 800,
        'supermarket': 600, 'religious': 600, 'church': 600, 'stadium': 400, 'semidetached': 1000,
        # 'warehouse': 500, 'industrial': 600,
    }
    DEFAULT_FULL_LOAD_HOURS = 550

    RESIDENTIAL_TYPES = {
        'house': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'detached': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'semidetached': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'semidetached_house': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'bungalow': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'static_caravan': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'stilt_house': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'tree_house': ('tabula_de_standard', 'tabula_de_single_family_house'),
        'residential': ('tabula_de_standard', 'tabula_de_multi_family_house'),
        'apartments': ('tabula_de_standard', 'tabula_de_multi_family_house'),
        'terrace': ('tabula_de_standard', 'tabula_de_multi_family_house'),
        'dormitory': ('tabula_de_standard', 'tabula_de_multi_family_house'),
    }

    NON_RESIDENTIAL_TYPES = {
        'school': ('iwu_heavy', 'bmvbs_institute'), 'university': ('iwu_heavy', 'bmvbs_institute4'),
        'college': ('iwu_heavy', 'bmvbs_institute4'), 'kindergarten': ('iwu_heavy', 'bmvbs_institute'),
        'hospital': ('iwu_heavy', 'bmvbs_institute8'), 'care_home': ('iwu_heavy', 'bmvbs_institute8'),
        'office': ('iwu_heavy', 'bmvbs_office'), 'government': ('iwu_heavy', 'bmvbs_office'),
        'commercial': ('iwu_heavy', 'bmvbs_office'), 'retail': ('iwu_heavy', 'bmvbs_office'),
        'supermarket': ('iwu_heavy', 'bmvbs_office'),
        # 'warehouse': ('iwu_heavy', 'bmvbs_institute'), 'industrial': ('iwu_heavy', 'bmvbs_institute'),
        'civic': ('iwu_heavy', 'bmvbs_institute'), 'fire_station': ('iwu_heavy', 'bmvbs_institute'),
        'police': ('iwu_heavy', 'bmvbs_institute'), 'train_station': ('iwu_heavy', 'bmvbs_institute'),
        'museum': ('iwu_heavy', 'bmvbs_institute'), 'public': ('iwu_heavy', 'bmvbs_institute'),
        'religious': ('iwu_heavy', 'bmvbs_institute'), 'cathedral': ('iwu_heavy', 'bmvbs_institute'),
        'church': ('iwu_heavy', 'bmvbs_institute'), 'mosque': ('iwu_heavy', 'bmvbs_institute'),
        'temple': ('iwu_heavy', 'bmvbs_institute'), 'stadium': ('iwu_heavy', 'bmvbs_institute'),
        'sports_centre': ('iwu_heavy', 'bmvbs_institute'), 'sports_hall': ('iwu_heavy', 'bmvbs_institute'),
    }

    ZERO_DEMAND_TYPES = {
        'garage', 'garages', 'carport', 'shed', 'storage', 'warehouse', 'greenhouse', 'barn', 'hangar',
        'industrial', 'utility', 'kiosk', 'containers', 'container', 'parking', 'parking_garage',
        'farm_auxiliary', 'conservatory', 'cowshed', 'stable', 'sty', 'allotment_house', 'boathouse',
        'windmill', 'bunker', 'roof', 'construction', 'outbuilding', 'service', 'tech_cab',
        'transformer_tower', 'ruins', 'demolished', 'abandoned', 'entrance', 'gatehouse', 'watchtower',
        'silo', 'water_tower', 'pump_house', 'substation', 'chimney', 'power_plant',
    }

    def _building_geometry_values(building_tag, raw_levels=None, raw_height=None):
        """Return validated levels, total height and TEASER storey height.

        OSM zero/negative values are treated as missing. A measured total
        height is used only when its implied storey height is plausible;
        otherwise the building-type default replaces it.
        """
        tag = norm_btype(building_tag) or "house"
        category = "non_residential" if tag in NON_RESIDENTIAL_TYPES else "residential"
        default_floor_height = float(
            FLOOR_HEIGHT_BY_TYPE.get(tag, FLOOR_HEIGHT_BY_TYPE.get(category, DEFAULT_HEIGHT))
        )
        minimum_floor_height = 2.8 if category == "non_residential" else 2.5
        default_levels = int(DEFAULT_LEVELS_BY_TYPE.get(tag, DEFAULT_LEVELS))

        levels_num = pd.to_numeric(raw_levels, errors="coerce")
        height_num = pd.to_numeric(raw_height, errors="coerce")
        levels_valid = pd.notna(levels_num) and float(levels_num) >= 1.0
        height_valid = pd.notna(height_num) and float(height_num) > 0.0

        if levels_valid:
            levels = max(int(round(float(levels_num))), 1)
        elif height_valid:
            levels = max(int(round(float(height_num) / default_floor_height)), 1)
        else:
            levels = max(default_levels, 1)

        implied_floor_height = float(height_num) / levels if height_valid else np.nan
        if height_valid and implied_floor_height >= minimum_floor_height:
            floor_height = implied_floor_height
            total_height = float(height_num)
        else:
            floor_height = default_floor_height
            total_height = levels * floor_height
        return levels, float(total_height), float(floor_height)

    def _stable_unit_interval(*parts):
        key = "|".join("" if p is None else str(p) for p in parts)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return int(digest[:12], 16) / float(0xFFFFFFFFFFFF)

    def _retrofit_probability_for_building(building_tag, year_built):
        tag = norm_btype(building_tag) or "house"
        if tag in ZERO_DEMAND_TYPES:
            return 0.0

        try:
            year = int(float(year_built))
        except Exception:
            year = 1978

        if year <= 1918:
            prob = 0.64
        elif year <= 1948:
            prob = 0.60
        elif year <= 1978:
            prob = 0.54
        elif year <= 1994:
            prob = 0.36
        elif year <= 2009:
            prob = 0.20
        elif year <= 2015:
            prob = 0.08
        else:
            prob = 0.03

        if tag in RESIDENTIAL_TYPES:
            if tag in {"apartments", "residential", "dormitory"}:
                prob += 0.05
            elif tag in {"house", "detached", "bungalow", "semidetached", "semidetached_house"}:
                prob -= 0.03
        elif tag in NON_RESIDENTIAL_TYPES:
            prob -= 0.06
            if tag in {"school", "university", "college", "kindergarten", "hospital"}:
                prob += 0.05
        else:
            prob -= 0.08

        return float(np.clip(prob, 0.0, 0.85))

    def _advanced_retrofit_probability(building_tag, year_built, retrofit_probability):
        tag = norm_btype(building_tag) or "house"
        try:
            year = int(float(year_built))
        except Exception:
            year = 1978
        share = 0.18 if year <= 1978 else 0.12 if year <= 1994 else 0.06
        if tag in NON_RESIDENTIAL_TYPES:
            share *= 0.75
        return float(np.clip(retrofit_probability * share, 0.0, retrofit_probability))

    def _estimate_retrofit_situation(building_tag, year_built, bidx=None):
        probability = _retrofit_probability_for_building(building_tag, year_built)
        advanced_probability = _advanced_retrofit_probability(building_tag, year_built, probability)
        u = _stable_unit_interval("retrofit", bidx, norm_btype(building_tag), year_built)
        if u < advanced_probability:
            situation = "advanced retrofit"
        elif u < probability:
            situation = "retrofit"
        else:
            situation = "standard"
        return situation, probability

    def _add_retrofit_defaults(
            df,
            type_col="building type",
            year_col="year built",
            bidx_col="bidx",
            situation_col="retrofit situation",
            probability_col="renovation probability (%)",
    ):
        if df is None or getattr(df, "empty", True):
            return df
        out = df.copy()
        if situation_col not in out.columns:
            out[situation_col] = None
        if probability_col not in out.columns:
            out[probability_col] = np.nan

        for idx, row in out.iterrows():
            bidx = row.get(bidx_col)
            building_tag = row.get(type_col)
            year_built = row.get(year_col)
            estimated_situation, probability = _estimate_retrofit_situation(building_tag, year_built, bidx)
            current = _normalize_retrofit_situation(row.get(situation_col))
            out.at[idx, situation_col] = current or estimated_situation
            out.at[idx, probability_col] = round(probability * 100.0, 1)
        return out

    def _retrofit_inputs_for_teaser(df, building_col="building", year_col="year_built"):
        out = df.copy()
        if "retrofit_situation" not in out.columns:
            out["retrofit_situation"] = None
        if "retrofit_probability" not in out.columns:
            out["retrofit_probability"] = np.nan
        for idx, row in out.iterrows():
            situation, probability = _estimate_retrofit_situation(
                row.get(building_col), row.get(year_col), row.get("bidx")
            )
            current = _normalize_retrofit_situation(row.get("retrofit_situation"))
            out.at[idx, "retrofit_situation"] = current or situation
            out.at[idx, "retrofit_probability"] = probability
        return out

    def map_osm_to_teaser(building_tag, retrofit_situation=None):
        building_tag = (building_tag or "").lower()
        retrofit_situation = _normalize_retrofit_situation(retrofit_situation) or RETROFIT_SITUATION_DEFAULT
        if building_tag in RESIDENTIAL_TYPES:
            construction_data, geometry_data = RESIDENTIAL_TYPES[building_tag]
            construction_data = RETROFIT_CONSTRUCTION_DATA.get(retrofit_situation, construction_data)
            category = 'residential'
        elif building_tag in NON_RESIDENTIAL_TYPES:
            construction_data, geometry_data = NON_RESIDENTIAL_TYPES[building_tag]
            category = 'non_residential'
        else:
            construction_data = RETROFIT_CONSTRUCTION_DATA.get(retrofit_situation, 'tabula_de_standard')
            geometry_data = 'tabula_de_single_family_house'
            category = 'residential'
        return construction_data, geometry_data, category

    def _repeat_schedule(values, hours):
        values = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0.0).to_numpy(float)
        if values.size == 0:
            return np.zeros(hours, dtype=float)
        return np.resize(values, hours)

    def _coarse_useful_gains_kwh(bldg, outside_c, gross_demand_kwh, category):
        """Return conservative useful internal and solar gains.

        Internal gains use TEASER's archetype powers and schedules. Solar
        gains use TEASER window geometry/g-values and deliberately coarse
        German heating-season irradiation by orientation. The combined gain
        credit is capped because this lightweight calculation does not model
        thermal storage, room temperatures or detailed shading.
        """
        outside_c = np.asarray(outside_c, dtype=float)
        hours = int(outside_c.size)
        if hours == 0 or gross_demand_kwh <= 0:
            return 0.0, 0.0

        internal_wh = 0.0
        solar_kwh = 0.0
        # Coarse incident irradiation on vertical glazing during the heating
        # season. Cardinal values are linearly interpolated for other azimuths.
        cardinal_irradiation = np.asarray([180.0, 300.0, 450.0, 300.0, 180.0])
        cardinal_azimuth = np.asarray([0.0, 90.0, 180.0, 270.0, 360.0])

        def finite_float(value, default=0.0):
            try:
                result = float(value)
                return result if np.isfinite(result) else float(default)
            except (TypeError, ValueError):
                return float(default)

        for zone in getattr(bldg, "thermal_zones", []):
            area = finite_float(getattr(zone, "area", 0.0))
            use = getattr(zone, "use_conditions", None)
            if use is not None and area > 0:
                schedules = getattr(use, "schedules", None)

                def schedule(name):
                    if isinstance(schedules, pd.DataFrame) and name in schedules.columns:
                        return _repeat_schedule(schedules[name], hours)
                    return _repeat_schedule(getattr(use, name, []), hours)

                persons = finite_float(getattr(use, "persons", 0.0))
                person_w = finite_float(getattr(use, "fixed_heat_flow_rate_persons", 70.0), 70.0)
                machines_w_m2 = finite_float(getattr(use, "machines", 0.0))
                lighting_w_m2 = finite_float(getattr(use, "lighting_power", 0.0))
                hourly_internal_w = area * (
                    persons * person_w * schedule("persons_profile")
                    + machines_w_m2 * schedule("machines_profile")
                    + lighting_w_m2 * schedule("lighting_profile")
                )
                threshold_k = finite_float(getattr(use, "T_threshold_heating", 288.15), 288.15)
                heating_hours = outside_c < (threshold_k - 273.15)
                internal_wh += float(np.nansum(hourly_internal_w[heating_hours]))

            for window in getattr(zone, "windows", []):
                window_area = finite_float(getattr(window, "area", 0.0))
                g_value = finite_float(getattr(window, "g_value", 0.0))
                shading = finite_float(getattr(window, "shading_g_total", 1.0), 1.0)
                raw_orientation = getattr(window, "orientation", 180.0)
                orientation = finite_float(raw_orientation, 180.0) % 360.0
                irradiation = float(np.interp(orientation, cardinal_azimuth, cardinal_irradiation))
                solar_kwh += window_area * irradiation * np.clip(g_value, 0.0, 1.0) * np.clip(shading, 0.0, 1.0)

        # Only part of the gains is useful for reducing space heating because
        # timing and thermal mass are not explicitly simulated.
        internal_kwh = internal_wh / 1000.0 * 0.70
        solar_kwh *= 0.45
        maximum_share = 0.30 if category == "residential" else 0.40
        maximum_credit = float(gross_demand_kwh) * maximum_share
        total = internal_kwh + solar_kwh
        if total > maximum_credit > 0:
            scale = maximum_credit / total
            internal_kwh *= scale
            solar_kwh *= scale
        return max(internal_kwh, 0.0), max(solar_kwh, 0.0)

    def estimate_heat_demand_for_buildings(buildings_df):
        """
        buildings_df must contain 'bidx', 'area_m2' and 'building', and may
        contain 'levels', 'height_m', 'name' and 'year_built'.
        We always use bidx as the stable ID for heat demand.
        """
        if buildings_df is None or buildings_df.empty:
            return 0.0, pd.DataFrame(
                columns=["input_index", "heat_demand_kWh", "peak_load_kW", "estimation_method"]
            )

        df = buildings_df.copy()

        # Ensure we have a stable ID column: bidx
        if "bidx" in df.columns:
            df["bidx"] = df["bidx"].astype(str)
        else:
            # Fall back once for very old projects, but then bidx is frozen
            df["bidx"] = df.index.astype(str)

        df = df.set_index("bidx", drop=False)

        project = Project()
        total_heat_demand = 0.0
        estimates = []

        weather = None
        coords = st.session_state.get("project_coords")
        if isinstance(coords, (list, tuple)) and len(coords) == 2:
            try:
                weather = get_location_weather(float(coords[0]), float(coords[1]))
                st.session_state["dwd_weather"] = weather
                st.session_state["design_outdoor_temperature_c"] = weather[
                    "design_outdoor_temperature_c"
                ]
                st.session_state["ground_temperature_c"] = weather["ground_temperature_c"]
                st.session_state["weather_data_message"] = (
                    f"DWD station {weather['station_name']} ({weather['station_id']}), "
                    f"{weather['station_distance_km']:.1f} km away; "
                    f"{weather['available_complete_year_count']} complete years available, "
                    f"using {weather['profile_year_count']} years "
                    f"({', '.join(map(str, weather['profile_years_used']))}) with the chronology of "
                    f"{weather['year']}."
                )
                st.session_state.pop("weather_data_warning", None)
            except Exception as exc:
                st.session_state["weather_data_warning"] = str(exc)
                weather = None

        building_category_by_name = {}
        building_retrofit_by_name = {}

        def get_id_from_row(row):
            # Always use bidx as ID
            return str(row["bidx"])

        # iterate in any order – IDs are explicit
        for bidx, row in df.iterrows():
            area = row.get("area_m2", None)
            if pd.isna(area) or area is None or float(area) <= 0:
                estimates.append({
                    "input_index": str(bidx), "heat_demand_kWh": np.nan,
                    "peak_load_kW": np.nan, "estimation_method": "not calculated",
                })
                continue

            raw_btype = row.get("building", "house")
            building_tag = norm_btype(raw_btype) or "house"
            building_tag = building_tag.lower()

            if building_tag in ZERO_DEMAND_TYPES:
                estimates.append({
                    "input_index": str(bidx), "heat_demand_kWh": 0.0,
                    "peak_load_kW": 0.0, "estimation_method": "unheated building type",
                })
                continue

            construction_data, geometry_data, category = map_osm_to_teaser(building_tag)

            levels, effective_height, floor_height = _building_geometry_values(
                building_tag,
                row.get("levels"),
                row.get("height_m", row.get("height")),
            )

            year_built = row.get("year_built", 2005)
            try:
                year_built = int(year_built)
                if year_built < 1900 or year_built > 2030:
                    year_built = 2005
            except Exception:
                year_built = 2005

            retrofit_situation = _normalize_retrofit_situation(row.get("retrofit_situation"))
            if retrofit_situation is None:
                retrofit_situation, _ = _estimate_retrofit_situation(building_tag, year_built, bidx)
            construction_data, geometry_data, category = map_osm_to_teaser(building_tag, retrofit_situation)

            net_factor = NET_AREA_FACTORS.get(building_tag, NET_AREA_FACTORS.get(category, 0.5))
            net_leased_area = float(area) * levels * net_factor

            id_part = str(bidx)
            raw_name = row.get("name", f"building_{id_part}")
            normalized_name = normalize_name(raw_name) or f"building{id_part}"
            unique_name = f"{normalized_name}__idx{id_part}"

            building_category_by_name[unique_name] = building_tag
            building_retrofit_by_name[unique_name] = retrofit_situation

            if category == "residential":
                project.add_residential(
                    construction_data=construction_data,
                    geometry_data=geometry_data,
                    name=unique_name,
                    year_of_construction=year_built,
                    number_of_floors=levels,
                    height_of_floors=floor_height,
                    net_leased_area=net_leased_area,
                )
            else:
                project.add_non_residential(
                    construction_data=construction_data,
                    geometry_data=geometry_data,
                    name=unique_name,
                    year_of_construction=year_built,
                    number_of_floors=levels,
                    height_of_floors=floor_height,
                    net_leased_area=net_leased_area,
                    with_ahu=False,
                    internal_gains_mode=1,
                    office_layout=1,
                    window_layout=1,
                )

        if weather is not None:
            project.set_location_parameters(
                t_outside=float(weather["design_outdoor_temperature_c"]) + 273.15,
                t_ground=float(weather["ground_temperature_c"]) + 273.15,
                calc_all_buildings=False,
            )

        # Current TEASER versions calculate non-residential archetypes while
        # adding them, and calc_building_parameter accumulates sum_heat_load.
        # Reset before the one authoritative project-wide calculation.
        for bldg in project.buildings:
            bldg.sum_heat_load = 0.0
        project.calc_all_buildings()

        def _weather_based_annual_kwh(bldg):
            if weather is None:
                return None
            outside_c = np.asarray(weather["temperature_c"], dtype=float)
            if outside_c.size == 0:
                return None
            building_wh = 0.0
            for zone in getattr(bldg, "thermal_zones", []):
                model = getattr(zone, "model_attr", None)
                h_out = getattr(model, "heat_load_outside_factor", None)
                h_ground = getattr(model, "heat_load_ground_factor", None)
                if h_out is None or h_ground is None:
                    return None
                use_conditions = getattr(zone, "use_conditions", None)
                setpoints = getattr(use_conditions, "heating_profile", None)
                if setpoints is None or len(setpoints) == 0:
                    setpoints = [getattr(zone, "t_inside", 293.15)]
                setpoints_c = np.resize(
                    np.asarray(setpoints, dtype=float) - 273.15, outside_c.size
                )
                threshold_k = getattr(use_conditions, "T_threshold_heating", 288.15)
                threshold_c = float(threshold_k) - 273.15
                ground_c = float(weather["ground_temperature_c"])
                hourly_w = (
                    float(h_out) * (setpoints_c - outside_c)
                    + float(h_ground) * (setpoints_c - ground_c)
                )
                hourly_w = np.where(
                    outside_c < threshold_c, np.maximum(hourly_w, 0.0), 0.0
                )
                building_wh += float(np.nansum(hourly_w))
            return building_wh / 1000.0

        results_by_name = {}
        for bldg in project.buildings:
            heating_load = (
                    getattr(bldg, "sum_heat_load", None)
                    or getattr(bldg, "max_heating_load", None)
                    or getattr(bldg, "heating_load", None)
                    or 0
            )
            tag = building_category_by_name.get(bldg.name, "house")
            flh = FULL_LOAD_HOURS_BY_TYPE.get(tag, DEFAULT_FULL_LOAD_HOURS)
            retrofit_situation = building_retrofit_by_name.get(bldg.name, RETROFIT_SITUATION_DEFAULT)
            retrofit_factor = (
                1.0
                if tag in RESIDENTIAL_TYPES
                else RETROFIT_NONRES_DEMAND_FACTORS.get(retrofit_situation, 1.0)
            )
            peak_load_kw = float(heating_load) * retrofit_factor / 1000.0 if heating_load else 0.0
            demand_kWh = _weather_based_annual_kwh(bldg)
            if demand_kWh is None:
                demand_kWh = heating_load * flh / 1000 if heating_load else 0
                method = "TEASER design load x building-type full-load-hour fallback"
            else:
                method = "TEASER heat-loss factors x DWD hourly temperature"
            demand_kWh *= retrofit_factor
            if weather is not None and demand_kWh > 0:
                internal_gain, solar_gain = _coarse_useful_gains_kwh(
                    bldg,
                    weather["temperature_c"],
                    demand_kWh,
                    "non_residential" if tag in NON_RESIDENTIAL_TYPES else "residential",
                )
                gain_share = 100.0 * (internal_gain + solar_gain) / demand_kWh
                demand_kWh = max(demand_kWh - internal_gain - solar_gain, 0.0)
                method += (
                    f" minus coarse useful TEASER internal/solar gains ({gain_share:.1f}%)"
                )
            results_by_name[bldg.name] = {
                "heat_demand_kWh": demand_kWh,
                "peak_load_kW": peak_load_kw,
                "estimation_method": method,
            }
            total_heat_demand += demand_kWh

        # Map back: always use bidx as ID
        for bidx, row in df.iterrows():
            id_part = str(bidx)
            raw_name = row.get("name", f"building_{id_part}")
            normalized_name = normalize_name(raw_name) or f"building{id_part}"
            unique_name = f"{normalized_name}__idx{id_part}"
            result = results_by_name.get(unique_name)
            estimates.append({
                "input_index": id_part,
                "heat_demand_kWh": result["heat_demand_kWh"] if result else np.nan,
                "peak_load_kW": result["peak_load_kW"] if result else np.nan,
                "estimation_method": result["estimation_method"] if result else "not calculated",
            })

        estimates_df = pd.DataFrame(estimates).drop_duplicates(subset=["input_index"])
        return total_heat_demand, estimates_df

    DHW_RESIDENTIAL_TYPE_MAP = {
        "house": "SFH",
        "detached": "SFH",
        "semidetached": "SFH",
        "semidetached_house": "SFH",
        "bungalow": "SFH",
        "static_caravan": "SFH",
        "stilt_house": "SFH",
        "tree_house": "SFH",
        "terrace": "TH",
        "townhouse": "TH",
        "row_house": "TH",
        "residential": "MFH",
        "multi_family_house": "MFH",
        "multifamily_house": "MFH",
        "apartments": "AB",
        "apartment": "AB",
        "apartment_block": "AB",
        "flats": "AB",
        "dormitory": "AB",
        "student_accommodation": "AB",
    }

    DHW_NONRES_DEFAULTS = {
        "office": ("OB", 20.0, 5.0),
        "office_building": ("OB", 20.0, 5.0),
        "government": ("OB", 20.0, 5.0),
        "townhall": ("OB", 20.0, 5.0),
        "courthouse": ("OB", 20.0, 5.0),
        "commercial": ("OB", 25.0, 5.0),
        "civic": ("OB", 25.0, 5.0),
        "fire_station": ("OB", 25.0, 5.0),
        "police": ("OB", 25.0, 5.0),
        "train_station": ("OB", 25.0, 5.0),
        "school": ("SC", 12.0, 5.0),
        "primary_school": ("SC", 12.0, 5.0),
        "secondary_school": ("SC", 12.0, 5.0),
        "school_building": ("SC", 12.0, 5.0),
        "kindergarten": ("SC", 8.0, 8.0),
        "childcare": ("SC", 8.0, 8.0),
        "university": ("UNI", 12.0, 5.0),
        "college": ("UNI", 12.0, 5.0),
        "supermarket": ("GS", 35.0, 5.0),
        "retail": ("RETAIL", 30.0, 5.0),
        "restaurant": ("RE", 5.0, 10.0),
        "hospital": ("HOSPITAL", 25.0, 40.0),
        "care_home": ("HOSPITAL", 30.0, 40.0),
        "museum": ("CULTURE", 40.0, 3.0),
        "public": ("CULTURE", 40.0, 3.0),
        "civic": ("CULTURE", 40.0, 3.0),
        "religious": ("CULTURE", 40.0, 3.0),
        "church": ("CULTURE", 40.0, 3.0),
        "cathedral": ("CULTURE", 40.0, 3.0),
        "mosque": ("CULTURE", 40.0, 3.0),
        "temple": ("CULTURE", 40.0, 3.0),
        "stadium": ("SPORT", 20.0, 15.0),
        "sports_centre": ("SPORT", 20.0, 15.0),
        "sports_hall": ("SPORT", 20.0, 15.0),
        "industrial": ("WORKSHOP", 35.0, 5.0),
    }

    def _dhw_building_tag(val):
        tag = norm_btype(val)
        if not tag:
            return "house"
        return re.sub(r"[\s\-]+", "_", str(tag).strip().lower())

    def _load_opendhw_module():
        try:
            import OpenDHW as opendhw
            if hasattr(opendhw, "generate_dhw_profile"):
                return opendhw
            from OpenDHW import OpenDHW as opendhw_mod
            return opendhw_mod
        except Exception as exc:
            raise RuntimeError(
                "OpenDHW is not installed in the current Python environment. "
                "Install the project requirements, then rerun this step."
            ) from exc

    def _stable_dhw_seed(seed, bidx):
        digest = hashlib.sha1(str(bidx).encode("utf-8")).hexdigest()
        return (int(seed) + int(digest[:8], 16)) % (2**32 - 1)

    def _dhw_levels_for(building_tag, raw_levels):
        try:
            levels = int(float(raw_levels))
            if levels > 0:
                return levels
        except Exception:
            pass
        return DEFAULT_LEVELS_BY_TYPE.get(building_tag, DEFAULT_LEVELS)

    def _current_building_inputs_for_dhw(info_df_current):
        info = info_df_current.copy() if isinstance(info_df_current, pd.DataFrame) else pd.DataFrame()
        if info.empty:
            _ensure_info_overridden()
            info = st.session_state.get("info_overridden", pd.DataFrame()).copy()
        if info.empty or "bidx" not in info.columns:
            return pd.DataFrame(columns=["bidx", "building", "area_m2", "levels", "name"])

        info["bidx"] = info["bidx"].astype(str)
        excluded = set(map(str, st.session_state.get("excluded_bidx", set())))
        info = info.loc[~info["bidx"].isin(excluded)].copy()

        area_col = "area (m²)" if "area (m²)" in info.columns else "area_m2"
        if area_col not in info.columns:
            info[area_col] = np.nan

        if "building type" not in info.columns:
            info["building type"] = None
        if "name" not in info.columns:
            info["name"] = None

        level_map = {}
        if "levels" in info.columns:
            level_map = pd.to_numeric(info.set_index("bidx")["levels"], errors="coerce").dropna().to_dict()
        gsrc = st.session_state.get("buildings_gdf")
        if gsrc is not None and not getattr(gsrc, "empty", True) and "bidx" in gsrc.columns and "levels" in gsrc.columns:
            g = gsrc.copy()
            g["bidx"] = g["bidx"].astype(str)
            for bidx, value in g.set_index("bidx")["levels"].to_dict().items():
                level_map.setdefault(str(bidx), value)

        out = pd.DataFrame({
            "bidx": info["bidx"].astype(str),
            "name": info["name"],
            "building": info["building type"].map(_dhw_building_tag),
            "area_m2": pd.to_numeric(info[area_col], errors="coerce"),
        })
        out["building"] = out["building"].fillna("house")
        out["levels"] = out.apply(
            lambda r: _dhw_levels_for(r["building"], level_map.get(str(r["bidx"]))),
            axis=1,
        )
        return out.reset_index(drop=True)

    def _numeric_profile_series(profile, column):
        if column in profile.columns:
            return pd.to_numeric(profile[column], errors="coerce").fillna(0)
        return pd.Series(np.zeros(len(profile), dtype=float), index=profile.index)

    def _compute_dhw_heat_columns(profile, s_step, temp_delta):
        if "Water_L" not in profile.columns:
            water_lph = _numeric_profile_series(profile, "Water_LperH")
            profile["Water_L"] = water_lph / 3600 * s_step
        if "Heat_kWh" not in profile.columns:
            water_l = _numeric_profile_series(profile, "Water_L")
            profile["Heat_kWh"] = water_l * 0.98 * 4180 * float(temp_delta) / 3_600_000
        if "Heat_kW" not in profile.columns:
            profile["Heat_kW"] = _numeric_profile_series(profile, "Heat_kWh") * 3600 / s_step
        return profile

    def estimate_dhw_demand_with_opendhw(
            buildings_df,
            s_step,
            categories,
            residential_l_per_person_day,
            residential_m2_per_person,
            temp_delta,
            include_nonres,
            nonres_multiplier,
            seed,
            country_code,
    ):
        if buildings_df is None or buildings_df.empty:
            return 0.0, pd.DataFrame(columns=DHW_ESTIMATE_COLUMNS), pd.DataFrame(columns=DHW_PROFILE_COLUMNS)

        opendhw = _load_opendhw_module()
        try:
            holidays = opendhw.get_holidays(country_code, 2019)
            if isinstance(holidays, str):
                holidays = []
        except Exception:
            holidays = []

        rows = []
        aggregate_profile = None

        for _, row in buildings_df.iterrows():
            bidx = str(row.get("bidx"))
            building_tag = _dhw_building_tag(row.get("building"))
            area = pd.to_numeric(row.get("area_m2"), errors="coerce")
            levels = _dhw_levels_for(building_tag, row.get("levels"))

            row_out = {
                "bidx": bidx,
                "building index": f"Building_{bidx}",
                "building type": building_tag,
                "area_m2": area,
                "levels": levels,
                "net_floor_area_m2": np.nan,
                "opendhw_type": None,
                "estimated_occupants": np.nan,
                "water_l_per_person_day": np.nan,
                "mean_drawoff_l_per_day": np.nan,
                "annual_dhw_demand_kWh": np.nan,
                "basis": None,
                "status": None,
            }

            if pd.isna(area) or float(area) <= 0:
                row_out["status"] = "missing area"
                rows.append(row_out)
                continue

            if building_tag in ZERO_DEMAND_TYPES:
                row_out["annual_dhw_demand_kWh"] = 0.0
                row_out["status"] = "not estimated: no DHW default for this building type"
                rows.append(row_out)
                continue

            # Use the same category-based fallback as the TEASER calculation:
            # 0.75 for residential/unknown types and 0.50 for recognized
            # non-residential types without an explicit factor.
            fallback_category = "non_residential" if building_tag in NON_RESIDENTIAL_TYPES else "residential"
            net_factor = NET_AREA_FACTORS.get(
                building_tag,
                NET_AREA_FACTORS.get(fallback_category, 0.5),
            )
            net_floor_area = float(area) * levels * net_factor
            row_out["net_floor_area_m2"] = net_floor_area

            if building_tag in DHW_RESIDENTIAL_TYPE_MAP:
                opendhw_type = DHW_RESIDENTIAL_TYPE_MAP[building_tag]
                area_per_user = float(residential_m2_per_person)
                l_per_user = float(residential_l_per_person_day)
                basis = f"{area_per_user:g} m²/person, {l_per_user:g} L/person/day"
                weekend_factor = 1.2
            elif include_nonres and building_tag in DHW_NONRES_DEFAULTS:
                opendhw_type, area_per_user, l_per_user = DHW_NONRES_DEFAULTS[building_tag]
                l_per_user = float(l_per_user) * float(nonres_multiplier)
                basis = f"{area_per_user:g} m²/user, {l_per_user:g} L/user/day"
                weekend_factor = 1.0
            else:
                row_out["annual_dhw_demand_kWh"] = 0.0
                row_out["status"] = "not estimated: enable non-residential defaults or set residential type"
                rows.append(row_out)
                continue

            occupancy = max(1.0, net_floor_area / max(float(area_per_user), 1.0))
            row_out["opendhw_type"] = opendhw_type
            row_out["estimated_occupants"] = occupancy
            row_out["water_l_per_person_day"] = l_per_user
            row_out["mean_drawoff_l_per_day"] = occupancy * l_per_user
            row_out["basis"] = basis

            try:
                stable_seed = _stable_dhw_seed(seed, bidx)
                random.seed(stable_seed)
                np.random.seed(stable_seed)

                profile = opendhw.generate_dhw_profile(
                    s_step=int(s_step),
                    categories=int(categories),
                    mean_drawoff_vol_per_day=float(l_per_user),
                    occupancy=float(occupancy),
                    holidays=holidays,
                    building_type=opendhw_type,
                    weekend_weekday_factor=float(weekend_factor),
                    initial_day=1,
                )
                try:
                    profile = opendhw.compute_heat(profile, temp_dT=float(temp_delta))
                except Exception:
                    profile = _compute_dhw_heat_columns(profile, int(s_step), float(temp_delta))
                profile = _compute_dhw_heat_columns(profile, int(s_step), float(temp_delta))

                heat_kwh = _numeric_profile_series(profile, "Heat_kWh")
                water_l = _numeric_profile_series(profile, "Water_L")
                water_lph = _numeric_profile_series(profile, "Water_LperH")
                annual_kwh = float(heat_kwh.sum())

                if aggregate_profile is None:
                    aggregate_profile = pd.DataFrame({
                        "timestamp": pd.to_datetime(profile.index),
                        "dhw_heat_kWh": np.zeros(len(profile), dtype=float),
                        "water_L": np.zeros(len(profile), dtype=float),
                        "water_LperH": np.zeros(len(profile), dtype=float),
                    })
                if len(aggregate_profile) == len(profile):
                    aggregate_profile["dhw_heat_kWh"] = aggregate_profile["dhw_heat_kWh"].to_numpy() + heat_kwh.to_numpy()
                    aggregate_profile["water_L"] = aggregate_profile["water_L"].to_numpy() + water_l.to_numpy()
                    aggregate_profile["water_LperH"] = aggregate_profile["water_LperH"].to_numpy() + water_lph.to_numpy()

                row_out["annual_dhw_demand_kWh"] = annual_kwh
                row_out["status"] = "estimated"
            except Exception as exc:
                row_out["status"] = f"OpenDHW failed: {exc}"

            rows.append(row_out)

        estimates_df = _std_dhw_estimates_df(pd.DataFrame(rows))
        if aggregate_profile is None:
            profile_df = pd.DataFrame(columns=DHW_PROFILE_COLUMNS)
        else:
            aggregate_profile["dhw_heat_kW"] = aggregate_profile["dhw_heat_kWh"] * 3600 / int(s_step)
            aggregate_profile = aggregate_profile[DHW_PROFILE_COLUMNS]
            profile_df = _std_dhw_profile_df(aggregate_profile)

        total_dhw = float(pd.to_numeric(estimates_df["annual_dhw_demand_kWh"], errors="coerce").fillna(0).sum())
        return total_dhw, estimates_df, profile_df

    ########################################################################################################################

    st.subheader("🗺️ Map-Based Building Demand Estimation")
    st.markdown("### Draw the Project Area")
    st.info(
        "In this step, you can draw the area that belongs to your project. Use the polygon tool on the "
        "left side of the map. After an area is drawn, the tool searches OpenStreetMap for buildings "
        "inside it."
    )

    # --- 0) session defaults ---
    st.session_state.setdefault("drawn_polygons", [])
    st.session_state.setdefault("map_center", [52.52, 13.405])  # Berlin fallback
    st.session_state.setdefault("map_zoom", 14)
    st.session_state.setdefault("selected_drawn_area_idx", 0)
    st.session_state.setdefault("_ignored_draw_fps", set())
    st.session_state.setdefault("_skip_next_draw_capture", 0)
    st.session_state.setdefault("_draw_map_rev", 0)

    def _manual_buildings_empty_df():
        return pd.DataFrame(
            columns=[
                "bidx", "name", "building type", "area (m²)", "year built", "retrofit situation",
                "building type source", "year source", "zensus age class", "zensus type class",
                "zensus type note", "zensus year note", "zensus source",
            ]
        )

    def _poly_fp(poly_or_wkt):
        try:
            poly = wkt.loads(poly_or_wkt) if isinstance(poly_or_wkt, str) else poly_or_wkt
            return hashlib.sha1(poly.wkb).hexdigest()
        except Exception:
            return None

    def _mark_area_outputs_stale():
        for k in [
            "_preloaded_buildings_gdf", "_last_buildings_gdf",
            "total_heat_demand", "info_overridden",
            "route_length", "attr_demand_conflicts"
        ]:
            st.session_state.pop(k, None)
        st.session_state["_persist_dirty"] = True
        st.session_state["results_stale"] = True
        if st.session_state.get("show_dhw_results"):
            st.session_state["dhw_results_stale"] = True

    def _save_empty_area_selection(curr_project):
        if not curr_project:
            return
        empty_attrs = pd.DataFrame(
            columns=["bidx", "building type", "area (m²)", "address", "year built", "retrofit situation"]
        )
        save_project_dataset_v2(
            project_name=curr_project,
            polygons_wkt=[],
            buildings_gdf=None,
            excluded_bidx=set(),
            manual_buildings_df=_manual_buildings_empty_df(),
            building_attrs_df=empty_attrs,
            estimates_df_final=None,
            estimates_df_original=None,
            total_heat_demand_kwh=None,
            route_length_m=None,
        )

    def _reset_drawn_areas_for_project():
        curr_project = st.session_state.get("current_project")
        draw_map_rev = int(st.session_state.get("_draw_map_rev", 0) or 0)
        ignored = set(st.session_state.get("_ignored_draw_fps", set()))
        for p in st.session_state.get("drawn_polygons", []):
            fp = _poly_fp(p)
            if fp:
                ignored.add(fp)

        reset_project_state()
        st.session_state["drawn_polygons"] = []
        st.session_state["polygons"] = []
        st.session_state["selected_polygons"] = []
        st.session_state["polygons_geojson"] = {"type": "FeatureCollection", "features": []}
        st.session_state["polygons_wkt"] = []
        st.session_state["bidx_map"] = {}
        st.session_state["next_bidx"] = 0
        st.session_state["excluded_bidx"] = set()
        st.session_state["manual_buildings"] = _manual_buildings_empty_df()
        st.session_state["selected_drawn_area_idx"] = 0
        st.session_state["_ignored_draw_fps"] = ignored
        st.session_state["_skip_next_draw_capture"] = 1
        st.session_state["_draw_map_rev"] = draw_map_rev + 1
        st.session_state["_persist_dirty"] = False
        st.session_state.setdefault("map_center", [52.52, 13.405])
        st.session_state.setdefault("map_zoom", 14)
        st.session_state["_osm_preloaded"] = True
        _save_empty_area_selection(curr_project)

    # --- 1) center from project info if available ---
    if "project_coords" in st.session_state:
        default_center = list(st.session_state["project_coords"])
        st.session_state["map_center"] = default_center
        st.session_state["map_zoom"] = st.session_state.get("project_zoom", 12)
    else:
        default_center = [52.52, 13.405]
        st.warning("⚠ No project location set yet. Using default center (Berlin).")

    drawn_count = len(st.session_state["drawn_polygons"])
    try:
        selected_area_idx = int(
            st.session_state.get(
                "selected_drawn_area_pick",
                st.session_state.get("selected_drawn_area_idx", 0),
            )
        )
    except Exception:
        selected_area_idx = 0
    if drawn_count:
        selected_area_idx = min(max(selected_area_idx, 0), drawn_count - 1)
    else:
        selected_area_idx = 0
    st.session_state["selected_drawn_area_idx"] = selected_area_idx

    # --- 2) build the drawing map (fit to polygons if any) ---
    if st.session_state["drawn_polygons"]:
        polygons = [wkt.loads(p) for p in st.session_state["drawn_polygons"]]
        combined_polygon = unary_union(polygons)
        minx, miny, maxx, maxy = combined_polygon.bounds
        pad = 0.001
        sw = [miny - pad, minx - pad]  # (lat, lon)
        ne = [maxy + pad, maxx + pad]
        m = folium.Map(location=default_center, zoom_start=st.session_state["map_zoom"])
        m.fit_bounds([sw, ne])
    else:
        m = folium.Map(location=st.session_state["map_center"], zoom_start=st.session_state["map_zoom"])

    Draw(
        export=False,  # <-- important: populate "all_drawings"
        draw_options={
            "polygon": {
                "allowIntersection": False,
                "showArea": True,
                "showLength": True,
            },
            "rectangle": True,
            "polyline": False,
            "circle": False,
            "circlemarker": False,
            "marker": False,
        },
        edit_options={"edit": False, "remove": False},
    ).add_to(m)

    # Finalize newly created shapes on the client so st_folium sees them
    from branca.element import MacroElement, Template
    _map_var = m.get_name()

    _js = f"""
    {{% macro script(this, kwargs) %}}
    // When a new shape is finished, put it on the map so the draw finalizes
    {_map_var}.on(L.Draw.Event.CREATED, function (e) {{
        e.layer.addTo({_map_var});
    }});
    {{% endmacro %}}
    """

    macro = MacroElement()
    macro._template = Template(_js)
    m.get_root().add_child(macro)

    # render existing polygons
    for i, poly_wkt in enumerate(st.session_state["drawn_polygons"]):
        poly = wkt.loads(poly_wkt)
        is_selected = i == selected_area_idx
        folium.GeoJson(
            data=mapping(poly),
            style_function=lambda x, is_selected=is_selected: {
                "fillColor": "#00a6d6" if is_selected else "#ff7800",
                "color": "#005f73" if is_selected else "black",
                "weight": 4 if is_selected else 1,
                "fillOpacity": 0.45 if is_selected else 0.3,
            },
            tooltip=f"Area {i + 1}" + (" (selected)" if is_selected else ""),
        ).add_to(m)

    # ---- render map ----
    proj_key = str(st.session_state.get("current_project", "default"))
    drawn_map_digest = hashlib.sha1(
        ("|".join(st.session_state["drawn_polygons"]) + f"|selected={selected_area_idx}").encode("utf-8")
    ).hexdigest()[:10]
    draw_map_rev = int(st.session_state.get("_draw_map_rev", 0) or 0)
    map_data = streamlit_folium.st_folium(
        m,
        width=700,
        height=500,
        key=f"draw_map_{proj_key}_{drawn_map_digest}_{draw_map_rev}",
        returned_objects=["last_active_drawing", "last_object_drawn", "all_drawings", "center", "zoom"],
    )

    # store center/zoom for next run
    if map_data and "center" in map_data and "zoom" in map_data:
        st.session_state["pending_center"] = [map_data["center"]["lat"], map_data["center"]["lng"]]
        st.session_state["pending_zoom"] = map_data["zoom"]

    new_geo = None
    if map_data:
        # 1) Prefer the field streamlit-folium sets most reliably
        lod = map_data.get("last_object_drawn")
        if lod and lod.get("geometry") and lod["geometry"].get("type") in {"Polygon", "MultiPolygon"}:
            new_geo = lod["geometry"]

        # 2) Some versions only fill last_active_drawing
        if new_geo is None:
            lad = map_data.get("last_active_drawing")
            if lad and lad.get("geometry") and lad["geometry"].get("type") in {"Polygon", "MultiPolygon"}:
                new_geo = lad["geometry"]

        # 3) Fallback: FeatureCollection (needs export=True)
        if new_geo is None:
            fc = map_data.get("all_drawings")
            try:
                feats = (fc or {}).get("features") or []
                for f in reversed(feats):
                    g = (f or {}).get("geometry")
                    if g and g.get("type") in {"Polygon", "MultiPolygon"}:
                        new_geo = g
                        break
            except Exception:
                pass

    skip_draw_capture = int(st.session_state.get("_skip_next_draw_capture", 0) or 0)
    if skip_draw_capture > 0:
        remaining_skip = skip_draw_capture - 1
        st.session_state["_skip_next_draw_capture"] = remaining_skip
        if remaining_skip <= 0:
            st.session_state["_ignored_draw_fps"] = set()
        new_geo = None

    # Only accept polygons
    if new_geo and new_geo.get("type") in {"Polygon", "MultiPolygon"}:
        try:
            new_poly = shape(new_geo)
            new_wkt = new_poly.wkt
            new_fp = _poly_fp(new_poly)
            ignored_fps = set(st.session_state.get("_ignored_draw_fps", set()))
            existing_fps = {_poly_fp(p) for p in st.session_state["drawn_polygons"]}
            if new_fp in ignored_fps:
                pass
            elif new_fp not in existing_fps:
                st.session_state["drawn_polygons"].append(new_wkt)
                st.success("✅ Added new drawn area!")
                st.session_state["selected_drawn_area_idx"] = len(st.session_state["drawn_polygons"]) - 1
                _mark_area_outputs_stale()
                st.rerun()
        except Exception:
            pass

    with st.expander("Area management", expanded=False):
        st.markdown(
            "Use the area management function when several areas were drawn. Select one area to highlight it on the map, "
            "or remove areas before continuing."
        )
        if st.session_state["drawn_polygons"]:
            options = list(range(len(st.session_state["drawn_polygons"])))
            radio_key = "selected_drawn_area_pick"
            desired_idx = st.session_state.get("selected_drawn_area_idx", options[0])
            if desired_idx not in options:
                desired_idx = options[0]
                st.session_state["selected_drawn_area_idx"] = desired_idx
            if st.session_state.get(radio_key) != desired_idx:
                st.session_state[radio_key] = desired_idx
            picked_area_idx = st.radio(
                "Highlighted area",
                options=options,
                format_func=lambda i: f"Area {i + 1}",
                key=radio_key,
                horizontal=True,
            )
            if picked_area_idx != st.session_state.get("selected_drawn_area_idx", 0):
                st.session_state["selected_drawn_area_idx"] = picked_area_idx

            for i, p in enumerate(st.session_state["drawn_polygons"]):
                c1, c2 = st.columns([1, 0.2])
                with c1:
                    label = f"Area {i + 1}"
                    if i == st.session_state.get("selected_drawn_area_idx", 0):
                        label += " (highlighted)"
                    st.caption(label)
                    st.code(p[:120] + ("..." if len(p) > 120 else ""), language="text")
                with c2:
                    if st.button("🗑️", key=f"del_poly_{i}"):
                        removed = st.session_state["drawn_polygons"].pop(i)
                        fp = _poly_fp(removed)
                        if fp:
                            ignored = set(st.session_state.get("_ignored_draw_fps", set()))
                            ignored.add(fp)
                            st.session_state["_ignored_draw_fps"] = ignored
                        st.session_state["_skip_next_draw_capture"] = 1
                        st.session_state["_draw_map_rev"] = int(st.session_state.get("_draw_map_rev", 0) or 0) + 1
                        if st.session_state["drawn_polygons"]:
                            st.session_state["selected_drawn_area_idx"] = min(
                                i,
                                len(st.session_state["drawn_polygons"]) - 1,
                            )
                            _mark_area_outputs_stale()
                        else:
                            _reset_drawn_areas_for_project()
                        st.rerun()

            if st.button("🔄 Reset all drawn areas", key="reset_drawn_areas_inline"):
                st.session_state["reset_confirm_inline"] = True

            if st.session_state.get("reset_confirm_inline"):
                st.warning("This will delete all drawn areas and clear associated building/results data.")
                c_yes, c_no = st.columns([1, 1])
                with c_yes:
                    if st.button("✅ Confirm reset areas", key="confirm_reset_inline"):
                        _reset_drawn_areas_for_project()
                        st.session_state.pop("reset_confirm_inline", None)
                        st.success("All drawn areas and associated results have been reset for this project.")
                        st.rerun()
                with c_no:
                    if st.button("Cancel", key="cancel_reset_inline"):
                        st.session_state.pop("reset_confirm_inline", None)
                        st.rerun()
        else:
            st.caption("No drawn areas yet.")

    # If we already have results (reopening a saved project), ensure info_overridden exists
    has_saved_results = (
            st.session_state.get("building_demand_estimates") is not None
            and not st.session_state["building_demand_estimates"].empty
    )
    if has_saved_results:
        _ensure_info_overridden()
        st.session_state.setdefault("results_stale", False)

    # ---------- STOP EARLY IF NO POLYGONS; the rest of the page depends on them ----------
    if not st.session_state["drawn_polygons"]:
        # st.info("Draw at least one area on the map above to continue. After that, buildings will be loaded automatically.")
        st.stop()

    # ---------- UNIFIED SOURCE SELECTION (preloaded OR fetch) ----------
    polygons = [wkt.loads(p) for p in st.session_state["drawn_polygons"]]
    combined_polygon = unary_union(polygons)
    osm_fetch_status = st.empty()
    osm_fetch_started = False

    pre_gdf = st.session_state.get("_preloaded_buildings_gdf")
    st.session_state.setdefault("_preloaded_drawn_polygons", list(st.session_state.get("drawn_polygons", [])))
    use_preloaded = bool(
        st.session_state.get("_osm_preloaded")
        and isinstance(pre_gdf, (pd.DataFrame, gpd.GeoDataFrame))
        and not getattr(pre_gdf, "empty", True)
    )

    prev_polys = list(st.session_state.get("_preloaded_drawn_polygons", []))
    same_areas = set(prev_polys) == set(st.session_state["drawn_polygons"])

    # build a stable key for dedup
    def _keyframe(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        elem = df["element"].astype(str) if "element" in df.columns else pd.Series(["way"] * len(df), index=df.index)
        df["__elem__"] = elem


        osmid = (pd.to_numeric(df["osmid"].apply(_norm_osmid), errors="coerce").astype("Int64")
                 if "osmid" in df.columns
                 else pd.Series(pd.array([pd.NA] * len(df), dtype="Int64"), index=df.index))
        df["__osmid__"] = osmid
        df["__gid__"] = df["__elem__"].astype(str) + "/" + df["__osmid__"].astype(str)
        if "geometry" in df.columns:
            df["__geomfp__"] = df["geometry"].apply(
                lambda g: hashlib.sha1(g.wkb).hexdigest() if g is not None else None)
        else:
            df["__geomfp__"] = None
        return df

    if use_preloaded and same_areas:
        buildings_raw = pre_gdf.copy()
    else:
        osm_fetch_started = True
        if use_preloaded:
            fetch_msg = "Fetching buildings from OpenStreetMap for new/expanded areas..."
            osm_fetch_status.info(fetch_msg)
            fetched_now = _fetch_osm_buildings(combined_polygon.wkt)
            base = pre_gdf.copy()
            add = fetched_now.copy()

            def _prep(df):
                if "element" not in df.columns and "element_type" in df.columns:
                    df = df.rename(columns={"element_type": "element"})
                return df

            base = _prep(base)
            add = _prep(add)

            base_k = _keyframe(base)
            add_k = _keyframe(add)

            known = set(base_k["__gid__"].dropna().astype(str))
            known_geom = set(base_k.loc[base_k["__gid__"].isna(), "__geomfp__"].dropna())

            mask_new = (~add_k["__gid__"].astype(str).isin(known)) & (
                    add_k["__gid__"].notna() | ~add_k["__geomfp__"].isin(known_geom)
            )
            buildings_raw = pd.concat([base_k, add_k.loc[mask_new]], ignore_index=True)
            buildings_raw = buildings_raw.drop(columns=["__osmid__", "__elem__", "__gid__", "__geomfp__"],
                                               errors="ignore")
        else:
            fetch_msg = "Fetching buildings from OpenStreetMap for all drawn areas..."
            osm_fetch_status.info(fetch_msg)
            buildings_raw = _fetch_osm_buildings(combined_polygon.wkt)

    if use_preloaded and not same_areas:
        st.session_state["_preloaded_drawn_polygons"] = list(st.session_state["drawn_polygons"])

    # ---------- ALWAYS RUN THE SAME NORMALIZATION/OVERRIDE PIPELINE ----------
    # 1) flatten multiindex if needed
    b = buildings_raw.reset_index() if isinstance(buildings_raw.index, pd.MultiIndex) else buildings_raw

    # 2) Find/create 'element' column (needed for stable ids)
    element_col = None
    for cand in ("element", "element_type"):
        if cand in b.columns:
            element_col = cand
            break
    if element_col is None:
        # Heuristic scan of first few columns
        for c in list(b.columns[:5]):
            vals = b[c].astype(str)
            if vals.isin(["node", "way", "relation"]).any():
                element_col = c
                break
    if element_col is None:
        b["element"] = "way"
    elif element_col != "element":
        b = b.rename(columns={element_col: "element"})

    # 3) Detect/normalize osmid and build osm_id
    osmid_col = "osmid" if "osmid" in b.columns else None
    if osmid_col is None and "index" in b.columns and "osmid" not in b.columns:
        if pd.api.types.is_integer_dtype(b["index"]) or pd.api.types.is_float_dtype(b["index"]):
            b = b.rename(columns={"index": "osmid"})
            osmid_col = "osmid"
    if "element_type" in b.columns and "element" not in b.columns:
        b = b.rename(columns={"element_type": "element"})
    if "osmid" not in b.columns:
        for cand in ("id", "osmid"):
            if cand in b.columns:
                b = b.rename(columns={cand: "osmid"})
                break
    if "osmid" not in b.columns:
        b["osmid"] = pd.Series(pd.array([pd.NA] * len(b), dtype="Int64"))
    # if osmid_col is None:
    #     b["osmid"] = pd.NA

    b["osmid"] = pd.to_numeric(b["osmid"].apply(_norm_osmid), errors="coerce").astype("Int64")
    b["osm_id"] = np.where(b["osmid"].notna(), b["element"].astype(str) + "/" + b["osmid"].astype(str), None)

    buildings = b

    if not (hasattr(buildings, "geometry") and "geometry" in buildings.columns):
        if "geom_wkt" in buildings.columns:
            try:
                buildings = gpd.GeoDataFrame(
                    buildings,
                    geometry=gpd.GeoSeries.from_wkt(buildings["geom_wkt"]),
                    crs="EPSG:4326",
                )
            except Exception:
                st.error(
                    "Loaded buildings have no usable geometry. "
                    "Please re-fetch buildings from OSM or recreate the project."
                )
                st.stop()
        else:
            st.error(
                "Loaded buildings have no geometry. "
                "Please re-fetch buildings from OSM or recreate the project."
            )
            st.stop()

    # ensure area_m2 exists (compute if missing or NaN-heavy)
    if "area_m2" not in buildings.columns or buildings["area_m2"].isna().all():
        try:
            tmp = gpd.GeoDataFrame(geometry=buildings.geometry, crs="EPSG:4326").to_crs(3857)
            buildings["area_m2"] = tmp.area.values
        except Exception:
            buildings["area_m2"] = np.nan

    if "building:levels" in buildings.columns:
        buildings["levels_osm"] = pd.to_numeric(
            buildings["building:levels"].astype(str).str.split(";").str[0].str.replace(",", ".", regex=False),
            errors="coerce",
        )
    elif "levels" in buildings.columns:
        buildings["levels_osm"] = pd.to_numeric(buildings["levels"], errors="coerce")
    else:
        buildings["levels_osm"] = np.nan

    raw_height = pd.to_numeric(
        buildings.get("height_m", pd.Series([np.nan] * len(buildings), index=buildings.index)),
        errors="coerce",
    )
    if "height" in buildings.columns:
        parsed_height = buildings["height"].apply(_extract_numeric_height)
        raw_height = raw_height.combine_first(pd.to_numeric(parsed_height, errors="coerce"))
    geometry_values = buildings.apply(
        lambda row: _building_geometry_values(
            row.get("building"), row.get("levels_osm"), raw_height.loc[row.name]
        ),
        axis=1,
    )
    buildings["levels"] = [values[0] for values in geometry_values]
    buildings["height_m"] = [values[1] for values in geometry_values]
    buildings["floor_height_m"] = [values[2] for values in geometry_values]

    # 4) polygons only
    if buildings.empty:
        if osm_fetch_started:
            osm_fetch_status.empty()
        st.info("No building polygons found in the drawn areas.")
        if has_saved_results:
            _ensure_info_overridden()
        st.stop()

    buildings = buildings[buildings.geometry.type.isin(["Polygon", "MultiPolygon"])].copy().reset_index(drop=True)

    bmap = st.session_state.get("bidx_map", {})
    next_id = st.session_state.get("next_bidx", 0)

    has_bidx_col = "bidx" in buildings.columns
    if has_bidx_col:
        raw_bidx = buildings["bidx"]
        missing_mask = (
                raw_bidx.isna()
                | raw_bidx.astype(str).str.strip().isin(["", "nan", "none", "None"])
        )
    else:
        missing_mask = pd.Series(True, index=buildings.index)

    # 1) First, record stable keys for rows that *already* have a valid bidx
    for _, r in buildings[~missing_mask].iterrows():
        k = _stable_key(r)
        if k is not None:
            bmap[k] = str(r["bidx"])
        try:
            next_id = max(next_id, int(r["bidx"]) + 1)
        except Exception:
            pass

    # 2) Now assign bidx for rows that are missing
    assigned = []
    for ix, r in buildings.iterrows():
        if not missing_mask.loc[ix]:
            assigned.append(str(r["bidx"]))
            continue

        k = _stable_key(r)
        if k is None or k not in bmap:
            bidx = str(next_id)
            next_id += 1
            if k is not None:
                bmap[k] = bidx
        else:
            bidx = bmap[k]
        assigned.append(bidx)

    buildings["bidx"] = pd.Series(assigned, index=buildings.index).astype(str)

    st.session_state["bidx_map"] = bmap
    st.session_state["next_bidx"] = next_id

    if getattr(buildings, "crs", None) is None:
        try:
            buildings = buildings.set_crs(epsg=4326)
        except Exception:
            pass

    try:
        buildings_utm = buildings.to_crs(epsg=3857)
    except Exception:
        buildings_utm = None

    st.session_state["_buildings_utm"] = buildings_utm

    # 6) area, info_df, address, name
    info_df = pd.DataFrame({"bidx": buildings["bidx"].astype(str)})
    info_df["area (m²)"] = buildings.get(
        "area_m2",
        pd.Series([np.nan] * len(buildings), index=buildings.index)
    )
    info_df["height (m)"] = (
        pd.to_numeric(buildings.get("height_m", pd.Series([np.nan] * len(buildings), index=buildings.index)),
                      errors="coerce")
        .fillna(pd.to_numeric(buildings.get("height", pd.Series([np.nan] * len(buildings), index=buildings.index)),
                              errors="coerce"))
    )
    info_df["levels"] = pd.to_numeric(
        buildings.get(
            "levels",
            pd.Series([np.nan] * len(buildings), index=buildings.index)
        ),
        errors="coerce",
    )
    try:
        _pad_width = max(2, len(str(info_df["bidx"].astype(int).max())))
    except Exception:
        _pad_width = 2

    def _fmt_idx(s: str) -> str:
        try:
            return f"Building_{int(s):0{_pad_width}d}"
        except Exception:
            return f"Building_{s}"

    info_df["building index"] = info_df["bidx"].astype(str).map(_fmt_idx)
    info_df["building type"] = (
        buildings["building"].map(norm_btype)
        if "building" in buildings.columns else pd.Series([None] * len(buildings), index=buildings.index)
    )
    info_df["retrofit situation"] = (
        buildings.get("retrofit_situation", pd.Series([None] * len(buildings), index=buildings.index))
        .apply(_normalize_retrofit_situation)
    )
    info_df["renovation probability (%)"] = (
        pd.to_numeric(
            buildings.get("retrofit_probability", pd.Series([np.nan] * len(buildings), index=buildings.index)),
            errors="coerce",
        ) * 100.0
    )
    info_df["retrofit situation source"] = buildings.get(
        "retrofit_situation_source", pd.Series([None] * len(buildings), index=buildings.index)
    )

    def _make_address_from_row(row_src):
        def getv(*keys):
            for k in keys:
                v = row_src.get(k)
                if pd.notna(v) and str(v).strip() != "":
                    return str(v).strip()
            return ""

        street = getv("addr:street")
        housenr = getv("addr:housenumber")
        postcode = getv("addr:postcode")
        city = getv("addr:city", "addr:town", "addr:village", "addr:hamlet", "addr:suburb")
        parts = []
        street_part = " ".join(p for p in [street, housenr] if p)
        if street_part: parts.append(street_part)
        locality = " ".join(p for p in [postcode, city] if p)
        if locality: parts.append(locality)
        return ", ".join(parts)

    if "address" not in buildings.columns:
        info_df["address"] = buildings.apply(_make_address_from_row, axis=1).replace("", None).astype("object")
    else:
        info_df["address"] = buildings["address"]
    buildings["address"] = info_df["address"].values

    # 6B) Zensus defaults for missing residential type/year data.
    zensus_summary_saved = st.session_state.get("zensus_summary")
    has_saved_zensus = (
        {"zensus_type_note", "zensus_year_note"} <= set(buildings.columns)
        and buildings[["zensus_type_note", "zensus_year_note"]].notna().all(axis=1).all()
        and isinstance(zensus_summary_saved, pd.DataFrame)
        and not zensus_summary_saved.empty
    )
    if has_saved_zensus:
        zmap = buildings.set_index("bidx", drop=False)
        for ui_col, g_col in [
            ("building type source", "building_type_source"),
            ("year built", "year_built"),
            ("year source", "year_built_source"),
            ("zensus age class", "zensus_age_class"),
            ("zensus type class", "zensus_type_class"),
            ("zensus type note", "zensus_type_note"),
            ("zensus year note", "zensus_year_note"),
            ("zensus source", "zensus_source"),
        ]:
            if g_col in zmap.columns:
                info_df[ui_col] = info_df["bidx"].map(zmap[g_col])
        st.session_state["zensus_warnings"] = []
    else:
        try:
            buildings, info_df, zensus_summary, zensus_warnings = apply_zensus_defaults(
                buildings,
                info_df,
                combined_polygon,
                residential_types=set(RESIDENTIAL_TYPES.keys()),
                zero_demand_types=set(ZERO_DEMAND_TYPES),
                norm_btype=norm_btype,
            )
            st.session_state["zensus_warnings"] = zensus_warnings
            if isinstance(zensus_summary, pd.DataFrame) and not zensus_summary.empty:
                st.session_state["zensus_summary"] = zensus_summary
        except Exception as e:
            st.session_state["zensus_warnings"] = [f"Zensus import failed: {e}"]

    # 7) apply saved attribute overrides
    attrs = st.session_state.get("building_attrs", pd.DataFrame())
    if isinstance(attrs, pd.DataFrame) and not attrs.empty and "bidx" in attrs.columns:
        a = attrs.copy();
        a["bidx"] = a["bidx"].astype(str)

        # building type
        if "building type" in a.columns:
            t = a[["bidx", "building type"]].dropna(subset=["building type"]).copy()
            t["building type"] = t["building type"].map(norm_btype)
            tmap = t.set_index("bidx")["building type"].to_dict()
            if tmap:
                info_df["building type"] = info_df.apply(lambda r: tmap.get(r["bidx"], r.get("building type")), axis=1)
                info_df["building type source"] = info_df.apply(lambda r: "user" if r["bidx"] in tmap else r.get("building type source"), axis=1)
                buildings["building"] = buildings.apply(lambda r: tmap.get(r["bidx"], r.get("building")), axis=1)
                buildings["building_type_source"] = buildings.apply(lambda r: "user" if r["bidx"] in tmap else r.get("building_type_source"), axis=1)

        # area
        if "area (m²)" in a.columns:
            u = a[["bidx", "area (m²)"]].copy()
            u["area (m²)"] = pd.to_numeric(u["area (m²)"], errors="coerce")
            amap = u.dropna(subset=["area (m²)"]).set_index("bidx")["area (m²)"].to_dict()
            if amap:
                info_df["area (m²)"] = info_df.apply(lambda r: amap.get(r["bidx"], r.get("area (m²)")), axis=1)
                buildings["area_m2"] = buildings.apply(lambda r: amap.get(r["bidx"], r.get("area_m2")), axis=1)

        # address
        def _clean_addr_local(v):
            if v is None or pd.isna(v): return None
            s = str(v).strip();
            return s if s else None

        if "address" in a.columns:
            v = a[["bidx", "address"]].copy()
            v["address"] = v["address"].map(_clean_addr_local)
            addr_map = v.dropna(subset=["address"]).set_index("bidx")["address"].to_dict()
            if addr_map:
                info_df["address"] = info_df.apply(lambda r: addr_map.get(r["bidx"], r.get("address")), axis=1)
                buildings["address"] = buildings.apply(lambda r: addr_map.get(r["bidx"], r.get("address")), axis=1)

        # year built
        if "year built" in a.columns:
            y = a[["bidx", "year built"]].copy()
            y["year built"] = pd.to_numeric(y["year built"], errors="coerce")
            ymap = y.dropna(subset=["year built"]).set_index("bidx")["year built"].to_dict()
            if ymap:
                info_df["year built"] = info_df.apply(lambda r: ymap.get(r["bidx"], r.get("year built")), axis=1)
                info_df["year source"] = info_df.apply(lambda r: "user" if r["bidx"] in ymap else r.get("year source"), axis=1)
                buildings["year_built"] = buildings.apply(lambda r: ymap.get(r["bidx"], r.get("year_built")), axis=1)
                buildings["year_built_source"] = buildings.apply(lambda r: "user" if r["bidx"] in ymap else r.get("year_built_source"), axis=1)

        # retrofit situation
        if "retrofit situation" in a.columns:
            r = a[["bidx", "retrofit situation"]].copy()
            r["retrofit situation"] = r["retrofit situation"].apply(_normalize_retrofit_situation)
            rmap = r.dropna(subset=["retrofit situation"]).set_index("bidx")["retrofit situation"].to_dict()
            if rmap:
                info_df["retrofit situation"] = info_df.apply(
                    lambda row: rmap.get(row["bidx"], row.get("retrofit situation")), axis=1
                )
                info_df["retrofit situation source"] = info_df.apply(
                    lambda row: "user" if row["bidx"] in rmap else row.get("retrofit situation source"), axis=1
                )
                buildings["retrofit_situation"] = buildings.apply(
                    lambda row: rmap.get(row["bidx"], row.get("retrofit_situation")), axis=1
                )
                buildings["retrofit_situation_source"] = buildings.apply(
                    lambda row: "user" if row["bidx"] in rmap else row.get("retrofit_situation_source"), axis=1
                )

    auto_retrofit_mask = ~info_df.get(
        "retrofit situation source", pd.Series([None] * len(info_df), index=info_df.index)
    ).astype(str).str.lower().eq("user")
    info_df.loc[auto_retrofit_mask, "retrofit situation"] = None
    info_df = _add_retrofit_defaults(info_df)
    info_df["retrofit situation source"] = info_df["retrofit situation source"].where(
        info_df["retrofit situation source"].astype(str).str.lower().eq("user"),
        "estimated",
    )
    buildings["retrofit_situation"] = info_df["retrofit situation"].values
    buildings["retrofit_probability"] = (
        pd.to_numeric(info_df["renovation probability (%)"], errors="coerce") / 100.0
    ).values
    if "retrofit_situation_source" not in buildings.columns:
        buildings["retrofit_situation_source"] = None
    buildings["retrofit_situation_source"] = info_df["retrofit situation source"].values

    # 8) persist for later sections
    st.session_state["_last_buildings_gdf"] = buildings
    st.session_state["buildings_gdf"] = buildings
    st.session_state["_preloaded_buildings_gdf"] = buildings.copy()
    st.session_state["_preloaded_drawn_polygons"] = list(st.session_state["drawn_polygons"])
    st.session_state["_osm_preloaded"] = True

    # convenience variables used later
    bounds = combined_polygon.bounds
    sw = [bounds[1], bounds[0]];
    ne = [bounds[3], bounds[2]]
    fit_bounds = [sw, ne]
    excluded = set(map(str, st.session_state.get("excluded_bidx", set())))

    st.markdown("---")
    st.markdown("### Check the Buildings Found on the Map")
    st.info(
        "In this step, you can check whether the buildings found by the tool really belong to your "
        "project. Click a building on the map to exclude it from the space-heating and DHW demand estimation. Click it "
        "again to include it. If buildings are missing or the attributes are incorrect, you can add "
        "buildings or correct building attributes in the next step."
    )
    st.write("#### Buildings in Selected Area")

    # Overview map with include/exclude (uses stable bidx)
    m_buildings = folium.Map(
        location=[(sw[0] + ne[0]) / 2, (sw[1] + ne[1]) / 2],
        zoom_start=14, control_scale=True, prefer_canvas=True,
    )
    m_buildings.fit_bounds(fit_bounds)

    # Buildings with tooltip = stable bidx
    for i, row in buildings.iterrows():
        geom = row.geometry
        bidx_val = row["bidx"]
        is_excluded = (str(bidx_val) in set(map(str, excluded)))
        color = "#666666" if is_excluded else "#0078ff"
        gj = folium.GeoJson(
            data=mapping(geom),
            style_function=lambda x, color=color, is_excluded=is_excluded: {
                "fillColor": color,
                "color": color,
                "weight": 1,
                "fillOpacity": 0.15 if is_excluded else 0.4,
            },
        )
        gj.add_child(folium.Tooltip(f"#{bidx_val}", sticky=True))
        gj.add_to(m_buildings)

    res_overview = streamlit_folium.st_folium(
        m_buildings, width=700, height=500, returned_objects=["last_object_clicked"]
    )
    st.caption("Blue = included • Grey = excluded")

    clicked = (res_overview or {}).get("last_object_clicked")
    if clicked:
        try:
            lat, lon = clicked["lat"], clicked["lng"]
            pt_wgs = Point(lon, lat)

            # Fast path: vectorized contains in WGS84
            pt_utm = gpd.GeoSeries([pt_wgs], crs="EPSG:4326").to_crs(epsg=3857).iloc[0]
            bld_utm = buildings_utm if buildings_utm is not None else buildings.to_crs(epsg=3857)
            mask = bld_utm.geometry.contains(pt_utm)
            if mask.any():
                hit_idx = mask[mask].index[0]
            else:
                buildings_utm_local = buildings_utm
                if buildings_utm_local is None:
                    try:
                        buildings_utm_local = buildings.to_crs(epsg=3857)
                    except Exception:
                        buildings_utm_local = None
                if buildings_utm_local is not None:
                    dists = buildings_utm_local.geometry.distance(pt_utm)
                    hit_idx = dists.idxmin() if float(dists.min()) <= 8.0 else None
                else:
                    hit_idx = None

            if hit_idx is not None:
                hit_bidx = str(buildings.loc[hit_idx, "bidx"])
                if hit_bidx in excluded:
                    excluded.remove(hit_bidx)
                else:
                    excluded.add(hit_bidx)
                st.session_state["excluded_bidx"] = excluded
                st.session_state["_persist_dirty"] = True
                st.session_state["results_stale"] = True
                st.session_state.pop("total_heat_demand", None)
                if st.session_state.get("show_dhw_results"):
                    st.session_state["dhw_results_stale"] = True
                # st.session_state.pop("building_demand_estimates", None)
                st.rerun()
        except Exception:
            pass

    if excluded:
        st.markdown(
            f"""
            <div style="
                background: #f3f4f6;
                color: #374151;
                border: 1px solid #e5e7eb;
                border-radius: 6px;
                padding: 0.55rem 0.75rem;
                margin: 0.35rem 0 0.6rem 0;
                font-size: 0.95rem;
            ">
                Excluded buildings: <strong>{len(excluded)}</strong>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Clear all exclusions"):
            st.session_state["excluded_bidx"] = set()
            st.session_state["_persist_dirty"] = True
            st.session_state["results_stale"] = True
            st.session_state.pop("total_heat_demand", None)
            if st.session_state.get("show_dhw_results"):
                st.session_state["dhw_results_stale"] = True
            # st.session_state.pop("building_demand_estimates", None)
            # st.session_state.pop("info_overridden", None)
            st.rerun()

    # -------------------- OSM-based info table (filtered view) --------------------
    num_buildings = len(info_df)
    st.markdown(f"**🏠 Total number of buildings in selected area:** {num_buildings}")

    if "show_zensus_details_widget" not in st.session_state:
        st.session_state["show_zensus_details_widget"] = bool(
            st.session_state.get("show_zensus_details", False)
        )
    show_zensus_details = bool(st.session_state.get("show_zensus_details_widget", False))
    st.session_state["show_zensus_details"] = show_zensus_details

    n_excl = len(excluded)
    n_incl = num_buildings - n_excl

    # Included-only (used for counts or other logic)
    info_df_included = info_df[~info_df["bidx"].isin(excluded)].copy()
    order = pd.to_numeric(info_df_included["bidx"], errors="coerce")
    info_df_included = (
        info_df_included
        .assign(__order__=order)
        .sort_values("__order__")
        .drop(columns="__order__")
        .reset_index(drop=True)
    )

    # All rows + status, sorted by bidx (numeric)
    info_df_all = info_df.copy()
    info_df_all["bidx"] = info_df_all["bidx"].astype(str)
    edited_demand_bidx = _edited_demand_set()
    info_df_all, show_status_info = add_status_col(
        info_df_all, bidx_col="bidx",
        extra_labels={"edited (demand)": edited_demand_bidx}
    )

    show_zensus_details = st.checkbox(
        "Show sources and Zensus details in the building information table",
        key="show_zensus_details_widget",
    )
    st.session_state["show_zensus_details"] = show_zensus_details

    with st.expander("Building Information Table", expanded=True):
        display_df = info_df_all.drop(columns=["bidx", "osm_id", "osmid"], errors="ignore").copy()
        display_df.drop(
            columns=["renovation probability (%)", "retrofit situation source"],
            inplace=True,
            errors="ignore",
        )
        zensus_detail_cols = [
            "building type source", "year source",
            "zensus age class", "zensus type class",
            "zensus type note", "zensus year note", "zensus source",
        ]
        if not show_zensus_details:
            display_df.drop(columns=zensus_detail_cols, inplace=True, errors="ignore")

        if "building type" in display_df.columns:
            display_df["building type"] = display_df["building type"].replace({"": None, "none": None}).astype("object")
        if "address" in display_df.columns:
            display_df["address"] = display_df["address"].replace(_REPLACE_MISSING).astype("object")
        if "name" in display_df.columns:
            name_has = display_df["name"].apply(_has_letters_safe)
            display_df.loc[~name_has, "name"] = None
            display_df["name"] = display_df["name"].astype("object")

        def _all_empty(s: pd.Series) -> bool:
            if s.dtype == object:
                return s.fillna("").astype(str).str.strip().eq("").all()
            return s.isna().all()

        empty_cols = [c for c in display_df.columns if _all_empty(display_df[c])]
        display_df.drop(columns=empty_cols, inplace=True)

        # choose preferred order, but only keep what exists
        preferred = [
            "building index", "address", "building type", "area (m²)", "year built",
            "levels", "height (m)", "retrofit situation", "status",
        ]
        if show_zensus_details:
            preferred = [
                "building index", "address", "building type", "area (m²)", "year built",
                "levels", "height (m)", "retrofit situation",
                "building type source", "year source", "zensus age class", "zensus type class",
                "zensus type note", "zensus year note", "status",
            ]
        cols = [c for c in preferred if c in display_df.columns]
        display_df = display_df[cols]

        order_idx = (info_df_all["bidx"].astype(int) if info_df_all["bidx"].astype(str).str.isnumeric().all()
                     else pd.to_numeric(info_df_all["bidx"], errors="coerce"))
        display_df["__order__"] = order_idx.values
        display_df = display_df.sort_values("__order__").drop(columns="__order__").reset_index(drop=True)

        number_formats = {c: "{:,.2f}" for c in display_df.select_dtypes("number").columns}
        if "year built" in number_formats:
            number_formats["year built"] = "{:.0f}"

        st.dataframe(
            display_df.style
            .format(number_formats)
            .apply(lambda row: [
                "opacity: 0.6" if ("status" in display_df.columns and "excluded" in str(row.get("status", ""))) else ""
                for _ in row.index
            ], axis=1),
            use_container_width=True,
            hide_index=True,
        )

        if show_status_info:
            st.caption(status_note())

    if osm_fetch_started:
        osm_fetch_status.empty()


    # --- 📍 Highlight building on map ---
    st.checkbox(
        "🔎 Find and highlight a building",
        key="hl_show",
        help="Cannot find a building? Select the building to highlight it on the map.",
    )

    if st.session_state["hl_show"]:
        with st.container(border=True):
            st.markdown("#### 📍 Highlight building on map")
            with st.container():
                scope = st.radio("Selection list shows", ["Included only", "All buildings"], horizontal=True,
                                 key="hl_scope_radio")

                picker_source = info_df_included.copy() if scope == "Included only" else info_df.copy()

                q = st.text_input("(Optional) Filter buildings (id/type...)", value="", key="hl_filter")
                if q.strip():
                    qlow = q.strip().lower()
                    qmatch = qlow.replace(" ", "_").replace("-", "_")
                    qnum = qmatch
                    if qnum.startswith("building_"):
                        qnum = qnum.split("building_", 1)[1]
                    qnum = qnum.lstrip("#")
                    qnum_norm = str(int(qnum)) if qnum.isdigit() else None

                    def _bidx_search_terms(v):
                        raw = "" if v is None else str(v).strip()
                        terms = {raw.lower(), f"building_{raw.lower()}"}
                        try:
                            intval = int(raw)
                            terms.update({
                                str(intval),
                                f"{intval:0{_pad_width}d}",
                                f"building_{intval:0{_pad_width}d}",
                            })
                        except Exception:
                            pass
                        return terms

                    def _row_match(r):
                        parts = list(_bidx_search_terms(r.get("bidx", "")))
                        if "building type" in picker_source.columns:
                            parts.append(str(r.get("building type", "")))
                        if "name" in picker_source.columns:
                            parts.append(str(r.get("name", "")))
                        if qnum_norm and qnum_norm in _bidx_search_terms(r.get("bidx", "")):
                            return True
                        return any(qlow in p.lower() or qmatch in p.lower() for p in parts)

                    picker_source = picker_source[picker_source.apply(_row_match, axis=1)]

                for col in ["name", "building type"]:
                    if col in picker_source.columns:
                        picker_source[col] = picker_source[col].fillna("")

                def display_name(row) -> str:
                    nm = row.get("name", "")
                    if _has_letters_safe(nm): return nm.strip()
                    bt = row.get("building type", "").strip()
                    return bt if bt else "Unnamed"

                labels = {}
                for _, r in picker_source.iterrows():
                    idx = r.get("bidx")
                    if idx is None: continue
                    idx_key = str(idx)
                    title = display_name(r)
                    area_txt = ""
                    if "area (m²)" in picker_source.columns:
                        try:
                            area_val = float(r["area (m²)"]);
                            area_txt = f" • {area_val:,.2f} m²"
                        except Exception:
                            pass

                    def _fmt_bidx(s: str) -> str:
                        try:
                            return f"{int(s):0{_pad_width}d}"
                        except Exception:
                            return s

                    labels[idx_key] = f"#{_fmt_bidx(idx_key)} — {title}{area_txt}"

                def _bidx_key(s):
                    try:
                        return int(s)
                    except:
                        return float("inf")

                options = sorted([str(x) for x in picker_source["bidx"].tolist()], key=_bidx_key)
                selected_bidx = st.selectbox(
                    "Choose a building to highlight (map will update below)",
                    options=options,
                    format_func=lambda i: labels.get(i, f"#{i}"),
                    key="building_selector",
                )

                zoom_to_selected = st.checkbox("Zoom to selected building", value=True, key="hl_zoom_checkbox")

                # Render highlight map
                base_center = [(sw[0] + ne[0]) / 2, (sw[1] + ne[1]) / 2]
                m2 = folium.Map(location=base_center, zoom_start=14, control_scale=True, prefer_canvas=True)

                sel_geom = None
                if zoom_to_selected and options and selected_bidx:
                    match = buildings.loc[buildings["bidx"].astype(str) == str(selected_bidx), "geometry"]
                    if not match.empty:
                        sel_geom = match.iloc[0]
                        # normalize to largest polygon
                        if sel_geom.geom_type == "MultiPolygon":
                            sel_geom = max(sel_geom.geoms, key=lambda g: g.area)
                        minx, miny, maxx, maxy = sel_geom.bounds
                        m2.fit_bounds([[miny, minx], [maxy, maxx]])
                    else:
                        m2.fit_bounds([sw, ne])
                else:
                    m2.fit_bounds([sw, ne])

                for poly in polygons:
                    folium.GeoJson(
                        data=mapping(poly),
                        style_function=lambda x: {"fillColor": "#ff7800", "color": "black", "weight": 1, "fillOpacity": 0.3},
                    ).add_to(m2)

                for _, r in buildings.iterrows():
                    geom = r.geometry
                    bidx_val = r["bidx"]
                    is_selected = (str(bidx_val) == str(selected_bidx))
                    is_excluded = (str(bidx_val) in excluded)
                    color = "#ff0000" if is_selected else ("#666666" if is_excluded else "#0078ff")
                    folium.GeoJson(
                        data=mapping(geom),
                        style_function=lambda x, color=color, is_selected=is_selected: {
                            "fillColor": color, "color": color, "weight": 2 if is_selected else 1,
                            "fillOpacity": 0.5 if is_selected else 0.3,
                        },
                    ).add_to(m2)

                streamlit_folium.st_folium(m2, width=700, height=500, key="highlight_map", returned_objects=[])
                st.caption("Blue = included • Red = highlighted")

    zensus_summary = st.session_state.get("zensus_summary")
    if isinstance(zensus_summary, pd.DataFrame) and not zensus_summary.empty:
        with st.expander("Additional information: data sources and Zensus assumptions", expanded=False):
            st.markdown(
                """
                The tool first reads building footprints and available attributes from OpenStreetMap.
                If residential building type or construction year is missing, Zensus 2022 grid data can
                be used as an assumption. The Zensus source data are official aggregated 100 m grid-cell
                counts from Destatis; the assignment of those aggregated counts to individual buildings is
                performed by this tool. Values from OpenStreetMap and user edits are kept first.
                """
            )
            zensus_warnings = st.session_state.get("zensus_warnings", [])
            if zensus_warnings:
                st.warning(" ".join(str(w) for w in zensus_warnings))
            st.markdown(
                """
                Zensus 2022 grid data are used only as assumptions for missing residential building attributes.
                The counts for building-age classes and residential building-type classes come from Zensus grid files.
                Missing residential building types are distributed to match the local Zensus building-type shares as
                closely as possible. Missing construction years are assigned from the Zensus age-class shares. When
                several buildings are assigned to the same Zensus age range, the years are spread across that range
                instead of using only the midpoint year, which reduces bias around TEASER construction-year thresholds.

                A building can remain without a Zensus-filled type or year when its OSM type is non-residential,
                zero-demand, outside the residential Zensus dataset, or when the selected grid cells contain no
                usable category counts because values are unavailable or suppressed.
                """
            )
            zshow = zensus_summary.copy()
            if "share" in zshow.columns:
                zshow["share"] = pd.to_numeric(zshow["share"], errors="coerce") * 100.0
            zcols = [c for c in ["topic", "label", "count", "share", "grid_cells", "source"] if c in zshow.columns]
            st.dataframe(
                zshow[zcols].style.format({"count": "{:,.0f}", "share": "{:.1f}%"}),
                use_container_width=True,
                hide_index=True,
            )
            st.caption("Zensus values are used only where OSM/user data are missing; user edits remain authoritative.")
    else:
        with st.expander("Additional information: data sources", expanded=False):
            st.markdown(
                """
                The tool first reads building footprints and available attributes from OpenStreetMap.
                If residential building type or construction year is missing and usable Zensus data are
                available for the selected area, those Zensus grid-cell distributions can be used as assumptions.
                The Zensus source data are official aggregated 100 m grid-cell counts from Destatis; the assignment
                of those aggregated counts to individual buildings is performed by this tool. Values from
                OpenStreetMap and user edits are kept first.
                """
            )
            zensus_warnings = st.session_state.get("zensus_warnings", [])
            if zensus_warnings:
                st.warning(" ".join(str(w) for w in zensus_warnings))
            else:
                st.caption("No Zensus summary is available for the current selection.")

    with st.expander("Additional information: retrofit assumptions", expanded=False):
        st.markdown(
            """
            Retrofit situation here is an assumption for district-scale planning, not an observed building
            attribute from OSM or Zensus. The tool estimates a renovation probability from the final building
            type and construction year. Older buildings receive higher probabilities, newer buildings lower
            probabilities. User edits in the building-attribute table override the automatic assumption.
            """
        )

        retrofit_prob_df = (
            info_df_all.copy()
            if isinstance(info_df_all, pd.DataFrame) and not info_df_all.empty
            else info_df.copy()
        )
        if isinstance(retrofit_prob_df, pd.DataFrame) and not retrofit_prob_df.empty:
            retrofit_prob_df = _add_retrofit_defaults(retrofit_prob_df)
            if "renovation probability (%)" in retrofit_prob_df.columns:
                def _retrofit_probability_split(row):
                    any_prob_pct = pd.to_numeric(row.get("renovation probability (%)"), errors="coerce")
                    if pd.isna(any_prob_pct):
                        return pd.Series({
                            "any retrofit probability (%)": np.nan,
                            "standard probability (%)": np.nan,
                            "retrofit probability (%)": np.nan,
                            "advanced retrofit probability (%)": np.nan,
                        })
                    any_prob = float(np.clip(any_prob_pct / 100.0, 0.0, 1.0))
                    advanced_prob = _advanced_retrofit_probability(
                        row.get("building type"),
                        row.get("year built"),
                        any_prob,
                    )
                    retrofit_prob = max(any_prob - advanced_prob, 0.0)
                    standard_prob = max(1.0 - any_prob, 0.0)
                    return pd.Series({
                        "any retrofit probability (%)": any_prob * 100.0,
                        "standard probability (%)": standard_prob * 100.0,
                        "retrofit probability (%)": retrofit_prob * 100.0,
                        "advanced retrofit probability (%)": advanced_prob * 100.0,
                    })

                split_cols = retrofit_prob_df.apply(_retrofit_probability_split, axis=1)
                for col in split_cols.columns:
                    retrofit_prob_df[col] = split_cols[col]
            if "building index" not in retrofit_prob_df.columns and "bidx" in retrofit_prob_df.columns:
                retrofit_prob_df["building index"] = retrofit_prob_df["bidx"].astype(str).map(_fmt_idx)
            prob_cols = [
                c for c in [
                    "building index", "building type", "year built",
                    "retrofit situation", "any retrofit probability (%)",
                    "standard probability (%)", "retrofit probability (%)",
                    "advanced retrofit probability (%)",
                ]
                if c in retrofit_prob_df.columns
            ]
            retrofit_prob_view = retrofit_prob_df[prob_cols + (["bidx"] if "bidx" in retrofit_prob_df.columns else [])].copy()
            if "bidx" in retrofit_prob_view.columns:
                retrofit_prob_view["__order__"] = pd.to_numeric(retrofit_prob_view["bidx"], errors="coerce")
                retrofit_prob_view = (
                    retrofit_prob_view
                    .sort_values(["__order__", "bidx"], na_position="last")
                    .drop(columns=["__order__", "bidx"], errors="ignore")
                )
            if "year built" in retrofit_prob_view.columns:
                retrofit_prob_view["year built"] = pd.to_numeric(retrofit_prob_view["year built"], errors="coerce")
            if "renovation probability (%)" in retrofit_prob_view.columns:
                retrofit_prob_view["renovation probability (%)"] = pd.to_numeric(
                    retrofit_prob_view["renovation probability (%)"],
                    errors="coerce",
                )
            for col in [
                "any retrofit probability (%)",
                "standard probability (%)",
                "retrofit probability (%)",
                "advanced retrofit probability (%)",
            ]:
                if col in retrofit_prob_view.columns:
                    retrofit_prob_view[col] = pd.to_numeric(retrofit_prob_view[col], errors="coerce")
            retrofit_prob_formats = {}
            if "year built" in retrofit_prob_view.columns:
                retrofit_prob_formats["year built"] = "{:.0f}"
            for col in [
                "renovation probability (%)",
                "any retrofit probability (%)",
                "standard probability (%)",
                "retrofit probability (%)",
                "advanced retrofit probability (%)",
            ]:
                if col in retrofit_prob_view.columns:
                    retrofit_prob_formats[col] = "{:.1f}"

            st.markdown("**Used renovation probabilities for the current buildings**")
            st.dataframe(
                retrofit_prob_view.style.format(retrofit_prob_formats),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No building-level retrofit probability table is available yet.")

        st.markdown(
            """
            **Current probability process in the tool:** The probabilities shown above are tool-defined heuristic
            defaults. They are not imported from an external dataset and they are not observed building properties.
            The current logic assumes that older buildings are more likely to have had some retrofit and newer
            buildings are less likely to have had retrofit. Broad building-type adjustments then increase or
            decrease that base probability.

            1. Start with a base probability from the construction-year class.
            2. Adjust it by broad building type.
            3. Clip the result to a plausible range between 0% and 85%.
            4. Use a stable building-specific draw to assign `standard`, `retrofit`, or `advanced retrofit` when
               the user has not edited the retrofit situation.
            """
        )

        retrofit_base_prob_df = pd.DataFrame(
            [
                {"construction year": "up to 1918", "base probability": 0.64},
                {"construction year": "1919-1948", "base probability": 0.60},
                {"construction year": "1949-1978", "base probability": 0.54},
                {"construction year": "1979-1994", "base probability": 0.36},
                {"construction year": "1995-2009", "base probability": 0.20},
                {"construction year": "2010-2015", "base probability": 0.08},
                {"construction year": "from 2016", "base probability": 0.03},
            ]
        )
        retrofit_adjustment_df = pd.DataFrame(
            [
                {"building group": "apartments, residential, dormitory", "adjustment": 0.05},
                {"building group": "house, detached, bungalow, semidetached", "adjustment": -0.03},
                {"building group": "non-residential buildings", "adjustment": -0.06},
                {"building group": "school, university, college, kindergarten, hospital", "adjustment": 0.05},
                {"building group": "unknown or other building type", "adjustment": -0.08},
            ]
        )
        st.markdown("**Heuristic probability rules used by the tool**")
        st.dataframe(
            retrofit_base_prob_df.style.format({"base probability": "{:.0%}"}),
            use_container_width=True,
            hide_index=True,
        )
        st.dataframe(
            retrofit_adjustment_df.style.format({"adjustment": "{:+.0%}"}),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "Type adjustments are added to the construction-year base probability. For example, schools first "
            "receive the general non-residential adjustment and then the public-building adjustment."
        )

        retrofit_definition_df = pd.DataFrame(
            [
                {
                    "retrofit situation": "standard",
                    "meaning": "No assigned retrofit, unknown current standard, or building kept at the base archetype.",
                    "automatic assignment probability": "any retrofit probability",
                    "residential TEASER input": "tabula_de_standard",
                },
                {
                    "retrofit situation": "retrofit",
                    "meaning": "Typical or partial retrofit assumption.",
                    "automatic assignment probability": "any retrofit probability  x  advanced retrofit probability",
                    "residential TEASER input": "tabula_de_retrofit",
                },
                {
                    "retrofit situation": "advanced retrofit",
                    "meaning": "Deeper retrofit assumption.",
                    "automatic assignment probability": "any retrofit probability  x  advanced-retrofit share",
                    "residential TEASER input": "tabula_de_adv_retrofit",
                },
            ]
        )
        advanced_share_df = pd.DataFrame(
            [
                {
                    "construction year": "up to 1978",
                    "residential advanced share of any retrofit": 0.18,
                    "non-residential advanced share of any retrofit": 0.18 * 0.75,
                },
                {
                    "construction year": "1979-1994",
                    "residential advanced share of any retrofit": 0.12,
                    "non-residential advanced share of any retrofit": 0.12 * 0.75,
                },
                {
                    "construction year": "from 1995",
                    "residential advanced share of any retrofit": 0.06,
                    "non-residential advanced share of any retrofit": 0.06 * 0.75,
                },
            ]
        )
        st.markdown("**Definition of retrofit situations and assignment probabilities**")
        st.dataframe(
            retrofit_definition_df,
            use_container_width=True,
            hide_index=True,
        )
        st.markdown("**Advanced-retrofit share inside the any-retrofit probability**")
        st.dataframe(
            advanced_share_df.style.format({
                "residential advanced share of any retrofit": "{:.1%}",
                "non-residential advanced share of any retrofit": "{:.1%}",
            }),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "`Any retrofit probability` means the probability of either `retrofit` or `advanced retrofit`. "
            "The automatic probabilities are therefore: `standard = 1 - any retrofit`, "
            "`advanced retrofit = any retrofit x advanced share`, and "
            "`retrofit = any retrofit - advanced retrofit`."
        )
        st.markdown(
            """
            **Validation status:** These probability values are assumptions and are not validated automatically.
            They can be checked or calibrated by comparing the resulting retrofit shares by construction-year class
            and building type with an independent reference dataset, local renovation records, or measured district
            heat/gas consumption. Without such calibration data, the safest interpretation is scenario screening
            rather than building-level evidence.
            """
        )

        st.markdown(
            """
            **How probability becomes a building assumption:** The probability is used internally only when no
            user-edited retrofit situation is available. The assignment is deterministic from the building id,
            building type and construction year, so the same project does not change randomly on rerun. User edits
            override this automatic assumption.

            **Retrofit standards used later in TEASER:**
            - `standard`: no retrofit or unknown current standard. Residential TEASER buildings use the German
              TABULA standard construction data.
            - `retrofit`: typical or partial retrofit. Residential TEASER buildings use `tabula_de_retrofit`.
            - `advanced retrofit`: deeper retrofit assumption. Residential TEASER buildings use
              `tabula_de_adv_retrofit`.

            These residential retrofit levels follow the German TABULA/EPISCOPE archetype logic used through
            TEASER. For non-residential buildings, the current TEASER archetype path does not expose
            equivalent TABULA retrofit variants in the same way, so the tool applies a simple demand-reduction
            factor after the base TEASER estimate. These factors are scenario defaults and should be replaced or
            calibrated when project-specific non-residential renovation data are available.
            """
        )

        nonres_factor_df = pd.DataFrame(
            [
                {
                    "retrofit situation": situation,
                    "non-residential demand factor": factor,
                    "demand reduction": 1.0 - factor,
                }
                for situation, factor in RETROFIT_NONRES_DEMAND_FACTORS.items()
            ]
        )
        st.markdown("**Non-residential demand-reduction factors**")
        st.dataframe(
            nonres_factor_df.style.format(
                {
                    "non-residential demand factor": "{:.2f}",
                    "demand reduction": "{:.0%}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

    # ---------------- route length (only OSM included) ----------------
    buildings["gross_floor_area"] = buildings["area_m2"] * buildings["levels"]
    gross_included = buildings.loc[~buildings["bidx"].isin(excluded), "gross_floor_area"].sum()
    total_building_floor_area = gross_included

    gdf = gpd.GeoDataFrame(geometry=[combined_polygon], crs="EPSG:4326")
    centroid = gdf.geometry.iloc[0].centroid
    utm_zone = int((centroid.x + 180) / 6) + 1
    is_northern = centroid.y >= 0
    utm_crs = f"+proj=utm +zone={utm_zone} +{'north' if is_northern else 'south'} +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
    gdf_utm = gdf.to_crs(utm_crs)
    total_selected_ground_area = gdf_utm.geometry.iloc[0].area
    plot_ratio = (total_building_floor_area / total_selected_ground_area) if total_selected_ground_area > 0 else 0
    route_length = 16.171 * (plot_ratio ** 0.1495) * 1000 * total_selected_ground_area / 1_000_000  # [m]

    st.session_state["_last_buildings_gdf"] = buildings

    with st.expander("Additional information: pipe route length estimate", expanded=False):
        st.markdown(f"Estimated total route length for a centralized network: **{route_length:.0f} m**")
        st.caption(
            "This technical estimate is used later for district-heating network assumptions. "
            "It is not required for checking individual building demand."
        )
    st.session_state["route_length"] = route_length
    st.session_state["route_length_source"] = "building-demand area estimate"

    # ---------------- TEASER estimation ----------------
    st.markdown("---")
    st.markdown("### Correct Building Attributes If Needed")

    st.info(
        """
        In this step, you can edit the building attributes before estimating space heating and DHW demand. If the values
        look correct, you can leave this part closed. If values are missing or wrong, open the editor,
        correct them, and click **Save building attributes**.
        """
    )

    # ---------------- attributes editor (with ability to add new buildings) ----------------
    with st.expander("Edit building attributes before demand estimation", expanded=False):
        if "show_attr_zensus_details_widget" not in st.session_state:
            st.session_state["show_attr_zensus_details_widget"] = bool(
                st.session_state.get(
                    "show_attr_zensus_details",
                    st.session_state.get("show_zensus_details", False),
                )
            )
        show_attr_zensus_details = st.checkbox(
            "Show sources and Zensus details in edit table",
            key="show_attr_zensus_details_widget",
            help="Shows source columns and Zensus classes/notes used to fill missing type or construction-year data.",
        )
        st.session_state["show_attr_zensus_details"] = show_attr_zensus_details

        attrib_df = info_df.copy()
        attrib_df["bidx"] = attrib_df["bidx"].astype(str)
        attrib_df["__order__"] = pd.to_numeric(attrib_df["bidx"], errors="coerce")
        attrib_df = (
            attrib_df
            .sort_values("__order__")
            .drop(columns="__order__")
            .reset_index(drop=True)
        )

        edited_demand_bidx = _edited_demand_set()
        show_status_attr = False

        existing_types = set()

        def _collect(col):
            if col is None:
                return
            try:
                vals = (
                    pd.Series(col)
                    .dropna()
                    .astype(str)
                    .str.strip()
                    .str.lower()
                )
                existing_types.update([v for v in vals if v])
            except Exception:
                pass

        _collect(info_df.get("building type"))
        _collect(st.session_state.get("building_attrs", pd.DataFrame()).get("building type"))
        _collect(st.session_state.get("manual_buildings", pd.DataFrame()).get("building type"))

        # apply previously saved edits for OSM items
        if "building_attrs" in st.session_state and not st.session_state["building_attrs"].empty:
            saved = st.session_state["building_attrs"].copy()
            if "building type" in saved.columns:
                saved["building type"] = saved["building type"].map(norm_btype)

            # bring back address too
            merge_cols = ["bidx", "building type", "area (m²)", "levels", "height (m)", "year built", "retrofit situation"]
            if "address" in saved.columns:
                merge_cols.append("address")
            merge_cols = [c for c in merge_cols if c in saved.columns]

            attrib_df = attrib_df.merge(
                saved[merge_cols],
                on="bidx", how="left", suffixes=("", "_saved")
            )

            for col in ["building type", "area (m²)", "levels", "height (m)", "year built", "retrofit situation"]:
                if f"{col}_saved" in attrib_df.columns:
                    attrib_df[col] = attrib_df[f"{col}_saved"].combine_first(attrib_df[col])
                    attrib_df.drop(columns=[f"{col}_saved"], inplace=True)

            # address: prefer saved non-empty string; otherwise keep base
            if "address_saved" in attrib_df.columns:
                def _pick_addr(row):
                    s = row.get("address_saved")
                    if s is None or pd.isna(s):
                        return row.get("address")
                    s = str(s).strip()
                    return row.get("address") if s == "" else s

                attrib_df["address"] = attrib_df.apply(_pick_addr, axis=1)
                attrib_df.drop(columns=["address_saved"], inplace=True, errors="ignore")

        def _next_free_bidx_for_manual():
            used = set(attrib_df["bidx"].astype(str))
            mb_existing = st.session_state.get("manual_buildings", pd.DataFrame())
            if isinstance(mb_existing, pd.DataFrame) and not mb_existing.empty and "bidx" in mb_existing.columns:
                used |= set(mb_existing["bidx"].astype(str))
            next_id_local = int(st.session_state.get("next_bidx", 0) or 0)
            numeric_used = pd.to_numeric(pd.Series(list(used)), errors="coerce")
            if numeric_used.notna().any():
                next_id_local = max(next_id_local, int(numeric_used.max()) + 1)
            while str(next_id_local) in used:
                next_id_local += 1
            return next_id_local

        def _reclaim_manual_bidx(removed_bidx):
            removed = {str(x) for x in (removed_bidx or []) if str(x).strip()}
            nums = []
            for b in removed:
                try:
                    nums.append(int(b))
                except Exception:
                    pass
            if nums:
                current_next = int(st.session_state.get("next_bidx", 0) or 0)
                st.session_state["next_bidx"] = min(current_next, min(nums))
            draft_ids = set(map(str, st.session_state.get("_draft_manual_bidx", set())))
            if draft_ids:
                st.session_state["_draft_manual_bidx"] = draft_ids - removed
            keep_ids = set(map(str, st.session_state.get("_manual_missing_area_keep", set())))
            if keep_ids:
                st.session_state["_manual_missing_area_keep"] = keep_ids - removed

        def _manual_bidx_missing_required_area():
            mb_now = st.session_state.get("manual_buildings", pd.DataFrame())
            if not isinstance(mb_now, pd.DataFrame) or mb_now.empty or "bidx" not in mb_now.columns:
                return []
            mb_now = _normalize_manual_buildings_df(mb_now)
            if mb_now.empty:
                return []
            has_info = (
                mb_now["name"].apply(_has_letters_safe)
                | mb_now["building type"].notna()
                | pd.to_numeric(mb_now["year built"], errors="coerce").notna()
            )
            missing_area = pd.to_numeric(mb_now["area (m²)"], errors="coerce").isna()
            vals = mb_now.loc[has_info & missing_area, "bidx"].astype(str).tolist()
            return sorted(vals, key=lambda x: int(x) if str(x).isdigit() else str(x))

        fill_manual_from_zensus = st.checkbox(
            "Fill missing type/year for user-added buildings from Zensus when saving",
            value=True,
            key="fill_manual_missing_from_zensus",
        )
        pending_missing_area_state = [
            str(x) for x in st.session_state.get("_manual_missing_area_pending", [])
            if str(x).strip()
        ]
        actual_missing_area = _manual_bidx_missing_required_area()
        pending_missing_area = pending_missing_area_state or actual_missing_area
        pending_manual_notice = st.session_state.pop("_manual_save_notice", None)
        if pending_manual_notice and not pending_missing_area:
            st.info(pending_manual_notice)

        if pending_missing_area:
            labels = ", ".join(f"Building_{b}" for b in pending_missing_area)
            st.warning(
                f"Area is required for user-added building(s): {labels}. "
                "You can keep them for now and enter the area later, or remove them from the table."
            )
            c_keep_missing, c_remove_missing = st.columns([1, 1])
            with c_keep_missing:
                if st.button("Keep buildings for now", key="keep_manual_missing_area"):
                    keep_ids = set(map(str, st.session_state.get("_manual_missing_area_keep", set())))
                    keep_ids.update(pending_missing_area)
                    st.session_state["_manual_missing_area_keep"] = keep_ids
                    st.session_state.pop("_manual_missing_area_pending", None)
                    st.rerun()
            with c_remove_missing:
                if st.button("Remove buildings without area", key="remove_manual_missing_area"):
                    mb_keep = st.session_state.get("manual_buildings", pd.DataFrame()).copy()
                    if isinstance(mb_keep, pd.DataFrame) and not mb_keep.empty and "bidx" in mb_keep.columns:
                        mb_keep["bidx"] = mb_keep["bidx"].astype(str)
                        mb_keep = mb_keep.loc[~mb_keep["bidx"].isin(pending_missing_area)].copy()
                        st.session_state["manual_buildings"] = mb_keep.reset_index(drop=True)
                    _reclaim_manual_bidx(pending_missing_area)
                    st.session_state.pop("_manual_missing_area_pending", None)
                    st.session_state["_manual_save_notice"] = (
                        "Removed user-added building(s) without area. The released building index can be reused."
                    )
                    st.rerun()

        if st.button("➕ Add user-defined building", key="add_manual_building_row"):
            new_bidx = str(_next_free_bidx_for_manual())
            mb = st.session_state.get(
                "manual_buildings",
                pd.DataFrame(columns=[
                    "bidx", "name", "building type", "area (m²)", "levels", "height (m)", "year built", "retrofit situation",
                    "building type source", "year source", "zensus age class", "zensus type class",
                    "zensus type note", "zensus year note", "zensus source",
                ])
            ).copy()
            if "bidx" not in mb.columns or not mb["bidx"].astype(str).eq(new_bidx).any():
                mb = pd.concat([
                    mb,
                    pd.DataFrame([{
                        "bidx": new_bidx,
                        "name": "",
                        "building type": None,
                        "area (m²)": np.nan,
                        "levels": np.nan,
                        "height (m)": np.nan,
                        "year built": np.nan,
                        "retrofit situation": None,
                        "building type source": None,
                        "year source": None,
                        "zensus age class": None,
                        "zensus type class": None,
                        "zensus type note": None,
                        "zensus year note": None,
                        "zensus source": None,
                    }])
                ], ignore_index=True)
                st.session_state["manual_buildings"] = mb
                st.session_state["next_bidx"] = int(new_bidx) + 1 if new_bidx.isdigit() else st.session_state.get("next_bidx", 0)
                draft_ids = set(map(str, st.session_state.get("_draft_manual_bidx", set())))
                draft_ids.add(new_bidx)
                st.session_state["_draft_manual_bidx"] = draft_ids
                st.rerun()

        # append manual buildings to editable table
        manual = st.session_state["manual_buildings"].copy()
        if not manual.empty:
            try:
                _pad_width_attr = max(_pad_width, len(str(
                    manual["bidx"].astype(int).max()))) if not manual.empty else _pad_width
            except Exception:
                _pad_width_attr = _pad_width

            manual_view = pd.DataFrame({
                "building index": manual["bidx"].astype(int).map(lambda i: f"Building_{i:0{_pad_width_attr}d}"),
                "address": None,
                "name": manual["name"].where(manual["name"].apply(_has_letters_safe), None).astype("object"),
                "building type": manual["building type"].map(norm_btype),  # <- normalize here
                "area (m²)": manual["area (m²)"],
                "levels": pd.to_numeric(manual.get("levels", pd.Series([np.nan] * len(manual))), errors="coerce"),
                "height (m)": pd.to_numeric(manual.get("height (m)", pd.Series([np.nan] * len(manual))), errors="coerce"),
                "year built": pd.to_numeric(manual.get("year built", pd.Series([np.nan] * len(manual))), errors="coerce"),
                "retrofit situation": manual.get("retrofit situation", pd.Series([None] * len(manual))),
                "renovation probability (%)": np.nan,
                "building type source": manual.get("building type source", pd.Series(["manual"] * len(manual))),
                "year source": manual.get(
                    "year source",
                    pd.Series(
                        np.where(
                            pd.to_numeric(manual.get("year built", pd.Series([np.nan] * len(manual))), errors="coerce").notna(),
                            "manual",
                            None,
                        )
                    ),
                ),
                "zensus age class": manual.get("zensus age class", pd.Series([None] * len(manual))),
                "zensus type class": manual.get("zensus type class", pd.Series([None] * len(manual))),
                "zensus type note": manual.get("zensus type note", pd.Series([None] * len(manual))),
                "zensus year note": manual.get("zensus year note", pd.Series([None] * len(manual))),
                "zensus source": manual.get("zensus source", pd.Series([None] * len(manual))),
                "bidx": manual["bidx"].astype(str)
            })
            attrib_df = pd.concat([attrib_df, manual_view], ignore_index=True)

        attrib_df = _add_retrofit_defaults(attrib_df)
        attrib_df.drop(columns=["renovation probability (%)"], inplace=True, errors="ignore")

        manual_bidx_set = (
            set(manual["bidx"].astype(str))
            if isinstance(manual, pd.DataFrame) and not manual.empty and "bidx" in manual.columns
            else set()
        )
        attrib_df, show_status_attr = add_status_col(
            attrib_df,
            bidx_col="bidx",
            extra_labels={
                "edited (demand)": edited_demand_bidx,
                "user added": manual_bidx_set,
            },
        )

        attrib_cols = [
            "building index", "address", "building type", "area (m²)",
            "year built", "levels", "height (m)", "retrofit situation",
            "name", "building type source",
            "year source", "zensus age class", "zensus type class",
            "zensus type note", "zensus year note", "bidx"
        ]
        attrib_df = attrib_df[[c for c in attrib_cols if c in attrib_df.columns] +
                              [c for c in attrib_df.columns if c not in attrib_cols]]

        show_name_attr = ("name" in attrib_df.columns) and attrib_df["name"].apply(_has_letters_safe).any()
        if show_name_attr:
            mask_real = attrib_df["name"].apply(_has_letters_safe)
            attrib_df.loc[~mask_real, "name"] = None
            attrib_df["name"] = attrib_df["name"].astype("object")

        type_opts = (
                set(RESIDENTIAL_TYPES.keys()) |
                set(NON_RESIDENTIAL_TYPES.keys()) |
                set(ZERO_DEMAND_TYPES)
        )

        observed = set()
        observed |= set(info_df.get("building type", pd.Series(dtype=object)).dropna().map(norm_btype).dropna())
        ba = st.session_state.get("building_attrs", pd.DataFrame())
        if isinstance(ba, pd.DataFrame) and not ba.empty and "building type" in ba.columns:
            observed |= set(ba["building type"].dropna().map(norm_btype).dropna())
        mb = st.session_state.get("manual_buildings", pd.DataFrame())
        if isinstance(mb, pd.DataFrame) and not mb.empty and "building type" in mb.columns:
            observed |= set(mb["building type"].dropna().map(norm_btype).dropna())

        # Normalize the column in the table itself
        if "building type" in attrib_df.columns:
            attrib_df["building type"] = attrib_df["building type"].map(norm_btype)

        extra_vals = set(attrib_df.get("building type", pd.Series(dtype=object)).dropna().tolist())
        type_options = sorted(((type_opts | observed | extra_vals) - {None, ""}))

        col_cfg_attr = {
            "building type": st.column_config.SelectboxColumn("building type", options=type_options),
            "area (m²)": st.column_config.NumberColumn("area (m²)", min_value=0.0, step=1.0, format="%.2f"),
            "levels": st.column_config.NumberColumn(
                "levels", min_value=1, max_value=200, step=1, format="%.0f",
                help="Number of above-ground building levels. Edit this when the OSM value is missing or incorrect.",
            ),
            "height (m)": st.column_config.NumberColumn(
                "height (m)", min_value=0.1, max_value=1000.0, step=0.1, format="%.2f",
                help="Total building height. Values that imply an implausibly low storey height are replaced by the building-type default.",
            ),
            "year built": st.column_config.NumberColumn("year built", min_value=1800, max_value=2030, step=1, format="%.0f"),
            "retrofit situation": st.column_config.SelectboxColumn(
                "retrofit situation",
                options=RETROFIT_SITUATION_OPTIONS,
                help="Estimated from building type and construction year; edit if known.",
            ),
            "building index": st.column_config.TextColumn("building index"),
            "address": st.column_config.TextColumn("address"),
            "building type source": st.column_config.TextColumn("building type source", disabled=True),
            "year source": st.column_config.TextColumn("year source", disabled=True),
            "zensus age class": st.column_config.TextColumn("zensus age class", disabled=True),
            "zensus type class": st.column_config.TextColumn("zensus type class", disabled=True),
            "zensus type note": st.column_config.TextColumn("zensus type note", disabled=True),
            "zensus year note": st.column_config.TextColumn("zensus year note", disabled=True),
        }
        if show_name_attr:
            col_cfg_attr["name"] = st.column_config.TextColumn("name")
        if show_status_attr and "status" in attrib_df.columns:
            col_cfg_attr["status"] = st.column_config.TextColumn("status", disabled=True)

        attr_display_cols = [
            "building index", "address", "building type", "area (m²)", "year built",
            "levels", "height (m)", "retrofit situation",
        ]
        if show_attr_zensus_details:
            attr_display_cols += ["building type source", "year source"]
            for c in ["zensus age class", "zensus type class", "zensus type note", "zensus year note"]:
                if c in attrib_df.columns and not attrib_df[c].isna().all():
                    attr_display_cols.append(c)
        if show_status_attr and "status" in attrib_df.columns:
            attr_display_cols.append("status")
        attr_display_cols = [c for c in attr_display_cols if c in attrib_df.columns]

        if "address" in attrib_df.columns:
            attrib_df["address"] = attrib_df["address"].replace(_REPLACE_MISSING)
            attrib_df.loc[attrib_df["address"].isna(), "address"] = None
            attrib_df["address"] = attrib_df["address"].astype("object")

        with st.form("building_attributes_form", clear_on_submit=False):
            attrib_edited = st.data_editor(
                attrib_df.copy(),
                use_container_width=True,
                column_config=col_cfg_attr,
                column_order=attr_display_cols,
                num_rows="fixed",
                hide_index=True,
                key="building_attributes_editor",
            )
            save_attributes_clicked = st.form_submit_button("💾 Save building attributes")

        if show_status_attr:
            st.caption(status_note())

        if save_attributes_clicked:
            osm_row_mask = ~attrib_edited["bidx"].astype(str).isin(manual_bidx_set)
            manual_content_mask = (
                attrib_edited.get("name", pd.Series("", index=attrib_edited.index)).fillna("").astype(str).str.strip().ne("")
                | attrib_edited.get("building type", pd.Series("", index=attrib_edited.index)).fillna("").astype(str).str.strip().ne("")
                | pd.to_numeric(attrib_edited.get("area (m²)"), errors="coerce").notna()
                | pd.to_numeric(attrib_edited.get("year built"), errors="coerce").notna()
                | attrib_edited.get("retrofit situation", pd.Series("", index=attrib_edited.index)).fillna("").astype(str).str.strip().ne("")
            )
            normalize_geometry_mask = osm_row_mask | manual_content_mask
            normalized_geometry = attrib_edited.loc[normalize_geometry_mask].apply(
                lambda row: _building_geometry_values(
                    row.get("building type"), row.get("levels"), row.get("height (m)")
                ),
                axis=1,
            )
            attrib_edited.loc[normalize_geometry_mask, "levels"] = [values[0] for values in normalized_geometry]
            attrib_edited.loc[normalize_geometry_mask, "height (m)"] = [values[1] for values in normalized_geometry]
            braw = attrib_edited["bidx"]
            is_new = (
                    braw.isna()
                    | braw.astype(str).str.strip().eq("")
                    | braw.astype(str).str.lower().isin(["nan", "none"])
            )
            existing = attrib_edited[~is_new].copy()
            new_rows = attrib_edited[is_new].copy()

            # OSM baseline for diffing
            baseline_osm = info_df[[
                c for c in [
                    "bidx", "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"
                ]
                if c in info_df.columns
            ]].copy()
            baseline_osm["bidx"] = baseline_osm["bidx"].astype(str)
            for col in ["building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"]:
                if col not in baseline_osm.columns:
                    baseline_osm[col] = np.nan

            saved = st.session_state.get("building_attrs", pd.DataFrame())
            if isinstance(saved, pd.DataFrame) and not saved.empty:
                s = saved.copy()
                s["bidx"] = s["bidx"].astype(str)
                if "building type" in s.columns:
                    s["building type"] = s["building type"].map(norm_btype)
                if "year built" in s.columns:
                    s["year built"] = pd.to_numeric(s["year built"], errors="coerce")
                if "address" not in s.columns:
                    s["address"] = None

                def _clean_addr(v):
                    if v is None or pd.isna(v):
                        return None
                    s = str(v).strip()
                    if s == "" or s.lower() in {"nan", "none", "unknown"}:
                        return None
                    return s

                s["address"] = s["address"].apply(_clean_addr)

                s_merge_cols = [
                    c for c in ["bidx", "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"]
                    if c in s.columns
                ]
                baseline_osm = (
                    baseline_osm.merge(s[s_merge_cols],
                                       on="bidx", how="left", suffixes=("", "_ovr"))
                )

                if "building type_ovr" in baseline_osm.columns:
                    baseline_osm["building type"] = baseline_osm["building type_ovr"].combine_first(
                        baseline_osm["building type"])

                if "area (m²)_ovr" in baseline_osm.columns:
                    baseline_osm["area (m²)"] = pd.to_numeric(baseline_osm["area (m²)_ovr"], errors="coerce") \
                        .combine_first(pd.to_numeric(baseline_osm["area (m²)"], errors="coerce"))

                if "levels_ovr" in baseline_osm.columns:
                    baseline_osm["levels"] = pd.to_numeric(baseline_osm["levels_ovr"], errors="coerce") \
                        .combine_first(pd.to_numeric(baseline_osm["levels"], errors="coerce"))

                if "height (m)_ovr" in baseline_osm.columns:
                    baseline_osm["height (m)"] = pd.to_numeric(baseline_osm["height (m)_ovr"], errors="coerce") \
                        .combine_first(pd.to_numeric(baseline_osm["height (m)"], errors="coerce"))

                if "address_ovr" in baseline_osm.columns:
                    baseline_osm["address"] = baseline_osm["address_ovr"].combine_first(baseline_osm["address"])
                if "year built_ovr" in baseline_osm.columns:
                    baseline_osm["year built"] = pd.to_numeric(baseline_osm["year built_ovr"], errors="coerce") \
                        .combine_first(pd.to_numeric(baseline_osm["year built"], errors="coerce"))
                if "retrofit situation_ovr" in baseline_osm.columns:
                    baseline_osm["retrofit situation"] = baseline_osm["retrofit situation_ovr"].combine_first(
                        baseline_osm["retrofit situation"]
                    )

                baseline_osm.drop(columns=[c for c in [
                    "building type_ovr", "area (m²)_ovr", "levels_ovr", "height (m)_ovr", "address_ovr", "year built_ovr",
                    "retrofit situation_ovr"
                ]
                                           if c in baseline_osm.columns], inplace=True)
            osm_bidx_set = set(baseline_osm["bidx"])

            existing = existing.copy()
            existing["bidx"] = existing["bidx"].astype(str)

            existing_osm = existing[existing["bidx"].isin(osm_bidx_set)].copy()
            existing_manual = existing[~existing["bidx"].isin(osm_bidx_set)].copy()

            # -------- OSM edits: keep only truly changed rows (preserve old overrides) --------
            building_attrs = pd.DataFrame(
                columns=["bidx", "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"]
            )

            merged = pd.DataFrame()
            type_changed = pd.Series(dtype=bool)
            area_changed = pd.Series(dtype=bool)
            levels_changed = pd.Series(dtype=bool)
            height_changed = pd.Series(dtype=bool)
            addr_changed = pd.Series(dtype=bool)
            year_changed = pd.Series(dtype=bool)
            retrofit_changed = pd.Series(dtype=bool)

            if not existing_osm.empty:
                merged = existing_osm.merge(
                    baseline_osm, on="bidx", how="left", suffixes=("", "_base")
                )

                def _norm_type(x):
                    return norm_btype(x)

                def _num(x):
                    return pd.to_numeric(x, errors="coerce")

                def _clean_addr(v):
                    if v is None or pd.isna(v): return None
                    s = str(v).strip()
                    return s if s else None

                def _canon_addr_lower(v):
                    v = _clean_addr(v)
                    return v.lower() if isinstance(v, str) else v

                # Compare against canonicalized baseline (which already includes previous overrides)
                baseline_osm["address"] = baseline_osm["address"].map(_clean_addr)

                type_changed = merged.apply(
                    lambda r: _norm_type(r.get("building type")) != _norm_type(r.get("building type_base")),
                    axis=1
                )
                area_now = _num(merged["area (m²)"])
                area_base = _num(merged["area (m²)_base"])
                area_changed = ~((area_now.isna() & area_base.isna()) |
                                 (area_now.notna() & area_base.notna() & np.isclose(area_now, area_base,
                                                                                    rtol=0.0, atol=1e-6)))
                levels_now = _num(merged["levels"])
                levels_base = _num(merged["levels_base"])
                levels_changed = ~((levels_now.isna() & levels_base.isna()) |
                                   (levels_now.notna() & levels_base.notna() & np.isclose(
                                       levels_now, levels_base, rtol=0.0, atol=1e-6
                                   )))
                height_now = _num(merged["height (m)"])
                height_base = _num(merged["height (m)_base"])
                height_changed = ~((height_now.isna() & height_base.isna()) |
                                   (height_now.notna() & height_base.notna() & np.isclose(
                                       height_now, height_base, rtol=0.0, atol=1e-6
                                   )))
                addr_changed = (merged["address"].map(_canon_addr_lower) != merged["address_base"].map(
                    _canon_addr_lower))
                if "year built" in merged.columns and "year built_base" in merged.columns:
                    year_now = _num(merged["year built"])
                    year_base = _num(merged["year built_base"])
                    year_changed = ~((year_now.isna() & year_base.isna()) |
                                     (year_now.notna() & year_base.notna() & np.isclose(year_now, year_base,
                                                                                        rtol=0.0, atol=1e-6)))
                else:
                    year_changed = pd.Series(False, index=merged.index)
                if "retrofit situation" in merged.columns and "retrofit situation_base" in merged.columns:
                    retrofit_now = merged["retrofit situation"].apply(_normalize_retrofit_situation)
                    retrofit_base = merged["retrofit situation_base"].apply(_normalize_retrofit_situation)
                    retrofit_changed = retrofit_now.fillna("__na__") != retrofit_base.fillna("__na__")
                else:
                    retrofit_changed = pd.Series(False, index=merged.index)

                # Start from previous overrides (so unchanged columns are preserved)
                prev = st.session_state.get(
                    "building_attrs",
                    pd.DataFrame(columns=[
                        "bidx", "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"
                    ])
                ).copy()
                if not prev.empty:
                    prev["bidx"] = prev["bidx"].astype(str)
                    if "building type" in prev: prev["building type"] = prev["building type"].map(norm_btype)
                    if "area (m²)" in prev:     prev["area (m²)"] = pd.to_numeric(prev["area (m²)"],
                                                                                  errors="coerce")
                    if "address" in prev:       prev["address"] = prev["address"].apply(_clean_addr)
                    if "year built" in prev:    prev["year built"] = pd.to_numeric(prev["year built"], errors="coerce")
                    if "retrofit situation" in prev:
                        prev["retrofit situation"] = prev["retrofit situation"].apply(_normalize_retrofit_situation)
                prev = prev.drop_duplicates(subset=["bidx"], keep="last")
                for col in ["building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"]:
                    if col not in prev.columns:
                        prev[col] = None

                base = prev.set_index("bidx", drop=False)

                # Apply only the fields that changed in THIS save
                for pos, (_, r) in enumerate(merged.iterrows()):
                    b = str(r["bidx"])
                    if b not in base.index:
                        base.loc[b, [
                            "bidx", "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"
                        ]] = [b, None, np.nan, np.nan, np.nan, None, np.nan, None]

                    if bool(type_changed.iat[pos]):
                        base.at[b, "building type"] = _norm_type(r.get("building type"))

                    if bool(area_changed.iat[pos]):
                        base.at[b, "area (m²)"] = _num(r.get("area (m²)"))

                    if bool(levels_changed.iat[pos]):
                        base.at[b, "levels"] = _num(r.get("levels"))

                    if bool(height_changed.iat[pos]):
                        base.at[b, "height (m)"] = _num(r.get("height (m)"))

                    if bool(addr_changed.iat[pos]):
                        base.at[b, "address"] = _clean_addr(r.get("address"))
                    if bool(year_changed.iat[pos]):
                        base.at[b, "year built"] = _num(r.get("year built"))
                    if bool(retrofit_changed.iat[pos]):
                        base.at[b, "retrofit situation"] = _normalize_retrofit_situation(
                            r.get("retrofit situation")
                        )

                # Drop rows where no override remains (all three empty)
                def _row_has_any_override(row):
                    return (row.get("building type") is not None) or pd.notna(row.get("area (m²)")) or pd.notna(row.get("levels")) or pd.notna(row.get("height (m)")) or (
                                row.get("address") is not None) or pd.notna(row.get("year built")) or (
                                row.get("retrofit situation") is not None)

                updated = base.reset_index(drop=True)
                updated = updated.loc[updated.apply(_row_has_any_override, axis=1)].reset_index(drop=True)

                st.session_state["building_attrs"] = updated

            # -------- Manual edits: write directly --------
            manual_cols = [
                "bidx", "name", "building type", "area (m²)", "levels", "height (m)", "year built", "retrofit situation",
                "building type source", "year source", "zensus age class", "zensus type class",
                "zensus type note", "zensus year note", "zensus source",
            ]
            manual = st.session_state.get(
                "manual_buildings",
                pd.DataFrame(columns=manual_cols)
            ).copy()
            manual_changed_now = set()

            def _manual_demand_fingerprint(row):
                """Canonical demand-relevant attributes for a user-added building."""
                def _number(value):
                    value = pd.to_numeric(value, errors="coerce")
                    return None if pd.isna(value) else round(float(value), 6)

                return (
                    norm_btype(row.get("building type")),
                    _number(row.get("area (m²)")),
                    _number(row.get("levels")),
                    _number(row.get("height (m)")),
                    _number(row.get("year built")),
                    _normalize_retrofit_situation(row.get("retrofit situation")),
                )

            manual_before = {
                str(row["bidx"]): _manual_demand_fingerprint(row)
                for _, row in manual.iterrows()
                if str(row.get("bidx", "")).strip()
            }

            if not manual.empty or not existing_manual.empty:
                manual["bidx"] = manual["bidx"].astype(str)
                for col in manual_cols:
                    if col not in manual.columns:
                        manual[col] = np.nan
                mb = manual.set_index("bidx")

                def _clean_source(v):
                    if v is None or pd.isna(v):
                        return None
                    s = str(v).strip()
                    return s if s and s.lower() not in {"none", "nan"} else None

                for _, r in existing_manual.iterrows():
                    b = str(r["bidx"])
                    if b not in mb.index:
                        mb.loc[b, [c for c in manual_cols if c != "bidx"]] = [
                            "", None, np.nan, np.nan, np.nan, np.nan, None, None, None, None, None, None, None, None
                        ]
                    if "name" in r.index:
                        val = str(r.get("name", "")).strip()
                        if val.lower() == "none": val = ""
                        mb.at[b, "name"] = val
                    if "building type" in r.index:
                        old_type = norm_btype(mb.at[b, "building type"])
                        new_type = norm_btype(r.get("building type"))
                        old_source = _clean_source(mb.at[b, "building type source"])
                        row_source = _clean_source(r.get("building type source")) or old_source
                        mb.at[b, "building type"] = new_type
                        if new_type is None:
                            mb.at[b, "building type source"] = None
                            mb.at[b, "zensus type class"] = None
                            mb.at[b, "zensus type note"] = None
                        elif new_type != old_type or row_source is None:
                            mb.at[b, "building type source"] = "manual"
                            mb.at[b, "zensus type class"] = None
                            mb.at[b, "zensus type note"] = None
                        else:
                            mb.at[b, "building type source"] = row_source
                    if "area (m²)" in r.index:
                        mb.at[b, "area (m²)"] = pd.to_numeric(r.get("area (m²)"), errors="coerce")
                    if "levels" in r.index:
                        mb.at[b, "levels"] = pd.to_numeric(r.get("levels"), errors="coerce")
                    if "height (m)" in r.index:
                        mb.at[b, "height (m)"] = pd.to_numeric(r.get("height (m)"), errors="coerce")
                    if "year built" in r.index:
                        old_year = pd.to_numeric(mb.at[b, "year built"], errors="coerce")
                        new_year = pd.to_numeric(r.get("year built"), errors="coerce")
                        old_source = _clean_source(mb.at[b, "year source"])
                        row_source = _clean_source(r.get("year source")) or old_source
                        mb.at[b, "year built"] = new_year
                        same_year = (
                            pd.isna(old_year) and pd.isna(new_year)
                        ) or (
                            pd.notna(old_year) and pd.notna(new_year) and np.isclose(old_year, new_year, rtol=0.0, atol=1e-6)
                        )
                        if pd.isna(new_year):
                            mb.at[b, "year source"] = None
                            mb.at[b, "zensus age class"] = None
                            mb.at[b, "zensus year note"] = None
                        elif not same_year or row_source is None:
                            mb.at[b, "year source"] = "manual"
                            mb.at[b, "zensus age class"] = None
                            mb.at[b, "zensus year note"] = None
                        else:
                            mb.at[b, "year source"] = row_source
                    if "retrofit situation" in r.index:
                        mb.at[b, "retrofit situation"] = _normalize_retrofit_situation(
                            r.get("retrofit situation")
                        )

                mb_out = mb.reset_index()
                for col in ["name", "building type", "building type source", "year source", "zensus age class",
                            "zensus type class", "zensus type note", "zensus year note", "zensus source"]:
                    mb_out[col] = mb_out[col].apply(lambda v: None if pd.isna(v) or str(v).strip().lower() in {"", "none", "nan"} else str(v).strip())
                mb_out["building type"] = mb_out["building type"].map(norm_btype)
                mb_out["retrofit situation"] = mb_out["retrofit situation"].apply(_normalize_retrofit_situation)
                mb_out["area (m²)"] = pd.to_numeric(mb_out["area (m²)"], errors="coerce")
                mb_out["levels"] = pd.to_numeric(mb_out["levels"], errors="coerce")
                mb_out["height (m)"] = pd.to_numeric(mb_out["height (m)"], errors="coerce")
                mb_out["year built"] = pd.to_numeric(mb_out["year built"], errors="coerce")
                mb_out.loc[mb_out["building type"].notna() & mb_out["building type source"].isna(), "building type source"] = "manual"
                mb_out.loc[mb_out["year built"].notna() & mb_out["year source"].isna(), "year source"] = "manual"

                manual_after = {
                    str(row["bidx"]): _manual_demand_fingerprint(row)
                    for _, row in mb_out.iterrows()
                    if str(row.get("bidx", "")).strip()
                }
                manual_changed_now = {
                    bidx for bidx in set(manual_before) | set(manual_after)
                    if manual_before.get(bidx) != manual_after.get(bidx)
                }

                blank_manual = (
                    mb_out["name"].isna()
                    & mb_out["building type"].isna()
                    & mb_out["area (m²)"].isna()
                    & mb_out["levels"].isna()
                    & mb_out["height (m)"].isna()
                    & mb_out["year built"].isna()
                    & mb_out["retrofit situation"].isna()
                )
                missing_area = mb_out["area (m²)"].isna()
                blank_bidx = mb_out.loc[blank_manual, "bidx"].astype(str).tolist()
                missing_area_bidx = mb_out.loc[missing_area & ~blank_manual, "bidx"].astype(str).tolist()
                if blank_bidx:
                    _reclaim_manual_bidx(blank_bidx)
                mb_out = mb_out.loc[~blank_manual].copy()
                keep_missing_area = set(map(str, st.session_state.get("_manual_missing_area_keep", set())))
                pending_missing_area = sorted(
                    [b for b in missing_area_bidx if b not in keep_missing_area],
                    key=lambda x: int(x) if str(x).isdigit() else str(x),
                )
                if pending_missing_area:
                    st.session_state["manual_buildings"] = mb_out[manual_cols].reset_index(drop=True)
                    st.session_state["_manual_missing_area_pending"] = pending_missing_area
                    st.rerun()
                else:
                    st.session_state.pop("_manual_missing_area_pending", None)

                filled_type = 0
                filled_year = 0
                if fill_manual_from_zensus and not mb_out.empty:
                    zdefaults = defaults_from_zensus_summary(st.session_state.get("zensus_summary"))
                    if zdefaults.get("building"):
                        mtype = mb_out["building type"].isna()
                        filled_type = int(mtype.sum())
                        mb_out.loc[mtype, "building type"] = zdefaults["building"]
                        mb_out.loc[mtype, "building type source"] = "zensus"
                        mb_out.loc[mtype, "zensus type class"] = zdefaults.get("type_label")
                        mb_out.loc[mtype, "zensus type note"] = "Filled from Zensus default for user-added building"
                        mb_out.loc[mtype, "zensus source"] = ZENSUS_SOURCE_LABEL
                    if pd.notna(zdefaults.get("year")):
                        myear = mb_out["year built"].isna()
                        filled_year = int(myear.sum())
                        mb_out.loc[myear, "year built"] = int(zdefaults["year"])
                        mb_out.loc[myear, "year source"] = "zensus"
                        mb_out.loc[myear, "zensus age class"] = zdefaults.get("age_label")
                        mb_out.loc[myear, "zensus year note"] = "Filled from Zensus default for user-added building"
                        mb_out.loc[myear, "zensus source"] = ZENSUS_SOURCE_LABEL

                if not mb_out.empty:
                    geometry_defaults = mb_out.apply(
                        lambda row: _building_geometry_values(
                            row.get("building type"), row.get("levels"), row.get("height (m)")
                        ),
                        axis=1,
                    )
                    mb_out["levels"] = [values[0] for values in geometry_defaults]
                    mb_out["height (m)"] = [values[1] for values in geometry_defaults]

                st.session_state["manual_buildings"] = mb_out[manual_cols].reset_index(drop=True)
                notice_bits = []
                if fill_manual_from_zensus and (filled_type or filled_year):
                    notice_bits.append(
                        f"Zensus defaults filled missing type/year for {max(filled_type, filled_year)} user-added building(s)."
                    )
                missing_manual_attrs = (
                    mb_out["building type"].isna() | mb_out["year built"].isna()
                    if not mb_out.empty else pd.Series(dtype=bool)
                )
                if fill_manual_from_zensus and missing_manual_attrs.any():
                    notice_bits.append(
                        "Some user-added buildings still miss type or year because no usable Zensus default was available."
                    )
                elif not fill_manual_from_zensus and missing_manual_attrs.any():
                    if missing_manual_attrs.any():
                        notice_bits.append("Some user-added buildings still miss type or year; they will use normal TEASER defaults where needed.")
                if notice_bits:
                    st.session_state["_manual_save_notice"] = " ".join(notice_bits)

            # -------- New manual buildings (no bidx) --------
            next_id = st.session_state["next_bidx"]
            created = []
            for _, r in new_rows.iterrows():
                if not (str(r.get("name", "")).strip() or str(r.get("building type", "")).strip() or pd.notna(
                        r.get("area (m²)")) or _normalize_retrofit_situation(r.get("retrofit situation"))):
                    continue
                new_bidx = str(next_id);
                next_id += 1
                nm = str(r.get("name", "")).strip()
                if nm.lower() == "none": nm = ""
                new_type = norm_btype(r.get("building type"))
                new_levels, new_height, _ = _building_geometry_values(
                    new_type, r.get("levels"), r.get("height (m)")
                )
                created.append({
                    "bidx": new_bidx,
                    "name": nm,
                    "building type": new_type,
                    "area (m²)": pd.to_numeric(r.get("area (m²)"), errors="coerce"),
                    "levels": new_levels,
                    "height (m)": new_height,
                    "year built": pd.to_numeric(r.get("year built"), errors="coerce"),
                    "retrofit situation": _normalize_retrofit_situation(r.get("retrofit situation")),
                    "building type source": "manual" if new_type else None,
                    "year source": "manual" if pd.notna(pd.to_numeric(r.get("year built"), errors="coerce")) else None,
                    "zensus age class": None,
                    "zensus type class": None,
                    "zensus type note": None,
                    "zensus year note": None,
                    "zensus source": None,
                })
            if created:
                mb = st.session_state.get("manual_buildings", pd.DataFrame(
                    columns=manual_cols)).copy()
                st.session_state["manual_buildings"] = pd.concat([mb, pd.DataFrame(created)], ignore_index=True)

            st.session_state["next_bidx"] = next_id
            # track which bidx changed this save
            changed_now = set()

            if not merged.empty:
                changed_mask = (
                    type_changed | area_changed | levels_changed | height_changed | year_changed | retrofit_changed
                )
                changed_now |= set(merged.loc[changed_mask, "bidx"].astype(str).tolist())

            changed_now |= manual_changed_now

            if created:
                changed_now |= {row["bidx"] for row in created}

            # Build a "current attributes" view = baseline + latest overrides
            cur_attrs = baseline_osm[[
                "bidx", "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"
            ]].copy()
            cur_attrs["bidx"] = cur_attrs["bidx"].astype(str)

            latest_attrs = st.session_state.get(
                "building_attrs",
                pd.DataFrame(columns=[
                    "bidx", "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"
                ])
            ).copy()

            if not latest_attrs.empty:
                latest_attrs["bidx"] = latest_attrs["bidx"].astype(str)
                # merge in latest overrides
                for col in ["building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"]:
                    if col in latest_attrs.columns:
                        cur_attrs = cur_attrs.merge(
                            latest_attrs[["bidx", col]],
                            on="bidx", how="left", suffixes=("", "_ovr")
                        )
                        cur_attrs[col] = cur_attrs[f"{col}_ovr"].combine_first(cur_attrs[col])
                        cur_attrs.drop(columns=[f"{col}_ovr"], inplace=True)

            def _fp_row(row):
                return (
                    norm_btype(row.get("building type")),
                    float(pd.to_numeric(row.get("area (m²)"), errors="coerce")),
                    float(pd.to_numeric(row.get("levels"), errors="coerce")),
                    float(pd.to_numeric(row.get("height (m)"), errors="coerce")),
                    (str(row.get("address", "")).strip().lower() or None),
                    float(pd.to_numeric(row.get("year built"), errors="coerce")),
                    _normalize_retrofit_situation(row.get("retrofit situation")),
                )

            fp_now = {}
            for _, r in cur_attrs.iterrows():
                fp_now[str(r["bidx"])] = _fp_row(r)

            # also include manual buildings
            mb_now = st.session_state.get("manual_buildings", pd.DataFrame()).copy()
            if not mb_now.empty:
                mb_now["bidx"] = mb_now["bidx"].astype(str)
                for _, r in mb_now.iterrows():
                    fp_now[str(r["bidx"])] = _fp_row(r)

            st.session_state["attr_change_fingerprint"] = fp_now

            edited_demand = _edited_demand_set()
            edit_counter = st.session_state.get("edit_counter", 0) + 1
            st.session_state["edit_counter"] = edit_counter

            attr_versions = st.session_state.get("attr_edit_version", {}).copy()
            for b in changed_now:
                attr_versions[str(b)] = edit_counter
            st.session_state["attr_edit_version"] = attr_versions

            demand_versions = st.session_state.get("demand_edit_version", {})

            def _attr_after_demand(b):
                b = str(b)
                return attr_versions.get(b, 0) > demand_versions.get(b, 0)

            # only conflict if demand is edited AND this attr change is newer
            conflicts = sorted(
                [b for b in changed_now & edited_demand if _attr_after_demand(b)],
                key=lambda x: int(x) if str(x).isdigit() else str(x),
            )

            # remember what changed and the fingerprint so the conflict UI can
            # avoid re-asking if nothing new changed
            st.session_state["last_attr_changed_set"] = set(changed_now)
            st.session_state["attr_change_fingerprint"] = fp_now

            # replace old conflicts with the new set (even if empty)
            if conflicts:
                st.session_state["attr_demand_conflicts"] = conflicts
            else:
                st.session_state.pop("attr_demand_conflicts", None)

            _ensure_info_overridden(force=True)

            if changed_now and st.session_state.get("show_dhw_results"):
                st.session_state["dhw_results_stale"] = True

            # Merely submitting an unchanged editor must not bring back the
            # stale-results warning.
            if changed_now:
                st.session_state["results_stale"] = True
                st.session_state["_persist_dirty"] = True
            st.success("Building attributes saved.")
            st.rerun()

    # --- Per-building conflict resolution (framed + sorted) ---
    conflicts = st.session_state.get("attr_demand_conflicts", [])

    if conflicts:
        policy = st.session_state.setdefault("demand_conflict_policy", {})  # {bidx: "keep"|"clear"}
        snapshot = st.session_state.setdefault("demand_conflict_snapshot", {})  # {bidx: (type, area)}
        fp_now = st.session_state.get("attr_change_fingerprint", {})  # {bidx: (type, area)}
        changed_all = set(map(str, st.session_state.get("last_attr_changed_set", set())))

        def _needs_prompt(b):
            cur = fp_now.get(b)
            prev = snapshot.get(b)
            return (prev is None) or (cur != prev)

        unresolved = [str(b) for b in conflicts if _needs_prompt(str(b))]
        unresolved.sort(key=lambda x: int(x) if x.isdigit() else x)

        # pad width for nice labels like Building_001, 002, ...
        try:
            pad_width = max(2, len(str(info_df["bidx"].astype(int).max())))
        except Exception:
            pad_width = 2

        if unresolved:
            with st.container(border=True):
                st.markdown("#### ⚠️ Edited demand vs new attribute change")
                st.write(
                    f"{len(unresolved)} building(s) have **edited demand** and just had **attribute updates**. "
                    "Choose what to do for each:"
                )

                picks = {}
                for b in unresolved:
                    label = f"#{int(b):0{pad_width}d}" if b.isdigit() else f"#{b}"
                    default = policy.get(b, "keep")
                    picks[b] = st.selectbox(
                        f"{label}:",
                        ["Keep my manual demand", "Clear manual demand"],
                        index=0 if default == "keep" else 1,
                        key=f"conflict_pick_{b}"
                    )

                c1, c2 = st.columns([1, 1])
                with c1:
                    if st.button("Apply choices", key="apply_conflicts"):
                        # store policy + current fingerprint snapshot
                        for b, ch in picks.items():
                            policy[b] = "keep" if ch.startswith("Keep") else "clear"
                            if b in fp_now:
                                snapshot[b] = fp_now[b]

                        # apply clears now
                        to_clear = [b for b in conflicts if policy.get(str(b)) == "clear"]
                        if to_clear:
                            final = st.session_state.get("building_demand_estimates")
                            if final is not None and not final.empty:
                                final = final.copy()
                                final["input_index"] = final["input_index"].astype(str)
                                mm = final["input_index"].isin([str(x) for x in to_clear])
                                final.loc[mm, "heat_demand_kWh"] = np.nan
                                st.session_state["building_demand_estimates"] = final
                                tot = float(
                                    pd.to_numeric(final["heat_demand_kWh"], errors="coerce").fillna(0).sum())
                                st.session_state["total_heat_demand"] = tot

                        st.session_state["demand_conflict_policy"] = policy
                        st.session_state["demand_conflict_snapshot"] = snapshot

                        conflicts_set = set(map(str, conflicts))
                        only_conflicts_changed = (changed_all - conflicts_set) == set()
                        all_keep = all(policy.get(b) == "keep" for b in conflicts_set)
                        st.session_state["results_stale"] = not (only_conflicts_changed and all_keep)

                        _ensure_info_overridden(force=True)
                        st.session_state.pop("attr_demand_conflicts", None)
                        st.session_state["_persist_dirty"] = True
                        st.rerun()

                with c2:
                    if st.button("Dismiss", key="dismiss_conflicts"):
                        _ensure_info_overridden(force=True)
                        st.session_state["results_stale"] = True
                        st.session_state.pop("attr_demand_conflicts", None)
                        st.rerun()
        else:
            conflicts_set = set(map(str, conflicts))
            only_conflicts_changed = (changed_all - conflicts_set) == set()
            all_keep = all(
                st.session_state.get("demand_conflict_policy", {}).get(b) == "keep" for b in conflicts_set)
            st.session_state["results_stale"] = not (only_conflicts_changed and all_keep)
            _ensure_info_overridden(force=True)
            st.session_state.pop("attr_demand_conflicts", None)
            st.rerun()

    st.markdown("---")
    st.markdown("### Estimate Space Heating Demand")
    st.info(
        "TEASER is used to estimate each included building's space-heating demand and design peak load. "
        "Check the results before continuing; manual demand edits are preserved when the estimation is rerun."
    )

    pending_missing_area_est = [
        str(x) for x in st.session_state.get("_manual_missing_area_pending", [])
        if str(x).strip()
    ] or _manual_bidx_missing_required_area()
    if pending_missing_area_est:
        labels = ", ".join(f"Building_{b}" for b in pending_missing_area_est)
        st.warning(
            f"Area is required for user-added building(s): {labels}. "
            "Enter the area in **Correct Building Attributes If Needed**, or choose whether to keep or "
            "remove the building there."
        )

    if st.button("Estimate Space Heating Demand"):
        try:
            with st.spinner("Estimating space heating demand..."):
                # 1) Build TEASER input from included OSM + manual (minimal required cols)
                buildings_cur = st.session_state["buildings_gdf"].copy()
                buildings_cur["bidx"] = buildings_cur["bidx"].astype(str)

                excluded = set(map(str, st.session_state.get("excluded_bidx", set())))
                osm_included = buildings_cur.loc[~buildings_cur["bidx"].isin(excluded)].copy()

                # Ensure needed columns present
                for col in ["building", "area_m2", "levels", "height_m", "year_built"]:
                    if col not in osm_included.columns:
                        osm_included[col] = None

                # If levels missing, derive defaults
                def _lev_for(t):
                    return DEFAULT_LEVELS_BY_TYPE.get(str(t), DEFAULT_LEVELS)

                mask = osm_included["levels"].isna()
                osm_included.loc[mask, "levels"] = osm_included.loc[mask, "building"].map(
                    lambda t: DEFAULT_LEVELS_BY_TYPE.get(str(t), DEFAULT_LEVELS)
                )
                osm_included["levels"] = pd.to_numeric(osm_included["levels"], errors="coerce").fillna(
                    DEFAULT_LEVELS).astype(int)

                # Manual buildings (optional)
                manual = st.session_state.get("manual_buildings", pd.DataFrame()).copy()
                manual_ready = pd.DataFrame(columns=[
                    "bidx", "name", "building", "area_m2", "levels", "height_m", "year_built",
                    "retrofit_situation", "retrofit_probability",
                ])
                manual_info = pd.DataFrame(
                    columns=[
                        "building index", "address", "name", "building type", "area (m²)", "year built",
                        "retrofit situation", "renovation probability (%)", "bidx",
                    ])
                if isinstance(manual, pd.DataFrame) and not manual.empty:
                    m = manual.copy()
                    m["bidx"] = m["bidx"].astype(str)
                    m["building"] = m["building type"].map(norm_btype)
                    m["area_m2"] = pd.to_numeric(m["area (m²)"], errors="coerce")
                    manual_geometry = m.apply(
                        lambda row: _building_geometry_values(
                            row.get("building"), row.get("levels"), row.get("height (m)")
                        ),
                        axis=1,
                    )
                    m["levels"] = [values[0] for values in manual_geometry]
                    m["height_m"] = [values[1] for values in manual_geometry]
                    m["year_built"] = pd.to_numeric(m.get("year built", pd.Series([np.nan] * len(m))), errors="coerce")
                    m["retrofit_situation"] = m.get(
                        "retrofit situation", pd.Series([None] * len(m), index=m.index)
                    ).apply(_normalize_retrofit_situation)
                    m = _retrofit_inputs_for_teaser(m, building_col="building", year_col="year_built")
                    manual_ready = m[[
                        "bidx", "name", "building", "area_m2", "levels", "height_m", "year_built",
                        "retrofit_situation", "retrofit_probability",
                    ]].dropna(
                        subset=["building", "area_m2"])
                    manual_info = pd.DataFrame({
                        "building index": m["bidx"].map(lambda x: f"Building_{x}"),
                        "address": None,
                        "name": m["name"].where(m["name"].apply(_has_letters_safe), None),
                        "building type": m["building type"].map(norm_btype),
                        "building type source": m.get("building type source", pd.Series(["manual"] * len(m))),
                        "area (m²)": m["area (m²)"],
                        "levels": m["levels"],
                        "height (m)": m["height_m"],
                        "year built": m["year_built"],
                        "retrofit situation": m["retrofit_situation"],
                        "retrofit situation source": "manual",
                        "renovation probability (%)": pd.to_numeric(m["retrofit_probability"], errors="coerce") * 100.0,
                        "year source": m.get(
                            "year source",
                            pd.Series(np.where(m["year_built"].notna(), "manual", None)),
                        ),
                        "zensus age class": m.get("zensus age class", pd.Series([None] * len(m))),
                        "zensus type class": m.get("zensus type class", pd.Series([None] * len(m))),
                        "zensus type note": m.get("zensus type note", pd.Series([None] * len(m))),
                        "zensus year note": m.get("zensus year note", pd.Series([None] * len(m))),
                        "zensus source": m.get("zensus source", pd.Series([None] * len(m))),
                        "bidx": m["bidx"].astype(str)
                    })

                # Apply attribute overrides (type/area/address) to OSM inputs first
                info_overridden = info_df.copy()
                info_overridden["bidx"] = info_overridden["bidx"].astype(str)
                if "building_attrs" in st.session_state and not st.session_state["building_attrs"].empty:
                    attrs = st.session_state["building_attrs"].copy()
                    attrs["bidx"] = attrs["bidx"].astype(str)
                    if "building type" in attrs: attrs["building type"] = attrs["building type"].map(norm_btype)

                    # building type
                    if "building type" in attrs.columns:
                        tmap = attrs.dropna(subset=["building type"]).set_index("bidx")[
                            "building type"].to_dict()
                        if tmap:
                            osm_included["building"] = osm_included.apply(
                                lambda r: tmap.get(r["bidx"], r.get("building")), axis=1
                            )
                            info_overridden["building type"] = info_overridden.apply(
                                lambda r: tmap.get(str(r["bidx"]), r.get("building type")), axis=1
                            )
                    # area
                    if "area (m²)" in attrs.columns:
                        amap = attrs.set_index("bidx")["area (m²)"].to_dict()
                        if amap:
                            osm_included["area_m2"] = osm_included.apply(
                                lambda r: pd.to_numeric(amap.get(r["bidx"]), errors="coerce")
                                if pd.notna(amap.get(r["bidx"])) else r.get("area_m2"),
                                axis=1
                            )
                            info_overridden["area (m²)"] = info_overridden.apply(
                                lambda r: pd.to_numeric(amap.get(str(r["bidx"])), errors="coerce")
                                if pd.notna(amap.get(str(r["bidx"]))) else r.get("area (m²)"),
                                axis=1
                            )
                    # levels
                    if "levels" in attrs.columns:
                        lmap = pd.to_numeric(attrs.set_index("bidx")["levels"], errors="coerce").to_dict()
                        if lmap:
                            osm_included["levels"] = osm_included.apply(
                                lambda r: pd.to_numeric(lmap.get(r["bidx"]), errors="coerce")
                                if pd.notna(lmap.get(r["bidx"])) else r.get("levels"),
                                axis=1,
                            )
                            info_overridden["levels"] = info_overridden.apply(
                                lambda r: pd.to_numeric(lmap.get(str(r["bidx"])), errors="coerce")
                                if pd.notna(lmap.get(str(r["bidx"]))) else r.get("levels"),
                                axis=1,
                            )
                    # total height
                    if "height (m)" in attrs.columns:
                        hmap = pd.to_numeric(attrs.set_index("bidx")["height (m)"], errors="coerce").to_dict()
                        if hmap:
                            osm_included["height_m"] = osm_included.apply(
                                lambda r: pd.to_numeric(hmap.get(r["bidx"]), errors="coerce")
                                if pd.notna(hmap.get(r["bidx"])) else r.get("height_m"),
                                axis=1,
                            )
                            info_overridden["height (m)"] = info_overridden.apply(
                                lambda r: pd.to_numeric(hmap.get(str(r["bidx"])), errors="coerce")
                                if pd.notna(hmap.get(str(r["bidx"]))) else r.get("height (m)"),
                                axis=1,
                            )
                    # address
                    if "address" in attrs.columns:
                        def _clean_addr(v):
                            if v is None or pd.isna(v): return None
                            s = str(v).strip()
                            return s if s else None

                        amap = attrs.assign(address=attrs["address"].apply(_clean_addr)) \
                            .dropna(subset=["address"]).set_index("bidx")["address"].to_dict()
                        if amap:
                            osm_included["address"] = osm_included.apply(
                                lambda r: amap.get(r["bidx"], r.get("address")), axis=1
                            )
                            if "address" in info_overridden.columns:
                                info_overridden["address"] = info_overridden.apply(
                                    lambda r: amap.get(str(r["bidx"]), r.get("address")), axis=1
                                )
                    # year built
                    if "year built" in attrs.columns:
                        ymap = pd.to_numeric(attrs.set_index("bidx")["year built"], errors="coerce").to_dict()
                        if ymap:
                            osm_included["year_built"] = osm_included.apply(
                                lambda r: pd.to_numeric(ymap.get(r["bidx"]), errors="coerce")
                                if pd.notna(ymap.get(r["bidx"])) else r.get("year_built"),
                                axis=1
                            )
                            info_overridden["year built"] = info_overridden.apply(
                                lambda r: pd.to_numeric(ymap.get(str(r["bidx"])), errors="coerce")
                                if pd.notna(ymap.get(str(r["bidx"]))) else r.get("year built"),
                                axis=1
                            )
                            if "year source" in info_overridden.columns:
                                info_overridden["year source"] = info_overridden.apply(
                                    lambda r: "user" if pd.notna(ymap.get(str(r["bidx"]))) else r.get("year source"),
                                    axis=1
                                )
                    # retrofit situation
                    if "retrofit situation" in attrs.columns:
                        rser = attrs.set_index("bidx")["retrofit situation"].apply(_normalize_retrofit_situation)
                        rmap = rser.dropna().to_dict()
                        if rmap:
                            osm_included["retrofit_situation"] = osm_included.apply(
                                lambda r: rmap.get(r["bidx"], r.get("retrofit_situation")), axis=1
                            )
                            info_overridden["retrofit situation"] = info_overridden.apply(
                                lambda r: rmap.get(str(r["bidx"]), r.get("retrofit situation")), axis=1
                            )
                            info_overridden["retrofit situation source"] = info_overridden.apply(
                                lambda r: "user" if str(r["bidx"]) in rmap else r.get("retrofit situation source"),
                                axis=1
                            )

                if "building" in osm_included.columns:
                    osm_included["building"] = osm_included["building"].map(norm_btype)
                    # optional default, if you like
                    osm_included["building"] = osm_included["building"].fillna("house")

                osm_geometry = osm_included.apply(
                    lambda row: _building_geometry_values(
                        row.get("building"), row.get("levels"), row.get("height_m")
                    ),
                    axis=1,
                )
                osm_included["levels"] = [values[0] for values in osm_geometry]
                osm_included["height_m"] = [values[1] for values in osm_geometry]
                osm_included["floor_height_m"] = [values[2] for values in osm_geometry]
                geometry_by_bidx = osm_included.set_index("bidx")[["levels", "height_m"]].to_dict("index")
                info_overridden["levels"] = info_overridden.apply(
                    lambda row: geometry_by_bidx.get(str(row["bidx"]), {}).get("levels", row.get("levels")),
                    axis=1,
                )
                info_overridden["height (m)"] = info_overridden.apply(
                    lambda row: geometry_by_bidx.get(str(row["bidx"]), {}).get("height_m", row.get("height (m)")),
                    axis=1,
                )

                osm_included = _retrofit_inputs_for_teaser(osm_included)
                info_overridden = _add_retrofit_defaults(info_overridden)
                info_overridden["retrofit situation source"] = info_overridden.get(
                    "retrofit situation source",
                    pd.Series([None] * len(info_overridden), index=info_overridden.index),
                ).where(
                    info_overridden.get(
                        "retrofit situation source",
                        pd.Series([None] * len(info_overridden), index=info_overridden.index),
                    ).astype(str).str.lower().eq("user"),
                    "estimated",
                )
                for col, default_value in [
                    ("levels", np.nan),
                    ("height_m", np.nan),
                    ("floor_height_m", np.nan),
                    ("retrofit_situation", None),
                    ("retrofit_probability", np.nan),
                    ("retrofit_situation_source", "estimated"),
                ]:
                    if col not in buildings_cur.columns:
                        buildings_cur[col] = default_value
                    value_map = osm_included.set_index("bidx")[col].to_dict()
                    buildings_cur[col] = buildings_cur.apply(
                        lambda r: value_map.get(str(r["bidx"]), r.get(col)),
                        axis=1,
                    )
                ss["buildings_gdf"] = buildings_cur
                ss["_preloaded_buildings_gdf"] = buildings_cur.copy()
                ss["_last_buildings_gdf"] = buildings_cur.copy()

                # 2) Build one TEASER dataframe
                cols = [
                    "bidx", "building", "area_m2", "levels", "height_m", "year_built",
                    "retrofit_situation", "retrofit_probability",
                ]
                if "name" in osm_included.columns: cols.insert(1, "name")
                est_osm = osm_included[cols].copy()
                est_all = pd.concat([est_osm, manual_ready], ignore_index=True)
                est_all = est_all.set_index("bidx", drop=False)

                # 3) Run TEASER
                total_auto, new_auto = estimate_heat_demand_for_buildings(est_all)

                prev_final = ss.get("building_demand_estimates")
                prev_auto = ss.get("building_demand_estimates_original")
                keep = _edited_demand_set(prev_final, prev_auto)
                keep_peak = _edited_peak_set(prev_final, prev_auto)

                ss["building_demand_estimates_original"] = _standardize_est(new_auto)
                merged_final = _merge_new_auto_into_final(
                    new_auto, prev_final, keep, keep_peak=keep_peak
                )
                ss["building_demand_estimates"] = merged_final

                ss["total_heat_demand"] = float(
                    pd.to_numeric(merged_final["heat_demand_kWh"], errors="coerce").fillna(0).sum()
                )

                # 6) Rebuild info_overridden (OSM overridden + manual info) for the results merge
                info_overridden["bidx"] = info_overridden["bidx"].astype(str)
                info_overridden["source"] = "osm"
                manual_info["source"] = "manual"
                combined_info = pd.concat([info_overridden, manual_info], ignore_index=True)
                if "building type" in combined_info.columns:
                    combined_info["building type"] = combined_info["building type"].map(norm_btype)

                ss["info_overridden"] = combined_info

                # 7) Show results now
                ss["show_teaser_results"] = True
                ss["results_stale"] = False

                # Legacy aliases (if needed elsewhere)
                ss["estimates_original_df"] = ss["building_demand_estimates_original"].rename(
                    columns={"input_index": "bidx", "heat_demand_kWh": "heat_kwh"}
                )
                ss["estimates_final_df"] = ss["building_demand_estimates"].rename(
                    columns={"input_index": "bidx", "heat_demand_kWh": "heat_kwh"}
                )

                st.success("TEASER space-heating run complete. Edited values were preserved.")
                st.session_state["_reset_edit_heat_toggle"] = True
            st.rerun()
        except Exception as e:
            st.error(f"TEASER run failed: {e}")

    if not st.session_state.get("show_teaser_results", False):
        st.stop()

    # ---------------- result table ----------------
    # If results are stale, ask the user to re-run TEASER and stop rendering the table
    if st.session_state.get("results_stale", False):
        st.warning(
            "Results are out of date due to recent changes "
            "(attributes, inclusion/exclusion, or drawn areas). "
            "The table below still shows the last TEASER run. "
            "Re-run **Estimate Space Heating Demand with TEASER** to update."
        )
        # st.stop()

    # Backfill total from FINAL if it's missing
    if "total_heat_demand" not in st.session_state or st.session_state["total_heat_demand"] is None:
        tmp_final = st.session_state["building_demand_estimates"]
        st.session_state["total_heat_demand"] = float(
            pd.to_numeric(tmp_final["heat_demand_kWh"], errors="coerce").fillna(0).sum()
        )

    total_heat_demand = st.session_state["total_heat_demand"]

    # ---- your existing rendering logic continues unchanged below ----
    estimates = st.session_state["building_demand_estimates"].copy()

    demand_df = estimates.rename(columns={
        "input_index": "bidx",
        "heat_demand_kWh": "Annual Space Heating Demand (kWh)",
        "peak_load_kW": "Peak Heat Load (kW)",
    })
    demand_df["bidx"] = demand_df["bidx"].astype(str)
    # demand_df["debug_bidx"] = demand_df["bidx"]

    # Merge with full info (OSM overridden + manual)
    if "info_overridden" not in st.session_state or \
            not isinstance(st.session_state["info_overridden"], pd.DataFrame) or \
            st.session_state["info_overridden"].empty:
        _ensure_info_overridden()

    info_all = st.session_state.get("info_overridden")
    # As a final fallback, derive from buildings_gdf without touching undefined info_df
    if not isinstance(info_all, pd.DataFrame) or info_all.empty:
        gsrc = st.session_state.get("buildings_gdf") or st.session_state.get("_preloaded_buildings_gdf")
        st.session_state.setdefault("_preloaded_drawn_polygons", list(st.session_state.get("drawn_polygons", [])))
        if gsrc is not None and not getattr(gsrc, "empty", True):
            g = gsrc.copy()
            g["bidx"] = g.get("bidx", g.index.astype(str)).astype(str)
            info_all = pd.DataFrame({
                "building index": g["bidx"].map(lambda x: f"Building_{x}"),
                "address": g.get("address", pd.Series([None] * len(g))),
                "name": g.get("name", pd.Series([None] * len(g))),
                "building type": (
                    g.get("building", pd.Series([None] * len(g))).map(norm_btype)
                    if "building" in g.columns else pd.Series([None] * len(g))
                ),
                "building type source": g.get("building_type_source", pd.Series([None] * len(g))),
                "area (m²)": g.get("area_m2", pd.Series([np.nan] * len(g))),
                "levels": pd.to_numeric(g.get("levels", pd.Series([np.nan] * len(g))), errors="coerce"),
                "height (m)": pd.to_numeric(
                    g.get("height_m", pd.Series([np.nan] * len(g))), errors="coerce"
                ),
                "year built": pd.to_numeric(g.get("year_built", pd.Series([np.nan] * len(g))), errors="coerce"),
                "year source": g.get("year_built_source", pd.Series([None] * len(g))),
                "retrofit situation": g.get("retrofit_situation", pd.Series([None] * len(g))),
                "renovation probability (%)": pd.to_numeric(
                    g.get("retrofit_probability", pd.Series([np.nan] * len(g))), errors="coerce"
                ) * 100.0,
                "zensus age class": g.get("zensus_age_class", pd.Series([None] * len(g))),
                "zensus type class": g.get("zensus_type_class", pd.Series([None] * len(g))),
                "bidx": g["bidx"].astype(str),
                "source": "osm",
            })

    demand_df = pd.merge(info_all, demand_df, on="bidx", how="left")

    if "building type" in demand_df.columns:
        demand_df["building type"] = demand_df["building type"].map(norm_btype)
    demand_df = _add_retrofit_defaults(demand_df)
    demand_df.drop(
        columns=["renovation probability (%)", "retrofit situation source"],
        inplace=True,
        errors="ignore",
    )

    if "name" in demand_df.columns:
        name_has_res = demand_df["name"].apply(_has_letters_safe)
        show_name_res = bool(name_has_res.any())
        if show_name_res:
            demand_df["name"] = demand_df["name"].where(name_has_res, None).astype("object")
        else:
            demand_df.drop(columns=["name"], inplace=True)
    else:
        show_name_res = False

    for c in ["address", "building type"]:
        if c in demand_df.columns:
            demand_df[c] = demand_df[c].replace(_REPLACE_MISSING).astype("object")

    # ---------- ordering ----------
    demand_df["__order__"] = demand_df["bidx"].astype(int)
    demand_df = demand_df.sort_values("__order__").drop(columns="__order__")

    # ---------- status flags ----------
    orig = st.session_state.get("building_demand_estimates_original")
    curr = st.session_state.get("building_demand_estimates")

    demand_edited_mask = pd.Series(False, index=demand_df.index)
    peak_edited_mask = pd.Series(False, index=demand_df.index)
    if orig is not None and curr is not None and not orig.empty and not curr.empty:
        o = orig.copy();
        o["bidx"] = o["input_index"].astype(str)
        c = curr.copy();
        c["bidx"] = c["input_index"].astype(str)
        merged_ed = (
            demand_df[["bidx"]]
            .merge(o[["bidx", "heat_demand_kWh"]].rename(columns={"heat_demand_kWh": "auto"}),
                   on="bidx", how="left")
            .merge(c[["bidx", "heat_demand_kWh"]].rename(columns={"heat_demand_kWh": "final"}),
                   on="bidx", how="left")
        )
        a = pd.to_numeric(merged_ed["auto"], errors="coerce")
        f = pd.to_numeric(merged_ed["final"], errors="coerce")
        demand_edited_mask = pd.Series(~np.isclose(a, f, equal_nan=True), index=demand_df.index)
        if "peak_load_kW" in o.columns and "peak_load_kW" in c.columns:
            merged_peak = (
                demand_df[["bidx"]]
                .merge(o[["bidx", "peak_load_kW"]].rename(columns={"peak_load_kW": "auto"}), on="bidx", how="left")
                .merge(c[["bidx", "peak_load_kW"]].rename(columns={"peak_load_kW": "final"}), on="bidx", how="left")
            )
            peak_edited_mask = pd.Series(
                ~np.isclose(
                    pd.to_numeric(merged_peak["auto"], errors="coerce"),
                    pd.to_numeric(merged_peak["final"], errors="coerce"),
                    equal_nan=True,
                ),
                index=demand_df.index,
            )

    status_parts = [[] for _ in range(len(demand_df))]

    # manual-entry checks (proxy: unknown address)
    source_series = demand_df.get("source", pd.Series("osm", index=demand_df.index))
    is_manual = source_series.eq("manual")
    for i, flag in enumerate(is_manual):
        if flag:
            status_parts[i].append("user added")
    type_missing = is_manual & demand_df["building type"].fillna("").str.strip().eq("")
    area_missing = is_manual & pd.to_numeric(demand_df["area (m²)"], errors="coerce").isna()

    for i, (tm, am) in enumerate(zip(type_missing, area_missing)):
        if tm and am:
            status_parts[i].append("type & area missing")
        elif tm:
            status_parts[i].append("type missing")
        elif am:
            status_parts[i].append("area missing")

    # edited?
    attrs = st.session_state.get("building_attrs", pd.DataFrame())
    edited_type_flags = pd.Series(False, index=demand_df.index)
    edited_area_flags = pd.Series(False, index=demand_df.index)
    edited_addr_flags = pd.Series(False, index=demand_df.index)
    edited_year_flags = pd.Series(False, index=demand_df.index)
    edited_retrofit_flags = pd.Series(False, index=demand_df.index)

    if attrs is not None and not attrs.empty:
        battr = attrs.copy()
        battr["bidx"] = battr["bidx"].astype(str)

        if "building type" in battr.columns:
            edited_type_set = set(battr.dropna(subset=["building type"])["bidx"].astype(str))
            edited_type_flags = demand_df["bidx"].astype(str).isin(edited_type_set)

        if "area (m²)" in battr.columns:
            edited_area_set = set(
                battr[~pd.to_numeric(battr["area (m²)"], errors="coerce").isna()]["bidx"].astype(str))
            edited_area_flags = demand_df["bidx"].astype(str).isin(edited_area_set)

        if "address" in battr.columns:
            edited_addr_set = set(
                battr.loc[
                    battr["address"].notna() & (battr["address"].astype(str).str.strip() != ""), "bidx"].astype(
                    str)
            )
            edited_addr_flags = demand_df["bidx"].astype(str).isin(edited_addr_set)
        if "year built" in battr.columns:
            edited_year_set = set(
                battr.loc[~pd.to_numeric(battr["year built"], errors="coerce").isna(), "bidx"].astype(str)
            )
            edited_year_flags = demand_df["bidx"].astype(str).isin(edited_year_set)
        if "retrofit situation" in battr.columns:
            battr["retrofit situation"] = battr["retrofit situation"].apply(_normalize_retrofit_situation)
            edited_retrofit_set = set(
                battr.loc[battr["retrofit situation"].notna(), "bidx"].astype(str)
            )
            edited_retrofit_flags = demand_df["bidx"].astype(str).isin(edited_retrofit_set)

    # edited_demand_set = _edited_demand_set()
    # edited_demand_flags = demand_df["bidx"].astype(str).isin(edited_demand_set)

    for i in range(len(demand_df)):
        if edited_type_flags.iat[i]:
            status_parts[i].append("edited (type)")
        if edited_area_flags.iat[i]:
            status_parts[i].append("edited (area)")
        if edited_addr_flags.iat[i]:
            status_parts[i].append("edited (address)")
        if edited_year_flags.iat[i]:
            status_parts[i].append("edited (year)")
        if edited_retrofit_flags.iat[i]:
            status_parts[i].append("edited (retrofit)")
        if demand_edited_mask.iat[i]:
            status_parts[i].append("edited (demand)")
        if peak_edited_mask.iat[i]:
            status_parts[i].append("edited (peak)")

    # materialize status + decide visibility
    demand_df["status"] = ["; ".join(p) if p else "" for p in status_parts]
    has_user_added = bool(is_manual.any())
    has_attr_edited = bool(
        edited_type_flags.any()
        or edited_area_flags.any()
        or edited_addr_flags.any()
        or edited_year_flags.any()
        or edited_retrofit_flags.any()
    )
    has_demand_edited = bool(demand_edited_mask.any() or peak_edited_mask.any())
    has_missing_manual_info = bool(type_missing.any() or area_missing.any())
    show_status_res = (
            has_attr_edited
            or has_demand_edited
            or has_user_added
            or has_missing_manual_info
    )

    # ---------- units ----------
    positive_values = pd.to_numeric(demand_df["Annual Space Heating Demand (kWh)"], errors="coerce")
    positive_values = positive_values[positive_values > 0]
    min_value = positive_values.min() if not positive_values.empty else 0

    if total_heat_demand >= 1_000_000:
        heat_unit = "GWh";
        heat_divisor = 1_000_000
    elif total_heat_demand >= 1_000:
        heat_unit = "MWh";
        heat_divisor = 1_000
    else:
        heat_unit = "kWh";
        heat_divisor = 1

    if min_value >= 1_000_000:
        building_unit = "GWh";
        building_divisor = 1_000_000
    elif min_value >= 1_000:
        building_unit = "MWh";
        building_divisor = 1_000
    else:
        building_unit = "kWh";
        building_divisor = 1

    # values (mask N/A rows)
    issue_mask = (
            demand_df["bidx"].astype(str).isin(st.session_state["excluded_bidx"]) |
            (is_manual & (
                    demand_df["building type"].fillna("").str.strip().eq("") |
                    pd.to_numeric(demand_df["area (m²)"], errors="coerce").isna()
            ))
    )
    demand_kwh_numeric = pd.to_numeric(demand_df["Annual Space Heating Demand (kWh)"], errors="coerce")
    demand_df["demand_value"] = demand_kwh_numeric / building_divisor
    demand_df["peak_value"] = pd.to_numeric(demand_df.get("Peak Heat Load (kW)"), errors="coerce")
    demand_df.loc[issue_mask, "demand_value"] = np.nan
    demand_df.loc[issue_mask, "peak_value"] = np.nan

    readonly_df = demand_df[issue_mask].copy()
    editable_df = demand_df[~issue_mask].copy()

    # ---------- columns & editor ----------
    cols_order = ["building index", "address"]
    for c in ["building type", "area (m²)", "year built", "levels", "height (m)", "retrofit situation"]:
        if c in demand_df.columns: cols_order.append(c)
    cols_order += ["demand_value", "peak_value"]
    if show_status_res and "status" in demand_df.columns:
        cols_order.append("status")
    cols_order.append("bidx")

    demand_df = demand_df[[c for c in cols_order if c in demand_df.columns] +
                          [c for c in demand_df.columns if c not in cols_order]]

    st.markdown("#### Building Space Heating Demand Results")
    help_txt = f"Edit numeric values ({building_unit})."

    col_cfg = {
        "demand_value": st.column_config.NumberColumn(
            f"Annual Space Heating Demand ({building_unit})",
            help="Leave blank for N/A. Right-aligned.",
            min_value=0.0,
            step=0.01 if building_unit != "kWh" else 1.0,
            format="%.2f",
        ),
        "peak_value": st.column_config.NumberColumn(
            "Design Peak Heat Load (kW)",
            help="TEASER design load or a user-provided building peak.",
            min_value=0.0,
            step=0.1,
            format="%.2f",
        ),
        "building index": st.column_config.TextColumn("building index", disabled=True),
        "address": st.column_config.TextColumn("address", disabled=True),
    }
    if show_name_res and "name" in demand_df.columns:
        col_cfg["name"] = st.column_config.TextColumn("name", disabled=True)
    if "building type" in demand_df.columns:
        col_cfg["building type"] = st.column_config.TextColumn("building type", disabled=True)
    if "area (m²)" in demand_df.columns:
        col_cfg["area (m²)"] = st.column_config.NumberColumn("area (m²)", disabled=True, format="%.2f")
    if "year built" in demand_df.columns:
        col_cfg["year built"] = st.column_config.NumberColumn("year built", disabled=True, format="%.0f")
    if "levels" in demand_df.columns:
        col_cfg["levels"] = st.column_config.NumberColumn("levels", disabled=True, format="%.0f")
    if "height (m)" in demand_df.columns:
        col_cfg["height (m)"] = st.column_config.NumberColumn("height (m)", disabled=True, format="%.2f")
    if "retrofit situation" in demand_df.columns:
        col_cfg["retrofit situation"] = st.column_config.TextColumn("retrofit situation", disabled=True)

    if show_status_res and "status" in demand_df.columns:
        col_cfg["status"] = st.column_config.TextColumn("status", disabled=True)
    display_cols = ["building index", "address"]
    for c in ["building type", "area (m²)", "year built", "levels", "height (m)", "retrofit situation"]:
        if c in demand_df.columns:
            display_cols.append(c)
    display_cols.extend(["demand_value", "peak_value"])
    if show_status_res and "status" in demand_df.columns:
        display_cols.append("status")
    if not readonly_df.empty:
        st.caption("Excluded buildings (read-only):")
        st.dataframe(
            readonly_df[
                [c for c in display_cols if c in readonly_df.columns]  # e.g. index, address, status, demand_value
            ].drop(columns=["demand_value"], errors="ignore"),
            use_container_width=True,
            hide_index=True,
        )

    if editable_df.empty:
        st.info("No buildings available for demand editing (all are excluded or incomplete).")
        enable_edit = False
        edited_df = editable_df.copy()
    else:
        enable_edit = st.checkbox(
            "Enable manual editing of space heating demand",
            key="edit_heat_toggle",
            help=help_txt,
        )

        st.caption(
            "⚠️ Note: After editing the table, please click the **Save** button before proceeding to the next step.")

        with st.form("heat_demand_edit_form", clear_on_submit=False):
            edited_df = st.data_editor(
                editable_df.copy(),
                num_rows="fixed",
                use_container_width=True,
                column_config=col_cfg,
                disabled=not enable_edit,
                column_order=display_cols,
                hide_index=True,
                key="heat_demand_editor",
            )
            col_save, col_reset = st.columns([1, 1])
            with col_save:
                save_clicked = st.form_submit_button("💾 Save edits", disabled=not enable_edit)
            with col_reset:
                discard_clicked = st.form_submit_button("↩️ Discard unsaved edits", disabled=not enable_edit)

    if show_status_res:
        msg_bits = []
        if has_user_added:
            msg_bits.append("buildings added by users")
        edited_bits = []
        if has_attr_edited:
            edited_bits.append("attributes")
        if has_demand_edited:
            edited_bits.append("demand")
        if edited_bits:
            msg_bits.append("buildings with edited " + " or ".join(edited_bits))
        if has_missing_manual_info:
            msg_bits.append("user-added buildings with missing required information")
        if msg_bits:
            if len(msg_bits) == 1:
                msg = msg_bits[0]
            else:
                msg = ", ".join(msg_bits[:-1]) + ", or " + msg_bits[-1]
            st.caption("The status column shows " + msg + ".")

    if enable_edit:
        if save_clicked:
            merged = demand_df.merge(
                edited_df[["bidx", "demand_value", "peak_value"]],
                on="bidx", how="left", suffixes=("", "_new")
            )
            merged_val_new = pd.to_numeric(merged["demand_value_new"], errors="coerce")
            merged_val_old = pd.to_numeric(merged["demand_value"], errors="coerce")
            merged_peak_new = pd.to_numeric(merged["peak_value_new"], errors="coerce")
            merged_peak_old = pd.to_numeric(merged["peak_value"], errors="coerce")

            edit_counter = st.session_state.get("edit_counter", 0) + 1
            st.session_state["edit_counter"] = edit_counter

            demand_versions = st.session_state.get("demand_edit_version", {}).copy()

            # bidx with a *real* change (new != old, not both NaN)
            changed_demand_mask = ~np.isclose(
                merged_val_new.fillna(np.nan),
                merged_val_old.fillna(np.nan),
                equal_nan=True,
            )
            changed_demand_bidx = merged.loc[changed_demand_mask, "bidx"].astype(str)

            for b in changed_demand_bidx:
                demand_versions[b] = edit_counter

            conflicts = st.session_state.get("attr_demand_conflicts", [])
            if conflicts:
                changed_set = set(changed_demand_bidx.astype(str))
                remaining = [c for c in conflicts if str(c) not in changed_set]
                if remaining:
                    st.session_state["attr_demand_conflicts"] = remaining
                else:
                    st.session_state.pop("attr_demand_conflicts", None)

            st.session_state["demand_edit_version"] = demand_versions

            merged_val = merged_val_new.where(~merged_val_new.isna(), merged_val_old)
            merged_peak = merged_peak_new.where(~merged_peak_new.isna(), merged_peak_old)
            edited_kwh = merged_val * building_divisor

            st.session_state["building_demand_estimates"] = (
                pd.DataFrame({
                    "input_index": merged["bidx"].astype(str),
                    "heat_demand_kWh": edited_kwh,  # NaNs allowed
                    "peak_load_kW": merged_peak,
                    "estimation_method": merged.get("estimation_method", "user/legacy estimate"),
                })
                .drop_duplicates(subset=["input_index"], keep="last")
            )

            st.session_state["total_heat_demand"] = float(np.nansum(edited_kwh.values))
            st.session_state["_reset_edit_heat_toggle"] = True
            st.session_state["_persist_dirty"] = True
            st.success("Edits saved. Totals updated.")
            st.rerun()

        if discard_clicked:
            st.session_state["_reset_edit_heat_toggle"] = True
            st.rerun()

    # Show total space heating demand in its own unit
    total_heat_demand_converted = total_heat_demand / heat_divisor
    st.markdown(
        f"**Total Annual Space Heating Demand for the selected area: {total_heat_demand_converted:.2f} {heat_unit}**")
    if heat_unit != "kWh":
        st.markdown(f"ℹ️ Which is equivalent to: **{total_heat_demand:,.2f} kWh**")

    st.markdown("---")
    st.markdown("### Estimate Domestic Hot Water Demand")
    st.info(
        "In this step, the district planning tool uses OpenDHW to estimate the annual **domestic hot water demand** and a DHW load profile "
        "for the included buildings."
    )
    if ss.get("dhw_results_stale") and ss.get("show_dhw_results"):
        st.warning(
            "DHW results are out of date due to recent changes "
            "(attributes, inclusion/exclusion, or drawn areas). "
            "The previous OpenDHW inputs and result table are kept below. Re-run "
            "**Estimate DHW Demand with OpenDHW** to update them."
        )

    current_info_df = ss.get("info_overridden")
    if not isinstance(current_info_df, pd.DataFrame) or current_info_df.empty:
        current_info_df = info_df if "info_df" in locals() and isinstance(info_df, pd.DataFrame) else pd.DataFrame()
    dhw_input_df = _current_building_inputs_for_dhw(current_info_df)

    with st.expander("OpenDHW assumptions", expanded=False):
        st.markdown(
            """
            These assumptions translate the available building attributes into OpenDHW inputs:
            - **Residential draw-off volume**: hot water volume per resident and day. Typical starting values are 25-30 L/person/day for low use, 40 L/person/day for a common default, and 50-60 L/person/day for higher use.
            - **Residential occupancy area**: net floor area per resident. Typical starting values are 35-40 m²/person for dense occupancy, 45 m²/person as a moderate default, and 55-70 m²/person for lower occupancy.
            - **Temperature rise**: temperature difference between cold water and delivered hot water. 30-35 K is common; 40-45 K is a conservative high-temperature assumption.
            - **Profile timestep**: temporal resolution of the generated DHW profile. For this district planning tool, **1 hour is preferred** because the system simulation use hourly time steps. Use 30 or 15 minutes only when you intentionally want more detailed short-term DHW peaks for separate checks.
            - **Drawoff categories**: OpenDHW event detail. `1` is simpler and faster; `4` separates event categories more finely.
            - **Random seed**: controls reproducibility of the stochastic draw-off events. The value entered here is only the base seed; the tool derives a different deterministic seed for each building from its building id, so buildings with the same assumptions do not receive identical DHW patterns or perfectly overlapping peaks. Keeping the same base seed makes a rerun reproducible; changing it creates a different but still plausible set of profiles.
            - **Holiday country code**: country used for holiday-sensitive profile generation. `DE` is the default for Germany.
            """
        )

        st.markdown(
            """
            **Input data used for DHW estimation:** The tool uses the final building attributes from the previous steps: building type, footprint area and number of levels. The OpenDHW category, such as `SFH`, `MFH`, `AB`, `OB` or `SC` is selected based the building type. The footprint area and number of levels are used to infer the number of occupants or users. Other OpenDHW inputs include the occupancy-area assumption, draw-off volume, temperature rise, timestep, draw-off category and holiday country.  
            """
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            dhw_res_lppd = st.number_input(
                "Residential draw-off volume (L/person/day)",
                min_value=1.0,
                max_value=300.0,
                value=40.0,
                step=1.0,
                key="dhw_res_lppd",
                help="Representative values: 25-30 low, 40 default, 50-60 high.",
            )
            dhw_temp_delta = st.number_input(
                "Temperature rise (K)",
                min_value=1.0,
                max_value=80.0,
                value=35.0,
                step=1.0,
                key="dhw_temp_delta",
                help="Representative values: 30-35 K common, 40-45 K conservative.",
            )
        with c2:
            dhw_m2_per_person = st.number_input(
                "Residential occupancy area (m²/person)",
                min_value=5.0,
                max_value=200.0,
                value=45.0,
                step=1.0,
                key="dhw_m2_per_person",
                help="Representative values: 35-40 dense, 45 default, 55-70 lower occupancy.",
            )
            dhw_timestep_label = st.selectbox(
                "Profile timestep",
                ["1 hour", "30 minutes", "15 minutes"],
                index=0,
                key="dhw_timestep_label",
                help="Preferred: 1 hour, because the district planning scenarios use hourly time series. Shorter steps are mainly for peak-detail checks.",
            )
        with c3:
            dhw_categories = st.selectbox(
                "Drawoff categories",
                [1, 4],
                index=0,
                key="dhw_categories",
                help="1 is a compact event model; 4 separates draw-off events into more detailed categories.",
            )
            dhw_seed = st.number_input(
                "Random seed",
                min_value=0,
                max_value=2_147_483_647,
                value=42,
                step=1,
                key="dhw_seed",
                help="Base seed for reproducible stochastic profiles. A different building-specific seed is derived from each building id, so identical assumptions do not create identical peak patterns.",
            )

        c4, c5 = st.columns([1, 1])
        with c4:
            dhw_include_nonres = st.checkbox(
                "Include rough non-residential defaults",
                value=True,
                key="dhw_include_nonres",
                help="Enable this to estimate DHW for offices, schools, retail, hospitals and similar buildings from area-based user assumptions.",
            )
        with c5:
            dhw_nonres_multiplier = st.number_input(
                "Non-residential volume multiplier",
                min_value=0.1,
                max_value=10.0,
                value=1.0,
                step=0.1,
                key="dhw_nonres_multiplier",
                disabled=not dhw_include_nonres,
                help="Scales the built-in non-residential L/user/day defaults. Use 0.5 for low use, 1.0 default, 1.5-2.0 high use.",
            )

        dhw_country_code = st.text_input(
            "Holiday country code",
            value="DE",
            max_chars=2,
            key="dhw_country_code",
            help="Two-letter holiday country code used by OpenDHW; `DE` is used for Germany.",
        ).strip().upper() or "DE"

    if dhw_input_df.empty:
        st.info("Select at least one included building with area data to run OpenDHW.")

    dhw_timestep_seconds = {"1 hour": 3600, "30 minutes": 1800, "15 minutes": 900}[dhw_timestep_label]
    if st.button(
            "Estimate DHW Demand with OpenDHW",
            key="estimate_dhw_opendhw",
            disabled=dhw_input_df.empty,
    ):
        try:
            with st.spinner("Estimating domestic hot water demand with OpenDHW..."):
                total_dhw, dhw_estimates, dhw_profile = estimate_dhw_demand_with_opendhw(
                    buildings_df=dhw_input_df,
                    s_step=dhw_timestep_seconds,
                    categories=dhw_categories,
                    residential_l_per_person_day=dhw_res_lppd,
                    residential_m2_per_person=dhw_m2_per_person,
                    temp_delta=dhw_temp_delta,
                    include_nonres=dhw_include_nonres,
                    nonres_multiplier=dhw_nonres_multiplier,
                    seed=dhw_seed,
                    country_code=dhw_country_code,
                )
                ss["dhw_demand_estimates"] = dhw_estimates
                ss["dhw_load_profile"] = dhw_profile
                ss["total_dhw_demand"] = total_dhw
                ss["show_dhw_results"] = True
                ss["dhw_results_stale"] = False
                ss["_persist_dirty"] = True
            st.success("OpenDHW DHW run complete.")
            st.rerun()
        except Exception as e:
            st.error(f"OpenDHW run failed: {e}")

    dhw_estimates_display = ss.get("dhw_demand_estimates")
    if ss.get("show_dhw_results") and isinstance(dhw_estimates_display, pd.DataFrame) and not dhw_estimates_display.empty:
        dhw_estimates_display = _std_dhw_estimates_df(dhw_estimates_display)
        total_dhw = ss.get("total_dhw_demand")
        if total_dhw is None:
            total_dhw = float(
                pd.to_numeric(dhw_estimates_display["annual_dhw_demand_kWh"], errors="coerce").fillna(0).sum()
            )
            ss["total_dhw_demand"] = total_dhw

        if total_dhw >= 1_000_000:
            dhw_unit, dhw_divisor = "GWh", 1_000_000
        elif total_dhw >= 1_000:
            dhw_unit, dhw_divisor = "MWh", 1_000
        else:
            dhw_unit, dhw_divisor = "kWh", 1

        st.markdown(
            f"**Total Annual DHW Demand for the selected area: {total_dhw / dhw_divisor:.2f} {dhw_unit}**"
        )
        if dhw_unit != "kWh":
            st.markdown(f"Which is equivalent to: **{total_dhw:,.2f} kWh**")

        dhw_table = dhw_estimates_display.rename(columns={
            "area_m2": "area (m²)",
            "net_floor_area_m2": "net floor area (m²)",
            "opendhw_type": "OpenDHW type",
            "estimated_occupants": "estimated occupants/users",
            "water_l_per_person_day": "volume (L/person/day)",
            "mean_drawoff_l_per_day": "draw-off volume (L/day)",
            "annual_dhw_demand_kWh": "Annual DHW Demand (kWh)",
        })
        dhw_cols = [
            "building index", "building type", "area (m²)", "levels", "net floor area (m²)",
            "OpenDHW type", "estimated occupants/users", "volume (L/person/day)",
            "draw-off volume (L/day)", "Annual DHW Demand (kWh)", "basis", "status",
        ]
        st.dataframe(
            dhw_table[[c for c in dhw_cols if c in dhw_table.columns]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "area (m²)": st.column_config.NumberColumn("area (m²)", format="%.2f"),
                "levels": st.column_config.NumberColumn("levels", format="%.0f"),
                "net floor area (m²)": st.column_config.NumberColumn("net floor area (m²)", format="%.2f"),
                "estimated occupants/users": st.column_config.NumberColumn("estimated occupants/users", format="%.2f"),
                "volume (L/person/day)": st.column_config.NumberColumn("volume (L/person/day)", format="%.2f"),
                "draw-off volume (L/day)": st.column_config.NumberColumn("draw-off volume (L/day)", format="%.2f"),
                "Annual DHW Demand (kWh)": st.column_config.NumberColumn("Annual DHW Demand (kWh)", format="%.2f"),
            },
        )

    st.markdown("---")
    st.markdown("#### Annual Demand Check")
    total_dhw_check = ss.get("total_dhw_demand")
    summary_rows = [
        {"Demand component": "Space heating", "Annual demand (kWh/a)": float(total_heat_demand)},
        {
            "Demand component": "Domestic hot water",
            "Annual demand (kWh/a)": float(total_dhw_check) if total_dhw_check is not None else np.nan,
        },
    ]
    if total_dhw_check is not None:
        summary_rows.append({
            "Demand component": "Space heating + DHW",
            "Annual demand (kWh/a)": float(total_heat_demand) + float(total_dhw_check),
        })
    st.dataframe(
        pd.DataFrame(summary_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Annual demand (kWh/a)": st.column_config.NumberColumn("Annual demand (kWh/a)", format="%.2f"),
        },
    )
    if total_dhw_check is None:
        st.warning(
            "Run **Estimate Domestic Hot Water Demand** before saving if this project should include "
            "DHW demand in the heat supply scenarios."
        )

    curr_project = ss.get("current_project")
    demand_results_fresh = not ss.get("results_stale", False) and not ss.get("dhw_results_stale", False)
    if curr_project and total_dhw_check is not None and demand_results_fresh:
        demand_signature = f"{float(total_heat_demand):.6f}|{float(total_dhw_check):.6f}"
        ignored_signature = ss.get("_scenario_demand_sync_ignored")
        scenario_mismatches = _scenario_demand_mismatches(
            curr_project,
            total_heat_demand,
            total_dhw_check,
        )
        if scenario_mismatches and ignored_signature != demand_signature:
            scenario_names = ", ".join(path.stem for path in scenario_mismatches)
            st.warning(
                "The current demand differs from demand previously copied into these scenarios: "
                f"**{scenario_names}**. Should those saved scenario values also be updated? "
                "Previously simulated results will then be out of date until the project is submitted again."
            )
            sync_yes, sync_no = st.columns([1, 1])
            if sync_yes.button("Yes, update old scenarios", key="sync_scenario_demands_yes"):
                try:
                    updated = _update_scenario_demands(
                        scenario_mismatches,
                        total_heat_demand,
                        total_dhw_check,
                        ss.get("dhw_load_profile"),
                    )
                    ss.pop("_scenario_demand_sync_ignored", None)
                    st.success(f"Updated demand in {len(updated)} scenario(s). Submit again to refresh simulation results.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"The scenario demand values could not be updated: {exc}")
            if sync_no.button("No, keep old scenario values", key="sync_scenario_demands_no"):
                ss["_scenario_demand_sync_ignored"] = demand_signature
                st.rerun()

    if st.session_state.get("_persist_dirty", False):
        st.warning(
            "Areas, building changes, or demand results have not been saved yet. "
            "Use **Save areas, buildings & results** below before leaving this page."
        )
    save_success_message = st.session_state.pop("_save_success_message", None)
    if save_success_message:
        st.success(save_success_message)
    save_warning_message = st.session_state.pop("_save_warning_message", None)
    if save_warning_message:
        st.warning(save_warning_message)

    # --- bottom save-all button ---
    st.markdown("---")

    has_areas = bool(st.session_state.get("drawn_polygons"))
    has_estimates = (
            st.session_state.get("building_demand_estimates") is not None
            and st.session_state.get("total_heat_demand") is not None
    )
    has_dhw_estimates = (
            isinstance(st.session_state.get("dhw_demand_estimates"), pd.DataFrame)
            and not st.session_state["dhw_demand_estimates"].empty
            and st.session_state.get("total_dhw_demand") is not None
    )

    if st.button("💾 Save areas, buildings & results", key="save_main", # type="primary"
                 ):
        st.session_state["_save_intent"] = True

    # Render confirm flow if there's an intent to save
    if st.session_state.get("_save_intent"):
        curr_project = st.session_state.get("current_project")

        if not curr_project:
            st.warning("No current project set — cannot save.")
            st.session_state.pop("_save_intent", None)

        elif not has_areas:
            st.info("Draw at least one area on the map to enable saving.")
            st.session_state.pop("_save_intent", None)

        elif not has_estimates:
            st.warning(
                "No space-heating estimation found. "
                "You can save areas/buildings now (result sheets will be empty), "
                "or run **Estimate Space Heating Demand with TEASER** first."
            )
            c1, c2 = st.columns([1, 1])
            with c1:
                proceed = st.button("✅ Save anyway (without results)", key="save_anyway")
            with c2:
                cancel = st.button("❌ Cancel", key="cancel_save")
            if cancel:
                st.session_state.pop("_save_intent", None)
                st.stop()
            if not proceed:
                st.stop()

        elif not has_dhw_estimates:
            st.warning(
                "No DHW demand estimation found. Run **Estimate Domestic Hot Water Demand** "
                "before saving the project results."
            )
            st.stop()

        # --- actually perform the save (shared for both paths) ---
        polygons = st.session_state.get("drawn_polygons", [])
        excluded = st.session_state.get("excluded_bidx", set())
        manual = st.session_state.get("manual_buildings",
                                      pd.DataFrame(columns=[
                                          "bidx", "name", "building type", "area (m²)", "year built", "retrofit situation",
                                          "building type source", "year source", "zensus age class", "zensus type class",
                                          "zensus type note", "zensus year note", "zensus source",
                                      ]))
        overrides = st.session_state.get("building_attrs",
                                         pd.DataFrame(columns=[
                                             "bidx", "building type", "area (m²)", "address", "year built",
                                             "retrofit situation",
                                         ]))

        g1 = st.session_state.get("_last_buildings_gdf")
        g2 = st.session_state.get("_preloaded_buildings_gdf")
        buildings_gdf = g1 if (g1 is not None and not getattr(g1, "empty", True)) else (
            g2 if (g2 is not None and not getattr(g2, "empty", True)) else None
        )

        estimates_df = st.session_state.get("building_demand_estimates") if has_estimates else None
        info_over_df = st.session_state.get("info_overridden") if has_estimates else None
        total_kwh = st.session_state.get("total_heat_demand") if has_estimates else None
        route_len_m = st.session_state.get("route_length")
        dhw_estimates_df = st.session_state.get("dhw_demand_estimates")
        dhw_profile_df = st.session_state.get("dhw_load_profile")
        total_dhw_kwh = st.session_state.get("total_dhw_demand")

        save_project_dataset_v2(
            project_name=curr_project,
            polygons_wkt=polygons,
            buildings_gdf=buildings_gdf,
            excluded_bidx=excluded,
            manual_buildings_df=manual,
            building_attrs_df=overrides,
            estimates_df_final=estimates_df,
            estimates_df_original=st.session_state.get("building_demand_estimates_original"),
            # info_overridden_df=info_over_df,
            total_heat_demand_kwh=total_kwh,
            route_length_m=route_len_m,
            dhw_estimates_df=dhw_estimates_df,
            dhw_profile_df=dhw_profile_df,
            total_dhw_demand_kwh=total_dhw_kwh,
        )

        if not has_estimates:
            st.session_state["_save_warning_message"] = (
                "Saved areas and buildings without space-heating results. Run **Estimate Space Heating Demand "
                "with TEASER** and save again to record results."
            )
        st.session_state.pop("_save_intent", None)
        st.session_state["_persist_dirty"] = False
        if has_estimates:
            st.session_state["_save_success_message"] = (
                "Saved areas, buildings & results to the project workbook."
            )
        st.rerun()

    if st.button("🔄 Reset all drawn areas"):
        st.session_state["reset_confirm"] = True

    if st.session_state.get("reset_confirm"):
        st.warning(
            "This will **delete all drawn areas** for this project, "
            "and clear the associated results."
        )
        c1, c2 = st.columns([1, 1])

        with c1:
            if st.button("✅ Yes, delete all areas", key="confirm_reset"):
                _reset_drawn_areas_for_project()
                st.session_state.pop("reset_confirm", None)
                st.success("All drawn areas and associated results have been reset for this project.")
                st.rerun()

        with c2:
            if st.button("❌ Cancel reset", key="cancel_reset"):
                st.session_state.pop("reset_confirm", None)
                st.rerun()
