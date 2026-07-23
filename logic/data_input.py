import streamlit as st
import openpyxl
import numpy as np
import pandas as pd
import os
from pathlib import Path
import shutil
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
import osmnx as ox
import geopy.distance
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import pyproj
import geopandas as gpd
import json
import importlib
from importlib import import_module
from shapely import wkt as _wkt
from shapely.geometry import shape as _shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry import shape, Polygon, MultiPolygon, LinearRing, mapping
from shapely.ops import unary_union
from shapely.geometry import Point

from logic.project_paths import (
    get_projects_root, get_project_dir, get_scenario_dir_for_project, get_project_results_dir,
    ensure_project_dirs, project_meta_path, input_done_flag_path,
    TEMPLATE_CEN_FILE, TEMPLATE_DEC_FILE, PV_COP_FILE
)
from logic.project_run_state import (
    project_input_fingerprint, read_run_status, results_match_current_inputs, write_run_status,
)
from logic.building_demand_analyse import _to_bool_series
from logic.dwd_weather import get_location_weather, heat_pump_cop_profiles

try:
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(Path(__file__).resolve().parents[1] / "cache" / "osmnx")
except Exception:
    pass

st.set_page_config(layout="wide")

SPACE_HEATING_DEMAND_LABEL = "Last_SH"
DHW_DEMAND_LABEL = "Last_DHW"
HEAT_LOSS_DEMAND_LABEL = "Loss"
OLD_HEAT_LOSS_DEMAND_LABEL = "Network_Heat_Loss"
DHW_PROFILE_COLUMN = "Last_DHW.fix"

########################################################################################################################

# -------------------------------
# 🌍 Data for climate assignment
known_city_coords = {
    "Hamburg": (53.5511, 9.9937),
    "Berlin": (52.52, 13.405),
    "Frankfurt": (50.1109, 8.6821),
    "Cologne": (50.9375, 6.9603),
    "Leipzig": (51.3397, 12.3731),
    "Dresden": (51.0504, 13.7373),
    "Munich": (48.1351, 11.5820),
    "Freiburg": (47.9990, 7.8421),
    "Nuremberg": (49.4521, 11.0767),
    "Stuttgart": (48.7758, 9.1829),
    "Kiel": (54.3233, 10.1228),
}

climate_zones = {
    "Hamburg": "Zone 1",
    "Berlin": "Zone 2",
    "Frankfurt": "Zone 3",
    "Cologne": "Zone 4",
    "Leipzig": "Zone 5",
    "Dresden": "Zone 6",
    "Munich": "Zone 7",
    "Freiburg": "Zone 8",
    "Nuremberg": "Zone 9",
    "Stuttgart": "Zone 10",
    "Kiel": "Zone 11",
}

zone_to_city = {v: k for k, v in climate_zones.items()}

tech_options = {
    "LPM": ["Biomass Boiler"],
    "BM_W": ["Biomass Boiler"],
    "BM_P": ["Biomass Boiler"],
    "NG": ["Gas Boiler", "CHP_NG"],
    "BG": ["Biogas Boiler", "CHP_BG"],
    "el": ["P2H", "ASHP", "GSHP_C", "GSHP_B", "WWSHP"],
    "DHS": ["DHS"],
}

display_names = {
    "Biomass Boiler": "Biomass Boiler",
    "Gas Boiler": "Gas Boiler",
    "Biogas Boiler": "Biogas Boiler",
    "CHP_NG": "Combined Heat and Power (Natural Gas)",
    "CHP_BG": "Combined Heat and Power (Biogas)",
    "P2H": "Power to Heat",
    "ASHP": "Air Source Heat Pump",
    "GSHP_C": "Ground Source Heat Pump (Shallow Collector)",
    "GSHP_B": "Ground Source Heat Pump (Borehole)",
    "WWSHP": "Waste Water Source Heat Pump",
    "DHS": "Existing District Heating systems",
}
rev_display_names = {v: k for k, v in display_names.items()}

tech_sheet_map = {
    "Biomass Boiler": ("transformers", "B_BM"),
    "Gas Boiler": ("transformers", "B_NG"),
    "Biogas Boiler": ("transformers", "B_BG"),
    "CHP_NG": ("chp", "CHP_NG"),
    "CHP_BG": ("chp", "CHP_BG"),
    "P2H": ("transformers", "P2H"),
    "ASHP": ("hp", "ASHP"),
    "GSHP_C": ("hp", "GSHP_C"),
    "GSHP_B": ("hp", "GSHP_B"),
    "WWSHP": ("hp", "WWSHP"),
    "DHS": ("transformers", "DHS"),
}

INTERNAL_HP_SOURCES = {"GS_C", "GS_B", "WW"}

climate_data = {
    "Climate Zone": [
        "Zone 1", "Zone 2", "Zone 3", "Zone 4", "Zone 5", "Zone 6", "Zone 7", "Zone 8", "Zone 9", "Zone 10", "Zone 11"
    ],
    "Representative City": [
        "Hamburg", "Berlin", "Frankfurt", "Cologne", "Leipzig", "Dresden", "Munich", "Freiburg", "Nuremberg", "Stuttgart", "Kiel"
    ],
    "Region": [
        "North", "Northeast", "Central-West", "West (NRW)", "Central-East", "Southeast Saxony", "South Bavaria",
        "Southwest", "North Bavaria", "Southwest BW", "Far North Coast"
    ],
    "Similar Cities": [
        "Bremen, Hannover, Kiel", "Potsdam, Cottbus, Brandenburg", "Wiesbaden, Mainz, Gießen", "Düsseldorf, Bonn, Aachen",
        "Halle, Erfurt, Jena", "Chemnitz, Zwickau", "Augsburg, Rosenheim, Regensburg",
        "Offenburg, Lörrach, Villingen-Schwenningen", "Würzburg, Bayreuth", "Heilbronn, Tübingen, Ulm", "Flensburg, Lübeck"
    ],
    "Climate Notes": [
        "Maritime: mild winters, cool summers, humid",
        "Continental mix: cold winters, warm summers",
        "Moderate central climate, good average case",
        "Maritime-influenced, mild winters, high humidity",
        "Dry inland, more continental, moderate heating loads",
        "Cold winters, sunny and dry, near Erzgebirge",
        "Alpine influence, cold winters, snow loads",
        "Warmest and sunniest in Germany",
        "Mix of inland cold and solar gains, moderate climate",
        "Warmer inland, good solar, dense urbanization",
        "Extreme maritime, cool and humid, low solar gains"
    ]
}

########################################################################################################################

def reset_building_estimation_state():
    """
    Clear all state that belongs to the heat-demand / building estimation UI.
    Call this when switching or deleting projects so old polygons/buildings
    don't leak into the new project.
    """
    KEYS = [
        # core selection / geometry
        "drawn_polygons",
        "polygons",
        "selected_polygons",
        "polygons_geojson",
        "polygons_wkt",

        # buildings & aliases
        "buildings_gdf",
        "osm_buildings_gdf",
        "buildings_osm_gdf",
        "selected_buildings_gdf",
        "gdf_osm",
        "gdf_selected",
        "gdf_buildings",
        "osm_gdf",
        "bldg_gdf",

        # manual buildings
        "manual_buildings",
        "manual_gdf",
        "manual_buildings_df",

        # indices / exclusions
        "bidx_map",
        "next_bidx",
        "excluded_bidx",
        "excluded",
        "excluded_list",

        # attributes / info
        "building_attrs",
        "attrs_df",
        "info_overridden",
        "building_info",

        # demand results
        "building_demand_estimates",
        "building_demand_estimates_original",
        "scenario_building_count",
        "estimates_final_df",
        "estimates_original_df",
        "total_heat_demand",
        "total_heat_demand_kwh",
        "total_heat_kwh",
        "total_dhw_demand",
        "total_dhw_demand_kwh",
        "total_dhw_kwh",
        "dhw_demand_estimates",
        "dhw_load_profile",
        "show_dhw_results",
        "dhw_results_stale",
        "heated_area_factor",
        "route_length",
        "route_length_m",
        "route_m",
        "route_length_source",
        "network_length_polygon_wkt",
        "network_length_polygons_wkt",
        "network_length_osm_buildings",
        "network_length_included_buildings",
        "network_length_excluded_ids",
        "network_length_last_building_click",
        "network_length_estimate_m",
        "network_length_manual_m",
        "network_length_mode",
        "network_length_map_rev",

        # OSM preload / cached data
        "_osm_preloaded",
        "_preloaded_buildings_gdf",
        "_preloaded_drawn_polygons",
        "_last_buildings_gdf",
        "_buildings_utm",

        # ✅ baseline snapshot / project switching guards
        "_osm_base_attrs",
        "_last_project",

        # map UI state
        "map_center",
        "map_zoom",
        "pending_center",
        "pending_zoom",

        # misc UI flags
        "_persist_dirty",
        "results_stale",
        "show_teaser_results",
        "attr_demand_conflicts",
        "demand_conflict_policy",
        "demand_conflict_snapshot",
        "last_attr_changed_set",
        "attr_change_fingerprint",
        "hl_show",
        "edit_heat_toggle",
        "_reset_edit_heat_toggle",

        # (optional) BDA UI gates — fine to clear, but don’t *force-set* them elsewhere
        "bda_ui_open",
        "bda_force_refresh",
    ]

    for k in KEYS:
        st.session_state.pop(k, None)

########################################################################################################################

def list_projects():
    root = get_projects_root()
    if not os.path.exists(root):
        return []
    return [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]

def get_current_project():
    # Fallback to a default if none chosen yet
    return st.session_state.get("current_project")

def load_project_meta_into_session(project_name: str):
    try:
        with open(project_meta_path(project_name), "r", encoding="utf-8") as f:
            meta = json.load(f)

        # The full hourly series is intentionally reloaded for this project's
        # coordinates. Never reuse a previous project's in-memory DWD profile.
        for weather_key in (
            "dwd_weather", "_dwd_weather_attempted_coords", "weather_data_warning",
            "weather_data_message", "design_outdoor_temperature_c", "ground_temperature_c",
            "dwd_weather_station", "dwd_weather_station_id", "dwd_weather_year",
            "dwd_profile_years_used", "dwd_available_complete_years",
            "dwd_available_complete_year_count", "dwd_station_distance_km",
            "dwd_profile_method", "dwd_ground_method", "dwd_weather_source",
        ):
            st.session_state.pop(weather_key, None)

        pc = meta.get("project_coords")
        if isinstance(pc, (list, tuple)) and len(pc) == 2:
            try:
                pc = (float(pc[0]), float(pc[1]))
            except (TypeError, ValueError):
                pc = None
        else:
            pc = None

        st.session_state["project_name"]         = meta.get("project_name", project_name)
        st.session_state["project_city_name"]    = meta.get("project_city_name", "")
        st.session_state["project_coords"]       = pc
        st.session_state["project_zoom"]         = meta.get("project_zoom", 12)

        if pc and st.session_state["project_city_name"]:
            st.session_state["_last_city_query"] = st.session_state["project_city_name"].strip()
        else:
            st.session_state["_last_city_query"] = ""

        cz = meta.get("project_climate_zone", "")
        if not cz and pc:
            # infer nearest climate city
            nearest = min(
                known_city_coords.items(),
                key=lambda item: geopy.distance.distance(pc, item[1]).km
            )
            cz = climate_zones.get(nearest[0], "Unknown")

        st.session_state["project_climate_zone"] = cz or ""
        for key in (
            "design_outdoor_temperature_c", "ground_temperature_c", "dwd_weather_station",
            "dwd_weather_station_id", "dwd_weather_year", "dwd_profile_years_used",
            "dwd_available_complete_years", "dwd_available_complete_year_count",
            "dwd_station_distance_km", "dwd_profile_method", "dwd_ground_method",
            "dwd_weather_source",
        ):
            if meta.get(key) is not None:
                st.session_state[key] = meta.get(key)
        if meta.get("route_length") is not None:
            st.session_state["route_length"] = float(meta["route_length"])
        if meta.get("route_length_source"):
            st.session_state["route_length_source"] = str(meta["route_length_source"])

    except FileNotFoundError:
        return
    except Exception as e:
        st.warning(f"Could not load project settings: {e}")

def save_project_meta_from_session(project_name: str):
    meta = {
        "project_name":         st.session_state.get("project_name", project_name),
        "project_city_name":    st.session_state.get("project_city_name", ""),
        "project_climate_zone": st.session_state.get("project_climate_zone", ""),
        "project_coords":       st.session_state.get("project_coords", None),
        "project_zoom":         st.session_state.get("project_zoom", 12),
        "route_length":         st.session_state.get("route_length"),
        "route_length_source":  st.session_state.get("route_length_source"),
        "design_outdoor_temperature_c": st.session_state.get("design_outdoor_temperature_c"),
        "ground_temperature_c": st.session_state.get("ground_temperature_c"),
        "dwd_weather_station": (st.session_state.get("dwd_weather") or {}).get(
            "station_name", st.session_state.get("dwd_weather_station")
        ),
        "dwd_weather_station_id": (st.session_state.get("dwd_weather") or {}).get(
            "station_id", st.session_state.get("dwd_weather_station_id")
        ),
        "dwd_weather_year": (st.session_state.get("dwd_weather") or {}).get(
            "year", st.session_state.get("dwd_weather_year")
        ),
        "dwd_profile_years_used": (st.session_state.get("dwd_weather") or {}).get(
            "profile_years_used", st.session_state.get("dwd_profile_years_used")
        ),
        "dwd_available_complete_year_count": (st.session_state.get("dwd_weather") or {}).get(
            "available_complete_year_count", st.session_state.get("dwd_available_complete_year_count")
        ),
        "dwd_available_complete_years": (st.session_state.get("dwd_weather") or {}).get(
            "available_complete_years", st.session_state.get("dwd_available_complete_years")
        ),
        "dwd_station_distance_km": (st.session_state.get("dwd_weather") or {}).get(
            "station_distance_km", st.session_state.get("dwd_station_distance_km")
        ),
        "dwd_profile_method": (st.session_state.get("dwd_weather") or {}).get(
            "profile_method", st.session_state.get("dwd_profile_method")
        ),
        "dwd_ground_method": (st.session_state.get("dwd_weather") or {}).get(
            "ground_method", st.session_state.get("dwd_ground_method")
        ),
        "dwd_weather_source": (st.session_state.get("dwd_weather") or {}).get(
            "source", st.session_state.get("dwd_weather_source")
        ),
    }
    os.makedirs(get_project_dir(project_name), exist_ok=True)
    with open(project_meta_path(project_name), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def reset_project_session():
    """Clear location- and climate-related state so a new project starts 'clean'."""
    defaults = {
        "project_city_name": "",
        "project_coords": None,
        "project_zoom": 12,
        "project_climate_zone": "",
        "location_method": "Search by city/area",
        "manual_coords_set": False,
        "map_coords_set": False,
        "building_demand_estimates": None,
        "route_length": None,
        "route_length_source": None,
        "total_heat_demand": None,
        "show_building_estimate": False,
        "dwd_weather": None,
        "design_outdoor_temperature_c": None,
        "ground_temperature_c": None,
        "dwd_weather_station": None,
        "dwd_weather_station_id": None,
        "dwd_weather_year": None,
        "dwd_profile_years_used": None,
        "dwd_available_complete_years": None,
        "dwd_available_complete_year_count": None,
        "dwd_station_distance_km": None,
        "dwd_profile_method": None,
        "dwd_ground_method": None,
        "dwd_weather_source": None,
    }
    for k, v in defaults.items():
        st.session_state[k] = v

    st.session_state.pop("_meta_loaded_for", None)

def delete_project(project_name: str):
    """Delete a whole project folder + its results folder (with confirmation done in UI)."""
    proj_dir = Path(get_project_dir(project_name))
    res_dir  = Path(get_project_results_dir(project_name))  # results/<project>

    deleted_any = False

    # Delete project input data
    if proj_dir.exists():
        shutil.rmtree(proj_dir)
        deleted_any = True

    # Delete simulation results
    if res_dir.exists():
        shutil.rmtree(res_dir)
        deleted_any = True

    if deleted_any:
        st.success(f"Project '{project_name}' deleted (including results).")
    else:
        st.info("Project already gone.")

    # Clear selection + state if we deleted the currently open project
    if st.session_state.get("current_project") == project_name:
        st.session_state.pop("current_project", None)
        st.session_state.pop("_meta_loaded_for", None)
        reset_project_session()
        reset_building_estimation_state()


def _project_has_simulation_results(project_name: str | None = None) -> bool:
    project_name = project_name or get_current_project()
    if not project_name:
        return False
    results_dir = Path(get_project_results_dir(project_name))
    return results_dir.exists() and any(results_dir.rglob("results.xlsx"))


def _delete_project_simulation_results(project_name: str) -> bool:
    """Delete results after an explicitly confirmed input update."""
    results_dir = Path(get_project_results_dir(project_name))
    if not results_dir.exists():
        return False
    shutil.rmtree(results_dir)
    return True


def duplicate_project(source_name: str, new_name: str) -> None:
    """Duplicate project inputs and any completed results under a new name."""
    source_dir = Path(get_project_dir(source_name))
    target_dir = Path(get_project_dir(new_name))
    target_results = Path(get_project_results_dir(new_name))
    if not source_dir.exists():
        raise FileNotFoundError(f"Project '{source_name}' does not exist.")
    if target_dir.exists():
        raise FileExistsError(f"Project '{new_name}' already exists.")
    if target_results.exists():
        raise FileExistsError(f"A results folder named '{new_name}' already exists.")

    shutil.copytree(source_dir, target_dir)
    copied_flag = target_dir / "input_done.flag"
    if copied_flag.exists():
        copied_flag.unlink()

    meta_path = Path(project_meta_path(new_name))
    if meta_path.exists():
        with meta_path.open("r", encoding="utf-8") as stream:
            meta = json.load(stream)
        meta["project_name"] = new_name
        with meta_path.open("w", encoding="utf-8") as stream:
            json.dump(meta, stream, ensure_ascii=False, indent=2)

    for scenario_file in Path(get_scenario_dir_for_project(new_name)).glob("*.xlsx"):
        update_meta_sheet(str(scenario_file), {"Project Name": new_name})

    source_results = Path(get_project_results_dir(source_name))
    if source_results.exists():
        shutil.copytree(source_results, target_results)
        old_status = read_run_status(source_name)
        if old_status.get("state") == "completed":
            details = {
                key: value for key, value in old_status.items()
                if key not in {"project_name", "state", "updated_at", "input_fingerprint"}
            }
            write_run_status(
                new_name,
                "completed",
                input_fingerprint=project_input_fingerprint(new_name),
                **details,
            )

def submit_project(project_name: str):
    """Mark this project as ready (per your previous global flag approach)."""
    proj_dir = get_project_dir(project_name)
    os.makedirs(proj_dir, exist_ok=True)

    # Optional: project-local flag
    project_flag_path = os.path.join(proj_dir, "input_done.flag")
    with open(project_flag_path, "w", encoding="utf-8") as f:
        f.write("done")

    # 🔧 Global flag + metadata for the main script
    global_flag_path = input_done_flag_path()

    input_fingerprint = project_input_fingerprint(project_name)
    flag_payload = {
        "project_name": project_name,
        "input_fingerprint": input_fingerprint,
    }

    write_run_status(project_name, "queued", input_fingerprint=input_fingerprint)

    with open(global_flag_path, "w", encoding="utf-8") as f:
        json.dump(flag_payload, f)

    st.success(f"✅ Project '{project_name}' submitted.")

def set_current_project(project_name: str):
    prev = st.session_state.get("current_project")

    if prev != project_name:
        # One clean wipe is enough
        reset_building_estimation_state()

        # Also clear project-switch guards that are outside the building reset
        for k in ["_auto_open_attempted", "_rehydrate_needed"]:
            st.session_state.pop(k, None)
        for k in ["current_scenario_file", "last_selected_scenario", "scenario_selectbox"]:
            st.session_state.pop(k, None)

    st.session_state["current_project"] = project_name
    st.session_state["results_project"] = project_name
    st.session_state["results_project_selector"] = project_name
    ensure_project_dirs(project_name)

    # Optional: keep UI clean
    st.session_state.pop("show_building_estimate", None)

def _get_map_loader():
    """
    Try to resolve a loader function from logic.building_demand_analyse,
    returning a callable or None.
    """
    try:
        bda = import_module("logic.building_demand_analyse")
    except Exception as e:
        st.warning(f"Could not import building_demand_analyse: {e}")
        return None

    for name in (
        "load_project_dataset_v2",
        "load_project_dataset",
        "load_saved_selection",
        "load_project_state",
    ):
        loader = getattr(bda, name, None)
        if callable(loader):
            return loader

    st.warning("No compatible loader found in logic.building_demand_analyse.")
    return None

def _bridge_bda_session_keys():
    ss = st.session_state

    if "drawn_polygons" in ss:
        current_polygons = ss.get("drawn_polygons") or []
        ss["polygons"] = list(current_polygons) if isinstance(current_polygons, list) else current_polygons
        ss["selected_polygons"] = list(current_polygons) if isinstance(current_polygons, list) else current_polygons
        ss["polygons_wkt"] = list(current_polygons) if isinstance(current_polygons, list) else current_polygons

        # Build polygons_geojson if possible
        try:
            polys = current_polygons
            features = []

            if isinstance(polys, list) and polys and isinstance(polys[0], str):
                for s in polys:
                    try:
                        g = _wkt.loads(s)
                        features.append({"type": "Feature", "properties": {}, "geometry": mapping(g)})
                    except Exception:
                        pass

            elif isinstance(polys, list) and polys and isinstance(polys[0], dict) and "type" in polys[0]:
                for f in polys:
                    if f.get("type") == "Feature":
                        features.append(f)
                    else:
                        features.append({"type": "Feature", "properties": {}, "geometry": f})

            elif isinstance(polys, dict) and polys.get("type") == "FeatureCollection":
                features = polys.get("features", [])

            ss["polygons_geojson"] = {"type": "FeatureCollection", "features": features}

            if "building_attrs" in ss:
                ss["attrs_df"] = ss["building_attrs"]
        except Exception:
            pass

    # 2) Exclusions
    if "excluded_bidx" in ss:
        ss.setdefault("excluded", ss["excluded_bidx"])
        if isinstance(ss["excluded_bidx"], set):
            ss.setdefault(
                "excluded_list",
                sorted(ss["excluded_bidx"], key=lambda x: int(x) if str(x).isdigit() else str(x))
            )

    # 3) Buildings tables
    if "buildings_gdf" in ss and hasattr(ss["buildings_gdf"], "columns") and "bidx" in ss["buildings_gdf"].columns:
        try:
            g = ss["buildings_gdf"].copy()
            g["bidx"] = g["bidx"].astype(str)
            g = g.sort_values(by="bidx", key=lambda s: pd.to_numeric(s, errors="coerce")).reset_index(drop=True)
            ss["buildings_gdf"] = g
        except Exception:
            pass

    if "buildings_gdf" in ss:
        ss["osm_buildings_gdf"] = ss["buildings_gdf"]
        ss["buildings_osm_gdf"] = ss["buildings_gdf"]
        ss["buildings"] = ss["buildings_gdf"]

    if "manual_buildings" in ss:
        ss.setdefault("manual_gdf", ss["manual_buildings"])
        ss.setdefault("manual_buildings_df", ss["manual_buildings"])

    if "building_attrs" in ss:
        ss.setdefault("attrs_df", ss["building_attrs"])

    # 4) Derived info tables / estimates
    if "info_overridden" in ss:
        ss.setdefault("building_info", ss["info_overridden"])

    if "building_demand_estimates" in ss:
        ss.setdefault("estimates_final_df", ss["building_demand_estimates"])
    if "building_demand_estimates_original" in ss:
        ss.setdefault("estimates_original_df", ss["building_demand_estimates_original"])

    # 5) Totals & route length (multiple spellings are common)
    if "total_heat_demand" in ss:
        ss.setdefault("total_heat_demand_kwh", ss["total_heat_demand"])
        ss.setdefault("total_heat_kwh", ss["total_heat_demand"])
    if "total_dhw_demand" in ss:
        ss.setdefault("total_dhw_demand_kwh", ss["total_dhw_demand"])
        ss.setdefault("total_dhw_kwh", ss["total_dhw_demand"])
    if "route_length" in ss:
        ss.setdefault("route_length_m", ss["route_length"])
        ss.setdefault("route_m", ss["route_length"])

def _gdf_from_any(obj):
    """Best-effort: turn a variety of things into a GeoDataFrame or DataFrame."""

    # Already a GeoDataFrame
    if 'geopandas' in str(type(obj)).lower():
        return obj

    # Plain DataFrame with geometry column?
    if isinstance(obj, pd.DataFrame) and ('geometry' in obj.columns or 'geom' in obj.columns):
        if gpd is not None:
            col = 'geometry' if 'geometry' in obj.columns else 'geom'
            return gpd.GeoDataFrame(obj.copy(), geometry=col, crs="EPSG:4326")
        return obj  # fallback as DataFrame

    # GeoJSON FeatureCollection / features
    if isinstance(obj, dict) and obj.get('type') == 'FeatureCollection':
        feats = obj.get('features', [])
        rows = []
        geoms = []
        for f in feats:
            props = (f.get('properties') or {}).copy()
            geom = f.get('geometry')
            if geom:
                try:
                    if gpd is not None:
                        geoms.append(shape(geom))
                    else:
                        geoms.append(geom)
                except Exception:
                    geoms.append(None)
            rows.append(props)
        df = pd.DataFrame(rows)
        if gpd is not None:
            return gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")
        return df

    # List of features (geojson)
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and obj[0].get('type') in ('Feature', 'Polygon', 'MultiPolygon'):
        fc = {'type': 'FeatureCollection', 'features': obj if obj[0].get('type') == 'Feature'
              else [{'type': 'Feature', 'properties': {}, 'geometry': g} for g in obj]}
        return _gdf_from_any(fc)

    return None

def _set_all_building_keys(gdf):
    """Publish the same buildings GDF under many common session keys."""
    gdf = gdf.copy()
    if "bidx" not in gdf.columns or gdf["bidx"].isna().all():
        # legacy fallback ONLY
        gdf["bidx"] = gdf.index.astype(str)
    gdf["bidx"] = gdf["bidx"].astype(str)

    st.session_state["buildings_gdf"] = gdf
    aliases = [
        "osm_buildings_gdf", "buildings_osm_gdf", "selected_buildings_gdf", "gdf_osm",
        "gdf_selected", "gdf_buildings", "osm_gdf", "bldg_gdf"
    ]
    for k in aliases:
        st.session_state[k] = gdf

def _promote_manual_to_gdf(df):
    """Best effort: turn manual_buildings into a GeoDataFrame (WKT or lon/lat)."""
    m = df.copy()
    geom_col = None

    # Case A: WKT in a column named 'geometry' or 'wkt'
    for cand in ["geometry", "wkt", "geom_wkt", "GEOMETRY"]:
        if cand in m.columns and m[cand].notna().any():
            try:
                m["geometry"] = m[cand].apply(lambda s: _wkt.loads(s) if isinstance(s, str) else None)
                geom_col = "geometry"
                break
            except Exception:
                pass

    # Case B: lon/lat
    if geom_col is None:
        lon_cols = [c for c in m.columns if c.lower() in ("lon", "lng", "longitude", "x")]
        lat_cols = [c for c in m.columns if c.lower() in ("lat", "latitude", "y")]
        if lon_cols and lat_cols:
            lc, ac = lon_cols[0], lat_cols[0]
            try:
                m["geometry"] = [
                    Point(float(lo), float(la)) if pd.notna(lo) and pd.notna(la) else None
                    for lo, la in zip(m[lc], m[ac])
                ]
                geom_col = "geometry"
            except Exception:
                pass

    if geom_col is None or m["geometry"].isna().all():
        return None

    gdf = gpd.GeoDataFrame(m, geometry="geometry", crs="EPSG:4326")
    if "area_m2" not in gdf.columns:
        try:
            gtmp = gdf.to_crs(3857)
            gdf["area_m2"] = gtmp.area
            gdf = gdf.to_crs(4326)
        except Exception:
            gdf["area_m2"] = None
    if "building" not in gdf.columns:
        gdf["building"] = None
    return gdf[["bidx"]]\
        .join(gdf.drop(columns=["bidx"], errors="ignore"), how="right") if "bidx" in gdf.columns else gdf

@st.cache_data(show_spinner=False, ttl=24 * 60 * 60, max_entries=32)
def _cached_osm_buildings(union_wkt: str):
    """Cache the slow Overpass request across reruns and scenario pages."""
    polygon = _wkt.loads(union_wkt)
    return ox.geometries_from_polygon(polygon, tags={"building": True})


def _rehydrate_osm_buildings_from_polygons(drawn_polygons):
    """Query OSM building footprints inside saved polygons.
       Accepts:
       - GeoJSON FeatureCollection
       - List[Feature]
       - List[{"lat":..,"lng":..}]  (Leaflet)
       - List[[lat, lon], ...]      (raw rings)
    """
    if not drawn_polygons:
        return None

    def _ring_from_latlngs(seq):
        coords = []
        for p in seq:
            if isinstance(p, dict) and "lat" in p and "lng" in p:
                coords.append((p["lng"], p["lat"]))  # shapely wants (x=lon, y=lat)
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                coords.append((float(p[1]), float(p[0])))
        if len(coords) >= 3:
            # ensure closed ring
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            try:
                LinearRing(coords)  # validates
                return Polygon(coords)
            except Exception:
                return None
        return None

    shapes = []

    if isinstance(drawn_polygons, list) and drawn_polygons and isinstance(drawn_polygons[0], str):
        try:
            for s in drawn_polygons:
                try:
                    g = _wkt.loads(s)
                    if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty:
                        shapes.append(g)
                except Exception:
                    pass
        except Exception:
            pass

    # Case A: GeoJSON FeatureCollection or list of Features
    feats = drawn_polygons
    if isinstance(drawn_polygons, dict) and drawn_polygons.get("type") == "FeatureCollection":
        feats = drawn_polygons.get("features", [])

    if isinstance(feats, list) and feats and isinstance(feats[0], dict) and feats[0].get("type") == "Feature":
        for f in feats:
            geom = f.get("geometry")
            if not geom:
                continue
            try:
                shp = shape(geom)
                if isinstance(shp, (Polygon, MultiPolygon)):
                    shapes.append(shp)
            except Exception:
                pass

    # Case B: plain list of lat/lng dicts or [lat,lon] pairs
    elif isinstance(feats, list) and feats:
        if isinstance(feats[0], dict) and {"lat","lng"} <= set(feats[0].keys()):
            poly = _ring_from_latlngs(feats)
            if poly is not None:
                shapes.append(poly)
        elif isinstance(feats[0], (list, tuple)):
            if feats and isinstance(feats[0][0], (list, tuple, dict)):
                for ring in feats:
                    poly = _ring_from_latlngs(ring)
                    if poly is not None:
                        shapes.append(poly)
            else:
                poly = _ring_from_latlngs(feats)
                if poly is not None:
                    shapes.append(poly)

    if not shapes:
        return None

    poly = unary_union(shapes)
    if poly.is_empty:
        return None

    try:
        gdf = _cached_osm_buildings(poly.wkt)
    except Exception as e:
        st.warning(f"OSM building query failed: {e}")
        return None

    if gdf is None or gdf.empty:
        return None

    gdf = gdf.reset_index()
    if "element_type" in gdf.columns and "element" not in gdf.columns:
        gdf = gdf.rename(columns={"element_type": "element"})
    gdf = gdf.loc[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    if gdf.empty:
        return None

    g3857 = gdf.to_crs(3857)
    gdf["area_m2"] = g3857.area
    gdf = gdf.to_crs(4326)

    for col, default in [("name", None), ("building", None)]:
        if col not in gdf.columns: gdf[col] = default

    if "name" not in gdf.columns:
        gdf["name"] = None
    if "addr:street" in gdf.columns or "addr:housenumber" in gdf.columns:
        gdf["address"] = gdf.get("addr:street", pd.Series([""]*len(gdf))).fillna("") + " " + \
                         gdf.get("addr:housenumber", pd.Series([""]*len(gdf))).fillna("")
        gdf["address"] = gdf["address"].str.strip().replace("", None)
    else:
        gdf["address"] = None

    gdf["building"] = gdf.get("building", None)
    # gdf["bidx"] = [str(i) for i in range(len(gdf))]
    if "bidx" in gdf.columns:
        gdf["bidx"] = gdf["bidx"].astype(str)
    if "osmid" in gdf.columns:
        gdf["network_id"] = (
            gdf.get("element", pd.Series("way", index=gdf.index)).astype(str)
            + "/" + gdf["osmid"].astype(str)
        )
    else:
        gdf["network_id"] = gdf.geometry.apply(lambda geom: geom.wkb_hex[:24])
    return gdf[[c for c in [
        "network_id", "bidx", "element", "osmid", "name", "address", "building", "area_m2",
        "building:levels", "levels", "height", "geometry",
    ] if c in gdf.columns]]

def _to_wkt_list(polys):
    """Normalize many polygon formats into a list[str WKT]."""

    if polys is None:
        return []

    # --- single WKT string ---
    if isinstance(polys, str):
        s = polys.strip()
        if any(tok in s.upper() for tok in ("POLYGON", "MULTIPOLYGON")):
            parts = [p for sep in ["\n", ";", "||", "|"] for p in s.split(sep)]
            parts = [p for p in parts if p.strip()]
            out = []
            for p in (parts or [s]):
                try:
                    _ = _wkt.loads(p)
                    out.append(p)
                except Exception:
                    pass
            if out:
                return out
        return []

    # --- GeoSeries ---
    if gpd is not None and isinstance(polys, gpd.GeoSeries):
        return [g.wkt for g in polys.dropna() if g is not None and not g.is_empty]

    # --- pandas Series (strings or shapely geoms) ---
    if isinstance(polys, pd.Series):
        return _to_wkt_list(polys.dropna().tolist())

    # --- numpy array ---
    if isinstance(polys, np.ndarray):
        return _to_wkt_list(polys.tolist())

    # --- list/tuple/Series/array of strings or shapely geometries ---
    if isinstance(polys, (list, tuple)):
        out = []
        for v in list(polys):
            if isinstance(v, str):
                try:
                    _ = _wkt.loads(v); out.append(v)
                except Exception:
                    pass
            elif isinstance(v, BaseGeometry):
                if not v.is_empty:
                    out.append(v.wkt)
            elif isinstance(v, dict) and v.get("type") in ("Polygon", "MultiPolygon", "Feature"):
                geom = v.get("geometry", v)
                try:
                    out.append(_shape(geom).wkt)
                except Exception:
                    pass
        if out:
            return out

    # --- GeoDataFrame with geometry/wkt column ---
    try:
        if hasattr(polys, "__class__") and polys.__class__.__name__ == "GeoDataFrame":
            gdf = polys
            if "geometry" in gdf.columns and not gdf.empty:
                return [g.wkt for g in gdf.geometry if g is not None and not g.is_empty]
            if "wkt" in gdf.columns:
                return _to_wkt_list(gdf["wkt"])
    except Exception:
        pass

    # --- GeoJSON FeatureCollection / Feature list / geometry dicts ---
    if isinstance(polys, dict) and polys.get("type") == "FeatureCollection":
        feats = polys.get("features", [])
        out = []
        for f in feats:
            g = f.get("geometry")
            if g:
                try:
                    out.append(_shape(g).wkt)
                except Exception:
                    pass
        return out
    if isinstance(polys, list) and polys and isinstance(polys[0], dict) and polys[0].get("type"):
        out = []
        for obj in polys:
            geom = obj.get("geometry", obj)
            try:
                out.append(_shape(geom).wkt)
            except Exception:
                pass
        return out

    return []

def _read_polygons_from_excel_files(project_name: str) -> list[str]:
    """
    Best-effort: scan project/scenario Excel files for any sheet/column that might contain polygons
    and return a list of WKT strings.
    """
    wkts: list[str] = []

    proj_dir = get_project_dir(project_name)
    scen_dir = get_scenario_dir_for_project(project_name)

    excel_paths = []
    # typical places:
    for base in [proj_dir, scen_dir]:
        if base and os.path.isdir(base):
            for fn in os.listdir(base):
                if fn.lower().endswith((".xlsx", ".xlsm", ".xls")):
                    excel_paths.append(os.path.join(base, fn))

    # candidate sheets/columns we’ll look for
    sheet_candidates = [
        "polygons", "aoi", "selection", "map", "meta", "manual_buildings", "manual_buildings_df",
        "bounds", "geometry", "geometries", "selected_area"
    ]
    col_candidates = [
        "wkt", "geometry", "geom_wkt", "GEOMETRY", "polygons_wkt", "polygon_wkt", "aoi_wkt",
        "value", "data", "area_wkt"
    ]
    # meta sheet might store a key -> value pair
    meta_keys = ["polygons_wkt", "aoi_wkt", "selected_area", "polygons", "aoi"]

    for path in excel_paths:
        try:
            xls = pd.ExcelFile(path, engine="openpyxl")
        except Exception:
            continue

        for sheet in xls.sheet_names:
            if sheet.lower() not in [s.lower() for s in sheet_candidates]:
                # still read lightweight to catch general cases
                pass

            try:
                df = pd.read_excel(path, sheet_name=sheet)
            except Exception:
                continue
            if df is None or df.empty:
                continue

            # 2A) meta-like key/value
            if set(df.columns.str.lower()) >= {"key", "value"}:
                try:
                    meta = dict(zip(df["Key"].astype(str), df["Value"]))
                except Exception:
                    # columns could be lowercase
                    meta = dict(zip(df[df.columns[0]].astype(str), df[df.columns[1]]))
                for k in meta_keys:
                    v = meta.get(k) or meta.get(k.upper()) or meta.get(k.capitalize())
                    if pd.isna(v) or v is None:
                        continue
                    wkts.extend(_to_wkt_list(v))
                continue  # still fall through to column scan

            # 2B) direct columns with WKT/GeoJSON
            for col in df.columns:
                col_low = str(col).lower()
                if any(c == col_low for c in [c.lower() for c in col_candidates]):
                    series = df[col].dropna()
                    if not series.empty:
                        wkts.extend(_to_wkt_list(series.tolist()))

            # 2C) embedded JSON/GeoJSON/WKT in any string column (last-ditch)
            for col in df.select_dtypes(include=["object"]).columns:
                series = df[col].dropna().astype(str)
                sample = series.head(20).tolist()
                # Heuristic: if we’ve already got a bunch, don’t over-append noise
                if len(wkts) > 0:
                    break
                for s in sample:
                    got = _to_wkt_list(s)
                    if got:
                        wkts.extend(got)

    # de-dup and validate
    uniq = []
    seen = set()
    for s in wkts:
        if s in seen:
            continue
        try:
            g = _wkt.loads(s)
            if not g.is_empty:
                uniq.append(s); seen.add(s)
        except Exception:
            continue
    return uniq

def _rehydrate_from_buildings_dataset_file(path: str) -> bool:
    """
    Read a saved 'buildings_dataset' sheet and repopulate session state:
    - building_attrs (overrides for OSM rows)
    - manual_buildings (manually created rows)
    - info_overridden (OSM+manual with final values)
    - building_demand_estimates / _original
    - excluded_bidx
    - buildings_gdf (geometry from geom_wkt) for mapping
    Returns True if anything was populated.
    """

    try:
        if "buildings_dataset" not in pd.ExcelFile(path).sheet_names:
            return False
        df = pd.read_excel(path, sheet_name="buildings_dataset")
        if df.empty:
            return False

        # --- normalize ---
        df["bidx"] = df["bidx"].astype(str)

        def _nz(s):
            return None if pd.isna(s) or str(s).strip()=="" else str(s).strip()

        def _retrofit_situation(s):
            val = _nz(s)
            if val is None:
                return None
            key = str(val).strip().lower().replace("_", " ").replace("-", " ")
            aliases = {
                "standard": "standard",
                "current": "standard",
                "original": "standard",
                "not renovated": "standard",
                "not retrofitted": "standard",
                "unrenovated": "standard",
                "retrofit": "retrofit",
                "retrofitted": "retrofit",
                "renovated": "retrofit",
                "partly renovated": "retrofit",
                "partially renovated": "retrofit",
                "typical retrofit": "retrofit",
                "advanced retrofit": "advanced retrofit",
                "deep retrofit": "advanced retrofit",
                "deep renovated": "advanced retrofit",
            }
            return aliases.get(key)

        def _coerce_int(x):
            try:
                if pd.isna(x):
                    return None
                return int(float(x))
            except Exception:
                return None

        bidx_numeric = pd.to_numeric(df["bidx"], errors="coerce")
        max_bidx = int(bidx_numeric.max()) if bidx_numeric.notna().any() else -1
        st.session_state["next_bidx"] = max_bidx + 1

        # bidx_map should use the SAME stable key strategy as your allocator.
        # Prefer OSM ids (osmid / osm_id), because addresses can change formatting.
        bidx_map = {}
        if "osmid" in df.columns or "osm_id" in df.columns:
            for _, r in df.iterrows():
                b = str(r["bidx"])
                osmid = _coerce_int(r["osmid"]) if "osmid" in df.columns else None
                osm_id = _nz(r.get("osm_id")) if "osm_id" in df.columns else None

                # Stable key priority: osm_id (e.g. "way/123") > osmid (e.g. 123)
                key = None
                if osm_id:
                    key = f"osm_id:{osm_id}"
                elif osmid is not None:
                    key = f"osmid:{osmid}"

                if key:
                    bidx_map[key] = b

        st.session_state["bidx_map"] = bidx_map

        # -------- excluded set --------
        # use robust parsing of WAHR/FALSCH, 1/0, True/False, ...
        if "included" in df.columns:
            inc = _to_bool_series(df["included"])
            excluded = set(df.loc[~inc, "bidx"].astype(str).tolist())
            st.session_state["excluded_bidx"] = excluded

        # -------- demand tables --------
        final_cols = ["bidx", "demand_kwh_final"]
        if "peak_kw_final" in df.columns:
            final_cols.append("peak_kw_final")
        if "heat_estimation_method" in df.columns:
            final_cols.append("heat_estimation_method")
        est_final = df.loc[:, final_cols].copy()
        est_final = est_final.rename(columns={
            "bidx":"input_index", "demand_kwh_final":"heat_demand_kWh",
            "peak_kw_final": "peak_load_kW", "heat_estimation_method": "estimation_method",
        })
        st.session_state["building_demand_estimates"] = est_final

        if "demand_kwh_auto" in df.columns:
            auto_cols = ["bidx", "demand_kwh_auto"]
            if "peak_kw_auto" in df.columns:
                auto_cols.append("peak_kw_auto")
            if "heat_estimation_method" in df.columns:
                auto_cols.append("heat_estimation_method")
            est_auto = df.loc[:, auto_cols].copy()
            est_auto = est_auto.rename(columns={
                "bidx":"input_index", "demand_kwh_auto":"heat_demand_kWh",
                "peak_kw_auto": "peak_load_kW", "heat_estimation_method": "estimation_method",
            })
            st.session_state["building_demand_estimates_original"] = est_auto

        st.session_state["total_heat_demand"] = float(
            pd.to_numeric(est_final["heat_demand_kWh"], errors="coerce").fillna(0).sum()
        )

        # -------- manual buildings --------
        man_mask = df["source"].astype(str).str.lower().eq("manual")
        manual = pd.DataFrame({
            "bidx": df.loc[man_mask, "bidx"].astype(str),
            "name": df.loc[man_mask, "name"].apply(_nz),
            "building type": df.loc[man_mask, "building_type_final"].apply(_nz),
            "area (m²)": pd.to_numeric(df.loc[man_mask, "area_final_m2"], errors="coerce"),
            "levels": pd.to_numeric(df.loc[man_mask, "levels"], errors="coerce") if "levels" in df.columns else np.nan,
            "height (m)": pd.to_numeric(df.loc[man_mask, "height_m"], errors="coerce") if "height_m" in df.columns else np.nan,
            "year built": pd.to_numeric(df.loc[man_mask, "year_built_final"], errors="coerce") if "year_built_final" in df.columns else np.nan,
            "retrofit situation": (
                df.loc[man_mask, "retrofit_situation_final"].apply(_retrofit_situation)
                if "retrofit_situation_final" in df.columns else None
            ),
            "building type source": df.loc[man_mask, "building_type_source"] if "building_type_source" in df.columns else None,
            "year source": df.loc[man_mask, "year_built_source"] if "year_built_source" in df.columns else None,
            "zensus age class": df.loc[man_mask, "zensus_age_class"] if "zensus_age_class" in df.columns else None,
            "zensus type class": df.loc[man_mask, "zensus_type_class"] if "zensus_type_class" in df.columns else None,
            "zensus type note": df.loc[man_mask, "zensus_type_note"] if "zensus_type_note" in df.columns else None,
            "zensus year note": df.loc[man_mask, "zensus_year_note"] if "zensus_year_note" in df.columns else None,
            "zensus source": df.loc[man_mask, "zensus_source"] if "zensus_source" in df.columns else None,
        })
        manual = manual.dropna(subset=["building type","area (m²)","year built"], how="all")
        st.session_state["manual_buildings"] = manual.reset_index(drop=True)

        # -------- per-building overrides for OSM rows --------
        # only keep columns the user actually edited
        osm_mask = df["source"].astype(str).str.lower().eq("osm")

        et   = _to_bool_series(df["edited_type"])    if "edited_type"    in df.columns else pd.Series(False, index=df.index)
        ea   = _to_bool_series(df["edited_area"])    if "edited_area"    in df.columns else pd.Series(False, index=df.index)
        el   = _to_bool_series(df["edited_levels"])  if "edited_levels"  in df.columns else pd.Series(False, index=df.index)
        eh   = _to_bool_series(df["edited_height"])  if "edited_height"  in df.columns else pd.Series(False, index=df.index)
        eadr = _to_bool_series(df["edited_address"]) if "edited_address" in df.columns else pd.Series(False, index=df.index)
        ey   = _to_bool_series(df["edited_year"])    if "edited_year"    in df.columns else pd.Series(False, index=df.index)
        er   = _to_bool_series(df["edited_retrofit"]) if "edited_retrofit" in df.columns else pd.Series(False, index=df.index)

        edited_mask = osm_mask & (et | ea | el | eh | eadr | ey | er)

        edit_cols = [
            c for c in [
                "bidx", "building_type_final", "area_final_m2", "levels", "height_m", "address",
                "year_built_final", "retrofit_situation_final"
            ]
            if c in df.columns
        ]
        a = df.loc[edited_mask, edit_cols].copy()
        rows = []
        for idx, r in a.iterrows():
            entry = {"bidx": str(r["bidx"])}
            if et.loc[idx]:
                entry["building type"] = _nz(r.get("building_type_final"))
            if ea.loc[idx]:
                av = pd.to_numeric(r.get("area_final_m2"), errors="coerce")
                if pd.notna(av):
                    entry["area (m²)"] = float(av)
            if el.loc[idx]:
                lv = pd.to_numeric(r.get("levels"), errors="coerce")
                if pd.notna(lv):
                    entry["levels"] = float(lv)
            if eh.loc[idx]:
                hv = pd.to_numeric(r.get("height_m"), errors="coerce")
                if pd.notna(hv):
                    entry["height (m)"] = float(hv)
            if eadr.loc[idx]:
                entry["address"] = _nz(r.get("address"))
            if ey.loc[idx] and "year_built_final" in r.index:
                yv = pd.to_numeric(r.get("year_built_final"), errors="coerce")
                if pd.notna(yv):
                    entry["year built"] = float(yv)
            if er.loc[idx] and "retrofit_situation_final" in r.index:
                rv = _retrofit_situation(r.get("retrofit_situation_final"))
                if rv is not None:
                    entry["retrofit situation"] = rv
            # keep only non-empty edits
            if any(k in entry for k in (
                "building type", "area (m²)", "levels", "height (m)", "address", "year built", "retrofit situation"
            )):
                rows.append(entry)

        st.session_state["building_attrs"] = pd.DataFrame(rows).drop_duplicates(subset=["bidx"], keep="last")

        # -------- info_overridden (for result merge & status) --------
        info = pd.DataFrame({
            "bidx": df["bidx"].astype(str),
            "building index": df["bidx"].astype(str).map(lambda x: f"Building_{x}"),
            "address": df["address"].apply(_nz),
            "name": df["name"].apply(_nz),
            "building type": df["building_type_final"].apply(_nz),
            "building type source": df["building_type_source"] if "building_type_source" in df.columns else None,
            "area (m²)": pd.to_numeric(df["area_final_m2"], errors="coerce"),
            "levels": pd.to_numeric(df["levels"], errors="coerce") if "levels" in df.columns else np.nan,
            "height (m)": pd.to_numeric(df["height_m"], errors="coerce") if "height_m" in df.columns else np.nan,
            "year built": pd.to_numeric(df["year_built_final"], errors="coerce") if "year_built_final" in df.columns else np.nan,
            "year source": df["year_built_source"] if "year_built_source" in df.columns else None,
            "retrofit situation": (
                df["retrofit_situation_final"].apply(_retrofit_situation)
                if "retrofit_situation_final" in df.columns else None
            ),
            "renovation probability (%)": (
                pd.to_numeric(df["retrofit_probability"], errors="coerce") * 100.0
                if "retrofit_probability" in df.columns else np.nan
            ),
            "zensus age class": df["zensus_age_class"] if "zensus_age_class" in df.columns else None,
            "zensus type class": df["zensus_type_class"] if "zensus_type_class" in df.columns else None,
            "zensus type note": df["zensus_type_note"] if "zensus_type_note" in df.columns else None,
            "zensus year note": df["zensus_year_note"] if "zensus_year_note" in df.columns else None,
            "zensus source": df["zensus_source"] if "zensus_source" in df.columns else None,
            "source": df["source"].astype(str).str.lower().map(lambda s: "manual" if s=="manual" else "osm"),
        })
        st.session_state["info_overridden"] = info
        # Keep display stable across loads
        st.session_state["info_overridden"] = st.session_state["info_overridden"].sort_values(
            by="bidx", key=lambda s: pd.to_numeric(s, errors="coerce")
        ).reset_index(drop=True)
        st.session_state["building_demand_estimates"] = st.session_state["building_demand_estimates"].sort_values(
            by="input_index", key=lambda s: pd.to_numeric(s, errors="coerce")
        ).reset_index(drop=True)

        if "building_demand_estimates_original" in st.session_state:
            st.session_state["building_demand_estimates_original"] = st.session_state[
                "building_demand_estimates_original"].sort_values(
                by="input_index", key=lambda s: pd.to_numeric(s, errors="coerce")
            ).reset_index(drop=True)

        # -------- buildings_gdf for map (optional but nice) --------
        if "geom_wkt" in df.columns:
            try:
                geom = df["geom_wkt"].apply(lambda s: _wkt.loads(s) if isinstance(s, str) else None)
                cols = {
                    "bidx": df["bidx"].astype(str),
                    "name": df["name"],
                    "address": df["address"],
                    "building": df["building_type_final"] if "building_type_final" in df.columns else df["building_type_base"],
                    "area_m2": pd.to_numeric(df["area_final_m2"] if "area_final_m2" in df.columns else df["area_base_m2"], errors="coerce"),
                }
                if "levels" in df.columns:
                    cols["levels"] = pd.to_numeric(df["levels"], errors="coerce")
                if "height_m" in df.columns:
                    cols["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
                if "building_type_source" in df.columns:
                    cols["building_type_source"] = df["building_type_source"]
                if "year_built_final" in df.columns:
                    cols["year_built"] = pd.to_numeric(df["year_built_final"], errors="coerce")
                if "year_built_source" in df.columns:
                    cols["year_built_source"] = df["year_built_source"]
                if "retrofit_situation_final" in df.columns:
                    cols["retrofit_situation"] = df["retrofit_situation_final"].apply(_retrofit_situation)
                if "retrofit_situation_source" in df.columns:
                    cols["retrofit_situation_source"] = df["retrofit_situation_source"]
                if "retrofit_probability" in df.columns:
                    cols["retrofit_probability"] = pd.to_numeric(df["retrofit_probability"], errors="coerce")
                for c in ["zensus_age_class", "zensus_type_class", "zensus_source", "zensus_type_note", "zensus_year_note"]:
                    if c in df.columns:
                        cols[c] = df[c]
                if "osmid" in df.columns:
                    cols["osmid"] = pd.to_numeric(df["osmid"], errors="coerce")
                if "osm_id" in df.columns:
                    cols["osm_id"] = df["osm_id"].astype(str)

                gdf = gpd.GeoDataFrame(cols, geometry=geom, crs="EPSG:4326")
                # publish under all aliases you use
                st.session_state["_preloaded_buildings_gdf"] = gdf
                st.session_state["buildings_gdf"] = gdf
                for alias in ["osm_buildings_gdf","buildings_osm_gdf","selected_buildings_gdf",
                              "gdf_osm","gdf_selected","gdf_buildings","osm_gdf","bldg_gdf"]:
                    st.session_state[alias] = gdf
            except Exception:
                pass

        return True
    except Exception:
        return False

def hydrate_saved_selection(project_name: str) -> bool:
    ss = st.session_state

    # If we already have renderable geometry AND previously preloaded, skip
    already_has_geometry = (
        (isinstance(ss.get("drawn_polygons"), list) and len(ss["drawn_polygons"]) > 0) or
        (isinstance(ss.get("buildings_gdf"), pd.DataFrame) and not ss["buildings_gdf"].empty) or
        (isinstance(ss.get("manual_buildings"), pd.DataFrame) and not ss["manual_buildings"].empty)
    )
    if ss.get("_osm_preloaded") and already_has_geometry:
        return True

    # 1) Import loader + rehydrator
    try:
        bda = importlib.import_module("logic.building_demand_analyse")
    except Exception as e:
        st.error("❌ Could not import `logic.building_demand_analyse`.")
        st.exception(e)
        return False

    loader = getattr(bda, "load_project_dataset_v2", None) or getattr(bda, "load_project_dataset", None)
    if loader is None:
        st.warning("⚠️ No compatible loader found in logic.building_demand_analyse.")
        return False

    # 2) Load everything
    try:
        persisted = loader(project_name) or {}
    except Exception as e:
        st.error("❌ Failed to load saved dataset from loader.")
        st.exception(e)
        return False

    # 3) Rehydrate using the central logic if available
    rehydrator = getattr(bda, "_rehydrate_project_state", None)
    if callable(rehydrator):
        rehydrator(persisted)
    else:
        # Fallback: minimal adoption only if _rehydrate_project_state does not exist
        def _adopt(src_key, dst_key):
            val = persisted.get(src_key)
            if val is None:
                return
            if hasattr(val, "empty") and getattr(val, "empty"):
                return
            ss[dst_key] = val

        _adopt("polygons_wkt", "drawn_polygons")
        _adopt("buildings_gdf", "buildings_gdf")
        _adopt("manual_buildings_df", "manual_buildings")
        _adopt("building_attrs_df", "building_attrs")
        _adopt("info_overridden", "info_overridden")  # <- use correct key here
        _adopt("estimates_df_final", "building_demand_estimates")
        _adopt("estimates_df_original", "building_demand_estimates_original")
        _adopt("total_heat_demand_kwh", "total_heat_demand")
        _adopt("dhw_estimates_df", "dhw_demand_estimates")
        _adopt("dhw_profile_df", "dhw_load_profile")
        _adopt("total_dhw_demand_kwh", "total_dhw_demand")
        _adopt("route_length_m", "route_length")

        excl = persisted.get("excluded_bidx")
        if excl is not None:
            try:
                ss["excluded_bidx"] = set(map(str, excl))
            except Exception:
                pass

    # Optional map center/zoom
    if "project_coords" in persisted and isinstance(persisted["project_coords"], (list, tuple)) and len(persisted["project_coords"]) == 2:
        ss["project_coords"] = tuple(map(float, persisted["project_coords"]))
    if "project_zoom" in persisted:
        ss["project_zoom"] = int(persisted["project_zoom"])

    # Ensure polygons_wkt in session matches UI key
    if ss.get("drawn_polygons") and not ss.get("polygons_wkt"):
        ss["polygons_wkt"] = ss["drawn_polygons"]

    # 4) Decide if we’re ready
    has_polys = bool(ss.get("drawn_polygons"))
    has_bldgs = isinstance(ss.get("buildings_gdf"), pd.DataFrame) and not ss["buildings_gdf"].empty
    has_manual = isinstance(ss.get("manual_buildings"), pd.DataFrame) and not ss["manual_buildings"].empty
    map_ready = has_polys or has_bldgs or has_manual

    # TEASER results flags (rehydrator probably already set these, but this is safe)
    has_results = isinstance(ss.get("building_demand_estimates"), pd.DataFrame) and not ss["building_demand_estimates"].empty
    if has_results:
        ss["show_teaser_results"] = True
        ss["results_stale"] = False

    ss["_osm_preloaded"] = map_ready

    return map_ready

def _render_demand_summary_row(summary: dict, prefix: str | None = None):
    if prefix:
        st.markdown(f"**{prefix}**")
    sh = float(summary.get("space_heating_kwh", 0.0))
    dhw = float(summary.get("dhw_kwh", 0.0))
    st.markdown(f"- **Space heating:** {sh / 1_000:,.1f} MWh/a")
    st.markdown(f"- **Domestic hot water:** {dhw / 1_000:,.1f} MWh/a")
    st.markdown(f"- **Total SH + DHW:** {(sh + dhw) / 1_000:,.1f} MWh/a")


def render_scenario_summary(project_name: str, scenario_name: str, show_demand=True):
    """Compact read-only summary of one scenario."""
    excel_file = get_scenario_file(scenario_name, project_name=project_name)

    try:
        if show_demand:
            _render_demand_summary_row(_scenario_demand_summary(excel_file), "Demand")

        # Sources -> which techs connected
        st.markdown("**Generation technologies**")
        source_df = pd.read_excel(excel_file, sheet_name="sources")
        source_bus_map = {row["label"]: row["to"] for _, row in source_df.iterrows()}
        source_bus_map["el"] = "b_el"
        existing_techs = read_existing_technologies(excel_file, tech_sheet_map)

        for source, bus in source_bus_map.items():
            techs = existing_techs.get(bus, [])
            if techs:
                tech_names = [display_names.get(t, t) for t in techs]
                st.markdown(f"- **{source}** → {', '.join(tech_names)}")

        # Storage quick peek
        st.markdown("**Storage**")
        try:
            storage_df = pd.read_excel(excel_file, sheet_name="storages")
            active_storage = storage_df[(storage_df["label"] == "Storage_th") & (storage_df["active"] == 1)]
            if not active_storage.empty:
                cap_raw = active_storage.iloc[0].get("max capacity", 0)
                cap = parse_number(cap_raw)
                st.markdown(f"- 🛢️ **Thermal Storage**: enabled, {cap:.1f} kWh")
            else:
                st.markdown("- 🛢️ **Thermal Storage**: _not used_")
        except Exception:
            pass

    except Exception as e:
        st.warning(f"Could not summarize scenario '{scenario_name}': {e}")


def _scenario_demand_summary(scenario_file: str) -> dict:
    """Return SH and DHW totals, excluding network heat loss."""
    try:
        demand = pd.read_excel(scenario_file, sheet_name="demand")
    except Exception:
        return {"space_heating_kwh": 0.0, "dhw_kwh": 0.0}
    if demand.empty or "nominal value" not in demand.columns:
        return {"space_heating_kwh": 0.0, "dhw_kwh": 0.0}

    labels = demand.get("label", pd.Series("", index=demand.index)).astype(str)
    active = pd.to_numeric(
        demand.get("active", pd.Series(1, index=demand.index)), errors="coerce"
    ).fillna(0).eq(1)
    thermal = demand.get("from", pd.Series("", index=demand.index)).astype(str).str.startswith("b_th_")
    not_loss = ~labels.isin({HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL})
    values = demand["nominal value"].apply(parse_number).fillna(0.0)
    dhw = labels.str.contains(r"dhw|domestic hot water|hot water", case=False, regex=True, na=False)
    valid = active & thermal & not_loss
    return {
        "space_heating_kwh": float(values[valid & ~dhw].sum()),
        "dhw_kwh": float(values[valid & dhw].sum()),
    }


def _copy_scenario_demand(source_file: str, target_file: str) -> None:
    """Copy demand rows and the normalized DHW profile between scenarios."""
    demand = pd.read_excel(source_file, sheet_name="demand")
    labels = demand.get("label", pd.Series("", index=demand.index)).astype(str)
    demand = demand.loc[~labels.isin({HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL})].copy()
    with pd.ExcelWriter(target_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        demand.to_excel(writer, sheet_name="demand", index=False)

    try:
        source_ts = pd.read_excel(source_file, sheet_name="time_series")
        if DHW_PROFILE_COLUMN in source_ts.columns:
            workbook = openpyxl.load_workbook(target_file)
            target = workbook["time_series"]
            headers = {str(target.cell(1, col).value): col for col in range(1, target.max_column + 1)}
            col = headers.get(DHW_PROFILE_COLUMN, target.max_column + 1)
            target.cell(1, col, DHW_PROFILE_COLUMN)
            for row, value in enumerate(source_ts[DHW_PROFILE_COLUMN].tolist(), start=2):
                target.cell(row, col, None if pd.isna(value) else float(value))
            workbook.save(target_file)
    except Exception:
        pass


def _import_latest_project_demand(scenario_file: str) -> dict:
    """Import the latest building-estimation totals without removing custom consumers."""
    project = get_current_project()
    if not project:
        raise ValueError("No project is open.")
    _load_saved_project_totals(project)
    sh_value = st.session_state.get("total_heat_demand")
    dhw_value = st.session_state.get("total_dhw_demand")
    if sh_value is None or dhw_value is None:
        raise ValueError("Complete space-heating and DHW estimation results are not available yet.")

    try:
        existing = pd.read_excel(scenario_file, sheet_name="demand")
    except Exception:
        existing = pd.DataFrame()
    if not existing.empty and "label" in existing.columns:
        replace_labels = {
            SPACE_HEATING_DEMAND_LABEL, DHW_DEMAND_LABEL,
            HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL,
        }
        existing = existing.loc[~existing["label"].astype(str).isin(replace_labels)].copy()

    imported = pd.DataFrame([
        _demand_row(SPACE_HEATING_DEMAND_LABEL, float(sh_value)),
        _demand_row(DHW_DEMAND_LABEL, float(dhw_value)),
    ])
    demand = pd.concat([existing, imported], ignore_index=True, sort=False)
    required = ["label", "active", "from", "nominal value", "Demand in MWh/a", "Demand in GWh/a"]
    for column in required:
        if column not in demand.columns:
            demand[column] = np.nan
    demand = demand[required + [c for c in demand.columns if c not in required]]
    with pd.ExcelWriter(scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        demand.to_excel(writer, sheet_name="demand", index=False)

    project_data = Path(get_project_dir(project)) / "project_data.xlsx"
    if not isinstance(st.session_state.get("dhw_load_profile"), pd.DataFrame) and project_data.exists():
        try:
            st.session_state["dhw_load_profile"] = pd.read_excel(project_data, sheet_name="dhw_profile")
        except Exception:
            pass
    write_dhw_profile_to_time_series(scenario_file)

    if _scenario_system_type(scenario_file) == "District heating (centralized)":
        add_network_heat_loss(scenario_file, loss_fraction=0.05)
    building_count = _connected_building_count_from_session()
    update_meta_sheet(
        scenario_file,
        {
            "Building Count": building_count,
            "Total Space Heating Demand (kWh/a)": float(sh_value),
            "Total DHW Demand (kWh/a)": float(dhw_value),
            "use_building_data": "True",
            "use_total_demand": "True",
        },
    )
    _update_scenario_load_profiles(scenario_file, building_count=building_count)
    return {"space_heating_kwh": float(sh_value), "dhw_kwh": float(dhw_value)}


def _load_saved_project_totals(project_name: str) -> None:
    """Load small persisted totals without rehydrating the full building dataset."""
    need_totals = any(
        st.session_state.get(key) is None
        for key in ("total_heat_demand", "total_dhw_demand", "route_length")
    )
    need_count = (
        not isinstance(st.session_state.get("building_demand_estimates"), pd.DataFrame)
        and st.session_state.get("scenario_building_count") is None
    )
    if not need_totals and not need_count:
        return
    path = os.path.join(get_project_dir(project_name), "project_data.xlsx")
    if not os.path.exists(path):
        return
    try:
        meta = pd.read_excel(path, sheet_name="meta")
        if {"key", "value"}.issubset(meta.columns):
            values = dict(zip(meta["key"].astype(str), meta["value"]))
            mappings = {
                "total_heat_demand_kwh": "total_heat_demand",
                "total_dhw_demand_kwh": "total_dhw_demand",
                "route_length_m": "route_length",
            }
            for source, target in mappings.items():
                value = pd.to_numeric(values.get(source), errors="coerce")
                if pd.notna(value) and st.session_state.get(target) is None:
                    st.session_state[target] = float(value)
    except Exception:
        pass
    if not isinstance(st.session_state.get("building_demand_estimates"), pd.DataFrame):
        try:
            buildings = pd.read_excel(path, sheet_name="buildings_dataset")
            if "included" in buildings.columns:
                buildings = buildings.loc[_to_bool_series(buildings["included"], default=True)]
            st.session_state["scenario_building_count"] = int(len(buildings))
        except Exception:
            pass


def _connected_building_count_from_session():
    estimates = st.session_state.get("building_demand_estimates")
    if isinstance(estimates, pd.DataFrame) and not estimates.empty:
        for column in ("heat_demand_kWh", "Annual Space Heating Demand (kWh)", "demand_kwh_final"):
            if column in estimates.columns:
                values = pd.to_numeric(estimates[column], errors="coerce")
                return int((values.notna() & values.gt(0)).sum())
        return int(len(estimates))
    value = pd.to_numeric(st.session_state.get("scenario_building_count"), errors="coerce")
    return int(value) if pd.notna(value) and value > 0 else None


def _sum_individual_building_peaks_kw():
    """Sum final TEASER/user building peaks from session or saved project data."""
    estimates = st.session_state.get("building_demand_estimates")
    if isinstance(estimates, pd.DataFrame) and "peak_load_kW" in estimates.columns:
        peaks = pd.to_numeric(estimates["peak_load_kW"], errors="coerce").clip(lower=0)
        if peaks.notna().any() and float(peaks.sum()) > 0:
            return float(peaks.sum())
    project = get_current_project()
    if project:
        path = os.path.join(get_project_dir(project), "project_data.xlsx")
        try:
            buildings = pd.read_excel(path, sheet_name="buildings_dataset")
            if "included" in buildings.columns:
                buildings = buildings.loc[_to_bool_series(buildings["included"], default=True)]
            if "peak_kw_final" in buildings.columns:
                peaks = pd.to_numeric(buildings["peak_kw_final"], errors="coerce").clip(lower=0)
                if peaks.notna().any() and float(peaks.sum()) > 0:
                    return float(peaks.sum())
        except Exception:
            pass
    return None

def get_valid_coords():
    c = st.session_state.get("project_coords")
    if isinstance(c, (list, tuple)) and len(c) == 2:
        try:
            return float(c[0]), float(c[1])
        except (TypeError, ValueError):
            return None
    return None


def _remember_dwd_weather(weather: dict) -> None:
    """Keep the exact weather selection available across project pages."""
    st.session_state["dwd_weather"] = weather
    mappings = {
        "design_outdoor_temperature_c": "design_outdoor_temperature_c",
        "ground_temperature_c": "ground_temperature_c",
        "station_name": "dwd_weather_station",
        "station_id": "dwd_weather_station_id",
        "year": "dwd_weather_year",
        "profile_years_used": "dwd_profile_years_used",
        "available_complete_years": "dwd_available_complete_years",
        "available_complete_year_count": "dwd_available_complete_year_count",
        "station_distance_km": "dwd_station_distance_km",
        "profile_method": "dwd_profile_method",
        "ground_method": "dwd_ground_method",
        "source": "dwd_weather_source",
    }
    for source, target in mappings.items():
        st.session_state[target] = weather.get(source)
    st.session_state["weather_data_message"] = (
        f"DWD station {weather.get('station_name')} ({weather.get('station_id')}), "
        f"using {weather.get('profile_year_count')} complete years: "
        f"{', '.join(map(str, weather.get('profile_years_used', [])))}."
    )
    st.session_state.pop("weather_data_warning", None)


def _ensure_project_dwd_weather(coords):
    """Load DWD weather once per selected coordinate pair."""
    signature = (round(float(coords[0]), 7), round(float(coords[1]), 7))
    weather = st.session_state.get("dwd_weather")
    if isinstance(weather, dict) and weather.get("temperature_c") is not None:
        return weather
    if st.session_state.get("_dwd_weather_attempted_coords") == signature:
        return None
    st.session_state["_dwd_weather_attempted_coords"] = signature
    try:
        weather = get_location_weather(*coords)
        _remember_dwd_weather(weather)
        return weather
    except Exception as exc:
        st.session_state["weather_data_warning"] = str(exc)
        return None


def _render_project_weather_information(coords) -> None:
    """Show the exact DWD inputs used by TEASER, profiles and heat pumps."""
    weather = _ensure_project_dwd_weather(coords)
    st.markdown("#### Weather data used")
    if weather is None:
        st.warning(
            "Exact DWD weather data could not be loaded. Until it becomes available, scenario generation "
            "uses the representative-city template only as an explicit fallback. "
            f"Details: {st.session_state.get('weather_data_warning', 'unknown error')}"
        )
        if st.button("Retry loading DWD weather", key="retry_project_dwd_weather"):
            st.session_state.pop("_dwd_weather_attempted_coords", None)
            st.rerun()
        return

    used_years = ", ".join(map(str, weather.get("profile_years_used", []))) or "not available"
    available_count = weather.get("available_complete_year_count", 0)
    st.markdown(
        f"- **DWD station:** {weather.get('station_name')} (ID {weather.get('station_id')}, "
        f"{float(weather.get('station_distance_km', 0.0)):.1f} km from the project location)\n"
        f"- **Hourly temperature profile:** {used_years} "
        f"({len(weather.get('profile_years_used', []))} used; {available_count} complete years available)\n"
        f"- **Reference chronology:** {weather.get('year')}\n"
        f"- **Outdoor design temperature:** {float(weather.get('design_outdoor_temperature_c')):.1f} °C\n"
        f"- **Average ground temperature:** {float(weather.get('ground_temperature_c')):.1f} °C — "
        f"{weather.get('ground_method')}\n"
        # f"- **Profile method:** {weather.get('profile_method')}"
    )
    st.caption(
        "The DWD temperature profiles are used for heat demand estimation, load profile generation and calculation of heat pump COP values. "
    )


def _invalidate_location_weather():
    for key in (
        "dwd_weather", "design_outdoor_temperature_c", "ground_temperature_c",
        "dwd_weather_station", "dwd_weather_station_id", "dwd_weather_year",
        "dwd_profile_years_used", "dwd_available_complete_year_count",
        "weather_data_message", "weather_data_warning",
        "dwd_available_complete_years", "dwd_station_distance_km",
        "dwd_profile_method", "dwd_ground_method", "dwd_weather_source",
        "_dwd_weather_attempted_coords",
    ):
        st.session_state.pop(key, None)


def _fit_weather_values(values, target_len: int) -> pd.Series:
    series = pd.to_numeric(pd.Series(values), errors="coerce").interpolate().ffill().bfill()
    if series.empty:
        return pd.Series(np.zeros(target_len, dtype=float))
    if len(series) < target_len:
        repeats = int(np.ceil(target_len / len(series)))
        return pd.Series(np.tile(series.to_numpy(float), repeats)[:target_len])
    return series.iloc[:target_len].reset_index(drop=True)


def _current_scenario_name(project_name: str) -> str | None:
    scenarios = list_scenarios(project_name)
    current_file = st.session_state.get("current_scenario_file")
    if current_file:
        candidate = Path(str(current_file)).stem
        if candidate in scenarios:
            return candidate
    selected = st.session_state.get("last_selected_scenario")
    if selected in scenarios:
        return selected
    return scenarios[0] if len(scenarios) == 1 else None


def _queue_scenario_weather_update(old_coords, new_coords) -> None:
    if old_coords is None or tuple(old_coords) == tuple(new_coords):
        return
    project = get_current_project()
    scenario = _current_scenario_name(project) if project else None
    if scenario:
        st.session_state.pop(f"confirm_weather_result_delete_{project}_{scenario}", None)
        st.session_state["pending_scenario_weather_update"] = {
            "project": project,
            "scenario": scenario,
        }


def _update_scenario_weather_data(scenario_file: str) -> dict:
    """Replace scenario weather/COP columns with data for the current location."""
    coords = get_valid_coords()
    if coords is None:
        raise ValueError("The project location is not set.")

    climate_zone = st.session_state.get("project_climate_zone", "")
    fallback_city = zone_to_city.get(climate_zone)
    if fallback_city:
        weather_df = pd.read_excel(PV_COP_FILE, sheet_name=fallback_city)
    else:
        weather_df = pd.DataFrame()

    timestamps = pd.read_excel(scenario_file, sheet_name="time_series", usecols=["timestamp"])
    target_len = int(timestamps["timestamp"].notna().sum())
    if not weather_df.empty:
        weather_df = pd.DataFrame({
            column: _fit_weather_values(weather_df[column], target_len)
            for column in weather_df.columns
        })

    dwd_weather = _ensure_project_dwd_weather(coords)
    if dwd_weather is None and weather_df.empty:
        raise RuntimeError("Neither exact DWD weather nor a fallback weather template is available.")
    if dwd_weather is not None:
        temperatures = _fit_weather_values(dwd_weather["temperature_c"], target_len)
        cop_profiles = heat_pump_cop_profiles(
            temperatures, dwd_weather["ground_temperature_c"]
        )
        for column in cop_profiles.columns:
            weather_df[column] = cop_profiles[column].to_numpy()
        weather_df["DWD_Temperature_C.fix"] = temperatures.to_numpy()
    if "WWSHP.fix" not in weather_df.columns and "GSHP_B_HT.fix" in weather_df.columns:
        weather_df["WWSHP.fix"] = weather_df["GSHP_B_HT.fix"]

    workbook = openpyxl.load_workbook(scenario_file)
    sheet = workbook["time_series"]
    headers = {
        str(sheet.cell(1, column).value): column
        for column in range(1, sheet.max_column + 1)
        if sheet.cell(1, column).value is not None
    }
    for column in weather_df.columns:
        column_index = headers.get(str(column), sheet.max_column + 1)
        if str(column) not in headers:
            headers[str(column)] = column_index
            sheet.cell(1, column_index, str(column))
        for row_index, value in enumerate(weather_df[column], start=2):
            sheet.cell(row_index, column_index, None if pd.isna(value) else float(value))
    if dwd_weather is None and "DWD_Temperature_C.fix" in headers:
        old_dwd_column = headers["DWD_Temperature_C.fix"]
        for row_index in range(2, sheet.max_row + 1):
            sheet.cell(row_index, old_dwd_column).value = None
    workbook.save(scenario_file)

    weather_meta = {
        "Project Location": st.session_state.get("project_city_name", "_Not set_"),
        "Legacy Fallback Climate Zone": climate_zone,
        "Legacy Fallback Representative City": fallback_city,
        "DWD Weather Station": dwd_weather.get("station_name") if dwd_weather else None,
        "DWD Station ID": dwd_weather.get("station_id") if dwd_weather else None,
        "DWD Weather Year": dwd_weather.get("year") if dwd_weather else None,
        "Design Outdoor Temperature (C)": dwd_weather.get("design_outdoor_temperature_c") if dwd_weather else None,
        "Ground Boundary Temperature (C)": dwd_weather.get("ground_temperature_c") if dwd_weather else None,
        "DWD Complete Years Available": dwd_weather.get("available_complete_year_count") if dwd_weather else None,
        "DWD Profile Years Used": (
            ", ".join(map(str, dwd_weather.get("profile_years_used", []))) if dwd_weather else None
        ),
        "DWD Temperature Profile Method": dwd_weather.get("profile_method") if dwd_weather else None,
    }
    update_meta_sheet(scenario_file, weather_meta)

    meta = _scenario_meta_values(scenario_file)
    count = pd.to_numeric(meta.get("Building Count"), errors="coerce")
    count = int(count) if pd.notna(count) and count > 0 else None
    stored_peak = pd.to_numeric(meta.get("Space Heating Peak Load (kW)"), errors="coerce")
    peak_edited = str(meta.get("Peak Load User Edited", "False")).lower() == "true"
    _update_scenario_load_profiles(
        scenario_file,
        building_count=count,
        peak_load_kw=float(stored_peak) if peak_edited and pd.notna(stored_peak) else None,
        user_edited=peak_edited,
    )
    return dwd_weather or {"station_name": fallback_city or "fallback template"}


def _render_pending_scenario_weather_update(project_name: str) -> None:
    pending = st.session_state.get("pending_scenario_weather_update")
    if not isinstance(pending, dict) or pending.get("project") != project_name:
        return
    scenario = pending.get("scenario")
    if scenario not in list_scenarios(project_name):
        st.session_state.pop("pending_scenario_weather_update", None)
        return

    st.warning(
        f"The project location changed. Update the weather data and heat-pump COP profiles in the current "
        f"scenario **{scenario}**?"
    )
    has_results = _project_has_simulation_results(project_name)
    confirmed = True
    if has_results:
        st.info(
            "Updating scenario weather changes simulation inputs, so existing simulation results will be deleted. "
            "Duplicate the project first if you want to retain and review those results."
        )
        confirmed = st.checkbox(
            "I understand that the existing simulation results will be deleted",
            key=f"confirm_weather_result_delete_{project_name}_{scenario}",
        )
    yes_col, no_col = st.columns([1, 1])
    if yes_col.button(
        "Update current scenario weather",
        disabled=not confirmed,
        key=f"update_weather_{project_name}_{scenario}",
        use_container_width=True,
    ):
        try:
            weather = _update_scenario_weather_data(get_scenario_file(scenario, project_name))
            if has_results:
                _delete_project_simulation_results(project_name)
            st.session_state.pop("pending_scenario_weather_update", None)
            st.success(f"Scenario weather updated using {weather.get('station_name')}.")
            st.rerun()
        except Exception as exc:
            st.error(f"Scenario weather could not be updated: {exc}")
    if no_col.button(
        "Keep scenario weather unchanged",
        key=f"keep_weather_{project_name}_{scenario}",
        use_container_width=True,
    ):
        st.session_state.pop("pending_scenario_weather_update", None)
        st.rerun()

def list_scenarios(project_name: str | None = None):
    """List scenario names (without .xlsx) for a given project.
    Defaults to the current project for backward compatibility."""
    proj = project_name or get_current_project()
    if not proj:
        return []
    scen_dir = get_scenario_dir_for_project(proj)
    if not os.path.exists(scen_dir):
        return []
    return [f[:-5] for f in os.listdir(scen_dir) if f.endswith(".xlsx")]

def get_scenario_file(name: str, project_name: str | None = None):
    """Get the full path to a scenario file for the given (or current) project."""
    proj = project_name or get_current_project()
    if not proj:
        raise ValueError("No project selected.")
    scen_dir = get_scenario_dir_for_project(proj)
    return os.path.join(scen_dir, f"{name}.xlsx")

def delete_scenario(name: str, project_name: str | None = None):
    """Delete a scenario file in the given (or current) project."""
    path = get_scenario_file(name, project_name=project_name)
    if os.path.exists(path):
        os.remove(path)
        st.success(f"Deleted scenario: {name}")
    else:
        st.error(f"Scenario '{name}' does not exist.")

def _demand_row(label, value, from_bus="b_th_HT"):
    value = parse_number(value)
    return {
        "label": label,
        "active": 1,
        "from": from_bus,
        "nominal value": value,
        "Demand in MWh/a": value / 1000,
        "Demand in GWh/a": value / 1e6,
    }

def _normalized_dhw_profile_values(target_len):
    profile_df = st.session_state.get("dhw_load_profile")
    values = None

    if isinstance(profile_df, pd.DataFrame) and not profile_df.empty:
        if "dhw_heat_kWh" in profile_df.columns:
            values = pd.to_numeric(profile_df["dhw_heat_kWh"], errors="coerce").fillna(0).to_numpy(dtype=float)
        elif "dhw_heat_kW" in profile_df.columns:
            values = pd.to_numeric(profile_df["dhw_heat_kW"], errors="coerce").fillna(0).to_numpy(dtype=float)

    if values is None or len(values) == 0 or float(np.nansum(values)) <= 0:
        values = np.ones(target_len, dtype=float)

    values = np.asarray(values, dtype=float)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)

    if len(values) < target_len:
        values = np.pad(values, (0, target_len - len(values)), mode="constant")
    elif len(values) > target_len:
        values = values[:target_len]

    total = float(values.sum())
    if total <= 0:
        values = np.ones(target_len, dtype=float) / max(target_len, 1)
    else:
        values = values / total
    return values

def write_dhw_profile_to_time_series(scenario_file):
    wb = openpyxl.load_workbook(scenario_file)
    if "time_series" not in wb.sheetnames:
        ws = wb.create_sheet("time_series")
        ws.cell(row=1, column=1, value="timestamp")
        for idx in range(8761):
            ws.cell(row=idx + 2, column=1, value=idx)
    else:
        ws = wb["time_series"]

    timestamp_col = None
    for col_idx in range(1, ws.max_column + 1):
        if str(ws.cell(row=1, column=col_idx).value or "").strip() == "timestamp":
            timestamp_col = col_idx
            break
    if timestamp_col is None:
        raise ValueError("The time_series sheet has no 'timestamp' column.")

    timestamp_rows = [
        row_idx
        for row_idx in range(2, ws.max_row + 1)
        if ws.cell(row=row_idx, column=timestamp_col).value is not None
    ]
    if not timestamp_rows:
        raise ValueError("The time_series sheet contains no timestamp values.")

    last_timestamp_row = timestamp_rows[-1]
    if timestamp_rows != list(range(2, last_timestamp_row + 1)):
        raise ValueError("The timestamp column in time_series contains empty rows between values.")

    # Do not use ws.max_row here. Empty but formatted template cells count toward
    # max_row and previously caused DHW values to be written below the timestamps.
    target_len = last_timestamp_row - 1
    values = _normalized_dhw_profile_values(target_len)

    target_col = None
    for col_idx in range(1, ws.max_column + 1):
        if str(ws.cell(row=1, column=col_idx).value or "").strip() == DHW_PROFILE_COLUMN:
            target_col = col_idx
            break
    if target_col is None:
        target_col = ws.max_column + 1

    ws.cell(row=1, column=target_col, value=DHW_PROFILE_COLUMN)
    for row_offset, value in enumerate(values, start=2):
        ws.cell(row=row_offset, column=target_col, value=float(value))

    for row_idx in range(len(values) + 2, ws.max_row + 1):
        ws.cell(row=row_idx, column=target_col, value=None)

    wb.save(scenario_file)


def winter_simultaneity_factor(building_count) -> float:
    """Winter et al. (2001) simultaneity factor for connected consumers."""
    try:
        count = max(float(building_count), 1.0)
    except (TypeError, ValueError):
        return 1.0
    if count <= 1:
        return 1.0
    a = 0.449677646267461
    b = 0.551234688
    c = 53.84382392
    d = 1.762743268
    return float(a + b / (1.0 + (count / c) ** d))


def _normalized_profile(values, target_len=None):
    values = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0.0).clip(lower=0.0).to_numpy(float)
    if target_len is not None:
        if len(values) < target_len:
            # Load profiles are periodic. Scenario templates may contain one
            # closing boundary row (8761 instead of 8760), which should repeat
            # the start of the year rather than introduce an artificial zero.
            values = np.resize(values, target_len) if len(values) else np.zeros(target_len)
        else:
            values = values[:target_len]
    total = float(values.sum())
    return values / total if total > 0 else np.full(len(values), 1.0 / max(len(values), 1))


def _profile_with_peak_limit(base_profile, target_peak_share):
    """Cap profile peaks and redistribute energy while retaining zero-load hours."""
    profile = _normalized_profile(base_profile)
    positive = profile > 0
    if not positive.any():
        return profile
    feasible_minimum = 1.0 / int(positive.sum())
    target = max(float(target_peak_share), feasible_minimum)
    current_peak = float(profile.max())
    if np.isclose(target, current_peak, rtol=1e-10, atol=1e-12):
        return profile
    if target > current_peak:
        target = min(target, 0.999999)

        def sharpen(exponent):
            powered = np.power(profile / current_peak, exponent)
            return powered / powered.sum()

        low, high = 1.0, 2.0
        while float(sharpen(high).max()) < target and high < 1024:
            high *= 2.0
        for _ in range(80):
            middle = (low + high) / 2.0
            if float(sharpen(middle).max()) < target:
                low = middle
            else:
                high = middle
        return sharpen(high)

    low, high = 0.0, 1.0
    while float(np.minimum(high * profile, target).sum()) < 1.0:
        high *= 2.0
    for _ in range(80):
        middle = (low + high) / 2.0
        if float(np.minimum(middle * profile, target).sum()) < 1.0:
            low = middle
        else:
            high = middle
    adjusted = np.minimum(high * profile, target)
    return adjusted / adjusted.sum()


def _scenario_meta_values(scenario_file) -> dict:
    try:
        meta = pd.read_excel(scenario_file, sheet_name="meta")
        if {"Key", "Value"}.issubset(meta.columns):
            return dict(zip(meta["Key"].astype(str), meta["Value"]))
    except Exception:
        pass
    return {}


_BDEW_TYPE_BY_BUILDING = {
    "house": "efh", "detached": "efh", "semidetached": "efh",
    "semidetached_house": "efh", "bungalow": "efh", "terrace": "efh",
    "static_caravan": "efh", "residential": "mfh", "apartments": "mfh",
    "dormitory": "mfh", "office": "gko", "government": "gko",
    "school": "gko", "university": "gko", "college": "gko",
    "kindergarten": "gko", "hospital": "gko", "care_home": "gko",
    "civic": "gko", "fire_station": "gko", "police": "gko",
    "train_station": "gko", "museum": "gko", "public": "gko",
    "retail": "gha", "supermarket": "gha", "commercial": "ghd",
    "restaurant": "gga", "hotel": "gbh", "industrial": "gmk",
    "religious": "gmf", "church": "gmf", "cathedral": "gmf",
    "mosque": "gmf", "temple": "gmf", "stadium": "gmf",
    "sports_centre": "gmf", "sports_hall": "gmf",
}


def _profile_building_rows() -> pd.DataFrame:
    """Return included building types and final annual SH demands."""
    project = get_current_project()
    if project:
        path = os.path.join(get_project_dir(project), "project_data.xlsx")
        try:
            rows = pd.read_excel(path, sheet_name="buildings_dataset")
            if "included" in rows.columns:
                rows = rows.loc[_to_bool_series(rows["included"], default=True)]
            out = pd.DataFrame({
                "bidx": rows.get("bidx", rows.index).astype(str),
                "building_type": rows.get("building_type_final", "house"),
                "annual_kwh": pd.to_numeric(rows.get("demand_kwh_final"), errors="coerce"),
                "year_built": pd.to_numeric(rows.get("year_built_final"), errors="coerce"),
                "retrofit": rows.get("retrofit_situation_final", "standard"),
            })
            out = out.loc[out["annual_kwh"].fillna(0).gt(0)]
            if not out.empty:
                return out
        except Exception:
            pass

    estimates = st.session_state.get("building_demand_estimates")
    info = st.session_state.get("info_overridden")
    if not isinstance(estimates, pd.DataFrame) or estimates.empty:
        return pd.DataFrame(columns=["bidx", "building_type", "annual_kwh", "year_built", "retrofit"])
    demand = estimates.copy().rename(columns={"input_index": "bidx", "heat_demand_kWh": "annual_kwh"})
    demand["bidx"] = demand["bidx"].astype(str)
    if isinstance(info, pd.DataFrame) and not info.empty and "bidx" in info.columns:
        attrs = info.copy()
        attrs["bidx"] = attrs["bidx"].astype(str)
        attrs = attrs.rename(columns={
            "building type": "building_type", "year built": "year_built",
            "retrofit situation": "retrofit",
        })
        keep = [column for column in ("bidx", "building_type", "year_built", "retrofit") if column in attrs]
        demand = demand.merge(attrs[keep].drop_duplicates("bidx"), on="bidx", how="left")
    for column, default in (("building_type", "house"), ("year_built", np.nan), ("retrofit", "standard")):
        if column not in demand:
            demand[column] = default
    demand["annual_kwh"] = pd.to_numeric(demand["annual_kwh"], errors="coerce")
    return demand.loc[demand["annual_kwh"].fillna(0).gt(0), [
        "bidx", "building_type", "annual_kwh", "year_built", "retrofit"
    ]]


def _bdew_building_class(year_built, retrofit) -> int:
    retrofit_key = str(retrofit or "").strip().lower()
    if retrofit_key in {"retrofit", "advanced retrofit", "renovated", "deep renovated"}:
        return 10
    year = pd.to_numeric(year_built, errors="coerce")
    if pd.isna(year):
        return 5
    if year <= 1948:
        return 2
    if year <= 1978:
        return 4
    if year <= 2001:
        return 7
    return 10


def _german_national_holidays(year: int) -> dict:
    """Return nationwide German holidays used by BDEW weekday factors."""
    import datetime as dt
    from dateutil.easter import easter

    easter_sunday = easter(int(year))
    return {
        dt.date(year, 1, 1): "New Year",
        easter_sunday - dt.timedelta(days=2): "Good Friday",
        easter_sunday + dt.timedelta(days=1): "Easter Monday",
        dt.date(year, 5, 1): "Labour Day",
        easter_sunday + dt.timedelta(days=39): "Ascension Day",
        easter_sunday + dt.timedelta(days=50): "Whit Monday",
        dt.date(year, 10, 3): "German Unity Day",
        dt.date(year, 12, 25): "Christmas Day",
        dt.date(year, 12, 26): "Second Christmas Day",
    }


def _bdew_weighted_profile(temperature, scenario_file, target_len=None):
    """Build a demand-weighted BDEW SH profile for the project's buildings."""
    try:
        import demandlib.bdew as bdew
    except Exception as exc:
        return None, {"method": f"DWD degree-hour fallback (demandlib unavailable: {exc})"}

    rows = _profile_building_rows()
    if rows.empty:
        return None, {"method": "DWD degree-hour fallback (no per-building demand mix)"}
    rows = rows.copy()
    rows["building_type"] = rows["building_type"].fillna("house").astype(str).str.strip().str.lower()
    rows["bdew_type"] = rows["building_type"].map(_BDEW_TYPE_BY_BUILDING).fillna("ghd")
    rows["building_class"] = [
        _bdew_building_class(year, retrofit) if bdew_type in {"efh", "mfh"} else 0
        for year, retrofit, bdew_type in zip(rows["year_built"], rows["retrofit"], rows["bdew_type"])
    ]

    raw_temperature = pd.to_numeric(pd.Series(temperature), errors="coerce").interpolate().ffill().bfill()
    annual_hours = min(len(raw_temperature), 8760)
    if annual_hours <= 0:
        return None, {"method": "DWD degree-hour fallback (empty temperature profile)"}
    raw_temperature = raw_temperature.iloc[:annual_hours].reset_index(drop=True)
    meta = _scenario_meta_values(scenario_file)
    year = pd.to_numeric(meta.get("DWD Weather Year"), errors="coerce")
    year = int(year) if pd.notna(year) else 2019
    index = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", inclusive="left")
    index = index[~((index.month == 2) & (index.day == 29))][:annual_hours]
    if len(index) != annual_hours:
        index = pd.date_range("2019-01-01", periods=annual_hours, freq="h")
    temperature_series = pd.Series(raw_temperature.to_numpy(float), index=index)

    aggregate = np.zeros(annual_hours, dtype=float)
    mix = []
    for (profile_type, building_class), group in rows.groupby(["bdew_type", "building_class"], dropna=False):
        annual_kwh = float(pd.to_numeric(group["annual_kwh"], errors="coerce").fillna(0).sum())
        if annual_kwh <= 0:
            continue
        try:
            profile = bdew.HeatBuilding(
                index,
                holidays=_german_national_holidays(year),
                temperature=temperature_series,
                shlp_type=str(profile_type),
                building_class=int(building_class),
                wind_class=0,
                annual_heat_demand=1.0,
                ww_incl=False,
                name=f"{profile_type}_{building_class}",
            ).get_bdew_profile()
            normalized = _normalized_profile(profile, annual_hours)
            aggregate += annual_kwh * normalized
            mix.append(f"{str(profile_type).upper()}:{len(group)}")
        except Exception as exc:
            return None, {"method": f"DWD degree-hour fallback (BDEW generation failed: {exc})"}
    if float(aggregate.sum()) <= 0:
        return None, {"method": "DWD degree-hour fallback (empty BDEW aggregate)"}
    return _normalized_profile(aggregate, target_len), {
        "method": "annual-demand-weighted BDEW/demandlib SH profiles using DWD temperature",
        "mix": ", ".join(mix),
        "groups": len(mix),
    }


def _scenario_profile_preview(scenario_file, building_count=None) -> dict:
    system_type = _scenario_system_type(scenario_file)
    template = TEMPLATE_CEN_FILE if system_type == "District heating (centralized)" else TEMPLATE_DEC_FILE
    template_ts = pd.read_excel(template, sheet_name="time_series")
    base_sh = _scenario_base_sh_profile(scenario_file, template_ts=template_ts)
    summary = _scenario_demand_summary(scenario_file)
    annual_sh = float(summary["space_heating_kwh"])
    factor = (
        winter_simultaneity_factor(building_count)
        if system_type == "District heating (centralized)"
        else 1.0
    )
    individual_peak_sum = _sum_individual_building_peaks_kw()
    if individual_peak_sum is not None and individual_peak_sum > 0:
        unconstrained_peak = individual_peak_sum
        peak_source = "sum of final building design peaks"
    else:
        unconstrained_peak = annual_sh * float(base_sh.max())
        peak_source = "annual-demand/template fallback"
    estimated_peak = unconstrained_peak * factor
    return {
        "annual_sh_kwh": annual_sh,
        "building_count": building_count,
        "simultaneity_factor": factor,
        "unconstrained_peak_kw": unconstrained_peak,
        "sum_individual_peaks_kw": individual_peak_sum,
        "peak_source": peak_source,
        "estimated_peak_kw": estimated_peak,
        "template": template,
        "system_type": system_type,
    }


def _scenario_base_sh_profile(scenario_file, target_len=None, template_ts=None, return_info=False):
    """Use weighted BDEW/DWD SH profiles, with explicit safe fallbacks."""
    try:
        scenario_ts = pd.read_excel(scenario_file, sheet_name="time_series")
        if "DWD_Temperature_C.fix" in scenario_ts.columns:
            temperature = pd.to_numeric(
                scenario_ts["DWD_Temperature_C.fix"], errors="coerce"
            ).interpolate().ffill().bfill()
            bdew_profile, profile_info = _bdew_weighted_profile(
                temperature, scenario_file, target_len=target_len
            )
            if bdew_profile is not None:
                return (bdew_profile, profile_info) if return_info else bdew_profile
            degree_hours = np.maximum(15.0 - temperature.to_numpy(float), 0.0)
            if float(np.nansum(degree_hours)) > 0:
                profile = _normalized_profile(degree_hours, target_len)
                return (profile, profile_info) if return_info else profile
    except Exception:
        pass
    if template_ts is None:
        system_type = _scenario_system_type(scenario_file)
        template = TEMPLATE_CEN_FILE if system_type == "District heating (centralized)" else TEMPLATE_DEC_FILE
        template_ts = pd.read_excel(template, sheet_name="time_series")
    profile = _normalized_profile(template_ts["Last_SH.fix"], target_len)
    info = {"method": "template space-heating profile fallback"}
    return (profile, info) if return_info else profile


def _update_scenario_load_profiles(scenario_file, building_count=None, peak_load_kw=None, user_edited=False):
    """Apply a peak-constrained SH profile and a load-responsive loss profile."""
    preview = _scenario_profile_preview(scenario_file, building_count)
    annual_sh = preview["annual_sh_kwh"]
    if annual_sh <= 0:
        return preview

    workbook = openpyxl.load_workbook(scenario_file)
    sheet = workbook["time_series"]
    timestamp_rows = [
        row for row in range(2, sheet.max_row + 1)
        if sheet.cell(row, 1).value is not None
    ]
    target_len = len(timestamp_rows)
    template_ts = pd.read_excel(preview["template"], sheet_name="time_series")
    base_sh, profile_info = _scenario_base_sh_profile(
        scenario_file, target_len=target_len, template_ts=template_ts, return_info=True
    )
    selected_peak = float(peak_load_kw) if peak_load_kw is not None else preview["estimated_peak_kw"]
    selected_peak = max(selected_peak, annual_sh / max(int((base_sh > 0).sum()), 1))
    applied_peak_factor = (
        selected_peak / preview["unconstrained_peak_kw"]
        if preview["unconstrained_peak_kw"] > 0
        else 1.0
    )
    adjusted_sh = _profile_with_peak_limit(base_sh, selected_peak / annual_sh)

    headers = {str(sheet.cell(1, col).value): col for col in range(1, sheet.max_column + 1)}
    sh_col = headers.get("Last_SH.fix", sheet.max_column + 1)
    sheet.cell(1, sh_col, "Last_SH.fix")
    for row, value in zip(timestamp_rows, adjusted_sh):
        sheet.cell(row, sh_col, float(value))

    if preview["system_type"] == "District heating (centralized)" and "Loss.fix" in template_ts.columns:
        base_loss = _normalized_profile(template_ts["Loss.fix"], target_len)
        # The template retains its seasonal/temperature-related component;
        # the adjusted SH profile adds a load-dependent component.
        adjusted_loss = _normalized_profile(0.70 * base_loss + 0.30 * adjusted_sh)
        loss_col = headers.get("Loss.fix", sheet.max_column + 1 if sh_col <= sheet.max_column else sh_col + 1)
        sheet.cell(1, loss_col, "Loss.fix")
        for row, value in zip(timestamp_rows, adjusted_loss):
            sheet.cell(row, loss_col, float(value))

    workbook.save(scenario_file)
    update_meta_sheet(
        scenario_file,
        {
            "Building Count": building_count,
            "Winter Simultaneity Factor": preview["simultaneity_factor"],
            "Applied Peak Factor": applied_peak_factor,
            "Estimated Space Heating Peak Load (kW)": preview["estimated_peak_kw"],
            "Sum Individual Building Peaks (kW)": preview["sum_individual_peaks_kw"],
            "Peak Load Source": preview["peak_source"],
            "Space Heating Peak Load (kW)": selected_peak,
            "Peak Load User Edited": str(bool(user_edited)),
            "Profile Adjustment Input": "peak load" if user_edited else "building count",
            "SH Profile Method": (
                f"{profile_info.get('method', 'unknown base profile')}; "
                "constrained by annual energy and coincident peak"
            ),
            "BDEW Building Mix": profile_info.get("mix"),
            "Loss Profile Method": "70% template seasonal pattern + 30% adjusted SH load pattern",
        },
    )
    preview["selected_peak_kw"] = selected_peak
    preview["applied_peak_factor"] = applied_peak_factor
    return preview

def create_scenario(name):
    # SCENARIO_DIR = os.path.join(os.getcwd(), "data", "scenarios")
    current_project = get_current_project()
    if not current_project:
        st.error("Please open or create a project first.")
        return None
    _, SCENARIO_DIR = ensure_project_dirs(current_project)

    new_file = os.path.join(SCENARIO_DIR, f"{name}.xlsx")

    if os.path.exists(new_file):
        st.warning(f"Scenario '{name}' already exists in project '{current_project}'.")
        return new_file

    solution_type = st.session_state.get("solution_type", "Not set")

    if solution_type == "District heating (centralized)":
        template_to_use = TEMPLATE_CEN_FILE
    else:
        template_to_use = TEMPLATE_DEC_FILE

    if not os.path.exists(template_to_use):
        st.error(f"Template file not found: {template_to_use}")
        return None

    shutil.copy(template_to_use, new_file)
    st.success(
        f"✅ Scenario file created from template '{os.path.basename(template_to_use)}' in project '{current_project}'.")

    # --- Gather project info from session state ---
    project_name = st.session_state.get("project_name", get_current_project())
    project_location = st.session_state.get("project_city_name", "_Not set_")
    climate_zone = st.session_state.get("project_climate_zone", "_Not set_")
    city_name = zone_to_city.get(climate_zone)

    # --- TEASER import ---
    building_count = None
    route_length = None
    total_heat_demand = None
    total_dhw_demand = None
    linear_heat_density = None
    average_pipe_dimension = None
    unit_cost_pipe = None

    # Import the connected-building count automatically with tool-estimated
    # demand; it is later used to shape the centralized SH peak profile.
    if st.session_state.get("use_building_data"):
        building_count = _connected_building_count_from_session()

    if st.session_state.get("use_total_demand"):
        total_heat_demand = st.session_state.get("total_heat_demand")
        total_dhw_demand = st.session_state.get("total_dhw_demand")

    # --- Only for CENTRAL (district heating) scenarios ---
    if solution_type == "District heating (centralized)":
        if st.session_state.get("use_route_length") and "route_length" in st.session_state:
            route_length = st.session_state["route_length"]

        if route_length and total_heat_demand:
            try:
                # Convert to MWh/rm·a
                linear_heat_density = total_heat_demand / 1000 / route_length
            except ZeroDivisionError:
                linear_heat_density = None
        if linear_heat_density and linear_heat_density > 0:
            average_pipe_dimension = 48.6 * np.log(linear_heat_density) + 63
            unit_cost_pipe = unit_cost_pipe_euro_m_year(average_pipe_dimension)

    # Store message for confirmation display
    st.session_state["scenario_creation_message"] = (
        f"The representative-city weather template for {city_name} is being used as a fallback. "
        "Open Project Setup to check whether exact DWD weather is available for this location."
    )

    if not city_name:
        st.warning("⚠️ Cannot assign weather data: project location not set.")
        return new_file

    # Load the representative-city template for PV and as a documented fallback.
    try:
        weather_df = pd.read_excel(PV_COP_FILE, sheet_name=city_name)
    except Exception as e:
        st.warning(f"❌ Could not read weather sheet '{city_name}' in PV_COP.xlsx: {e}")
        return new_file

    try:
        scenario_timestamps = pd.read_excel(new_file, sheet_name="time_series", usecols=["timestamp"])
        weather_target_len = int(scenario_timestamps["timestamp"].notna().sum())
    except Exception:
        weather_target_len = len(weather_df)

    def _fit_annual_values(values, target_len):
        series = pd.to_numeric(pd.Series(values), errors="coerce").interpolate().ffill().bfill()
        if series.empty:
            return pd.Series(np.zeros(target_len, dtype=float))
        if len(series) < target_len:
            repeats = int(np.ceil(target_len / len(series)))
            series = pd.Series(np.tile(series.to_numpy(float), repeats)[:target_len])
        else:
            series = series.iloc[:target_len].reset_index(drop=True)
        return series

    if len(weather_df) != weather_target_len:
        fitted_weather = {}
        for column in weather_df.columns:
            fitted_weather[column] = _fit_annual_values(weather_df[column], weather_target_len)
        weather_df = pd.DataFrame(fitted_weather)

    # Prefer coordinate-based DWD temperatures. The representative-city file
    # remains the offline fallback and still supplies the PV profile.
    dwd_weather = st.session_state.get("dwd_weather")
    coords = get_valid_coords()
    if dwd_weather is None and coords is not None:
        try:
            dwd_weather = get_location_weather(*coords)
            st.session_state["dwd_weather"] = dwd_weather
        except Exception as exc:
            st.warning(
                "DWD hourly weather could not be loaded for this scenario; the representative-city "
                f"COP profiles remain in use. Details: {exc}"
            )
    if dwd_weather is not None:
        temperatures = _fit_annual_values(dwd_weather["temperature_c"], len(weather_df))
        cop_profiles = heat_pump_cop_profiles(
            temperatures, dwd_weather["ground_temperature_c"]
        )
        for column in cop_profiles.columns:
            weather_df[column] = cop_profiles[column].to_numpy()
        weather_df["DWD_Temperature_C.fix"] = temperatures.to_numpy()
        st.session_state["design_outdoor_temperature_c"] = dwd_weather[
            "design_outdoor_temperature_c"
        ]
        st.session_state["ground_temperature_c"] = dwd_weather["ground_temperature_c"]
        st.session_state["scenario_creation_message"] = (
            f"DWD hourly weather from {dwd_weather['station_name']} "
            f"(station {dwd_weather['station_id']}; {dwd_weather['profile_year_count']} averaged years: "
            f"{', '.join(map(str, dwd_weather['profile_years_used']))}; chronology {dwd_weather['year']}) "
            "is used for "
            "BDEW space-heating profile generation and heat-pump COP."
        )
    if "WWSHP.fix" not in weather_df.columns and "GSHP_B_HT.fix" in weather_df.columns:
        weather_df["WWSHP.fix"] = weather_df["GSHP_B_HT.fix"]

    try:
        # --- Write weather data to time_series sheet ---
        wb = openpyxl.load_workbook(new_file)

        # Write weather data
        if "time_series" not in wb.sheetnames:
            ws = wb.create_sheet("time_series")
            ws.append(weather_df.columns.tolist())
            for row in weather_df.itertuples(index=False):
                ws.append(list(row))
        else:
            ws = wb["time_series"]
            # Replace an existing weather/COP column when present. This keeps
            # repeated scenario updates from creating duplicate headers whose
            # pandas suffixes could make the simulation's COP choice ambiguous.
            existing_columns = {
                str(ws.cell(row=1, column=column).value): column
                for column in range(1, ws.max_column + 1)
                if ws.cell(row=1, column=column).value is not None
            }
            for col_name in weather_df.columns:
                col_idx = existing_columns.get(str(col_name))
                if col_idx is None:
                    col_idx = ws.max_column + 1
                    ws.cell(row=1, column=col_idx, value=col_name)
                    existing_columns[str(col_name)] = col_idx
                for row_idx, value in enumerate(weather_df[col_name], start=2):
                    ws.cell(row=row_idx, column=col_idx, value=value)

        # --- Write project metadata to 'meta' sheet ---
        if "meta" in wb.sheetnames:
            meta_ws = wb["meta"]
        else:
            meta_ws = wb.create_sheet("meta")

        # Clear old content
        for row in meta_ws.iter_rows():
            for cell in row:
                cell.value = None

        # Fill in metadata
        meta_ws["A1"] = "Key"
        meta_ws["B1"] = "Value"
        meta_ws["A2"] = "Project Name"
        meta_ws["B2"] = project_name
        meta_ws["A3"] = "Project Location"
        meta_ws["B3"] = project_location
        meta_ws["A4"] = "Legacy Fallback Climate Zone"
        meta_ws["B4"] = climate_zone
        meta_ws["A5"] = "Legacy Fallback Representative City"
        meta_ws["B5"] = city_name
        meta_ws["A6"] = "System Type"
        meta_ws["B6"] = solution_type
        meta_ws["A7"] = "Building Count"
        meta_ws["B7"] = building_count
        meta_ws["A8"] = "Route Length (rm)"
        meta_ws["B8"] = route_length
        meta_ws["A9"] = "average pipe dimension (DN)"
        meta_ws["B9"] = average_pipe_dimension
        meta_ws["A10"] = "unit cost pipe (€/m/year)"
        meta_ws["B10"] = unit_cost_pipe
        meta_ws["A11"] = "linear heat density (MWh/rm·a)"
        meta_ws["B11"] = linear_heat_density
        meta_ws["A12"] = "Total Space Heating Demand (kWh/a)"
        meta_ws["B12"] = total_heat_demand
        meta_ws["A13"] = "Total DHW Demand (kWh/a)"
        meta_ws["B13"] = total_dhw_demand
        meta_ws["A14"] = "DWD Weather Station"
        meta_ws["B14"] = dwd_weather.get("station_name") if dwd_weather else None
        meta_ws["A15"] = "DWD Station ID"
        meta_ws["B15"] = dwd_weather.get("station_id") if dwd_weather else None
        meta_ws["A16"] = "DWD Weather Year"
        meta_ws["B16"] = dwd_weather.get("year") if dwd_weather else None
        meta_ws["A17"] = "Design Outdoor Temperature (C)"
        meta_ws["B17"] = dwd_weather.get("design_outdoor_temperature_c") if dwd_weather else None
        meta_ws["A18"] = "Ground Boundary Temperature (C)"
        meta_ws["B18"] = dwd_weather.get("ground_temperature_c") if dwd_weather else None
        meta_ws["A19"] = "COP Method"
        meta_ws["B19"] = (
            "DWD source temperatures with 50% Carnot quality grade; HT 60 C, NT 40 C; "
            "ASHP COP x 0.80 below 5 C for defrosting"
            if dwd_weather else "representative-city template fallback"
        )
        meta_ws["A20"] = "DWD Complete Years Available"
        meta_ws["B20"] = dwd_weather.get("available_complete_year_count") if dwd_weather else None
        meta_ws["A21"] = "DWD Profile Years Used"
        meta_ws["B21"] = ", ".join(map(str, dwd_weather.get("profile_years_used", []))) if dwd_weather else None
        meta_ws["A22"] = "DWD Temperature Profile Method"
        meta_ws["B22"] = dwd_weather.get("profile_method") if dwd_weather else None

        wb.save(new_file)

    except Exception as e:
        st.error(f"❌ Failed to update Excel file '{new_file}': {e}")

    try:
        wb = openpyxl.load_workbook(new_file)
        if "transformers" in wb.sheetnames:
            transformers_ws = wb["transformers"]
            transformers_df = pd.DataFrame(transformers_ws.values)
            transformers_df.columns = transformers_df.iloc[0]
            transformers_df = transformers_df[1:].dropna(how='all')
            transformers_df = transformers_df.rename(columns=lambda x: str(x).strip())
            transformers_df["label"] = transformers_df["label"].astype(str).str.strip()

            # --- 1. Pipe Row Handling ---
            if solution_type == "District heating (centralized)":
                pipe_mask = transformers_df["label"].str.lower().str.contains("pipe")
                transformers_df = transformers_df[~(pipe_mask & (transformers_df["active"] == 1))]
                pipe_tpl = transformers_df[
                    transformers_df["label"].astype(str).str.contains("pipe", case=False, na=False) &
                    (transformers_df["active"] == 0)
                    ]
                if not pipe_tpl.empty and route_length and unit_cost_pipe:
                    pipe_row = pipe_tpl.iloc[0].copy()
                    pipe_row["active"] = 1
                    pipe_row["ep_costs"] = unit_cost_pipe
                    pipe_row["minimum"] = route_length *2
                    pipe_row["maximum"] = route_length *2
                    transformers_df = pd.concat([transformers_df, pipe_row.to_frame().T], ignore_index=True)

            for r_idx, row in enumerate([transformers_df.columns.tolist()] + transformers_df.values.tolist(), start=1):
                for c_idx, value in enumerate(row, start=1):
                    transformers_ws.cell(row=r_idx, column=c_idx, value=value)

            wb.save(new_file)

    except Exception as e:
        st.error(f"❌ Failed to update demand sheet in '{new_file}': {e}")

    return new_file

def get_file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0

def add_network_heat_loss(scenario_file, loss_fraction=0.05):
    """Recompute and (re)write Loss for centralized scenarios.
       Safe to call multiple times — it replaces the row if present.
    """
    # 1) Only for centralized solutions
    try:
        meta_df = pd.read_excel(scenario_file, sheet_name="meta")
        meta_dict = dict(zip(meta_df["Key"].astype(str), meta_df["Value"]))
        solution_type = str(meta_dict.get("System Type", "") or "").lower()
    except Exception:
        solution_type = str(st.session_state.get("solution_type", "") or "").lower()

    if "district" not in solution_type:
        return  # decentral: do nothing

    # 2) Load demand as DataFrame (headers preserved)
    try:
        df = pd.read_excel(scenario_file, sheet_name="demand")
    except Exception:
        return

    if df.empty or "label" not in df.columns or "from" not in df.columns:
        return

    # Normalize nominal value only (handles '1,23' -> 1.23)
    if "nominal value" not in df.columns:
        return
    df["nominal value"] = df["nominal value"].apply(parse_number).astype(float)

    # 3) Remove any existing heat-loss rows so we can re-add cleanly
    loss_labels = {HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL}
    df = df[~df["label"].astype(str).isin(loss_labels)]

    # 4) Sum only thermal demand rows
    df_heat = df[df["from"].astype(str).str.match(r"^b_th_", na=False)]
    total_nominal = float(df_heat["nominal value"].sum()) if not df_heat.empty else 0.0

    # 5) (Re)append loss row if there is thermal demand; otherwise keep it removed
    if total_nominal > 0:
        heat_loss = round(total_nominal * loss_fraction, 6)
        loss_row = {
            "label": HEAT_LOSS_DEMAND_LABEL,
            "active": 1,
            "from": "b_th_HT",  # default to HT bus
            "nominal value": heat_loss,
            "Demand in MWh/a": heat_loss / 1000.0,
            "Demand in GWh/a": heat_loss / 1e6,
        }
        df = pd.concat([df, pd.DataFrame([loss_row])], ignore_index=True)

    # 6) Persist (with comma-decimal as in your pipeline)
    # Keep nominal value numeric (comma-decimals safe)
    if "nominal value" in df.columns:
        df["nominal value"] = df["nominal value"].apply(parse_number).astype(float)

    # Recompute derived columns from nominal value (avoids comma-decimal parsing issues)
    df["Demand in MWh/a"] = df["nominal value"] / 1000.0
    df["Demand in GWh/a"] = df["nominal value"] / 1e6

    with pd.ExcelWriter(scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        df.to_excel(writer, sheet_name="demand", index=False)

def update_meta_sheet(scenario_file, new_meta_dict):
    wb = openpyxl.load_workbook(scenario_file)

    # If meta sheet doesn't exist yet, create it *logically* and start from empty DataFrame
    if "meta" not in wb.sheetnames:
        wb.create_sheet("meta")
        existing_meta = pd.DataFrame(columns=["Key", "Value"])
    else:
        # Meta sheet already exists on disk -> safe to read with pandas
        existing_meta = pd.read_excel(scenario_file, sheet_name="meta")

    # Rebuild meta dict
    if set(existing_meta.columns) >= {"Key", "Value"}:
        meta_dict = dict(zip(existing_meta["Key"], existing_meta["Value"]))
    else:
        meta_dict = {}

    meta_dict.update(new_meta_dict)
    updated_meta = pd.DataFrame(list(meta_dict.items()), columns=["Key", "Value"])

    # Overwrite meta sheet
    with pd.ExcelWriter(scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        updated_meta.to_excel(writer, sheet_name="meta", index=False)

def ensure_excel_file(excel_file, sheet_name, default_columns):
    if not os.path.exists(excel_file):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name
        ws.append(default_columns)
        wb.save(excel_file)
    else:
        wb = openpyxl.load_workbook(excel_file)
        if sheet_name not in wb.sheetnames:
            ws = wb.create_sheet(title=sheet_name)
            ws.append(default_columns)
            wb.save(excel_file)

def get_column_mapping(ws):
    header = [cell.value for cell in ws[1]]
    return {name: idx + 1 for idx, name in enumerate(header)}

def parse_number(val):
    if "," in str(val):
        val = str(val).replace(",", ".")
    try:
        return float(val)
    except:
        return 0.0

def value_to_comma_decimal(v):
    """Keep UI floats as '.', but when writing to Excel convert floats to '1,23' strings.
       Ints/None/str are left as-is."""
    if isinstance(v, float) and not np.isnan(v):
        # avoid scientific notation; keep reasonable precision
        s = f"{v:.12g}"
        return s.replace('.', ',')
    return v

def df_to_comma_decimal(df: pd.DataFrame, only_float_cols=True) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if only_float_cols:
            mask = pd.api.types.is_float_dtype(out[c])
        else:
            mask = pd.api.types.is_numeric_dtype(out[c])
        if mask:
            out[c] = out[c].apply(value_to_comma_decimal)
    return out

def dot_number_input(label: str, value: float | int | str, key: str, help: str | None = None):
    """
    Text-based numeric input that always displays a dot-decimal.
    Returns a float. Accepts both "." and "," from the user; displays ".".
    """
    try:
        init = f"{float(str(value).replace(',', '.')):.12g}"
    except Exception:
        init = "0"
    raw = st.text_input(label, value=init, key=key, help=help)
    # live-normalize commas to dots for display-then-parse
    normalized = raw.replace(",", ".")
    try:
        return float(normalized)
    except Exception:
        return 0.0

def reconcile_nv_flt_am(nv, flt, am, tol=1e-6, flt_default=8760.0):
    """
    Enforce: am (kWh) = nv (kW) * flt (h)

    Rules:
    - If 2 of (nv, flt, am) are provided -> compute the third.
    - If 3 are provided but inconsistent -> prefer nv & am, overwrite flt.
    - If only 1 provided -> keep others as None.
    - Special: if am is missing and flt == 8760 (default) -> keep am as None
      unless user explicitly provided am.
    """
    def pos(x):
        try:
            return x is not None and float(x) > 0
        except Exception:
            return False

    nv_ok, flt_ok, am_ok = pos(nv), pos(flt), pos(am)

    # Three provided: prefer nv & am
    if nv_ok and am_ok and flt_ok:
        implied_am = float(nv) * float(flt)
        if abs(float(am) - implied_am) > tol * max(1.0, abs(float(am))):
            flt = float(am) / float(nv)
        return float(nv), float(flt), float(am)

    # Two provided: compute third
    if nv_ok and flt_ok and not am_ok:
        # if flt is default 8760 and am was not given -> keep am empty
        if abs(float(flt) - float(flt_default)) < 1e-9:
            return float(nv), float(flt), None
        am = float(nv) * float(flt)
        return float(nv), float(flt), float(am)

    if nv_ok and am_ok and not flt_ok:
        flt = float(am) / float(nv)
        return float(nv), float(flt), float(am)

    if flt_ok and am_ok and not nv_ok:
        nv = float(am) / float(flt)
        return float(nv), float(flt), float(am)

    # Only one (or none): keep as-is (allows am to remain None)
    return nv, flt, am

# -------------------------------
# 🔧 Pipe cost model (€/m/year) with linear interpolation/extrapolation
PIPE_COST_POINTS_EUR_PER_M_PER_YEAR = [
    (25,  29.94604476),
    (32,  31.12966772),
    (40,  32.47396078),
    (50,  34.04778256),
    (63,  35.71341581),
    (75,  43.36974835),
    (90,  45.24195744),
    (110, 53.33439453),
    (125, 56.05585984),
    (160, 61.99072707),
]

def unit_cost_pipe_euro_m_year(dn_mm: float) -> float:
    """
    Returns pipe cost in €/m/year for a given pipe dimension DN (mm),
    using piecewise-linear interpolation between data points and
    linear extrapolation outside the range using the nearest two points.
    """
    pts = sorted(PIPE_COST_POINTS_EUR_PER_M_PER_YEAR, key=lambda t: t[0])
    x = float(dn_mm)

    # left extrapolation
    if x <= pts[0][0]:
        (x0, y0), (x1, y1) = pts[0], pts[1]
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    # right extrapolation
    if x >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    # interpolation inside range
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    # should never happen
    return float("nan")

def save_components(excel_file, sheet_name, components):
    wb = openpyxl.load_workbook(excel_file)
    ws = wb[sheet_name]
    col_map = get_column_mapping(ws)

    # Read existing labels and row numbers
    existing_labels = {}
    for row in ws.iter_rows(min_row=2, values_only=False):
        label_cell = row[col_map['label'] - 1]
        if label_cell.value:
            existing_labels[label_cell.value] = label_cell.row

    # Normalize labels by replacing spaces with underscores
    for comp in components:
        if "label" in comp:
            comp["label"] = comp["label"].strip().replace(" ", "_")

    new_labels = {comp['label'] for comp in components if comp['label'].strip() != ""}

    # Write/update components
    for comp in components:
        label = comp['label']
        if label.strip() == "":
            continue

        row_num = existing_labels.get(label)
        if not row_num:
            row_num = ws.max_row + 1
            while any(cell.value for cell in ws[row_num]):
                row_num += 1

        for key, value in comp.items():
            col = col_map.get(key)
            if col:
                ws.cell(row=row_num, column=col, value=value_to_comma_decimal(value))

    # Delete rows that were removed (based on missing labels)
    labels_to_delete = [label for label in existing_labels if label not in new_labels]
    for row_num in sorted([existing_labels[label] for label in labels_to_delete], reverse=True):
        ws.delete_rows(row_num, 1)

    wb.save(excel_file)

def read_existing_technologies(file, tech_map):
    existing = {}
    for tech_name, (sheet, tag) in tech_map.items():
        try:
            df = pd.read_excel(file, sheet_name=sheet)
            df = df[
                (df["active"] == 1)
                & (~df["label"].astype(str).str.contains("z_"))
                & (df["label"].astype(str).str.startswith(tag))
                ]
            for _, row in df.iterrows():
                source = row.get("from", "")
                if not source:
                    continue
                if source not in existing:
                    existing[source] = set()
                existing[source].add(tech_name)
        except Exception:
            continue
    return {k: list(v) for k, v in existing.items()}


def available_technology_options(excel_file, source):
    """Return only technologies defined by this scenario type's canonical template."""
    system_type = _scenario_system_type(excel_file)
    definition_file = (
        TEMPLATE_CEN_FILE
        if system_type == "District heating (centralized)"
        else TEMPLATE_DEC_FILE
    )
    available = []
    for tech in tech_options.get(source, []):
        sheet_name, tag = tech_sheet_map[tech]
        try:
            df = pd.read_excel(definition_file, sheet_name=sheet_name)
            labels = df.get("label", pd.Series(dtype=str)).astype(str).str.strip()
            if labels.str.startswith(tag).any():
                available.append(tech)
        except Exception:
            continue
    return available

def run_app():
    curr = get_current_project()
    # if curr and not st.session_state.get("_osm_preloaded") and not st.session_state.get("_auto_open_attempted"):
    #     st.session_state["_auto_open_attempted"] = True
    #     loaded_any = hydrate_saved_selection(curr)
    #     _bridge_bda_session_keys()
    #     st.session_state["show_building_estimate"] = bool(loaded_any)
    #     # st.rerun()

    st.sidebar.title("Workflow")
    page_options = [
        "Overview",
        "Project Setup",
        "Building Heat Demand",
        "Heat Supply Scenarios",
        "Project Review & Simulation",
        "Simulation Results",
    ]
    page_aliases = {
        "Introduction": "Overview",
        "Start / Overview": "Overview",
        "General Project Information": "Project Setup",
        "Map-based Estimation Tool": "Building Heat Demand",
        "Define Possible Scenarios": "Heat Supply Scenarios",
        "Project Management & Submit": "Project Review & Simulation",
        "Review & Run": "Project Review & Simulation",
        "Results": "Simulation Results",
        "1. Project Setup": "Project Setup",
        "2. Building Heat Demand": "Building Heat Demand",
        "3. Heat Supply Scenarios": "Heat Supply Scenarios",
        "4. Review & Run": "Project Review & Simulation",
    }
    current_page = st.session_state.get("_nav_radio", page_options[0])
    if current_page not in page_options:
        st.session_state["_nav_radio"] = page_aliases.get(current_page, page_options[0])
    requested_page = st.session_state.pop("requested_page", None)
    if requested_page in page_options:
        st.session_state["_nav_radio"] = requested_page

    page = st.sidebar.radio(
        "Go to",
        page_options,
        key="_nav_radio"
    )

    if page != "Building Heat Demand" and st.session_state.get("_persist_dirty", False):
        st.warning(
            "There are unsaved areas, building attributes, or demand results on "
            "**Building Heat Demand**. Return there and click "
            "**Save areas, buildings & results** before closing the tool or opening another project."
        )

    if page == "Overview":
        run_introduction_page()
    elif page == "Project Setup":
        run_general_project_page()
    elif page == "Building Heat Demand":
        run_building_estimation_page()
    elif page == "Heat Supply Scenarios":
        run_scenarios_page()
    elif page == "Project Review & Simulation":
        run_project_management_page()
    elif page == "Simulation Results":
        run_results_page()

def display_city_map_with_boundary(city_name: str, coords: tuple[float, float], show_marker=False, key=None):
    try:
        city_gdf = ox.geocode_to_gdf(city_name)
        geometry = city_gdf.geometry.iloc[0]

        # Center map based on geometry bounds
        bounds = geometry.bounds  # (minx, miny, maxx, maxy)
        center_lat = (bounds[1] + bounds[3]) / 2
        center_lon = (bounds[0] + bounds[2]) / 2

        # ✅ Initialize map with center
        m = folium.Map(location=[center_lat, center_lon], zoom_start=12)

        # ✅ Add city boundary shape
        folium.GeoJson(
            data=mapping(geometry),
            name="City Boundary",
            style_function=lambda x: {
                "fillColor": "#0078ff",
                "color": "black",
                "weight": 2,
                "fillOpacity": 0.1
            }
        ).add_to(m)

        # ✅ Optionally add marker at selected location
        if show_marker:
            folium.Marker(location=coords, tooltip=city_name).add_to(m)

        # ✅ Zoom map to bounds if valid
        m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
        st_folium(m, width=700, height=420, key=key, returned_objects=[])

    except Exception as e:
        st.warning(f"Could not load city boundary: {e}")
        m = folium.Map(location=coords, zoom_start=10)
        if show_marker:
            folium.Marker(location=coords, tooltip="Selected location").add_to(m)
        st_folium(m, width=700, height=420, key=key, returned_objects=[])

########################################################################################################################

def run_introduction_page():
    st.markdown(
        """
        <style>
        .start-page-title {
            font-size: 2.35rem;
            font-weight: 750;
            margin: 0 0 1.35rem 0;
        }
        .start-welcome {
            font-size: 1.35rem;
            font-weight: 700;
            margin: 0 0 1rem 0;
        }
        .start-copy {
            font-size: 1rem;
            line-height: 1.65;
            margin-bottom: 0.85rem;
        }
        .page-setup { color: #0369a1; font-weight: 700; }
        .page-demand { color: #b45309; font-weight: 700; }
        .page-scenarios { color: #047857; font-weight: 700; }
        .page-run { color: #7c3aed; font-weight: 700; }
        .page-results { color: #c2a85a; font-weight: 700; }
        </style>

        <div class="start-page-title">Overview</div>
        <div class="start-welcome">Welcome to the District Planning Tool!</div>

        <p class="start-copy">
        The District Planning Tool supports the identification and comparison of suitable heat supply
        solutions for a district.
        </p>

        <p class="start-copy">
        Start by creating a new project or opening an existing one under
        <span class="page-setup">Project Setup</span>.
        </p>

        <p class="start-copy">
        If the total space heating demand is unknown, use
        <span class="page-demand"> Building Heat Demand</span>. There you can select an area on a map,
        check the buildings found from OpenStreetMap, estimate space heating demand with TEASER, and estimate DHW demand with OpenDHW.
        </p>

        <p class="start-copy">
        In <span class="page-scenarios"> Heat Supply Scenarios</span>, specify available fuels and
        energy sources, select heat generation technologies, and define storage capacities to create
        and compare multiple system configurations.
        </p>

        <p class="start-copy">
        Finally, use <span class="page-run">Project Review & Simulation</span> to review the project and submit it
        for simulation and optimization.
        </p>

        <p class="start-copy">
        Depending on the number and complexity of scenarios, the optimization may take a few minutes.
        Once completed, results are automatically visualized on the
        <span class="page-results">Simulation Results</span> page to support transparent comparison
        and data-driven decision-making.
        </p>
        """,
        unsafe_allow_html=True,
    )

def run_general_project_page():
    st.title("Project Setup")
    st.caption(
        "Create or open a project, then set its location. The exact DWD station, temperature years, design "
        "temperature and ground-temperature source used later are shown here."
    )

    # -------------------------------
    # Session defaults
    if "project_name" not in st.session_state:
        st.session_state["project_name"] = ""
    if "project_city_name" not in st.session_state:
        st.session_state["project_city_name"] = ""
    if "project_climate_zone" not in st.session_state:
        st.session_state["project_climate_zone"] = "_Not set yet_"
    if "location_method" not in st.session_state:
        st.session_state["location_method"] = "Search by city/area"
    if "_skip_one_save" not in st.session_state:
        st.session_state["_skip_one_save"] = False
    if "_last_status" not in st.session_state:
        st.session_state["_last_status"] = None
    if "_last_coords" not in st.session_state:
        st.session_state["_last_coords"] = None
    if "_last_city_query" not in st.session_state:
        st.session_state["_last_city_query"] = ""

    projects = list_projects()
    curr = get_current_project()

    # -------------------------------
    # 1) Project section
    st.subheader("Project")

    left, right = st.columns([2, 1], vertical_alignment="top")

    with left:
        mode = st.radio(
            "What would you like to do?",
            ["Open an existing project", "Create a new project"],
            index=0 if projects else 1,
            horizontal=True,
        )

        if mode == "Open an existing project":
            if not projects:
                st.info("No projects found yet. Create a new project to get started.")
            else:
                selected_proj = st.selectbox(
                    "Choose a project",
                    projects,
                    index=(projects.index(curr) if curr in projects else 0),
                )
                if st.button("📂 Open", use_container_width=True):
                    set_current_project(selected_proj)
                    load_project_meta_into_session(selected_proj)
                    st.session_state["_skip_one_save"] = True
                    st.session_state["_last_status"] = f"Opened project: {selected_proj}"
                    st.rerun()

        else:
            # Single canonical name field (used for creation + later editing)
            st.session_state["project_name"] = st.text_input(
                "Project name",
                value=st.session_state.get("project_name", ""),
                placeholder="e.g. District Berlin Mitte",
            )

            name = st.session_state["project_name"].strip()

            if st.button("➕ Create & open", use_container_width=True):
                if not name:
                    st.session_state["_last_status"] = ("error", "Please enter a project name.")
                    st.rerun()

                if name in projects:
                    st.session_state["duplicate_project_attempt"] = name
                    st.rerun()

                set_current_project(name)
                reset_project_session()
                st.session_state["project_name"] = name
                save_project_meta_from_session(name)
                st.session_state["_last_status"] = f"Created and opened project: {name}"
                st.rerun()

            # Duplicate handling (cleaner + inline)
            if "duplicate_project_attempt" in st.session_state:
                pname = st.session_state["duplicate_project_attempt"]
                st.warning(f"A project named **{pname}** already exists.")
                c1, c2 = st.columns([1, 1])
                with c1:
                    if st.button("📂 Open existing instead", key="open_dup_proj", use_container_width=True):
                        set_current_project(pname)
                        load_project_meta_into_session(pname)
                        st.session_state["_skip_one_save"] = True
                        st.session_state.pop("duplicate_project_attempt", None)
                        st.session_state["_last_status"] = f"Opened project: {pname}"
                        st.rerun()
                with c2:
                    if st.button("❌ Choose a different name", key="cancel_dup_proj", use_container_width=True):
                        st.session_state.pop("duplicate_project_attempt", None)
                        st.session_state["_last_status"] = None
                        st.rerun()

    with right:
        curr = get_current_project()
        if curr:
            st.info(f"🗂 **Current project:** {curr}")
        else:
            st.warning("🗂 **No project opened yet.**")

        # Optional autosave toggle (keeps behavior explicit for new users)
        st.session_state["autosave_enabled"] = st.toggle(
            "Auto-save changes",
            value=st.session_state.get("autosave_enabled", True),
            help="When enabled, changes on this page are saved automatically to the current project.",
        )

    # Status message (shown once per rerun)
    if st.session_state.get("_last_status"):
        msg = st.session_state["_last_status"]
        if isinstance(msg, tuple) and msg[0] == "error":
            st.error(msg[1])
        else:
            st.success(str(msg))
        st.session_state["_last_status"] = None

    # If no project, stop here (prevents location + saving confusion)
    curr = get_current_project()
    if not curr:
        return

    with st.expander("Duplicate and rename this project", expanded=False):
        st.caption(
            "Create an independent copy with a new name. Project inputs, scenarios, and existing simulation "
            "results are copied, and the new project is opened automatically."
        )
        duplicate_name = st.text_input(
            "New project name",
            key=f"duplicate_project_name_{curr}",
            placeholder=f"Copy of {curr}",
        ).strip()
        if st.button("Duplicate project", key=f"duplicate_project_{curr}"):
            if not duplicate_name:
                st.warning("Enter a name for the duplicated project.")
            elif duplicate_name in list_projects():
                st.warning(f"A project named '{duplicate_name}' already exists.")
            else:
                try:
                    duplicate_project(curr, duplicate_name)
                    set_current_project(duplicate_name)
                    load_project_meta_into_session(duplicate_name)
                    st.session_state["_skip_one_save"] = True
                    st.session_state["_last_status"] = f"Duplicated and opened project: {duplicate_name}"
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not duplicate the project: {exc}")

    st.divider()

    # -------------------------------
    # 2) Location section
    st.subheader("Location")

    location_options = ["Search by city/area", "Pick on map"]
    location_aliases = {
        "Enter city or area name": "Search by city/area",
        "Search by city/area": "Search by city/area",
        "Select on map": "Pick on map",
        "Pick on map": "Pick on map",
    }
    current_location_method = location_aliases.get(
        st.session_state.get("location_method"),
        "Search by city/area",
    )
    st.session_state["location_method"] = current_location_method

    location_method = st.radio(
        "Set project location",
        location_options,
        index=location_options.index(current_location_method),
        horizontal=True,
    )
    st.session_state["location_method"] = location_method

    # Initialize geocoder only when location is used (avoid unnecessary calls)
    geolocator = Nominatim(user_agent="district_planning_tool", timeout=5)
    reverse = RateLimiter(geolocator.reverse, min_delay_seconds=1)

    city_input = ""

    if location_method == "Search by city/area":
        city_input = st.text_input(
            "City or area name",
            value=st.session_state.get("project_city_name", ""),
            placeholder="e.g. Berlin",
        )

        city_query = city_input.strip()

        # Show a neutral map only while there is no city query to resolve.
        if not st.session_state.get("project_coords") and not city_query:
            with st.expander("Show map", expanded=True):
                fallback_map = folium.Map(location=[51.1657, 10.4515], zoom_start=6)
                st_folium(
                    fallback_map,
                    width=700,
                    height=360,
                    key=f"project_fallback_map_{curr}",
                    returned_objects=[],
                )

        # Only geocode when input actually changes (prevents repeated popups)
        current_city = (st.session_state.get("project_city_name") or "").strip()
        has_valid_coords = get_valid_coords() is not None
        should_geocode = (
            bool(city_query)
            and city_query != st.session_state.get("_last_city_query", "")
            and not (has_valid_coords and city_query == current_city)
        )
        if should_geocode:
            st.session_state["_last_city_query"] = city_query
            try:
                location = geolocator.geocode(city_query)
                if not location:
                    st.error("City or area not found. Try a more specific name.")
                else:
                    coords = (location.latitude, location.longitude)
                    if st.session_state.get("project_coords") != coords:
                        _queue_scenario_weather_update(st.session_state.get("project_coords"), coords)
                        _invalidate_location_weather()
                    st.session_state["project_coords"] = coords
                    st.session_state["project_city_name"] = city_query

                    # Set zoom heuristic (optional)
                    zoom = 12
                    if hasattr(location, "raw") and "boundingbox" in location.raw:
                        bbox = list(map(float, location.raw["boundingbox"]))  # [south, north, west, east]
                        lat_span = bbox[1] - bbox[0]
                        if lat_span > 1.0:
                            zoom = 10
                        elif lat_span > 0.3:
                            zoom = 12
                        else:
                            zoom = 14
                    st.session_state["project_zoom"] = zoom

                    # Retained only for the representative-city offline fallback.
                    nearest = min(
                        known_city_coords.items(),
                        key=lambda item: geopy.distance.distance(coords, item[1]).km,
                    )
                    st.session_state["project_climate_zone"] = climate_zones.get(nearest[0], "Unknown")

                    st.success(f"📍 Location set to {st.session_state['project_city_name']}")
            except Exception as e:
                st.error(f"Geocoding failed: {e}")

        # ✅ Always render city map once coords exist
        coords = get_valid_coords()
        if coords:
            display_city_map_with_boundary(
                st.session_state.get("project_city_name", "Unknown"),
                coords,
                show_marker=False,
                key=f"project_boundary_map_{curr}_search",
            )

    else:  # Pick on map
        coords_default = st.session_state.get("project_coords") or (51.1657, 10.4515)

        with st.expander("Click on the map to set the location", expanded=True):
            pick_map_rev = int(st.session_state.get("project_pick_map_rev", 0) or 0)
            m = folium.Map(location=coords_default, zoom_start=6)
            map_data = st_folium(
                m,
                width=700,
                height=360,
                key=f"project_pick_map_{curr}_{pick_map_rev}",
                returned_objects=["last_clicked"],
            )

        if map_data.get("last_clicked"):
            lat = map_data["last_clicked"]["lat"]
            lon = map_data["last_clicked"]["lng"]
            coords = (lat, lon)
            click_sig = (round(float(lat), 7), round(float(lon), 7))

            # Only update when coords change
            if (
                st.session_state.get("_last_project_pick_click") != click_sig
                and st.session_state.get("project_coords") != coords
            ):
                st.session_state["_last_project_pick_click"] = click_sig
                _queue_scenario_weather_update(st.session_state.get("project_coords"), coords)
                _invalidate_location_weather()
                st.session_state["project_coords"] = coords

                # Retained only for the representative-city offline fallback.
                nearest = min(
                    known_city_coords.items(),
                    key=lambda item: geopy.distance.distance(coords, item[1]).km,
                )
                st.session_state["project_climate_zone"] = climate_zones.get(nearest[0], "Unknown")

                # Reverse geocode (best-effort)
                try:
                    location_info = reverse(coords, exactly_one=True)
                    address = location_info.raw.get("address", {}) if location_info else {}
                    city_name = (
                        address.get("city")
                        or address.get("town")
                        or address.get("village")
                        or address.get("hamlet")
                        or address.get("municipality")
                        or "Unknown"
                    )
                    st.session_state["project_city_name"] = city_name
                except Exception:
                    st.session_state["project_city_name"] = "Unknown"

                st.session_state["project_pick_map_rev"] = pick_map_rev + 1
                st.rerun()

        coords = get_valid_coords()
        if coords:
            city_name = st.session_state.get("project_city_name", "Unknown")

            display_city_map_with_boundary(
                city_name,
                coords,
                show_marker=False,
                key=f"project_boundary_map_{curr}_pick",
            )
            st.success(f"📍 Location set to {city_name}")

    coords = get_valid_coords()
    if coords:
        _render_project_weather_information(coords)
        _render_pending_scenario_weather_update(curr)

    # -------------------------------
    # 3) Save (explicit + less surprising)
    if st.session_state.get("autosave_enabled", True):
        if st.session_state.pop("_skip_one_save", False):
            pass  # skip exactly once after opening
        else:
            save_project_meta_from_session(curr)

    st.divider()

    # -------------------------------
    # 3) Review
    st.subheader("Review")

    project_name_disp = (st.session_state.get("project_name") or "").strip() or "_Not set yet_"
    coords = get_valid_coords()
    city_disp = (st.session_state.get("project_city_name") or "").strip() or "_Not set yet_"

    # st.markdown(f"- **Project:** {curr}")
    st.markdown(f"- **Project name:** {project_name_disp}")
    st.markdown(f"- **Location:** {city_disp}")

    if coords:
        lat, lon = coords
        st.markdown(f"- **Coordinates:** ({lat:.4f}, {lon:.4f})")
    else:
        st.markdown("- **Coordinates:** _Not set yet_")

def run_building_estimation_page():
    st.title("Building Heat Demand")
    st.caption("Select the project area, check the buildings found on the map, and estimate annual space heating and DHW demand.")

    curr = get_current_project()
    if not curr:
        reset_building_estimation_state()
        st.warning(
            "No project is currently open. Go to **Project Setup** to create or open a project."
        )
        return

    st.info(f"🗂 Active project: {curr}")

    coords = get_valid_coords()
    if not coords:
        st.warning("📍 Project location is not set yet.")
        st.info("Go to **Project Setup** and set a location to enable map-based estimation.")
        return

    lat, lon = coords
    weather = st.session_state.get("dwd_weather")
    if isinstance(weather, dict) and weather.get("station_name"):
        st.caption(
            f"📍 Location: ({lat:.4f}, {lon:.4f}) · DWD station: "
            f"{weather['station_name']} ({weather.get('station_id', 'unknown ID')})"
        )
    else:
        st.caption(f"📍 Location: ({lat:.4f}, {lon:.4f})")

    def _has_any_saved():
        ss = st.session_state
        if isinstance(ss.get("drawn_polygons"), list) and ss["drawn_polygons"]:
            return True
        if isinstance(ss.get("buildings_gdf"), pd.DataFrame) and not ss["buildings_gdf"].empty:
            return True
        if isinstance(ss.get("manual_buildings"), pd.DataFrame) and not ss["manual_buildings"].empty:
            return True
        if isinstance(ss.get("building_demand_estimates"), pd.DataFrame) and not ss["building_demand_estimates"].empty:
            return True
        return False

    if not _has_any_saved():
        st.info("No saved area or buildings yet. Draw a polygon on the map below to start.")

    _bridge_bda_session_keys()

    try:
        from logic.building_demand_analyse import run_building_heat_demand_page
        run_building_heat_demand_page()
    except Exception as e:
        st.error("❌ The building estimation module could not be loaded.")
        with st.expander("Show technical details"):
            st.exception(e)
        return

def _network_length_from_buildings(area_wkt: str, buildings, excluded_ids=None):
    """Estimate route length from already-loaded buildings."""
    polygon = _wkt.loads(area_wkt)
    if buildings is None or getattr(buildings, "empty", True):
        raise ValueError("No OSM building footprints were found in the selected area.")

    b = buildings.copy()
    excluded_ids = set(map(str, excluded_ids or set()))
    if excluded_ids and "network_id" in b.columns:
        b = b.loc[~b["network_id"].astype(str).isin(excluded_ids)].copy()
    raw_levels = (
        b["building:levels"]
        if "building:levels" in b.columns
        else b.get("levels", pd.Series(np.nan, index=b.index))
    )
    levels = pd.to_numeric(
        raw_levels.astype(str).str.split(";").str[0].str.replace(",", ".", regex=False),
        errors="coerce",
    )
    levels = levels.where(levels >= 1)
    if "height" in b.columns:
        height_m = pd.to_numeric(
            b["height"].astype(str).str.replace("m", "", regex=False).str.strip(),
            errors="coerce",
        )
        height_m = height_m.where(height_m > 0)
        levels = levels.fillna((height_m / 3.0).round().clip(lower=1))
    levels = levels.fillna(3).clip(lower=1)

    footprint = pd.to_numeric(b["area_m2"], errors="coerce").fillna(0)
    total_floor_area = float((footprint * levels).sum())

    area_gdf = gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326")
    try:
        metric_crs = area_gdf.estimate_utm_crs()
        selected_area_m2 = float(area_gdf.to_crs(metric_crs).geometry.iloc[0].area)
    except Exception:
        selected_area_m2 = float(area_gdf.to_crs(3857).geometry.iloc[0].area)
    if selected_area_m2 <= 0:
        raise ValueError("The selected polygon has no measurable area.")

    plot_ratio = total_floor_area / selected_area_m2
    route_length = 16.171 * (plot_ratio ** 0.1495) * 1000 * selected_area_m2 / 1_000_000
    return float(route_length), b, selected_area_m2, total_floor_area


def _network_length_from_polygon(area_wkt: str, excluded_ids=None):
    """Load OSM buildings once, then estimate route length."""
    buildings = _rehydrate_osm_buildings_from_polygons([area_wkt])
    return _network_length_from_buildings(area_wkt, buildings, excluded_ids)


def _scenario_system_type(scenario_file):
    try:
        meta_df = pd.read_excel(scenario_file, sheet_name="meta")
        if {"Key", "Value"}.issubset(meta_df.columns):
            values = dict(zip(meta_df["Key"], meta_df["Value"]))
            return str(values.get("System Type", ""))
    except Exception:
        pass
    return ""


def _scenario_route_length(scenario_file):
    try:
        meta_df = pd.read_excel(scenario_file, sheet_name="meta")
        if {"Key", "Value"}.issubset(meta_df.columns):
            values = dict(zip(meta_df["Key"], meta_df["Value"]))
            value = values.get("Route Length (rm)")
            value = pd.to_numeric(value, errors="coerce")
            return float(value) if pd.notna(value) and float(value) > 0 else None
    except Exception:
        pass
    return None


def _apply_network_length_to_scenario(scenario_file, route_length):
    """Persist a project network length in a centralized scenario workbook."""
    if not scenario_file or not os.path.exists(scenario_file):
        return
    route_length = float(route_length)
    if _scenario_system_type(scenario_file) != "District heating (centralized)":
        update_meta_sheet(scenario_file, {"Route Length (rm)": route_length})
        return

    total_heat_demand = 0.0
    try:
        demand = pd.read_excel(scenario_file, sheet_name="demand")
        labels = demand.get("label", pd.Series("", index=demand.index)).astype(str)
        thermal = demand.get("from", pd.Series("", index=demand.index)).astype(str).str.match(r"^b_th_", na=False)
        thermal &= ~labels.isin({HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL})
        total_heat_demand = float(pd.to_numeric(demand.loc[thermal, "nominal value"], errors="coerce").sum())
    except Exception:
        pass

    linear_heat_density = total_heat_demand / 1_000 / route_length if total_heat_demand > 0 else None
    average_pipe_dimension = (
        48.6 * np.log(linear_heat_density) + 63
        if linear_heat_density is not None and linear_heat_density > 0 else None
    )
    unit_cost_pipe = (
        unit_cost_pipe_euro_m_year(average_pipe_dimension)
        if average_pipe_dimension is not None else None
    )
    update_meta_sheet(
        scenario_file,
        {
            "Route Length (rm)": route_length,
            "average pipe dimension (DN)": average_pipe_dimension,
            "unit cost pipe (€/m/year)": unit_cost_pipe,
            "linear heat density (MWh/rm·a)": linear_heat_density,
        },
    )

    try:
        df = pd.read_excel(scenario_file, sheet_name="transformers")
        labels = df.get("label", pd.Series("", index=df.index)).astype(str)
        pipe_mask = labels.str.contains("pipe", case=False, na=False)
        active = pd.to_numeric(df.get("active", 0), errors="coerce").fillna(0)
        active_pipe = pipe_mask & active.eq(1)
        if active_pipe.any():
            df.loc[active_pipe, ["minimum", "maximum"]] = route_length * 2
            if unit_cost_pipe is not None and "ep_costs" in df.columns:
                df.loc[active_pipe, "ep_costs"] = unit_cost_pipe
        else:
            template_pipe = df.loc[pipe_mask & active.eq(0)]
            if not template_pipe.empty:
                row = template_pipe.iloc[0].copy()
                row["active"] = 1
                row["minimum"] = route_length * 2
                row["maximum"] = route_length * 2
                if unit_cost_pipe is not None:
                    row["ep_costs"] = unit_cost_pipe
                df = pd.concat([df, row.to_frame().T], ignore_index=True)
        with pd.ExcelWriter(scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            df.to_excel(writer, sheet_name="transformers", index=False)
    except Exception as exc:
        st.warning(f"The length was saved, but the scenario pipe settings could not be updated: {exc}")


def run_network_length_page(scenario_file=None):
    st.markdown("#### Network Length")
    st.caption(
        "Set the district-heating route length for this centralized supply scenario. "
        "This is the one-way route occupied by a supply-and-return pair, not the sum of both pipes. "
    )

    curr = get_current_project()
    if not curr:
        st.warning("Open or create a project on **Project Setup** first.")
        return

    system_type = _scenario_system_type(scenario_file) if scenario_file else ""
    if system_type and system_type != "District heating (centralized)":
        st.info("This scenario is decentralized, so a district-heating network length is not required.")
        return

    scenario_length = _scenario_route_length(scenario_file)
    project_length = st.session_state.get("route_length")
    current_length = scenario_length or project_length
    if scenario_file and scenario_length is None and project_length:
        _apply_network_length_to_scenario(scenario_file, project_length)
        scenario_length = float(project_length)
        current_length = scenario_length
    if current_length is not None:
        source = st.session_state.get("route_length_source") or "building-demand estimate"
        st.success(
            f"Current network route length: **{float(current_length):,.0f} m** ({source}); "
            f"total supply + return pipe length: **{2 * float(current_length):,.0f} m**. "
            "You do not need to enter anything unless you want to replace this value."
        )
    else:
        st.warning(
            "This centralized scenario has no network length. Enter a known value below or estimate one "
            "from an OpenStreetMap area."
        )

    mode = st.radio(
        "How should the network length be set?",
        ["Enter a known length", "Estimate from an OSM area"],
        horizontal=True,
        key="network_length_mode",
    )

    if mode == "Enter a known length":
        known_length = st.number_input(
            "Network trench/route length (m)",
            min_value=0.0,
            value=float(current_length or 0.0),
            step=10.0,
            key="network_length_manual_m",
            help=(
                "Enter the one-way trench/route length. Do not add supply and return together; "
                "the model doubles this value automatically."
            ),
        )
        if st.button("Save network length", key="save_manual_network_length", disabled=known_length <= 0):
            st.session_state["route_length"] = float(known_length)
            st.session_state["route_length_source"] = "user input"
            save_project_meta_from_session(curr)
            _apply_network_length_to_scenario(scenario_file, known_length)
            st.success("Network length saved for this project.")
            st.rerun()
        return

    coords = get_valid_coords() or (52.52, 13.405)
    polygon_wkts = list(st.session_state.get("network_length_polygons_wkt") or [])
    legacy_polygon = st.session_state.get("network_length_polygon_wkt")
    if legacy_polygon and not polygon_wkts:
        polygon_wkts = [legacy_polygon]
    polygon_wkts = [value for value in polygon_wkts if value]
    st.session_state["network_length_polygons_wkt"] = polygon_wkts
    selected_geometries = [_wkt.loads(value) for value in polygon_wkts]
    selected_area_geometry = unary_union(selected_geometries) if selected_geometries else None
    polygon_wkt = selected_area_geometry.wkt if selected_area_geometry is not None else None

    estimated_buildings = st.session_state.get("network_length_osm_buildings")
    excluded_network_ids = set(map(str, st.session_state.get("network_length_excluded_ids", set())))
    needs_building_load = (
        not isinstance(estimated_buildings, (pd.DataFrame, gpd.GeoDataFrame))
        or getattr(estimated_buildings, "empty", True)
    )
    if polygon_wkt and needs_building_load:
        try:
            with st.spinner("Loading buildings from OpenStreetMap for the selected areas..."):
                estimated_buildings = _rehydrate_osm_buildings_from_polygons(polygon_wkts)
                estimate, included, selected_area, floor_area = _network_length_from_buildings(
                    polygon_wkt, estimated_buildings, excluded_network_ids
                )
            st.session_state["network_length_osm_buildings"] = estimated_buildings
            st.session_state["network_length_included_buildings"] = included
            st.session_state["network_length_estimate_m"] = estimate
            st.session_state["network_length_estimate_area_m2"] = selected_area
            st.session_state["network_length_estimate_floor_m2"] = floor_area
        except Exception as exc:
            st.warning(f"Buildings could not be loaded automatically: {exc}")

    m = folium.Map(location=list(coords), zoom_start=st.session_state.get("project_zoom", 13))
    Draw(
        export=False,
        draw_options={
            "polygon": {"allowIntersection": False, "showArea": True},
            "rectangle": True,
            "polyline": False,
            "circle": False,
            "circlemarker": False,
            "marker": False,
        },
        edit_options={"edit": False, "remove": False},
    ).add_to(m)

    # streamlit-folium versions differ in which drawing field they populate.
    # Explicitly add the completed Leaflet layer so all supported return fields
    # can observe a newly drawn polygon.
    from branca.element import MacroElement, Template
    map_var = m.get_name()
    macro = MacroElement()
    macro._template = Template(f"""
    {{% macro script(this, kwargs) %}}
    {map_var}.on(L.Draw.Event.CREATED, function (e) {{
        e.layer.addTo({map_var});
    }});
    {{% endmacro %}}
    """)
    m.get_root().add_child(macro)

    if selected_area_geometry is not None:
        folium.GeoJson(
            mapping(selected_area_geometry),
            style_function=lambda _: {"color": "#005f73", "weight": 3, "fillOpacity": 0.18},
            tooltip="Selected network area",
        ).add_to(m)
        minx, miny, maxx, maxy = selected_area_geometry.bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]])

    if isinstance(estimated_buildings, (pd.DataFrame, gpd.GeoDataFrame)) and not estimated_buildings.empty:
        folium.GeoJson(
            estimated_buildings.__geo_interface__,
            style_function=lambda feature: {
                "color": "#4b5563" if str(feature.get("properties", {}).get("network_id")) in excluded_network_ids else "#2563eb",
                "weight": 2 if str(feature.get("properties", {}).get("network_id")) in excluded_network_ids else 1,
                "fillColor": "#9ca3af" if str(feature.get("properties", {}).get("network_id")) in excluded_network_ids else "#60a5fa",
                "fillOpacity": 0.55 if str(feature.get("properties", {}).get("network_id")) in excluded_network_ids else 0.25,
            },
            tooltip=folium.GeoJsonTooltip(fields=["network_id"], aliases=["Building:"]),
        ).add_to(m)

    map_result = st_folium(
        m,
        width=800,
        height=520,
        key=f"network_length_map_{curr}_{st.session_state.get('network_length_map_rev', 0)}",
        returned_objects=["last_object_drawn", "all_drawings", "last_object_clicked"],
    )
    clicked = (map_result or {}).get("last_object_clicked")
    clicked_buildings = None
    if clicked and isinstance(estimated_buildings, (pd.DataFrame, gpd.GeoDataFrame)) and not estimated_buildings.empty:
        try:
            clicked_point = Point(float(clicked["lng"]), float(clicked["lat"]))
            clicked_buildings = estimated_buildings.loc[
                estimated_buildings.geometry.intersects(clicked_point)
            ]
        except Exception:
            clicked_buildings = None

    geometry = None
    # A clicked building is also a polygon and some streamlit-folium versions
    # expose it as an active drawing. Only Leaflet.Draw output may replace the
    # selected network area; building clicks are handled separately below.
    if map_result:
        drawings = map_result.get("all_drawings") or {}
        features = drawings.get("features") or [] if isinstance(drawings, dict) else drawings
        for feature in reversed(features):
            candidate = (feature or {}).get("geometry")
            if candidate and candidate.get("type") in {"Polygon", "MultiPolygon"}:
                geometry = candidate
                break
        is_building_click = clicked_buildings is not None and not clicked_buildings.empty
        if geometry is None and not is_building_click:
            drawn = map_result.get("last_object_drawn")
            candidate = (drawn or {}).get("geometry")
            if candidate and candidate.get("type") in {"Polygon", "MultiPolygon"}:
                geometry = candidate
    if geometry and geometry.get("type") in {"Polygon", "MultiPolygon"}:
        new_wkt = _shape(geometry).wkt
        new_geometry = _wkt.loads(new_wkt)
        already_selected = any(new_geometry.equals(existing) for existing in selected_geometries)
        if not already_selected:
            polygon_wkts.append(new_wkt)
            st.session_state["network_length_polygons_wkt"] = polygon_wkts
            st.session_state["network_length_polygon_wkt"] = unary_union(
                [_wkt.loads(value) for value in polygon_wkts]
            ).wkt
            st.session_state.pop("network_length_osm_buildings", None)
            st.session_state.pop("network_length_excluded_ids", None)
            st.session_state.pop("network_length_last_building_click", None)
            st.session_state["network_length_map_rev"] = int(
                st.session_state.get("network_length_map_rev", 0)
            ) + 1
            st.rerun()

    if clicked and clicked_buildings is not None and not clicked_buildings.empty:
        try:
            click_key = f"{float(clicked['lat']):.7f},{float(clicked['lng']):.7f}"
            if click_key != st.session_state.get("network_length_last_building_click"):
                if not clicked_buildings.empty:
                    building_id = str(clicked_buildings.iloc[-1]["network_id"])
                    excluded = set(map(str, st.session_state.get("network_length_excluded_ids", set())))
                    if building_id in excluded:
                        excluded.remove(building_id)
                    else:
                        excluded.add(building_id)
                    st.session_state["network_length_excluded_ids"] = excluded
                    st.session_state["network_length_last_building_click"] = click_key
                    estimate, included, selected_area, floor_area = _network_length_from_buildings(
                        polygon_wkt, estimated_buildings, excluded
                    )
                    st.session_state["network_length_estimate_m"] = estimate
                    st.session_state["network_length_included_buildings"] = included
                    st.session_state["network_length_estimate_area_m2"] = selected_area
                    st.session_state["network_length_estimate_floor_m2"] = floor_area
                    st.session_state["network_length_map_rev"] = int(
                        st.session_state.get("network_length_map_rev", 0)
                    ) + 1
                    st.rerun()
        except Exception:
            pass

    if not polygon_wkt:
        st.info("Draw one or more polygons or rectangles around the intended network areas.")
        return

    st.caption(f"Selected network areas: {len(polygon_wkts)}")
    if st.button("Clear all selected network areas", key="network_length_clear_areas"):
        for key in (
            "network_length_polygon_wkt", "network_length_polygons_wkt",
            "network_length_osm_buildings", "network_length_included_buildings",
            "network_length_excluded_ids", "network_length_estimate_m",
        ):
            st.session_state.pop(key, None)
        st.session_state["network_length_map_rev"] = int(
            st.session_state.get("network_length_map_rev", 0)
        ) + 1
        st.rerun()

    if isinstance(estimated_buildings, (pd.DataFrame, gpd.GeoDataFrame)) and not estimated_buildings.empty:
        excluded_count = len(excluded_network_ids)
        st.caption(
            "Click a building footprint on the map to exclude or include it. "
            f"Excluded buildings are gray. Currently excluded: {excluded_count}."
        )
        if excluded_count and st.button("Include all buildings", key="network_length_include_all"):
            st.session_state["network_length_excluded_ids"] = set()
            estimate, included, selected_area, floor_area = _network_length_from_buildings(
                polygon_wkt, estimated_buildings, set()
            )
            st.session_state["network_length_estimate_m"] = estimate
            st.session_state["network_length_included_buildings"] = included
            st.session_state["network_length_estimate_area_m2"] = selected_area
            st.session_state["network_length_estimate_floor_m2"] = floor_area
            st.session_state["network_length_map_rev"] = int(
                st.session_state.get("network_length_map_rev", 0)
            ) + 1
            st.rerun()

    if st.button("Reload buildings and recalculate network length", key="estimate_network_length"):
        try:
            with st.spinner("Reloading OSM buildings and estimating the route length..."):
                buildings = _rehydrate_osm_buildings_from_polygons(polygon_wkts)
                estimate, included, selected_area, floor_area = _network_length_from_buildings(
                    polygon_wkt, buildings, excluded_network_ids
                )
            st.session_state["network_length_estimate_m"] = estimate
            st.session_state["network_length_osm_buildings"] = buildings
            st.session_state["network_length_included_buildings"] = included
            st.session_state["network_length_estimate_area_m2"] = selected_area
            st.session_state["network_length_estimate_floor_m2"] = floor_area
            st.rerun()
        except Exception as exc:
            st.error(f"Network-length estimation failed: {exc}")

    estimate = st.session_state.get("network_length_estimate_m")
    if estimate is not None:
        st.markdown(
            f"Estimated trench/route length: **{float(estimate):,.0f} m**  "
            f"(supply + return pipes: **{2 * float(estimate):,.0f} m**)"
        )
        st.caption(
            "This uses the same plot-density equation as the Building Heat Demand page. "
            "OSM levels are used when available; otherwise height/3 m or three levels is assumed."
        )
        if st.button(
            "Use this estimated network length",
            key="save_estimated_network_length",
            disabled=float(estimate) <= 0,
        ):
            st.session_state["route_length"] = float(estimate)
            st.session_state["route_length_source"] = "OSM area estimate"
            save_project_meta_from_session(curr)
            _apply_network_length_to_scenario(scenario_file, estimate)
            st.success("Estimated network length saved for this project.")
            st.rerun()


def run_scenarios_page():
    st.title("Heat Supply Scenarios")

    st.subheader("Create Scenarios")

    current_project = get_current_project()
    if current_project:
        _load_saved_project_totals(current_project)

    # Load existing scenarios
    scenarios = list_scenarios()

    if not scenarios:
        st.session_state["show_create_scenario"] = True

    # --- Toggle form visibility ---
    if "show_create_scenario" not in st.session_state:
        st.session_state["show_create_scenario"] = False

    # --- Button to trigger scenario creation ---
    if scenarios and not st.session_state["show_create_scenario"]:
        if st.button("➕ Create New Scenario"):
            st.session_state["show_create_scenario"] = True

    # --- Scenario creation form (conditionally shown) ---
    if st.session_state["show_create_scenario"]:
        with st.expander("🆕 Create a New Scenario", expanded=True):
            st.markdown("**Scenario Configuration**")
            solution_type = st.radio(
                "Choose system type:",
                ["District heating (centralized)", "Decentralized solution"],
                key="new_scenario_solution_type",
            )

            with st.form("create_scenario_form", clear_on_submit=False):
                st.markdown("**Scenario Name**")
                new_name = st.text_input("Enter the scenario name here", key="new_scenario_name")

                sh_value = st.session_state.get("total_heat_demand")
                dhw_value = st.session_state.get("total_dhw_demand")
                tool_demand_available = sh_value is not None and dhw_value is not None
                route_available = bool(st.session_state.get("route_length") and st.session_state["route_length"] > 0)
                existing_demand_scenarios = {
                    scenario: _scenario_demand_summary(get_scenario_file(scenario))
                    for scenario in scenarios
                }
                existing_demand_scenarios = {
                    scenario: totals for scenario, totals in existing_demand_scenarios.items()
                    if totals["space_heating_kwh"] > 0 or totals["dhw_kwh"] > 0
                }

                if not route_available:
                    st.session_state["new_scenario_use_route_length"] = False
                if not tool_demand_available:
                    st.session_state["new_scenario_use_total_demand"] = False
                use_building_data = tool_demand_available
                use_route_length = False
                if solution_type == "District heating (centralized)":
                    use_route_length = st.checkbox(
                        "Use saved network trench/route length",
                        value=route_available,
                        key="new_scenario_use_route_length",
                        disabled=not route_available,
                    )
                    if use_route_length and st.session_state.get("route_length") is None:
                        st.warning(
                            "No total network length is saved. You can create the scenario now, then enter a value "
                            "or estimate it in its **🟡 Network Length** section."
                        )
                use_total_demand = st.checkbox(
                    "Use total space heating and DHW demand from **Building Heat Demand**",
                    value=tool_demand_available,
                    key="new_scenario_use_total_demand",
                    disabled=not tool_demand_available,
                )

                copy_existing_demand = False
                demand_source_scenario = None
                if not tool_demand_available and existing_demand_scenarios:
                    copy_existing_demand = st.checkbox(
                        "Use demand already entered in an existing scenario?",
                        value=True,
                        key="new_scenario_copy_existing_demand",
                    )
                    demand_source_scenario = st.selectbox(
                        "Demand source scenario",
                        list(existing_demand_scenarios),
                        disabled=not copy_existing_demand,
                        key="new_scenario_demand_source",
                    )
                elif not tool_demand_available:
                    st.caption("No estimated or previously entered demand is available. Add demand after creation.")

                col1, col2 = st.columns([1, 1])
                create_clicked = col1.form_submit_button("✅ Confirm and Create", use_container_width=True)
                cancel_clicked = col2.form_submit_button("❌ Cancel", use_container_width=True)

            if cancel_clicked:
                st.session_state["show_create_scenario"] = False
                st.rerun()

            if create_clicked:
                clean_name = new_name.strip()
                use_building_data = bool(use_total_demand and tool_demand_available)
                scenario_building_count = _connected_building_count_from_session() if use_building_data else None
                if not clean_name:
                    st.warning("Enter a scenario name.")
                elif clean_name in scenarios:
                    st.warning(f"Scenario '{clean_name}' already exists.")
                else:
                    sh_value = st.session_state.get("total_heat_demand")
                    dhw_value = st.session_state.get("total_dhw_demand")
                    missing_demand_parts = []
                    if use_total_demand and sh_value is None:
                        missing_demand_parts.append("space heating")
                    if use_total_demand and dhw_value is None:
                        missing_demand_parts.append("DHW")

                    if missing_demand_parts:
                        missing_txt = " and ".join(missing_demand_parts)
                        st.warning(
                            f"No saved {missing_txt} demand was found. Run the full building demand workflow first, "
                            "or uncheck the demand import option."
                        )
                    else:
                        st.session_state["use_building_data"] = use_building_data
                        st.session_state["use_route_length"] = use_route_length
                        st.session_state["use_total_demand"] = use_total_demand
                        st.session_state["solution_type"] = solution_type
                        scenario_file = create_scenario(clean_name)
                        st.session_state["scenario_just_created"] = clean_name
                        st.session_state["show_create_scenario"] = False

                        update_meta_sheet(
                            scenario_file,
                            {
                                "use_building_data": str(use_building_data),
                                "use_route_length": str(use_route_length),
                                "use_total_demand": str(use_total_demand),
                                "demand_source_scenario": demand_source_scenario if copy_existing_demand else "",
                            },
                        )

                        if use_total_demand:
                            sheet_name = "demand"
                            ensure_excel_file(
                                scenario_file,
                                sheet_name,
                                ["label", "active", "from", "nominal value", "Demand in MWh/a", "Demand in GWh/a"],
                            )
                            auto_save_components = [
                                _demand_row(SPACE_HEATING_DEMAND_LABEL, sh_value),
                                _demand_row(DHW_DEMAND_LABEL, dhw_value),
                            ]
                            df_dem = pd.DataFrame(auto_save_components)
                            for c in ["nominal value", "Demand in MWh/a", "Demand in GWh/a"]:
                                df_dem[c] = pd.to_numeric(df_dem[c], errors="coerce")
                            df_dem["active"] = pd.to_numeric(
                                df_dem["active"], errors="coerce"
                            ).fillna(0).astype(int)

                            with pd.ExcelWriter(
                                scenario_file,
                                engine="openpyxl",
                                mode="a",
                                if_sheet_exists="replace",
                            ) as writer:
                                df_dem.to_excel(writer, sheet_name=sheet_name, index=False)

                            write_dhw_profile_to_time_series(scenario_file)
                            if solution_type == "District heating (centralized)":
                                add_network_heat_loss(scenario_file, loss_fraction=0.05)
                            _update_scenario_load_profiles(
                                scenario_file,
                                building_count=scenario_building_count,
                            )
                            st.success("Space heating and DHW demand were automatically saved to the new scenario.")

                        elif copy_existing_demand and demand_source_scenario:
                            source_file = get_scenario_file(demand_source_scenario)
                            _copy_scenario_demand(source_file, scenario_file)
                            source_meta = _scenario_meta_values(source_file)
                            source_count = pd.to_numeric(source_meta.get("Building Count"), errors="coerce")
                            source_count = int(source_count) if pd.notna(source_count) and source_count > 0 else None
                            source_peak = pd.to_numeric(
                                source_meta.get("Space Heating Peak Load (kW)"), errors="coerce"
                            )
                            source_peak = float(source_peak) if pd.notna(source_peak) and source_peak > 0 else None
                            source_peak_edited = str(source_meta.get("Peak Load User Edited", "False")).lower() == "true"
                            copied_totals = _scenario_demand_summary(scenario_file)
                            update_meta_sheet(
                                scenario_file,
                                {
                                    "Total Space Heating Demand (kWh/a)": copied_totals["space_heating_kwh"],
                                    "Total DHW Demand (kWh/a)": copied_totals["dhw_kwh"],
                                },
                            )
                            if solution_type == "District heating (centralized)":
                                add_network_heat_loss(scenario_file, loss_fraction=0.05)
                                if use_route_length:
                                    _apply_network_length_to_scenario(
                                        scenario_file, st.session_state.get("route_length")
                                    )
                            _update_scenario_load_profiles(
                                scenario_file,
                                building_count=source_count,
                                peak_load_kw=source_peak,
                                user_edited=source_peak_edited,
                            )
                            st.success(f"Demand was copied from scenario '{demand_source_scenario}'.")

                        st.session_state["last_selected_scenario"] = clean_name
                        st.session_state["current_scenario_file"] = scenario_file
                        st.rerun()

    # --- Scenario creation feedback ---
    if "scenario_just_created" in st.session_state:
        created_name = st.session_state.pop("scenario_just_created")
        st.success(f"Scenario '{created_name}' opened.")

    if "scenario_creation_message" in st.session_state:
        st.info(st.session_state.pop("scenario_creation_message"))

    # --- Existing Scenarios ---
    if scenarios:
        st.subheader("Edit Scenarios")

        previous_selection = st.session_state.get("last_selected_scenario", None)
        default_index = scenarios.index(previous_selection) if previous_selection in scenarios else 0
        selected = st.selectbox("Select a scenario to edit", scenarios, index=default_index, key="scenario_selectbox")

        if selected:
            if selected != previous_selection:
                keys_to_clear = [k for k in st.session_state.keys() if k.startswith("components_")]
                for k in keys_to_clear:
                    del st.session_state[k]

                st.session_state["last_selected_scenario"] = selected

            scenario_file = get_scenario_file(selected)
            st.session_state['current_scenario_file'] = scenario_file
            st.info(f"### ✏️ Currently editing scenario: **{selected}**")

            # Show metadata...
            try:
                wb = openpyxl.load_workbook(scenario_file, data_only=True)
                if "meta" in wb.sheetnames:
                    meta_ws = wb["meta"]
                    meta = {row[0].value: row[1].value for row in meta_ws.iter_rows(min_row=2, max_col=2)}

                    project_name = meta.get("Project Name", "Unknown")
                    climate_zone = meta.get(
                        "Legacy Fallback Climate Zone", meta.get("Climate Zone", "Unknown")
                    )
                    city = meta.get(
                        "Legacy Fallback Representative City", meta.get("Representative City", "Unknown")
                    )
                    dwd_station = meta.get("DWD Weather Station")
                    dwd_station_id = meta.get("DWD Station ID")
                    dwd_years = meta.get("DWD Profile Years Used")
                    solution_type = meta.get("System Type", "Unknown")

                    st.markdown("📁 **Project Metadata for this Scenario:**")
                    st.markdown(f"- **Project Name**: {project_name}")
                    if dwd_station:
                        st.markdown(
                            f"- **DWD weather:** {dwd_station} (station {dwd_station_id}); "
                            f"profile years {dwd_years or 'not recorded'}"
                        )
                    else:
                        st.markdown(
                            f"- **Weather fallback:** {city} template (legacy zone {climate_zone})"
                        )
                    st.markdown(f"- **System Type**: {solution_type}")
            except Exception as e:
                st.warning(f"⚠️ Could not read project metadata: {e}")

            st.markdown("### 🔧 Scenario Inputs")
            section_options = [
                "🟠 Demand",
                "🟡 Network Length",
                "🟢 Sources",
                "🔵 Generation Techs",
                "🟣 Storage",
            ]
            section_key = f"scenario_input_section_{selected}"
            selected_section = st.radio(
                "Scenario input section",
                section_options,
                horizontal=True,
                key=section_key,
            )

            if selected_section == "🟠 Demand":
                st.markdown("#### Demand Inputs")
                run_demand_page(scenario_file)
            elif selected_section == "🟡 Network Length":
                run_network_length_page(scenario_file)
            elif selected_section == "🟢 Sources":
                st.markdown("#### Source Configuration")
                run_sources_page(scenario_file)
            elif selected_section == "🔵 Generation Techs":
                st.markdown("#### Generation Technology Setup")
                run_transformers_page(scenario_file, selected)
            else:
                st.markdown("#### Thermal Storage Setup")
                run_storage_page(scenario_file)

def _render_scenario_peak_load_editor(excel_file):
    meta = _scenario_meta_values(excel_file)
    building_count_raw = pd.to_numeric(meta.get("Building Count"), errors="coerce")
    stored_building_count = (
        int(building_count_raw)
        if pd.notna(building_count_raw) and building_count_raw > 0
        else _connected_building_count_from_session()
    )
    preview = _scenario_profile_preview(excel_file, stored_building_count)
    if preview["annual_sh_kwh"] <= 0:
        st.info("Add space-heating demand to calculate and edit its peak load.")
        return

    stored_peak = pd.to_numeric(meta.get("Space Heating Peak Load (kW)"), errors="coerce")
    peak_was_edited = str(meta.get("Peak Load User Edited", "False")).lower() == "true"
    default_peak = (
        float(stored_peak)
        if pd.notna(stored_peak) and stored_peak > 0
        else preview["estimated_peak_kw"]
    )
    scenario_key = os.path.basename(excel_file)

    st.info(
        "If you know the connected-building count or a measured/design peak load, you can use it to adjust "
        "the scenario peak and space-heating profile below."
    )
    controls = st.expander("Adjust space-heating peak load and load profile", expanded=False)
    controls.caption(
        "The simultaneity factor describes how much of the individual building peak loads occurs at the same "
        "time. Annual heat demand remains unchanged when the profile is adjusted."
    )

    method_key = f"profile_adjustment_method_{scenario_key}"
    if method_key not in st.session_state:
        st.session_state[method_key] = "Enter a known peak load" if peak_was_edited else "Use building count"
    method = controls.radio(
        "How should the load profile be adjusted?",
        ["Use building count", "Enter a known peak load"],
        horizontal=True,
        key=method_key,
    )

    count_key = f"scenario_building_count_{scenario_key}"
    if count_key not in st.session_state:
        st.session_state[count_key] = int(stored_building_count or 1)
    building_count = int(controls.number_input(
        "Connected buildings",
        min_value=1,
        step=1,
        key=count_key,
        disabled=method != "Use building count",
        help="For district-heating scenarios, more connected buildings reduce the coincident peak-load factor.",
    ))
    count_preview = _scenario_profile_preview(excel_file, building_count)

    peak_key = f"scenario_peak_kw_{scenario_key}"
    pending_peak_key = f"_pending_{peak_key}"
    if pending_peak_key in st.session_state:
        st.session_state[peak_key] = st.session_state.pop(pending_peak_key)
    if peak_key not in st.session_state:
        st.session_state[peak_key] = default_peak
    peak_load = controls.number_input(
        "Known space-heating peak load (kW)",
        min_value=0.0,
        step=max(1.0, count_preview["estimated_peak_kw"] * 0.01),
        key=peak_key,
        disabled=method != "Enter a known peak load",
        help=(
            "Use this when a design calculation or measurement already provides the coincident peak load."
        ),
    )

    if method == "Use building count":
        if count_preview["sum_individual_peaks_kw"] is not None:
            controls.markdown(
                f"- **Sum of individual building peaks:** "
                f"{count_preview['sum_individual_peaks_kw']:,.1f} kW"
            )
        controls.markdown(f"- **Simultaneity factor:** {count_preview['simultaneity_factor']:.3f}")
        controls.markdown(f"- **Estimated simultaneous peak load:** {count_preview['estimated_peak_kw']:,.1f} kW")
        if count_preview["system_type"] != "District heating (centralized)":
            controls.caption("Building-count simultaneity is only applied to district-heating scenarios.")
    else:
        implied_factor = (
            float(peak_load) / count_preview["unconstrained_peak_kw"]
            if count_preview["unconstrained_peak_kw"] > 0
            else 1.0
        )
        controls.markdown(f"- **Effective peak factor:** {implied_factor:.3f}")

    if controls.button("Apply adjustment and update profiles", key=f"apply_peak_{scenario_key}"):
        use_known_peak = method == "Enter a known peak load"
        result = _update_scenario_load_profiles(
            excel_file,
            building_count=building_count,
            peak_load_kw=float(peak_load) if use_known_peak else None,
            user_edited=use_known_peak,
        )
        st.session_state[pending_peak_key] = result.get("selected_peak_kw", peak_load)
        st.success(
            f"Load profiles updated. Applied space-heating peak: "
            f"{result.get('selected_peak_kw', peak_load):,.1f} kW."
        )
        st.rerun()


def run_demand_page(excel_file):
    st.markdown("""
        On this page, you can define the demand data for consumers.

        There are three possible demand types:  
        - **HT** – High-temperature heat demand (50–70 °C)  
        - **LT** – Low-temperature heat demand (30–50 °C)  
        - **el** – Electricity demand

        **Note:** Space heating and DHW results from **Building Heat Demand** are set to high-temperature users by default. Please check and adjust the user type if needed.

        To add more consumers, click **Add another component**.
        Click **Save** after entering or modifying data.
        To delete a component, simply clear its name field.  
    """)

    project = get_current_project()
    if project:
        _load_saved_project_totals(project)
    estimated_demand_available = (
        st.session_state.get("total_heat_demand") is not None
        and st.session_state.get("total_dhw_demand") is not None
    )
    if estimated_demand_available:
        st.markdown("#### Import latest building demand estimation")
        st.caption(
            "Use this after estimating building demand to add or update the standard space-heating and DHW "
            "consumers in this scenario. Other custom consumers are retained."
        )
        import_has_results = _project_has_simulation_results(project)
        import_confirm_key = f"confirm_import_demand_results_{os.path.basename(excel_file)}"
        if not import_has_results:
            st.session_state.pop(import_confirm_key, None)
        import_confirmed = True
        if import_has_results:
            st.warning(
                "Existing simulation results will be out of date and deleted when the scenario demand is updated. "
                "You can first duplicate the project on Project Setup if you want to retain and review them."
            )
            import_confirmed = st.checkbox(
                "I understand that importing demand will delete the existing simulation results",
                key=import_confirm_key,
            )
        if st.button(
            "Import latest space-heating and DHW estimation",
            disabled=not import_confirmed,
            key=f"import_project_demand_{os.path.basename(excel_file)}",
        ):
            totals = _import_latest_project_demand(excel_file)
            if import_has_results:
                _delete_project_simulation_results(project)
            st.session_state.pop(f"components_{os.path.basename(excel_file)}", None)
            st.session_state.pop(f"components_{os.path.basename(excel_file)}_file_mtime", None)
            st.success(
                f"Imported {totals['space_heating_kwh']:,.0f} kWh/a space heating and "
                f"{totals['dhw_kwh']:,.0f} kWh/a DHW."
            )
            st.rerun()
    else:
        st.info(
            "After estimating space-heating and DHW demand on Building Heat Demand, you can import the results here."
        )

    # --- Harden layout so all widgets share the same baseline/height and headers show clearly ---
    st.markdown("""
    <style>
      /* Make text inputs and selects the same height */
      input[type="text"] {
        height: 38px !important;
      }
      /* Streamlit selectbox uses baseweb; normalize its control height & paddings */
      div[data-baseweb="select"] > div {
        height: 38px !important;
        min-height: 38px !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
      }
      /* Remove top margins that push some widgets down */
      .stTextInput, .stSelectbox {
        margin-top: 0 !important;
        margin-bottom: 0.25rem !important;
      }
      /* Ensure each column aligns items vertically centered (safe selector) */
      div[data-testid="column"] > div:first-child {
        display: flex;
        align-items: center;
      }
      /* Tighten the header row spacing */
      .field-headers p {
        margin-bottom: 0.25rem !important;
      }
    </style>
    """, unsafe_allow_html=True)

    sheet_name = "demand"
    ensure_excel_file(
        excel_file, sheet_name,
        ["label", "active", "from", "nominal value", "Demand in MWh/a", "Demand in GWh/a"]
    )

    scenario_key = f"components_{os.path.basename(excel_file)}"
    version_key = f"{scenario_key}_file_mtime"

    # ---- Load current demand sheet
    df_existing = pd.read_excel(excel_file, sheet_name=sheet_name)
    if "nominal value" in df_existing.columns:
        df_existing["nominal value"] = df_existing["nominal value"].apply(parse_number).astype(float)

    # ---- Ensure heat-loss exists immediately for centralized solutions
    try:
        meta_df = pd.read_excel(excel_file, sheet_name="meta")
        meta_dict = dict(zip(meta_df["Key"].astype(str), meta_df["Value"]))
        solution_type_meta = (
            meta_dict.get("System Type")
            or meta_dict.get("solution_type")
            or st.session_state.get("solution_type")
            or "Decentralized solution"
        )
    except Exception:
        solution_type_meta = st.session_state.get("solution_type", "Decentralized solution")

    is_central = isinstance(solution_type_meta, str) and ("district" in solution_type_meta.lower())
    if is_central:
        has_loss = "label" in df_existing.columns and df_existing["label"].astype(str).eq(HEAT_LOSS_DEMAND_LABEL).any()
        if not has_loss and "from" in df_existing.columns:
            df_heat = df_existing[df_existing["from"].astype(str).str.match(r"^b_th_", na=False)]
            total_heat = float(df_heat["nominal value"].sum()) if not df_heat.empty else 0.0
            if total_heat > 0:
                add_network_heat_loss(excel_file, loss_fraction=0.05)
                df_existing = pd.read_excel(excel_file, sheet_name=sheet_name)
                if "nominal value" in df_existing.columns:
                    df_existing["nominal value"] = df_existing["nominal value"].apply(parse_number).astype(float)

    # ---- Seed editor state if file changed
    current_mtime = get_file_mtime(excel_file)
    needs_seed = (scenario_key not in st.session_state) or (st.session_state.get(version_key) != current_mtime)

    if needs_seed:
        st.session_state[scenario_key] = []
        for _, row in df_existing.iterrows():
            lbl = str(row.get('label', '') or '')
            if lbl in {HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL}:
                continue  # auto-managed for centralized
            from_val = str(row.get('from', '') or '')
            if from_val == "b_el":
                type_ = "el"
            elif from_val.startswith("b_th_"):
                type_ = from_val.replace("b_th_", "")
            else:
                type_ = "HT"
            st.session_state[scenario_key].append({
                "label": lbl,
                "values": float(row.get("nominal value", 0.0) or 0.0),
                "type": type_
            })
        st.session_state[version_key] = current_mtime

    with st.form("demand_form"):
        new_components = []

        for i, comp in enumerate(st.session_state[scenario_key]):
            try:
                col1, col2, col3 = st.columns([3, 2, 2], vertical_alignment="center")
            except TypeError:
                col1, col2, col3 = st.columns([3, 2, 2])

            with col1:
                label = st.text_input(
                    "Building/District",
                    value=comp["label"],
                    key=f"d_label_{i}_{scenario_key}"
                )
            with col2:
                value = dot_number_input(
                    "Annual demand (kWh/a)",
                    comp["values"],
                    key=f"d_value_{i}_{scenario_key}"
                )
            with col3:
                ctype = st.selectbox(
                    "Type",
                    ["HT", "LT", "el"],
                    index=["HT", "LT", "el"].index(comp["type"]),
                    key=f"d_type_{i}_{scenario_key}"
                )

            from_bus = "b_el" if ctype == "el" else f"b_th_{ctype}"
            new_components.append({
                "label": label,
                "active": 1,
                "from": from_bus,
                "nominal value": value,
                "Demand in MWh/a": value / 1000,
                "Demand in GWh/a": value / 1e6
            })

        manual_has_results = _project_has_simulation_results(project)
        manual_confirm_key = f"confirm_manual_demand_results_{os.path.basename(excel_file)}"
        if not manual_has_results:
            st.session_state.pop(manual_confirm_key, None)
        manual_results_confirmed = True
        if manual_has_results:
            st.warning(
                "Saving changed heat demand will delete the existing simulation results because they will no "
                "longer match the scenario. Duplicate the project on Project Setup first if you want to retain them."
            )
            manual_results_confirmed = st.checkbox(
                "I understand that saving demand changes will delete the existing simulation results",
                key=manual_confirm_key,
            )

        c1, c2 = st.columns([1, 1])
        add_clicked = c1.form_submit_button("Add another component")
        save_clicked = c2.form_submit_button("Save", disabled=not manual_results_confirmed)

        if add_clicked:
            st.session_state[scenario_key].append({"label": "", "values": 0.0, "type": "HT"})
            st.rerun()

        if save_clicked:
            # build df from UI (skip empty & loss row)
            filtered_components = []
            for comp in new_components:
                lbl = comp["label"].strip()
                if not lbl or lbl in {HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL}:
                    continue
                filtered_components.append(comp)

            df_demand = pd.DataFrame(filtered_components)

            # Remove any heat-loss (we'll re-add if centralized)
            if not df_demand.empty:
                df_demand = df_demand[~df_demand["label"].astype(str).isin({HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL})]

            # Save (replace sheet) with comma-decimal conversion
            to_write = (df_demand if not df_demand.empty else
                        pd.DataFrame(columns=["label", "active", "from", "nominal value", "Demand in MWh/a", "Demand in GWh/a"]))
            for c in ["nominal value", "Demand in MWh/a", "Demand in GWh/a"]:
                if c in to_write.columns:
                    to_write[c] = pd.to_numeric(to_write[c], errors="coerce")

            compare_columns = ["label", "active", "from", "nominal value"]
            old_compare = df_existing.copy()
            if not old_compare.empty and "label" in old_compare.columns:
                old_compare = old_compare.loc[
                    ~old_compare["label"].astype(str).isin(
                        {HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL}
                    )
                ]
            for frame in (old_compare, to_write):
                for column in compare_columns:
                    if column not in frame.columns:
                        frame[column] = np.nan
            old_compare = old_compare[compare_columns].sort_values("label").reset_index(drop=True)
            new_compare = to_write[compare_columns].sort_values("label").reset_index(drop=True)
            demand_changed = not old_compare.equals(new_compare)
            with pd.ExcelWriter(excel_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                to_write.to_excel(writer, sheet_name="demand", index=False)

            if is_central:
                add_network_heat_loss(excel_file, loss_fraction=0.05)

            profile_meta = _scenario_meta_values(excel_file)
            count_raw = pd.to_numeric(profile_meta.get("Building Count"), errors="coerce")
            profile_count = int(count_raw) if pd.notna(count_raw) and count_raw > 0 else None
            peak_was_edited = str(profile_meta.get("Peak Load User Edited", "False")).lower() == "true"
            stored_peak = pd.to_numeric(profile_meta.get("Space Heating Peak Load (kW)"), errors="coerce")
            _update_scenario_load_profiles(
                excel_file,
                building_count=profile_count,
                peak_load_kw=float(stored_peak) if peak_was_edited and pd.notna(stored_peak) else None,
                user_edited=peak_was_edited,
            )

            if demand_changed and manual_has_results and project:
                _delete_project_simulation_results(project)

            st.success("Demand data saved and heat loss updated.")

            # Reseed from file
            st.session_state.pop(scenario_key, None)
            st.session_state.pop(version_key, None)
            st.rerun()

    _render_scenario_peak_load_editor(excel_file)

def run_sources_page(excel_file):
    st.markdown("""
        On this page, you can define or edit the properties of various energy sources (e.g., fuels, electricity).
        Several predefined sources are loaded automatically.
        To add more sources, click **Add another source**. Click **Save** to apply changes.
        To remove a source, simply clear its name field.
        
        The following abbreviations are used for the predefined sources:  
        - **el**:   Electricity  
        - **LPM**: Biomass (Wood, local available)  
        - **BM_W**: Biomass (Wood, from market)  
        - **BM_P**: Biomass (Pellets, from market)  
        - **BG**:   Biogas  
        - **NG**:   Natural Gas  
        - **GS_C**:  Ground Source (Shallow Collector)
        - **GS_B**:  Ground Source (Borehole)
        - **WW**:  Waste Water
        - **DHS**:  Existing District Heating Systems
        
        For limited renewable heat pump sources (e.g. ground source, waste water), maximum capacity and annual consumption can be defined on this page.
        
        Since the nominal value (maximum allowed capacity), full load hours, and maximum annual consumption are interrelated, these values will be adjusted automatically based on the inputs:
        **Maximum annual consumption = nominal value × full load hours**
    """)

    # Keep inputs aligned and at the same height
    st.markdown("""
    <style>
      input[type="text"] { height: 38px !important; }
      .stTextInput { margin-top: 0 !important; margin-bottom: 0.25rem !important; }
      div[data-testid="column"] > div:first-child { display: flex; align-items: center; }
    </style>
    """, unsafe_allow_html=True)

    sheet_name = "sources"
    ensure_excel_file(excel_file, sheet_name, [
        "label", "variable costs", "emission factor",
        "nominal value", "full load time", "annual max", "to"
    ])

    df_existing = pd.read_excel(excel_file, sheet_name=sheet_name)

    scenario_key = f"sources_{os.path.basename(excel_file)}"
    version_key = f"{scenario_key}_file_mtime"
    current_mtime = get_file_mtime(excel_file)

    needs_seed = (scenario_key not in st.session_state) or (st.session_state.get(version_key) != current_mtime)

    # -------------------------------
    # 📋 Copy sources from another scenario
    # -------------------------------
    with st.expander("📋 Copy sources from another scenario", expanded=False):
        current_scenario = os.path.splitext(os.path.basename(excel_file))[0]
        all_scenarios = list_scenarios()  # same project
        other_scenarios = [s for s in all_scenarios if s != current_scenario]

        if not other_scenarios:
            st.info("No other scenarios found in this project.")
        else:
            # Put interactive widgets into a form so changing the selectbox does NOT rerun
            with st.form(key=f"copy_sources_form_{current_scenario}", clear_on_submit=False):
                pick = st.selectbox(
                    "Copy sources from scenario:",
                    ["— Select —"] + other_scenarios,
                    index=0,
                    key=f"copy_sources_pick_{current_scenario}",
                )
                do_import = st.form_submit_button("Import sources into current scenario")

            if do_import:
                if pick == "— Select —":
                    st.warning("Please select a scenario to copy from.")
                else:
                    src_file = get_scenario_file(pick)  # same project
                    try:
                        src_df = pd.read_excel(src_file, sheet_name=sheet_name)

                        # Normalize & clean
                        if "label" in src_df.columns:
                            src_df["label"] = src_df["label"].astype(str).str.strip()
                            src_df = src_df[src_df["label"] != ""].copy()

                        # Ensure required numeric columns are numeric
                        for col in ["variable costs", "emission factor", "nominal value", "full load time",
                                    "annual max"]:
                            if col in src_df.columns:
                                src_df[col] = src_df[col].apply(parse_number)

                        # Ensure 'to' exists
                        def _default_to(lbl: str) -> str:
                            lk = (lbl or "").strip().lower()
                            if lk in {"el", "electricity"}:
                                return "b_el"
                            return "b_s_" + (lbl or "").strip().replace(" ", "_")

                        if "to" not in src_df.columns:
                            src_df["to"] = src_df["label"].apply(_default_to)

                        # Convert to list-of-dicts for your existing save_components()
                        imported_sources = []
                        for _, r in src_df.iterrows():
                            label = str(r.get("label", "")).strip()
                            if not label:
                                continue

                            am_raw = r.get("annual max", None)
                            am_val = None if pd.isna(am_raw) or str(am_raw).strip() == "" else float(
                                parse_number(am_raw))

                            imported_sources.append({
                                "label": label,
                                "active": 1,
                                "variable costs": float(parse_number(r.get("variable costs", 0.0))),
                                "emission factor": float(parse_number(r.get("emission factor", 0.0))),
                                "nominal value": float(parse_number(r.get("nominal value", 1e10))),
                                "full load time": float(parse_number(r.get("full load time", 8760.0))),
                                "annual max": am_val,
                                "to": str(r.get("to", "b_s_" + label.replace(" ", "_"))),
                            })

                        # Save sources into CURRENT scenario
                        df_sources = pd.DataFrame(imported_sources)

                        num_cols = ["variable costs", "emission factor", "nominal value", "full load time",
                                    "annual max"]
                        for c in num_cols:
                            if c in df_sources.columns:
                                df_sources[c] = pd.to_numeric(df_sources[c], errors="coerce")

                        if "active" in df_sources.columns:
                            df_sources["active"] = pd.to_numeric(df_sources["active"], errors="coerce").fillna(
                                0).astype(int)

                        with pd.ExcelWriter(excel_file, engine="openpyxl", mode="a",
                                            if_sheet_exists="replace") as writer:
                            df_sources.to_excel(writer, sheet_name=sheet_name, index=False)

                        # Ensure buses exist (same logic you already use on Save)
                        new_buses = pd.DataFrame([{
                            "label": src["to"],
                            "active": 1,
                            "excess": 1,
                            "shortage": 1,
                            "shortage costs": 1e20,
                            "excess costs": 1e20
                        } for src in imported_sources])

                        try:
                            existing_buses = pd.read_excel(excel_file, sheet_name="buses")
                        except Exception:
                            existing_buses = pd.DataFrame(
                                columns=["label", "active", "excess", "shortage", "shortage costs", "excess costs"]
                            )

                        if "label" in existing_buses.columns and not existing_buses.empty:
                            new_buses = new_buses[~new_buses["label"].isin(existing_buses["label"])]

                        updated_buses = pd.concat([existing_buses, new_buses], ignore_index=True)

                        with pd.ExcelWriter(excel_file, engine="openpyxl", mode="a",
                                            if_sheet_exists="replace") as writer:
                            updated_buses.to_excel(writer, sheet_name="buses", index=False)

                        # Force UI to reload sources from file (important!)
                        st.success(f"Imported sources from scenario '{pick}' into '{current_scenario}'.")
                        st.session_state.pop(scenario_key, None)
                        st.session_state.pop(version_key, None)
                        st.rerun()

                    except Exception as e:
                        st.error(f"Failed to import sources from '{pick}': {e}")

    # Normalize numeric cols
    for col in ['variable costs', 'emission factor', 'nominal value', 'full load time', 'annual max']:
        if col in df_existing.columns:
            df_existing[col] = df_existing[col].apply(parse_number).astype(float)

    if needs_seed:
        st.session_state[scenario_key] = []
        for _, row in df_existing.iterrows():
            am_cell = row.get("annual max", None)
            am_val = None if pd.isna(am_cell) else float(am_cell)
            st.session_state[scenario_key].append({
                "label": row.get("label", ""),
                "variable costs": float(row.get("variable costs", 0.0)),
                "emission factor": float(row.get("emission factor", 0.0)),
                "nominal value": float(row.get("nominal value", 1e10)),
                "full load time": float(row.get("full load time", 8760.0)),
                "annual max": am_val,
                "to": row.get("to", ""),
            })
        st.session_state[version_key] = current_mtime

    with st.form("source_form"):
        new_sources = []

        for i, src in enumerate(st.session_state[scenario_key]):
            try:
                col1, col2, col3, col4, col5, col6 = st.columns([3, 2, 2, 2, 2, 2], vertical_alignment="center")
            except TypeError:  # Streamlit < 1.32
                col1, col2, col3, col4, col5, col6 = st.columns([3, 2, 2, 2, 2, 2])

            with col1:
                label = st.text_input("Source name", value=src["label"], key=f"src_label_{i}_{scenario_key}")
            with col2:
                vc = dot_number_input("Variable costs (€/kWh)", src["variable costs"], key=f"src_costs_{i}_{scenario_key}")
            with col3:
                ef = dot_number_input("Emission factor (kg/kWh)", src["emission factor"], key=f"src_emission_{i}_{scenario_key}")
            with col4:
                nv = dot_number_input("Nominal value (kW)", src["nominal value"], key=f"src_nominal_{i}_{scenario_key}")
            with col5:
                am_str = st.text_input(
                    "Annual max (kWh) (optional)",
                    value="" if src.get("annual max") in [None, "", np.nan] else str(src.get("annual max")),
                    key=f"src_am_{i}_{scenario_key}"
                )
                am = parse_number(am_str) if am_str.strip() else None
            flt_val = src.get("full load time", None)
            if (flt_val is None or np.isnan(flt_val)) and nv and am:
                flt_val = am / nv
            if flt_val is None or np.isnan(flt_val):
                flt_val = 8760.0
            with col6:
                flt = dot_number_input("Full load (h)", flt_val, key=f"src_full_{i}_{scenario_key}")

            nv, flt, am = reconcile_nv_flt_am(nv, flt, am)

            label_clean = (label or "").strip()
            label_key = label_clean.lower()

            # keep existing 'to' if label unchanged and it exists
            prev_label = (src.get("label", "") or "").strip()
            prev_to = (src.get("to", "") or "").strip()

            if label_key in {"el", "electricity"}:
                to_bus = "b_el"
            elif prev_to and prev_label == label_clean:
                to_bus = prev_to
            else:
                to_bus = "b_s_" + label_clean.replace(" ", "_")

            new_sources.append({
                "label": label,
                "active": 1,
                "variable costs": vc,
                "emission factor": ef,
                "nominal value": nv,
                "full load time": flt,
                "annual max": am,
                "to": to_bus
            })

        c1, c2 = st.columns([1, 1])
        add_src = c1.form_submit_button("Add another source")
        save_src = c2.form_submit_button("Save")

        if add_src:
            st.session_state[scenario_key].append({
                "label": "",
                "variable costs": 0.0,
                "emission factor": 0.0,
                "nominal value": 1e10,
                "full load time": 8760.0,
                "annual max": None,
                "to": "",
            })
            st.rerun()

        if save_src:
            cleaned_sources = [src for src in new_sources if src["label"].strip() != ""]

            df_sources = pd.DataFrame(cleaned_sources)

            # Force numeric types (REAL numbers in Excel, not strings)
            num_cols = ["variable costs", "emission factor", "nominal value", "full load time", "annual max"]
            for c in num_cols:
                if c in df_sources.columns:
                    df_sources[c] = pd.to_numeric(df_sources[c], errors="coerce")

            # Optional: keep active as int
            if "active" in df_sources.columns:
                df_sources["active"] = pd.to_numeric(df_sources["active"], errors="coerce").fillna(0).astype(int)

            # Overwrite sources sheet with numeric values
            with pd.ExcelWriter(excel_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                df_sources.to_excel(writer, sheet_name=sheet_name, index=False)

            # Ensure buses exist (your existing logic, unchanged)
            new_buses = pd.DataFrame([{
                "label": src["to"],
                "active": 1,
                "excess": 1,
                "shortage": 1,
                "shortage costs": 1e20,
                "excess costs": 1e20
            } for src in cleaned_sources])

            try:
                existing_buses = pd.read_excel(excel_file, sheet_name="buses")
            except Exception:
                existing_buses = pd.DataFrame(
                    columns=["label", "active", "excess", "shortage", "shortage costs", "excess costs"]
                )

            new_buses = new_buses[~new_buses["label"].isin(existing_buses["label"])]
            updated_buses = pd.concat([existing_buses, new_buses], ignore_index=True)

            with pd.ExcelWriter(excel_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                updated_buses.to_excel(writer, sheet_name="buses", index=False)

            st.success("Sources updated.")
            st.session_state.pop(scenario_key, None)
            st.session_state.pop(version_key, None)
            st.rerun()

def run_transformers_page(excel_file, scenario_name):
    # st.title("Generation Technologies")
    st.markdown("""
    On this page, you can assign generation technologies to energy sources defined on the **Sources** page.

    Click **Save** after making your selections. To remove a technology, simply clear its name field.

    The following abbreviations are used for the predefined sources:  
    - **el**:   Electricity  
    - **LPM**: Biomass (Wood, local available)  
    - **BM_W**: Biomass (Wood, from market)  
    - **BM_P**: Biomass (Pellets, from market)  
    - **BG**:   Biogas  
    - **NG**:   Natural Gas  
    - **DHS**:  Existing District Heating Systems
    
    The heat pumps can be chosen under **electricity-consuming technologies**.
    The following heat pump sources are created internally for their corresponding technologies and are therefore hidden on this page.
    - **GS_C**:  Ground Source (Shallow Collector)
    - **GS_B**:  Ground Source (Borehole)
    - **WW**:  Waste Water
    """)

    source_df_all = pd.read_excel(excel_file, sheet_name="sources")
    source_df_all["label"] = source_df_all["label"].astype(str).str.strip()

    # Ensure electricity exists as a selectable category in the UI
    if "el" not in set(source_df_all["label"]):
        # Add a UI-only row. (Do NOT have to write it into Excel if you don’t want.)
        source_df_all = pd.concat([
            source_df_all,
            pd.DataFrame([{"label": "el", "to": "b_el"}])
        ], ignore_index=True)

    # Hide internal HP sources from UI
    source_df = source_df_all[
        ~source_df_all["label"].isin(INTERNAL_HP_SOURCES)
    ].copy()

    source_bus_map = {
        row["label"]: row["to"]
        for _, row in source_df_all.iterrows()  # keep full map for correctness
    }
    source_bus_map["el"] = "b_el"

    scenario_id = os.path.basename(excel_file)
    session_key = f"tech_selection_{scenario_id}"
    version_key = f"{session_key}_file_mtime"
    current_mtime = get_file_mtime(excel_file)

    if st.session_state.get(version_key) != current_mtime:
        # drop stored selections for this scenario so we reload from Excel
        st.session_state.pop(session_key, None)

        # also clear multiselect widget states so defaults can apply again
        keys_to_clear = [k for k in st.session_state if k.startswith(f"tech_{scenario_name}_")]
        for k in keys_to_clear:
            st.session_state.pop(k, None)

        st.session_state[version_key] = current_mtime

    if session_key not in st.session_state:
        st.session_state[session_key] = {}

    existing_techs = read_existing_technologies(excel_file, tech_sheet_map)

    # Always initialize the selection for every source from Excel active techs (or empty list)
    for source in source_df["label"]:
        if source not in st.session_state[session_key]:
            source_bus = source_bus_map.get(source, "")
            # get active techs or empty list if none found
            active_techs = existing_techs.get(source_bus, [])
            st.session_state[session_key][source] = active_techs

    # --- Form ---
    with st.form("tech_form"):
        for source in source_df["label"]:
            st.subheader(f"Source: {source}")
            configured_options = tech_options.get(source, [])
            options = available_technology_options(excel_file, source)
            display_options = [display_names[opt] for opt in options]

            unavailable = [tech for tech in configured_options if tech not in options]
            if unavailable:
                unavailable_names = ", ".join(display_names.get(tech, tech) for tech in unavailable)
                st.caption(
                    "Not available for this scenario's system type: " + unavailable_names
                )

            prev_selection = st.session_state[session_key].get(source, [])
            prev_display_selection = [
                display_names.get(tech, tech) for tech in prev_selection if
                display_names.get(tech, tech) in display_options
            ]

            widget_key = f"tech_{scenario_id}_{source}"
            selected_display = st.multiselect(
                f"Select technologies for {source}",
                options=display_options,
                default=prev_display_selection,
                key=widget_key
            )

            selected = [rev_display_names[disp] for disp in selected_display]
            st.session_state[session_key][source] = selected

        submitted = st.form_submit_button("Save")

    # --- Save Logic ---
    if submitted:
        updated_dfs = {}
        sheets_to_clean = set(sheet for sheet, _ in tech_sheet_map.values())

        # Read and clean all relevant sheets
        for sheet_name in sheets_to_clean:
            try:
                existing_df = pd.read_excel(excel_file, sheet_name=sheet_name)
                cleaned_df = existing_df[
                    ~(
                            (existing_df["active"] == 1) &
                            (~existing_df["label"].astype(str).str.contains("z_"))
                    )
                ]
                if "label" in cleaned_df.columns and "active" in cleaned_df.columns:
                    lab = cleaned_df["label"].astype(str).str.strip()
                    act = pd.to_numeric(cleaned_df["active"], errors="coerce").fillna(0).astype(int)

                    # inactive example rows should KEEP "_X" (append if missing)
                    mask_inactive = (act == 0) & (~lab.str.endswith("_X"))
                    cleaned_df.loc[mask_inactive, "label"] = lab[mask_inactive] + "_X"
                updated_dfs[sheet_name] = [cleaned_df]

            except Exception as e:
                st.error(f"Error reading sheet '{sheet_name}': {e}")

        for source, selected_techs in st.session_state[session_key].items():
            source_bus = source_bus_map.get(source)

            for tech in selected_techs:
                sheet_name, tag = tech_sheet_map[tech]

                try:
                    existing_df = pd.read_excel(excel_file, sheet_name=sheet_name)
                    template_rows = existing_df[
                        (existing_df["label"].astype(str).str.startswith(tag)) &
                        (existing_df["active"] == 0)
                        ].copy()

                    if template_rows.empty:
                        continue

                    new_rows = []

                    suffix_map = {"LPM": "LPM", "BM_W": "W", "BM_P": "P"}
                    suffix = suffix_map.get(source, None)

                    for _, row in template_rows.iterrows():
                        new_row = row.copy()
                        new_row["active"] = 1

                        orig_label = str(row["label"]).strip()

                        # ONLY drop "_X" for active (=copied) rows
                        if orig_label.endswith("_X"):
                            orig_label = orig_label[:-2]

                        # Add biomass suffix only for copied rows
                        if suffix:
                            new_row["label"] = f"{orig_label}_{suffix}"
                        else:
                            new_row["label"] = orig_label

                        if "from" in new_row:
                            new_row["from"] = source_bus
                        new_rows.append(new_row)

                    updated_dfs[sheet_name].append(pd.DataFrame(new_rows))

                except Exception as e:
                    st.error(f"Error adding tech {tech}: {e}")

        if updated_dfs:
            with pd.ExcelWriter(excel_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                for sheet_name, df_list in updated_dfs.items():
                    combined_df = pd.concat(df_list, ignore_index=True)

                    if "nonconvex" in combined_df.columns:
                        combined_df["nonconvex"] = combined_df["nonconvex"].apply(
                            lambda x: "True" if bool(x) else "False")

                    numeric_cols = {"active", "minimum", "maximum", "ep_costs", "capex", "opex",
                                    "efficiency", "emissions", "eta", "nonconvex_minimum"}  # adjust to your template
                    existing_numeric = [c for c in combined_df.columns if c in numeric_cols]
                    for c in existing_numeric:
                        combined_df[c] = pd.to_numeric(combined_df[c], errors="coerce")

                    combined_df.to_excel(writer, sheet_name=sheet_name, index=False)

        st.success("Technologies updated.")

    # --- Summary Display ---
    st.write("### Saved Technologies")
    for source in source_df["label"]:
        techs = st.session_state[session_key].get(source, [])
        if techs:
            tech_names = [display_names.get(t, t) for t in techs]
            st.markdown(f"**{source}** → {', '.join(tech_names)}")

def run_storage_page(excel_file):
    st.markdown("Configure a thermal storage tank (optional).")

    # Load storage sheet and extract any active configuration
    try:
        df = pd.read_excel(excel_file, sheet_name="storages")
        active_row = df[(df["label"] == "Storage_th") & (df["active"] == 1)]
        storage_exists = not active_row.empty
        default_kwh = parse_number(active_row.iloc[0]["max capacity"]) if storage_exists else 0.0
    except Exception:
        storage_exists = False
        default_kwh = 0.0

    storage_key = f"use_storage_{os.path.basename(excel_file)}"
    if storage_key not in st.session_state:
        st.session_state[storage_key] = storage_exists

    use_storage = st.checkbox("Enable thermal storage", key=storage_key)

    max_kwh = 0

    if use_storage:
        unit = st.radio("Choose input unit", ["kWh", "m³"], key=f"storage_unit_{os.path.basename(excel_file)}")
        if unit == "kWh":
            max_kwh = max(
                0.0,
                dot_number_input(
                    "Enter max capacity (kWh)",
                    default_kwh,
                    key=f"storage_cap_kwh_{os.path.basename(excel_file)}"
                )
            )
        else:
            default_m3 = default_kwh / 1.163
            max_m3 = max(
                0.0,
                dot_number_input(
                    "Enter max volume (m³)",
                    default_m3,
                    key=f"storage_cap_m3_{os.path.basename(excel_file)}"
                )
            )
            max_kwh = max_m3 * 1.163

    if st.button("Save Storage Configuration"):
        try:
            df = pd.read_excel(excel_file, sheet_name="storages")

            # Clean existing active non-template row
            df = df[~((df["label"] == "Storage_th") & (df["active"] == 1))]

            if use_storage and max_kwh > 0:
                template_row = df[(df["label"] == "Storage_th") & (df["active"] == 0)]

                if not template_row.empty:
                    new_row = template_row.iloc[0].copy()
                    new_row["active"] = 1
                    new_row["max capacity"] = max_kwh
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

            df = df.copy()
            if "max capacity" in df.columns:
                df["max capacity"] = df["max capacity"].apply(value_to_comma_decimal)

            with pd.ExcelWriter(excel_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                df.to_excel(writer, sheet_name="storages", index=False)

            st.success("Storage configuration saved.")
        except Exception as e:
            st.error(f"Error saving storage: {e}")

def _render_project_run_controls(active_project: str):
    """Render live run state and guarded project submission."""
    results_dir = Path(get_project_results_dir(active_project))
    has_results = results_dir.exists() and any(results_dir.rglob("results.xlsx"))
    run_status = read_run_status(active_project)
    run_state = run_status.get("state")
    is_running = run_state in {"queued", "running"}
    results_current = has_results and results_match_current_inputs(active_project, run_status)

    if run_state == "queued":
        st.info("Simulation is queued. This page will update automatically when it starts.")
    elif run_state == "running":
        scenario = run_status.get("scenario")
        index = run_status.get("scenario_index")
        count = run_status.get("scenario_count")
        progress = f" ({index} of {count})" if index and count else ""
        scenario_line = f"Current simulated scenario{progress}: **{scenario}**" if scenario else "Preparing the first scenario."
        st.info(
            f"{scenario_line}\n\n"
            "Simulation is running. Please keep this webpage open; it will update automatically.\n\n"
            "Depending on its complexity, one scenario can take seconds or several minutes to simulate."
        )
    elif run_state == "failed":
        st.error(f"The last simulation failed: {run_status.get('error', 'Unknown error')}")
    elif run_state == "completed":
        st.success("Simulation completed.")
        if st.session_state.get("navigate_to_results_when_complete") == active_project:
            st.session_state.pop("navigate_to_results_when_complete", None)
            st.session_state["results_project"] = active_project
            st.session_state["results_project_selector"] = active_project
            st.session_state["requested_page"] = "Simulation Results"
            st.rerun()

    col_a, col_b = st.columns([1, 1])
    with col_a:
        if has_results and results_current and not is_running:
            with st.popover("Submit this project", use_container_width=True):
                st.info("No input changes have been made since the current results were produced.")
                if st.button("Yes, submit again", type="primary", key=f"pm_confirm_unchanged_submit_{active_project}"):
                    st.session_state["navigate_to_results_when_complete"] = active_project
                    submit_project(active_project)
                    st.rerun()
        elif st.button(
            "Submit this project",
            use_container_width=True,
            disabled=is_running,
            help="A simulation is already running." if is_running else None,
            key=f"pm_submit_{active_project}",
        ):
            st.session_state["navigate_to_results_when_complete"] = active_project
            submit_project(active_project)
            st.rerun()
    with col_b:
        with st.popover("Delete this project", use_container_width=True):
            st.warning("This will permanently delete the project and all its scenarios.")
            if st.button("Yes, delete", type="primary", key=f"pm_delete_project_{active_project}"):
                delete_project(active_project)
                st.rerun()

    # Refresh only while work is active; once completed, the page becomes
    # static again and the result charts are not needlessly redrawn.
    if is_running:
        import time
        time.sleep(3)
        st.rerun()


def run_project_management_page():
    st.title("Project Review & Simulation")

    projects = list_projects()
    curr = get_current_project()

    # 1) Current project banner
    if curr:
        st.success(f"Current project: **{curr}**")
        active_project = curr
    else:
        st.warning("No project is currently open.")
        active_project = None

    # Guard: if no projects at all, stop early and guide user
    if not projects:
        st.info("No projects yet. Please create one on the **Project Setup** page.")
        return

    # 2) Project general info (for the active/current project)
    if active_project:
        st.markdown("---")
        st.subheader("ℹ️ Project General Info")

        try:
            with open(project_meta_path(active_project), "r", encoding="utf-8") as f:
                meta = json.load(f)
            city  = meta.get("project_city_name", "") or "_Not set_"
            dwd_station = meta.get("dwd_weather_station") or "_Not loaded_"
            dwd_station_id = meta.get("dwd_weather_station_id") or "unknown ID"
            dwd_years = meta.get("dwd_profile_years_used") or []
            coords = meta.get("project_coords") or None
            if isinstance(coords, (list, tuple)) and len(coords) == 2:
                coords_str = f"({float(coords[0]):.4f}, {float(coords[1]):.4f})"
            else:
                coords_str = "_Not set_"

            st.markdown(f"- **Project name:** {active_project}")
            st.markdown(f"- **Location:** {city}")
            st.markdown(
                f"- **DWD weather:** {dwd_station} (station {dwd_station_id}); "
                f"profile years {', '.join(map(str, dwd_years)) or '_Not loaded_'}"
            )
            st.markdown(f"- **Coordinates:** {coords_str}")
        except FileNotFoundError:
            st.info("No saved metadata yet for this project.")
        except Exception as e:
            st.warning(f"Could not read project meta: {e}")

        # 3) Scenarios of the current project
        st.markdown("---")
        st.subheader("📄 Scenarios in this project")

        scenarios = list_scenarios(project_name=active_project)
        if not scenarios:
            st.info("No scenarios found for this project.")
            st.caption("Create scenarios on **Heat Supply Scenarios**.")
        else:
            scenario_demands = {
                scenario: _scenario_demand_summary(get_scenario_file(scenario, project_name=active_project))
                for scenario in scenarios
            }
            demand_signatures = {
                (
                    round(values["space_heating_kwh"], 6),
                    round(values["dhw_kwh"], 6),
                )
                for values in scenario_demands.values()
            }
            common_demand = len(demand_signatures) == 1
            if common_demand:
                _render_demand_summary_row(next(iter(scenario_demands.values())), "Demand used by all scenarios")

            for sc in scenarios:
                with st.expander(f"Scenario: {sc}", expanded=True):
                    render_scenario_summary(active_project, sc, show_demand=not common_demand)

            # Bulk delete scenarios
            st.markdown("#### Delete scenarios")
            to_delete = st.multiselect(
                "Select scenarios to delete",
                scenarios,
                key=f"pm_delete_scenarios_{active_project}"
            )

            # If nothing selected, show a disabled button to keep layout stable
            if not to_delete:
                st.button("🗑️ Delete selected scenarios", disabled=True)
            else:
                # Same UX as project deletion: popover with warning + confirm button
                with st.popover("🗑️ Delete selected scenarios", use_container_width=True):
                    st.warning(
                        "This will permanently delete the selected scenarios for this project."
                    )
                    st.write("**Scenarios to be deleted:**")
                    st.write(", ".join(to_delete))

                    if st.button("Yes, delete", type="primary", key=f"pm_confirm_delete_{active_project}"):
                        for name in to_delete:
                            delete_scenario(name, project_name=active_project)
                            # clear any cached UI keyed by file name
                            scenario_prefix = f"{name}.xlsx"
                            keys = [k for k in st.session_state.keys() if scenario_prefix in k]
                            for k in keys:
                                del st.session_state[k]
                        st.success("Selected scenarios deleted.")
                        st.rerun()

        # 4) Submit / Delete current project
        st.markdown("---")
        st.subheader("Submit or Delete Project")
        st.info(
            "When the project information and all scenarios are correct, submit the project to run "
            "simulation and optimization. You can also delete this project below, or "
            "open a different project."
        )
        _render_project_run_controls(active_project)

    # 5) Choose another project (at the very end)
    st.markdown("---")
    st.markdown("🔁 Manage another project")

    # order: current first (if exists), then the rest alphabetically
    ordered = ([curr] if curr in projects else []) + sorted([p for p in projects if p != curr])

    col_pick, col_open = st.columns([3, 1])

    with col_pick:
        st.markdown("Select a project to open")  # label shown above
        pick = st.selectbox(
            " ",  # hidden label so it doesn’t push widget down
            ordered,
            index=0 if curr in projects else 0,
            key="pm_selected_project_bottom",
            label_visibility="collapsed"
        )

    with col_open:
        st.markdown("&nbsp;")  # spacer so button aligns with selectbox
        if st.button("Open", use_container_width=True, key="pm_open_bottom"):
            set_current_project(pick)
            load_project_meta_into_session(pick)
            st.success(f"Opened project: {pick}")
            st.rerun()


def run_results_page():
    st.title("Simulation Results")
    projects = list_projects()
    if not projects:
        st.info("No projects are available yet.")
        return

    preferred = st.session_state.get("results_project") or get_current_project()
    index = projects.index(preferred) if preferred in projects else 0
    project = st.selectbox("Project", projects, index=index, key="results_project_selector")
    st.session_state["results_project"] = project

    results_dir = Path(get_project_results_dir(project))
    has_results = results_dir.exists() and any(results_dir.rglob("results.xlsx"))
    run_status = read_run_status(project)
    run_state = run_status.get("state")

    if run_state in {"queued", "running"}:
        scenario = run_status.get("scenario")
        index_value = run_status.get("scenario_index")
        count = run_status.get("scenario_count")
        progress = f" ({index_value} of {count})" if index_value and count else ""
        scenario_line = f"Current simulated scenario{progress}: **{scenario}**" if scenario else "Preparing the first scenario."
        st.info(
            f"{scenario_line}\n\n"
            f"Simulation is {run_state}. Please keep this webpage open; it will update automatically.\n\n"
            "Depending on its complexity, one scenario can take seconds or several minutes to simulate."
        )
        import time
        time.sleep(3)
        st.rerun()
    if run_state == "failed":
        st.error(f"The last simulation failed: {run_status.get('error', 'Unknown error')}")
    if not has_results:
        st.info(
            "No completed simulation results are available for this project. Submit the project on "
            "the **Project Review & Simulation** page to run simulation and optimization and create results."
        )
        return

    results_current = results_match_current_inputs(project, run_status)
    if not results_current:
        st.warning(
            "These are old results and do not reflect the current demand-estimation or scenario inputs."
        )
    else:
        st.success("These results match the current project inputs.")

    from logic.show_results import MultiScenarioViewer
    viewer = MultiScenarioViewer(project)
    tab1, tab2, tab3, tab4 = st.tabs([
        "Summary (CO₂ & Cost)",
        "Investment Capacity",
        "Generation Comparison",
        "Operational Control Overview",
    ])
    with tab1:
        viewer.show_summary_comparison()
    with tab2:
        viewer.show_investment_comparison()
    with tab3:
        viewer.show_generation_comparison()
    with tab4:
        viewer.show_heat_dispatch()

if __name__ == "__main__":
    run_app()
