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
import gc
import time
import re
import html
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
from logic.utilities import natural_sort_key
from logic.building_demand_analyse import _to_bool_series
from logic.dwd_weather import (
    get_location_weather,
    heat_pump_cop_profiles,
    solar_station_info,
)

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
PV_PROFILE_COLUMN = "PV.fix"
PV_FALLBACK_IRRADIATION_KWH_PER_M2 = 1100.0
PV_ELECTRICAL_EFFICIENCY = 0.20
PV_FALLBACK_CITY = "Berlin"
PV_ROOF_AREA_SHARE = 0.40
PV_SOURCE_DISPLAY = "Solar"
PV_RENEWABLE_LABELS = ("PV_50", "PV_100", "PV_750", "PV_1000")
PV_TRANSFORMER_LABELS = (
    "PV_plant_50", "PV_plant_100", "PV_plant_750", "PV_plant_1000"
)
PV_FEED_IN_LABEL = "z_PV_Einspeisung"
PV_LEGACY_FEED_IN_LABELS = (
    "z_PV_Einspeisung_50",
    "z_PV_Einspeisung_100",
    "z_PV_Einspeisung_750",
    "z_PV_Einspeisung_1000",
)
PV_FEED_IN_LABELS = (PV_FEED_IN_LABEL, *PV_LEGACY_FEED_IN_LABELS)
LOCAL_DEMAND_BASE_SHEET = "local_demand_base"
LOCAL_CONSUMERS_SHEET = "local_consumers"
DECENTRAL_DEMAND_BASE_SHEET = "decentral_demand_base"
DECENTRAL_CONSUMERS_SHEET = "decentral_consumers"
DECENTRAL_COMPONENT_RE = re.compile(r"_DC\d+$", re.IGNORECASE)
DECENTRAL_PROFILE_RE = re.compile(r"_DC\d+\.fix$", re.IGNORECASE)
LOCAL_CONSUMER_SUFFIX_RE = re.compile(r"_LC\d{2}$", re.IGNORECASE)
LOCAL_BUS_RE = re.compile(r"^b_th_HT_LC\d{2}$", re.IGNORECASE)
LOCAL_PROFILE_RE = re.compile(r"_LC\d{2}\.fix$", re.IGNORECASE)
LOCAL_BOOSTER_OPTIONS = ("P2H", "ASHP")
LOCAL_BOOSTER_TECHNOLOGIES_BY_SOURCE = {"el": LOCAL_BOOSTER_OPTIONS}


def _system_type_display_name(value: str) -> str:
    """Return user-facing terminology without changing stored identifiers."""
    return (
        "Decentral solution"
        if str(value) == "Decentralized solution"
        else str(value)
    )


def _is_dhw_demand_label(label) -> bool:
    text = str(label or "").strip().lower()
    return (
        "dhw" in text
        or "domestic hot water" in text
        or "hot water" in text
    )


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
    PV_SOURCE_DISPLAY: ["PV plant"],
    "LPM": ["Biomass Boiler"],
    "BM_W": ["Biomass Boiler"],
    "BM_P": ["Biomass Boiler"],
    "NG": ["Gas Boiler", "CHP_NG"],
    "BG": ["Biogas Boiler", "CHP_BG"],
    "el": ["P2H", "ASHP", "GSHP_C", "GSHP_B", "WWSHP"],
}

display_names = {
    "PV plant": "PV panels",
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
}

INTERNAL_HP_SOURCES = {"GS_C", "GS_B", "WW"}
HIDDEN_LEGACY_SOURCES = {"DHS"}

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
        "network_length_map_center",
        "network_length_search_query",

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


def _project_weather_path(project_name: str) -> Path:
    return Path(get_project_dir(project_name)) / "dwd_weather.json"


def _weather_json_value(value):
    if isinstance(value, (pd.Series, pd.Index, np.ndarray)):
        return [_weather_json_value(item) for item in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_weather_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _weather_json_value(item) for key, item in value.items()}
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except Exception:
        pass
    return value


def _save_project_weather(project_name: str, coords, weather: dict) -> None:
    if not project_name or not coords or not isinstance(weather, dict):
        return
    payload = {
        "project_coords": [float(coords[0]), float(coords[1])],
        "weather": _weather_json_value(weather),
    }
    path = _project_weather_path(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)


def _load_project_weather(project_name: str, coords):
    if not project_name or not coords:
        return None
    path = _project_weather_path(project_name)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        saved_coords = payload.get("project_coords")
        if not isinstance(saved_coords, list) or len(saved_coords) != 2:
            return None
        if any(
            not np.isclose(float(saved), float(current), rtol=0, atol=1e-7)
            for saved, current in zip(saved_coords, coords)
        ):
            return None
        weather = payload.get("weather")
        if (
            isinstance(weather, dict)
            and weather.get("temperature_c")
            and weather.get("pv_profile_unit") == "kWh/m2 global irradiation"
        ):
            distance = pd.to_numeric(
                weather.get("solar_station_distance_km"), errors="coerce"
            )
            if (
                weather.get("solar_station_id")
                and (
                    pd.isna(distance)
                    or (
                        float(distance) == 0.0
                        and weather.get("solar_station_latitude") is None
                    )
                )
            ):
                try:
                    details = solar_station_info(
                        weather.get("solar_station_id"),
                        float(coords[0]),
                        float(coords[1]),
                    )
                    if details:
                        weather["solar_station_latitude"] = float(details["latitude"])
                        weather["solar_station_longitude"] = float(details["longitude"])
                        weather["solar_station_distance_km"] = float(
                            details["distance_km"]
                        )
                        _save_project_weather(project_name, coords, weather)
                except Exception:
                    pass
            return weather
    except Exception:
        return None
    return None


def _weather_from_existing_scenario(project_name: str, coords, project_meta: dict):
    """Migrate already-imported hourly weather from an older scenario workbook."""
    scenario_dir = Path(get_scenario_dir_for_project(project_name))
    for scenario_file in sorted(scenario_dir.glob("*.xlsx")):
        try:
            time_series = pd.read_excel(scenario_file, sheet_name="time_series")
            if "DWD_Temperature_C.fix" not in time_series.columns:
                continue
            temperature = pd.to_numeric(
                time_series["DWD_Temperature_C.fix"], errors="coerce"
            ).dropna()
            if temperature.empty:
                continue
            try:
                scenario_meta_df = pd.read_excel(scenario_file, sheet_name="meta")
                scenario_meta = dict(
                    zip(
                        scenario_meta_df.get("Key", pd.Series(dtype=str)).astype(str),
                        scenario_meta_df.get("Value", pd.Series(dtype=object)),
                    )
                )
            except Exception:
                scenario_meta = {}
            pv_profile = None
            if "PV.fix" in time_series.columns:
                pv_values = pd.to_numeric(time_series["PV.fix"], errors="coerce")
                if pv_values.notna().any():
                    pv_profile = pv_values.fillna(0.0).tolist()
            used_years = project_meta.get("dwd_profile_years_used") or []
            solar_years_raw = scenario_meta.get("DWD Solar Profile Years Used") or ""
            solar_years = [
                int(value.strip())
                for value in str(solar_years_raw).split(",")
                if value.strip().isdigit()
            ]
            solar_station_id = (
                project_meta.get("dwd_solar_station_id")
                or scenario_meta.get("DWD Solar Station ID")
            )
            solar_details = None
            try:
                solar_details = solar_station_info(
                    solar_station_id, float(coords[0]), float(coords[1])
                )
            except Exception:
                solar_details = None
            saved_solar_distance = project_meta.get(
                "dwd_solar_station_distance_km"
            )
            if saved_solar_distance is None:
                saved_solar_distance = scenario_meta.get(
                    "DWD Solar Station Distance (km)"
                )
            if saved_solar_distance is None and solar_details:
                saved_solar_distance = solar_details.get("distance_km")
            return {
                "temperature_c": temperature.tolist(),
                "year": project_meta.get("dwd_weather_year"),
                "available_complete_years": project_meta.get(
                    "dwd_available_complete_years"
                ) or [],
                "available_complete_year_count": project_meta.get(
                    "dwd_available_complete_year_count"
                ) or len(used_years),
                "profile_years_used": used_years,
                "profile_year_count": len(used_years),
                "profile_method": project_meta.get("dwd_profile_method"),
                "design_outdoor_temperature_c": project_meta.get(
                    "design_outdoor_temperature_c"
                ),
                "ground_temperature_c": project_meta.get("ground_temperature_c"),
                "ground_temperature_depth_cm": project_meta.get(
                    "ground_temperature_depth_cm"
                ) or scenario_meta.get("Ground Temperature Depth (cm)"),
                "station_id": project_meta.get("dwd_weather_station_id"),
                "station_name": project_meta.get("dwd_weather_station"),
                "station_distance_km": project_meta.get("dwd_station_distance_km") or 0,
                "pv_profile": pv_profile,
                "pv_profile_unit": (
                    "kWh/m2 global irradiation" if pv_profile is not None else None
                ),
                "solar_year": scenario_meta.get("DWD Solar Weather Year"),
                "solar_profile_years_used": solar_years,
                "solar_profile_year_count": len(solar_years),
                "solar_profile_method": scenario_meta.get("DWD Solar Profile Method"),
                "solar_station_id": solar_station_id,
                "solar_station_name": (
                    project_meta.get("dwd_solar_station")
                    or scenario_meta.get("DWD Solar Station")
                ),
                "solar_station_latitude": (
                    project_meta.get("dwd_solar_station_latitude")
                    or scenario_meta.get("DWD Solar Station Latitude")
                    or (solar_details or {}).get("latitude")
                ),
                "solar_station_longitude": (
                    project_meta.get("dwd_solar_station_longitude")
                    or scenario_meta.get("DWD Solar Station Longitude")
                    or (solar_details or {}).get("longitude")
                ),
                "solar_station_distance_km": saved_solar_distance,
                "source": project_meta.get("dwd_weather_source"),
                "ground_method": (
                    project_meta.get("dwd_ground_method")
                    or scenario_meta.get("Ground Temperature Method")
                ),
            }
        except Exception:
            continue
    return None


def load_project_meta_into_session(project_name: str):
    try:
        with open(project_meta_path(project_name), "r", encoding="utf-8") as f:
            meta = json.load(f)

        # The full hourly series is intentionally reloaded for this project's
        # coordinates. Never reuse a previous project's in-memory DWD profile.
        for weather_key in (
            "dwd_weather", "_dwd_weather_attempted_coords", "weather_data_warning",
            "weather_data_message", "design_outdoor_temperature_c", "ground_temperature_c",
            "ground_temperature_depth_cm",
            "dwd_weather_station", "dwd_weather_station_id", "dwd_weather_year",
            "dwd_profile_years_used", "dwd_available_complete_years",
            "dwd_available_complete_year_count", "dwd_station_distance_km",
            "dwd_profile_method", "dwd_ground_method", "dwd_weather_source",
            "dwd_solar_station", "dwd_solar_station_id",
            "dwd_solar_station_latitude", "dwd_solar_station_longitude",
            "dwd_solar_station_distance_km", "dwd_solar_profile_years_used",
        ):
            st.session_state.pop(weather_key, None)
        st.session_state.pop("location_geocoding_warning", None)

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
        saved_weather = _load_project_weather(project_name, pc)
        if saved_weather is None:
            saved_weather = _weather_from_existing_scenario(
                project_name, pc, meta
            )
            if saved_weather is not None:
                _save_project_weather(project_name, pc, saved_weather)
        if saved_weather is not None:
            _remember_dwd_weather(saved_weather, persist=False)
        for key in (
            "design_outdoor_temperature_c", "ground_temperature_c", "dwd_weather_station",
            "ground_temperature_depth_cm",
            "dwd_weather_station_id", "dwd_weather_year", "dwd_profile_years_used",
            "dwd_available_complete_years", "dwd_available_complete_year_count",
            "dwd_station_distance_km", "dwd_profile_method", "dwd_ground_method",
            "dwd_weather_source", "dwd_solar_station", "dwd_solar_station_id",
            "dwd_solar_station_latitude", "dwd_solar_station_longitude",
            "dwd_solar_station_distance_km", "dwd_solar_profile_years_used",
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
        "ground_temperature_depth_cm": st.session_state.get(
            "ground_temperature_depth_cm"
        ),
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
        "dwd_solar_station": (st.session_state.get("dwd_weather") or {}).get(
            "solar_station_name", st.session_state.get("dwd_solar_station")
        ),
        "dwd_solar_station_id": (st.session_state.get("dwd_weather") or {}).get(
            "solar_station_id", st.session_state.get("dwd_solar_station_id")
        ),
        "dwd_solar_station_latitude": (st.session_state.get("dwd_weather") or {}).get(
            "solar_station_latitude", st.session_state.get("dwd_solar_station_latitude")
        ),
        "dwd_solar_station_longitude": (st.session_state.get("dwd_weather") or {}).get(
            "solar_station_longitude", st.session_state.get("dwd_solar_station_longitude")
        ),
        "dwd_solar_station_distance_km": (st.session_state.get("dwd_weather") or {}).get(
            "solar_station_distance_km", st.session_state.get("dwd_solar_station_distance_km")
        ),
        "dwd_solar_profile_years_used": (st.session_state.get("dwd_weather") or {}).get(
            "solar_profile_years_used", st.session_state.get("dwd_solar_profile_years_used")
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
        "ground_temperature_depth_cm": None,
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
        "dwd_solar_station": None,
        "dwd_solar_station_id": None,
        "dwd_solar_station_latitude": None,
        "dwd_solar_station_longitude": None,
        "dwd_solar_station_distance_km": None,
        "dwd_solar_profile_years_used": None,
        "location_geocoding_warning": None,
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

def _project_missing_network_lengths(project_name: str) -> list[str]:
    missing = []
    for scenario in list_scenarios(project_name=project_name):
        scenario_file = get_scenario_file(scenario, project_name=project_name)
        if (
                _scenario_system_type(scenario_file) == "District heating (centralized)"
                and _scenario_route_length(scenario_file) is None
        ):
            missing.append(scenario)
    return missing


def _repair_pv_feed_in_emissions(scenario_file: str) -> None:
    """Backfill the defined PV feed-in CO2 credit in older scenario files."""
    workbook = openpyxl.load_workbook(scenario_file)
    if "feed_in_trafo" not in workbook.sheetnames:
        return
    sheet = workbook["feed_in_trafo"]
    headers = {
        str(sheet.cell(1, column).value).strip(): column
        for column in range(1, sheet.max_column + 1)
        if sheet.cell(1, column).value is not None
    }
    label_column = headers.get("label")
    emission_column = headers.get("emission_factor")
    if label_column is None or emission_column is None:
        return
    changed = False
    for row in range(2, sheet.max_row + 1):
        label = str(sheet.cell(row, label_column).value or "").strip()
        if label not in PV_FEED_IN_LABELS:
            continue
        value = pd.to_numeric(
            sheet.cell(row, emission_column).value, errors="coerce"
        )
        if pd.isna(value):
            sheet.cell(row, emission_column, -0.34)
            changed = True
    if changed:
        workbook.save(scenario_file)


def _configure_chp_feed_in(scenario_file: str) -> None:
    """Activate the shared CHP feed-in path exactly when a CHP is active."""
    required = {"chp", "feed_in_trafo", "buses", "transformers"}
    with pd.ExcelFile(scenario_file) as workbook:
        sheet_names = set(workbook.sheet_names)
    if not required.issubset(sheet_names):
        return

    chp = pd.read_excel(scenario_file, sheet_name="chp")
    feed_in = pd.read_excel(scenario_file, sheet_name="feed_in_trafo")
    buses = pd.read_excel(scenario_file, sheet_name="buses")
    transformers = pd.read_excel(scenario_file, sheet_name="transformers")
    original_chp = chp.copy()
    original_feed_in = feed_in.copy()
    original_buses = buses.copy()
    original_transformers = transformers.copy()
    chp_active = pd.to_numeric(
        chp.get("active", 0), errors="coerce"
    ).fillna(0).eq(1)
    enabled = bool(chp_active.any())

    feed_labels = feed_in.get("label", pd.Series("", index=feed_in.index)).astype(str)
    shared_feed = feed_labels.str.replace(r"_X$", "", regex=True).eq(
        "z_CHP_Einspeisung"
    )
    if shared_feed.any():
        feed_in.loc[shared_feed, "active"] = int(enabled)
        if "emission_factor" in feed_in.columns:
            emission = pd.to_numeric(
                feed_in.loc[shared_feed, "emission_factor"], errors="coerce"
            )
            feed_in.loc[shared_feed, "emission_factor"] = emission.fillna(-0.34)
        chp_bus = str(feed_in.loc[shared_feed, "from"].iloc[0] or "b_el_CHP")
        feed_in_bus = str(
            feed_in.loc[shared_feed, "to 1"].iloc[0] or "b_el_Einspeisung"
        )
    else:
        chp_bus = "b_el_CHP"
        feed_in_bus = "b_el_Einspeisung"

    transformer_labels = transformers.get(
        "label", pd.Series("", index=transformers.index)
    ).astype(str)
    internal_link = transformer_labels.str.replace(r"_X$", "", regex=True).eq(
        "z_CHP_el"
    )
    if not internal_link.any():
        try:
            template_transformers = pd.read_excel(
                TEMPLATE_DEC_FILE, sheet_name="transformers"
            )
            template_labels = template_transformers["label"].astype(str)
            template_link = template_transformers.loc[
                template_labels.str.replace(r"_X$", "", regex=True).eq("z_CHP_el")
            ]
            if not template_link.empty:
                transformers = pd.concat(
                    [transformers, template_link.iloc[[0]].copy()], ignore_index=True
                )
                internal_link = transformers["label"].astype(str).str.replace(
                    r"_X$", "", regex=True
                ).eq("z_CHP_el")
        except Exception:
            pass
    if internal_link.any():
        transformers.loc[internal_link, "active"] = int(enabled)
        transformers.loc[internal_link, "from"] = chp_bus
        transformers.loc[internal_link, "to"] = "b_el"

    bus_labels = buses.get("label", pd.Series("", index=buses.index)).astype(str)
    for required_bus in (chp_bus, feed_in_bus):
        if required_bus in set(bus_labels):
            continue
        template_bus = pd.DataFrame()
        try:
            central_buses = pd.read_excel(TEMPLATE_CEN_FILE, sheet_name="buses")
            template_bus = central_buses.loc[
                central_buses["label"].astype(str).eq(required_bus)
            ]
        except Exception:
            pass
        bus_row = (
            template_bus.iloc[0].to_dict()
            if not template_bus.empty else
            {
                "label": required_bus,
                "active": 1,
                "excess": 1,
                "shortage": 1,
                "shortage costs": 1e20,
                "excess costs": 0 if required_bus == feed_in_bus else 1e20,
            }
        )
        buses = pd.concat([buses, pd.DataFrame([bus_row])], ignore_index=True)
        bus_labels = buses["label"].astype(str)

    if enabled and "to 2" in chp.columns:
        chp.loc[chp_active, "to 2"] = chp_bus

    if not (
        chp.equals(original_chp)
        and feed_in.equals(original_feed_in)
        and buses.equals(original_buses)
        and transformers.equals(original_transformers)
    ):
        with pd.ExcelWriter(
            scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
        ) as writer:
            chp.to_excel(writer, sheet_name="chp", index=False)
            feed_in.to_excel(writer, sheet_name="feed_in_trafo", index=False)
            buses.to_excel(writer, sheet_name="buses", index=False)
            transformers.to_excel(writer, sheet_name="transformers", index=False)


def _repair_pv_transformer_ep_costs(scenario_file: str) -> None:
    """Synchronize categorized PV investment costs before simulation."""
    costs = _pv_transformer_ep_costs_from_template()
    workbook = openpyxl.load_workbook(scenario_file)
    if "transformers" not in workbook.sheetnames:
        raise ValueError("The scenario has no transformers sheet.")
    sheet = workbook["transformers"]
    headers = {
        str(sheet.cell(1, column).value).strip(): column
        for column in range(1, sheet.max_column + 1)
        if sheet.cell(1, column).value is not None
    }
    label_column = headers.get("label")
    ep_costs_column = headers.get("ep_costs")
    if label_column is None or ep_costs_column is None:
        raise ValueError(
            "The scenario transformer sheet must contain label and ep_costs columns."
        )
    changed = False
    for row in range(2, sheet.max_row + 1):
        label = str(sheet.cell(row, label_column).value or "").strip()
        normalized_label = label[:-2] if label.endswith("_X") else label
        if normalized_label not in costs:
            continue
        expected = costs[normalized_label]
        current = pd.to_numeric(sheet.cell(row, ep_costs_column).value, errors="coerce")
        if pd.isna(current) or not np.isclose(float(current), expected):
            sheet.cell(row, ep_costs_column, expected)
            changed = True
    if changed:
        workbook.save(scenario_file)


def submit_project(project_name: str) -> bool:
    """Mark this project as ready (per your previous global flag approach)."""
    missing_network_lengths = _project_missing_network_lengths(project_name)
    if missing_network_lengths:
        st.error(
            "Add a network length before submitting these centralized scenarios: "
            + ", ".join(missing_network_lengths)
        )
        return False
    local_errors = {}
    for scenario in list_scenarios(project_name=project_name):
        scenario_file = get_scenario_file(scenario, project_name=project_name)
        error = _local_booster_validation_error(scenario_file)
        if error:
            local_errors[scenario] = error
    if local_errors:
        st.error(
            "Complete the LT-network configuration before submitting: "
            + "; ".join(
                f"{scenario}: {error}" for scenario, error in local_errors.items()
            )
        )
        return False
    # Scenario-specific Save buttons already materialize demand clusters,
    # technologies, storage, PV and feed-in components. Rebuilding all of those
    # sheets here used to rewrite each 8,760-row time-series sheet and made
    # submission unnecessarily slow. Submission must use the saved workbook
    # state (especially when the user intentionally ignores unsaved UI edits),
    # so only lightweight compatibility repairs remain here.
    for scenario in list_scenarios(project_name=project_name):
        scenario_file = get_scenario_file(scenario, project_name=project_name)
        system_type = _scenario_system_type(scenario_file)
        if system_type in {
            "District heating (centralized)",
            "Decentralized solution",
        }:
            _repair_pv_feed_in_emissions(scenario_file)
            _repair_pv_transformer_ep_costs(scenario_file)

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
    return True

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
            with pd.ExcelFile(path, engine="openpyxl") as xls:
                sheet_names = list(xls.sheet_names)
        except Exception:
            continue

        for sheet in sheet_names:
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
        with pd.ExcelFile(path) as workbook:
            if "buildings_dataset" not in workbook.sheet_names:
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
    st.markdown(f"- **Total heat demand:** {(sh + dhw) / 1_000:,.1f} MWh/a")


def render_scenario_summary(project_name: str, scenario_name: str, show_demand=True):
    """Compact read-only summary of one scenario."""
    excel_file = get_scenario_file(scenario_name, project_name=project_name)

    def review_heading(text: str, separated: bool = False) -> None:
        top_margin = "1.35rem" if separated else "0.45rem"
        left_margin = "0" if separated else "1.25rem"
        st.markdown(
            f'<div style="margin-left: {left_margin}; margin-top: {top_margin}; '
            f'margin-bottom: 0.4rem; line-height: 1.6;">'
            f'<strong>{html.escape(str(text))}</strong></div>',
            unsafe_allow_html=True,
        )

    def review_line(label: str, value: str) -> None:
        st.markdown(
            f'<div style="margin-left: 2.5rem; margin-bottom: 0.35rem; '
            f'line-height: 1.8;">'
            f'<strong>{html.escape(str(label))}:</strong> '
            f'{html.escape(str(value))}</div>',
            unsafe_allow_html=True,
        )

    def review_source(source: str, technologies: str) -> None:
        st.markdown(
            f'<div style="margin-left: 2.5rem; margin-bottom: 0.35rem; '
            f'line-height: 1.8;">'
            f'<span style="margin-right: 0.45rem;">&#8226;</span>'
            f'<strong>{html.escape(str(source))}:</strong> '
            f'{html.escape(str(technologies))}</div>',
            unsafe_allow_html=True,
        )

    try:
        system_type = _scenario_system_type(excel_file)
        local_network = _local_network_is_enabled(excel_file)
        if system_type != "District heating (centralized)":
            ht_share = 100.0 * _decentral_space_heating_ht_share(excel_file)
            supply_description = (
                "Decentral plants — "
                f"{ht_share:,.1f}% HT space-heating demand"
            )
        elif local_network:
            supply_description = "LT network with local booster plants"
        else:
            supply_description = "HT network"
        st.markdown(
            f'<div style="color: #006400; '
            f'margin-bottom: 0.9rem;">'
            f'<strong>Supply configuration:</strong> '
            f'{html.escape(supply_description)}</div>',
            unsafe_allow_html=True,
        )

        if show_demand and local_network:
            base_demand = _local_network_base_demand(excel_file)
            values = pd.to_numeric(
                base_demand.get(
                    "nominal value", pd.Series(0, index=base_demand.index)
                ), errors="coerce"
            ).fillna(0)
            active = pd.to_numeric(
                base_demand.get(
                    "active", pd.Series(1, index=base_demand.index)
                ), errors="coerce"
            ).fillna(0).eq(1)
            buses = base_demand.get(
                "from", pd.Series("", index=base_demand.index)
            ).fillna("").astype(str)
            lt_kwh = float(values[active & buses.eq("b_th_LT")].sum())
            ht_kwh = float(values[active & buses.eq("b_th_HT")].sum())
            review_heading("Demand")
            review_line(
                "LT demand supplied by the central plant",
                f"{lt_kwh / 1_000:,.1f} MWh/a",
            )
            review_line(
                "HT demand supplied by local booster plants",
                f"{ht_kwh / 1_000:,.1f} MWh/a",
            )
        elif show_demand:
            demand_summary = _scenario_demand_summary(excel_file)
            total_heat = float(demand_summary.get("space_heating_kwh", 0.0)) + float(
                demand_summary.get("dhw_kwh", 0.0)
            )
            review_heading("Demand")
            review_line("Total heat demand", f"{total_heat / 1_000:,.1f} MWh/a")

        central_heading = (
            "Central heating plant technologies"
            if system_type == "District heating (centralized)"
            else "Decentral plant technologies"
        )
        review_heading(central_heading, separated=True)
        source_df = pd.read_excel(excel_file, sheet_name="sources")
        source_bus_map = {row["label"]: row["to"] for _, row in source_df.iterrows()}
        source_bus_map["el"] = "b_el"
        existing_techs = read_existing_technologies(excel_file, tech_sheet_map)

        for source, bus in source_bus_map.items():
            techs = existing_techs.get(bus, [])
            if techs:
                tech_names = [display_names.get(t, t) for t in techs]
                review_source(str(source), ", ".join(tech_names))

        if _scenario_pv_is_enabled(excel_file):
            if system_type == "Decentralized solution":
                review_heading("Aggregated PV system", separated=True)
            review_source("Solar", "PV panels")

            pv_meta = _scenario_meta_values(excel_file)
            maximum_pv_area = pd.to_numeric(
                pv_meta.get("Maximum PV Collector Area (m2)"), errors="coerce"
            )
            if pd.isna(maximum_pv_area):
                maximum_pv_area = pd.to_numeric(
                    pv_meta.get("Maximum PV Roof Area (m2)"), errors="coerce"
                )
            if pd.isna(maximum_pv_area):
                maximum_pv_area = _default_pv_roof_area_m2(project_name)
            review_line(
                "Maximum PV area",
                f"{max(float(maximum_pv_area), 0.0):,.1f} m²",
            )

        storage_heading = (
            "Central heating plant storage"
            if system_type == "District heating (centralized)"
            else "Decentral storage"
        )
        review_heading(storage_heading, separated=True)
        try:
            storage_df = pd.read_excel(excel_file, sheet_name="storages")
            active_mask = pd.to_numeric(
                storage_df.get("active", 0), errors="coerce"
            ).fillna(0).eq(1)
            if system_type == "Decentralized solution":
                active_storage = storage_df.loc[
                    active_mask
                    & storage_df["label"].astype(str).str.contains(
                        r"_DC\d+$", case=False, regex=True
                    )
                ]
            else:
                active_storage = storage_df.loc[
                    active_mask & storage_df["label"].eq("Storage_th")
                ]
            if not active_storage.empty:
                capacities = active_storage["max capacity"].apply(parse_number)
                if system_type == "Decentralized solution":
                    review_line(
                        "Configuration",
                        f"Enabled, Three days of each consumer's summer DHW demand",
                    )
                else:
                    review_line(
                        "Configuration", f"Enabled, {float(capacities.iloc[0]):.1f} kWh"
                    )
            else:
                review_line("Configuration", "Not used")
        except Exception:
            pass

        if local_network:
            local_meta = _scenario_meta_values(excel_file)
            selected_local = {
                item.strip().upper()
                for item in str(
                    local_meta.get("Local Booster Technologies") or ""
                ).split(",")
                if item.strip()
            }
            review_heading("Local booster plant technologies", separated=True)
            for local_source, local_options in (
                LOCAL_BOOSTER_TECHNOLOGIES_BY_SOURCE.items()
            ):
                technology_names = [
                    "Power-to-heat (P2H)" if item == "P2H"
                    else "Air-source heat pump (ASHP)"
                    for item in local_options if item in selected_local
                ]
                review_source(
                    str(local_source),
                    ", ".join(technology_names)
                    if technology_names else "None selected",
                )

            review_heading("Local consumer storage", separated=True)
            if _meta_bool(local_meta.get("Local Storage Enabled")):
                mode = local_meta.get("Local Storage Capacity Mode")
                review_line("Configuration", f"Enabled, {mode}")
            else:
                review_line("Configuration", "Not used")

    except Exception as e:
        st.warning(f"Could not summarize scenario '{scenario_name}': {e}")


def _scenario_demand_summary(scenario_file: str) -> dict:
    """Return SH and DHW totals, excluding network heat loss."""
    try:
        demand = (
            _decentral_base_demand(scenario_file)
            if _scenario_system_type(scenario_file) == "Decentralized solution"
            else pd.read_excel(scenario_file, sheet_name="demand")
        )
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
    dhw = labels.apply(_is_dhw_demand_label)
    valid = active & thermal & not_loss
    return {
        "space_heating_kwh": float(values[valid & ~dhw].sum()),
        "dhw_kwh": float(values[valid & dhw].sum()),
    }


def _copy_scenario_demand(source_file: str, target_file: str) -> None:
    """Copy demand rows and the normalized DHW profile between scenarios."""
    demand = (
        _decentral_base_demand(source_file)
        if _scenario_system_type(source_file) == "Decentralized solution"
        else _local_network_base_demand(source_file)
    )
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
    if _scenario_system_type(target_file) == "District heating (centralized)":
        source_meta = _scenario_meta_values(source_file)
        local_keys = (
            "LT Network With Local Boosters",
            "LT Temperature Decision Saved",
            "Network Supply Temperature",
            "Local Booster Technologies",
            "Local Storage Enabled",
            "Local Storage Capacity Mode",
            "Local Storage Max Capacity per Consumer (kWh)",
        )
        update_meta_sheet(
            target_file,
            {key: source_meta[key] for key in local_keys if key in source_meta},
        )
        _save_local_network_base_demand(target_file, demand)
        _apply_local_booster_configuration(target_file)
        _apply_network_temperature_assumptions(target_file)
    elif _scenario_system_type(target_file) == "Decentralized solution":
        source_meta = _scenario_meta_values(source_file)
        decentral_temperature_keys = (
            "Decentral Space Heating HT Share (%)",
            "Decentral Space Heating Temperature Mode",
        )
        update_meta_sheet(
            target_file,
            {
                key: source_meta[key]
                for key in decentral_temperature_keys
                if key in source_meta
            },
        )
        _save_decentral_base_demand(target_file, demand)
        _apply_decentral_configuration(target_file)


def _scenario_demand_rows_without_loss(scenario_file: str) -> pd.DataFrame:
    """Return the scenario demand rows that are directly editable by the user."""
    if _local_network_is_enabled(scenario_file):
        demand = _local_network_base_demand(scenario_file)
        if demand.empty or "label" not in demand.columns:
            return demand
        labels = demand["label"].fillna("").astype(str).str.strip()
        return demand.loc[labels.ne("")].copy()
    if _scenario_system_type(scenario_file) == "Decentralized solution":
        demand = _decentral_base_demand(scenario_file)
        if demand.empty or "label" not in demand.columns:
            return demand
        labels = demand["label"].fillna("").astype(str).str.strip()
        return demand.loc[labels.ne("")].copy()
    try:
        demand = pd.read_excel(scenario_file, sheet_name="demand")
    except Exception:
        return pd.DataFrame()
    if demand.empty or "label" not in demand.columns:
        return demand
    labels = demand["label"].fillna("").astype(str).str.strip()
    return demand.loc[
        labels.ne("")
        & ~labels.isin({HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL})
    ].copy()


def _import_latest_project_demand(
        scenario_file: str,
        keep_existing: bool = False,
        import_building_count: bool = True,
) -> dict:
    """Import building-estimation totals, either after or instead of current demand rows."""
    project = get_current_project()
    if not project:
        raise ValueError("No project is open.")
    _load_saved_project_totals(project)
    sh_value = st.session_state.get("total_heat_demand")
    dhw_value = st.session_state.get("total_dhw_demand")
    if sh_value is None or dhw_value is None:
        raise ValueError("Complete space-heating and DHW estimation results are not available yet.")

    existing = (
        _scenario_demand_rows_without_loss(scenario_file)
        if keep_existing
        else pd.DataFrame()
    )

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
    _save_demand_and_apply_temperature_configuration(scenario_file, demand)

    project_data = Path(get_project_dir(project)) / "project_data.xlsx"
    if not isinstance(st.session_state.get("dhw_load_profile"), pd.DataFrame) and project_data.exists():
        try:
            st.session_state["dhw_load_profile"] = pd.read_excel(project_data, sheet_name="dhw_profile")
        except Exception:
            pass
    write_dhw_profile_to_time_series(scenario_file)

    scenario_meta = _scenario_meta_values(scenario_file)
    existing_count = pd.to_numeric(scenario_meta.get("Building Count"), errors="coerce")
    existing_count = int(existing_count) if pd.notna(existing_count) and existing_count > 0 else None
    imported_count = _connected_building_count_from_session()
    profile_count = imported_count if import_building_count else existing_count
    meta_updates = {
        "Total Space Heating Demand (kWh/a)": float(sh_value),
        "Total DHW Demand (kWh/a)": float(dhw_value),
        "use_total_demand": "True",
    }
    if import_building_count:
        meta_updates.update({
            "Building Count": imported_count,
            "use_building_data": "True",
        })
    update_meta_sheet(scenario_file, meta_updates)
    _update_scenario_load_profiles(scenario_file, building_count=profile_count)
    if _scenario_system_type(scenario_file) == "District heating (centralized)":
        _apply_local_booster_configuration(scenario_file)
        _apply_network_temperature_assumptions(scenario_file)
    elif _scenario_system_type(scenario_file) == "Decentralized solution":
        _apply_decentral_configuration(scenario_file)
    if import_building_count:
        st.session_state.pop(
            f"scenario_building_count_{os.path.basename(scenario_file)}",
            None,
        )
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


def _detailed_reverse_location_name(address: dict, fallback: str = "") -> str:
    """Prefer a clicked district/municipality while retaining its parent city."""
    address = address if isinstance(address, dict) else {}
    detail = next(
        (
            str(address.get(key)).strip()
            for key in (
                "city_district",
                "borough",
                "suburb",
                "municipality",
                "quarter",
            )
            if address.get(key) and str(address.get(key)).strip()
        ),
        "",
    )
    parent = next(
        (
            str(address.get(key)).strip()
            for key in ("city", "town", "village", "hamlet", "county")
            if address.get(key) and str(address.get(key)).strip()
        ),
        "",
    )
    if detail and parent and detail.casefold() != parent.casefold():
        return f"{detail}, {parent}"
    return detail or parent or str(fallback or "").strip() or "Unknown"


def _remember_dwd_weather(weather: dict, persist: bool = True) -> None:
    """Keep the exact weather selection available across project pages."""
    st.session_state["dwd_weather"] = weather
    mappings = {
        "design_outdoor_temperature_c": "design_outdoor_temperature_c",
        "ground_temperature_c": "ground_temperature_c",
        "ground_temperature_depth_cm": "ground_temperature_depth_cm",
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
        "solar_station_name": "dwd_solar_station",
        "solar_station_id": "dwd_solar_station_id",
        "solar_station_latitude": "dwd_solar_station_latitude",
        "solar_station_longitude": "dwd_solar_station_longitude",
        "solar_station_distance_km": "dwd_solar_station_distance_km",
        "solar_profile_years_used": "dwd_solar_profile_years_used",
    }
    for source, target in mappings.items():
        st.session_state[target] = weather.get(source)
    st.session_state["weather_data_message"] = (
        f"DWD station {weather.get('station_name')} ({weather.get('station_id')}), "
        f"using {weather.get('profile_year_count')} complete years: "
        f"{', '.join(map(str, weather.get('profile_years_used', [])))}."
    )
    st.session_state.pop("weather_data_warning", None)
    if persist:
        project = get_current_project()
        coords = get_valid_coords()
        if project and coords:
            try:
                _save_project_weather(project, coords, weather)
            except Exception as exc:
                st.session_state["weather_data_warning"] = (
                    f"Weather data were loaded but could not be saved: {exc}"
                )


def _ensure_project_dwd_weather(coords):
    """Load DWD weather once per selected coordinate pair."""
    signature = (round(float(coords[0]), 7), round(float(coords[1]), 7))
    weather = st.session_state.get("dwd_weather")
    if (
        isinstance(weather, dict)
        and weather.get("temperature_c") is not None
        and weather.get("pv_profile_unit") == "kWh/m2 global irradiation"
    ):
        return weather
    if st.session_state.get("_dwd_weather_attempted_coords") == signature:
        if st.session_state.get("weather_data_warning"):
            return None
        # Older sessions could retain an attempted-coordinate marker without
        # the accompanying exception. Retry instead of showing "unknown error".
        st.session_state.pop("_dwd_weather_attempted_coords", None)
    try:
        weather = get_location_weather(*coords)
        _remember_dwd_weather(weather)
        return weather
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        st.session_state["_dwd_weather_attempted_coords"] = signature
        st.session_state["weather_data_warning"] = (
            f"{type(exc).__name__}: {detail}"
        )
        return None


def _render_project_weather_information(coords) -> None:
    """Show the exact DWD inputs used by TEASER, profiles and heat pumps."""
    cached_weather = st.session_state.get("dwd_weather")
    cache_is_valid = (
        isinstance(cached_weather, dict)
        and cached_weather.get("temperature_c") is not None
        and cached_weather.get("pv_profile_unit") == "kWh/m2 global irradiation"
    )
    if cache_is_valid:
        weather = cached_weather
    else:
        with st.spinner("Loading weather data from DWD..."):
            weather = _ensure_project_dwd_weather(coords)
    st.markdown("#### Weather data used")
    if weather is None:
        st.warning(
            "Exact DWD weather data could not be loaded. Until it becomes available, scenario generation "
            "uses the representative-city template only as an explicit fallback. "
            f"Details: {st.session_state.get('weather_data_warning') or 'The request did not complete. Please retry.'}"
        )
        if st.button("Retry loading DWD weather", key="retry_project_dwd_weather"):
            st.session_state.pop("_dwd_weather_attempted_coords", None)
            st.rerun()
        return

    used_years = ", ".join(map(str, weather.get("profile_years_used", []))) or "not available"
    solar_years = (
        ", ".join(map(str, weather.get("solar_profile_years_used", [])))
        or "not available"
    )
    available_count = weather.get("available_complete_year_count", 0)
    solar_distance = pd.to_numeric(
        weather.get("solar_station_distance_km"), errors="coerce"
    )
    solar_distance_text = (
        f"{float(solar_distance):.1f} km"
        if (
            pd.notna(solar_distance)
            and not (
                float(solar_distance) == 0.0
                and weather.get("solar_station_latitude") is None
            )
        )
        else "distance unavailable"
    )
    solar_station_text = (
        f"{weather.get('solar_station_name')} (ID {weather.get('solar_station_id')}, "
        f"{solar_distance_text}); "
        f"years {solar_years}; annual irradiation "
        f"{float(pd.to_numeric(weather.get('pv_profile'), errors='coerce').sum()):,.1f} kWh/m²"
        if weather.get("pv_profile") is not None
        else f"not available ({weather.get('solar_error') or 'unknown reason'})"
    )
    st.markdown(
        f"- **DWD station:** {weather.get('station_name')} (ID {weather.get('station_id')}, "
        f"{float(weather.get('station_distance_km', 0.0)):.1f} km from the project location)\n"
        f"- **Hourly temperature profile:** {used_years} "
        f"({len(weather.get('profile_years_used', []))} used; {available_count} complete years available)\n"
        f"- **Reference chronology:** {weather.get('year')}\n"
        f"- **Outdoor design temperature:** {float(weather.get('design_outdoor_temperature_c')):.1f} °C\n"
        f"- **Average ground temperature:** {float(weather.get('ground_temperature_c')):.1f} °C — "
        f"{weather.get('ground_method')}\n"
        f"- **Global radiation:** {solar_station_text}\n"
        # f"- **Profile method:** {weather.get('profile_method')}"
    )
    st.caption(
        "The DWD temperature profiles are used for heat demand estimation, load profile generation and heat-pump COP calculation. "
        "The DWD global-radiation profile is used for PV calculation when available. "
        "As DWD has fewer eligible solar-radiation stations, the nearest solar station may be farther "
        "away than the temperature station."
    )


def _invalidate_location_weather():
    for key in (
        "dwd_weather", "design_outdoor_temperature_c", "ground_temperature_c",
        "dwd_weather_station", "dwd_weather_station_id", "dwd_weather_year",
        "dwd_profile_years_used", "dwd_available_complete_year_count",
        "weather_data_message", "weather_data_warning",
        "dwd_available_complete_years", "dwd_station_distance_km",
        "dwd_profile_method", "dwd_ground_method", "dwd_weather_source",
        "dwd_solar_station", "dwd_solar_station_id",
        "dwd_solar_station_latitude", "dwd_solar_station_longitude",
        "dwd_solar_station_distance_km", "dwd_solar_profile_years_used",
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


def _fit_pv_profile(
    values, target_len: int, annual_sum: float | None = None
) -> pd.Series:
    profile = _fit_weather_values(values, target_len).clip(lower=0)
    total = float(profile.sum())
    if not np.isfinite(total) or total <= 0:
        raise ValueError("The PV profile contains no positive annual yield.")
    if annual_sum is not None:
        profile = profile * (float(annual_sum) / total)
    return profile


def _berlin_pv_profile(target_len: int) -> pd.Series:
    """Return the Berlin fallback irradiation profile at scenario resolution."""
    berlin = pd.read_excel(
        PV_COP_FILE,
        sheet_name=PV_FALLBACK_CITY,
        usecols=[PV_PROFILE_COLUMN],
    )[PV_PROFILE_COLUMN]
    return _fit_pv_profile(
        berlin,
        target_len,
        annual_sum=PV_FALLBACK_IRRADIATION_KWH_PER_M2,
    )


def _scenario_has_dwd_pv_profile(scenario_file: str) -> bool:
    """Return whether the scenario profile is documented as project DWD data."""
    meta = _scenario_meta_values(scenario_file)
    station = str(meta.get("DWD Solar Station") or "").strip()
    method = str(meta.get("DWD Solar Profile Method") or "").strip()
    return bool(station and method)


def _write_scenario_pv_profile(
    scenario_file: str, profile: pd.Series
) -> None:
    """Write one complete PV.fix column without disturbing other time series."""
    workbook = openpyxl.load_workbook(scenario_file)
    if "time_series" not in workbook.sheetnames:
        raise ValueError("The scenario has no time_series sheet.")
    sheet = workbook["time_series"]
    headers = {
        str(sheet.cell(1, column).value): column
        for column in range(1, sheet.max_column + 1)
        if sheet.cell(1, column).value is not None
    }
    timestamp_column = headers.get("timestamp")
    if timestamp_column is None:
        raise ValueError("The scenario time_series sheet has no timestamp column.")
    pv_column = headers.get(PV_PROFILE_COLUMN)
    if pv_column is None:
        pv_column = sheet.max_column + 1
        sheet.cell(1, pv_column, PV_PROFILE_COLUMN)
    timestamp_rows = [
        row for row in range(2, sheet.max_row + 1)
        if sheet.cell(row, timestamp_column).value is not None
    ]
    fitted = _fit_pv_profile(profile, len(timestamp_rows))
    for row, value in zip(timestamp_rows, fitted):
        sheet.cell(row, pv_column, float(value))
    timestamp_row_set = set(timestamp_rows)
    for row in range(2, sheet.max_row + 1):
        if row not in timestamp_row_set:
            sheet.cell(row, pv_column).value = None
    workbook.save(scenario_file)


def _ensure_scenario_pv_profile(scenario_file: str) -> str:
    """Keep local DWD PV data or install Berlin data as the explicit fallback."""
    if _scenario_has_dwd_pv_profile(scenario_file):
        try:
            profile = pd.read_excel(
                scenario_file,
                sheet_name="time_series",
                usecols=[PV_PROFILE_COLUMN],
            )[PV_PROFILE_COLUMN]
            values = pd.to_numeric(profile, errors="coerce").clip(lower=0)
            if values.notna().any() and float(values.max()) > 0:
                return "DWD"
        except Exception:
            pass

    timestamps = pd.read_excel(
        scenario_file, sheet_name="time_series", usecols=["timestamp"]
    )["timestamp"]
    target_len = int(timestamps.notna().sum())
    if target_len <= 0:
        raise ValueError("The scenario time_series sheet has no timestamps.")
    profile = _berlin_pv_profile(target_len)
    _write_scenario_pv_profile(scenario_file, profile)
    update_meta_sheet(
        scenario_file,
        {
            "PV Profile Source": "Berlin fallback from PV_COP.xlsx",
            "PV Peak Method": (
                "maximum hourly Berlin fallback irradiation x 20% efficiency; "
                "used because project DWD solar data are unavailable"
            ),
        },
    )
    return "Berlin fallback"


def _default_pv_roof_area_m2(project_name: str | None = None) -> float:
    """Return 40% of included building footprints (not useful floor area)."""
    project = project_name or get_current_project()
    if project:
        path = Path(get_project_dir(project)) / "project_data.xlsx"
        try:
            buildings = pd.read_excel(path, sheet_name="buildings_dataset")
            if "included" in buildings.columns:
                buildings = buildings.loc[
                    _to_bool_series(buildings["included"], default=True)
                ]
            area_column = next(
                (
                    column
                    for column in ("area_final_m2", "area_base_m2", "area_m2")
                    if column in buildings.columns
                ),
                None,
            )
            if area_column:
                total = float(
                    pd.to_numeric(buildings[area_column], errors="coerce")
                    .clip(lower=0)
                    .fillna(0)
                    .sum()
                )
                if total > 0:
                    return PV_ROOF_AREA_SHARE * total
        except Exception:
            pass

    building_info = st.session_state.get("info_overridden")
    if isinstance(building_info, pd.DataFrame) and not building_info.empty:
        area_column = next(
            (
                column
                for column in ("area (m²)", "area_m2", "area_final_m2")
                if column in building_info
            ),
            None,
        )
        if area_column:
            total = float(
                pd.to_numeric(building_info[area_column], errors="coerce")
                .clip(lower=0)
                .fillna(0)
                .sum()
            )
            if total > 0:
                return PV_ROOF_AREA_SHARE * total

    buildings = st.session_state.get("buildings_gdf")
    if isinstance(buildings, pd.DataFrame) and not buildings.empty:
        area_column = next(
            (column for column in ("area_m2", "area_final_m2") if column in buildings),
            None,
        )
        if area_column:
            rows = buildings
            if "included" in rows:
                rows = rows.loc[_to_bool_series(rows["included"], default=True)]
            total = float(
                pd.to_numeric(rows[area_column], errors="coerce")
                .clip(lower=0)
                .fillna(0)
                .sum()
            )
            return PV_ROOF_AREA_SHARE * total
    return 0.0


def _scenario_pv_peak_kw_per_m2(scenario_file: str) -> float:
    """Derive local peak electrical output from hourly irradiation at 20%."""
    meta = _scenario_meta_values(scenario_file)
    documented_source = str(meta.get("PV Profile Source") or "").lower()
    profile_is_supported = (
        _scenario_has_dwd_pv_profile(scenario_file)
        or "berlin fallback" in documented_source
    )
    if profile_is_supported:
        try:
            profile = pd.read_excel(
                scenario_file,
                sheet_name="time_series",
                usecols=[PV_PROFILE_COLUMN],
            )[PV_PROFILE_COLUMN]
            irradiation_peak = pd.to_numeric(
                profile, errors="coerce"
            ).clip(lower=0).max()
            if pd.notna(irradiation_peak) and float(irradiation_peak) > 0:
                return float(irradiation_peak) * PV_ELECTRICAL_EFFICIENCY
        except Exception:
            pass
    berlin = _berlin_pv_profile(8760)
    return float(berlin.max()) * PV_ELECTRICAL_EFFICIENCY


def _pv_transformer_parameters_from_template() -> dict[str, dict[str, float]]:
    """Return immutable PV category bounds and costs from the template."""
    template = pd.read_excel(TEMPLATE_CEN_FILE, sheet_name="transformers")
    required = {"label", "minimum", "maximum", "ep_costs"}
    if not required.issubset(template.columns):
        raise ValueError(
            "The centralized template transformer sheet must contain label, minimum, maximum, and ep_costs columns."
        )
    labels = template["label"].astype(str).str.replace(r"_X$", "", regex=True)
    result = {}
    for label in PV_TRANSFORMER_LABELS:
        matching = template.loc[labels.eq(label)]
        if matching.empty:
            raise ValueError(
                f"The centralized template has no transformer row for {label}."
            )
        values = {
            column: pd.to_numeric(matching.iloc[0][column], errors="coerce")
            for column in ("minimum", "maximum", "ep_costs")
        }
        if any(pd.isna(value) or not np.isfinite(float(value)) for value in values.values()):
            raise ValueError(
                f"The centralized template has invalid PV category parameters for {label}."
            )
        if float(values["minimum"]) < 0 or float(values["maximum"]) <= float(values["minimum"]):
            raise ValueError(
                f"The centralized template has an invalid capacity range for {label}."
            )
        result[label] = {column: float(value) for column, value in values.items()}
    return result


def _pv_transformer_ep_costs_from_template() -> dict[str, float]:
    """Return validated PV transformer investment costs from the template."""
    return {
        label: parameters["ep_costs"]
        for label, parameters in _pv_transformer_parameters_from_template().items()
    }


def _pv_renewable_parameters_from_template() -> dict[str, dict[str, float]]:
    """Return immutable PV source costs and area bounds from the template."""
    template = pd.read_excel(TEMPLATE_CEN_FILE, sheet_name="renewables")
    required = {"label", "ep_costs", "maximum"}
    if not required.issubset(template.columns):
        raise ValueError(
            "The centralized template renewable sheet must contain label, "
            "ep_costs, and maximum columns."
        )
    labels = template["label"].astype(str).str.replace(r"_X$", "", regex=True)
    result = {}
    for label in PV_RENEWABLE_LABELS:
        matching = template.loc[labels.eq(label)]
        if matching.empty:
            raise ValueError(
                f"The centralized template has no renewable row for {label}."
            )
        parameters = {
            column: pd.to_numeric(matching.iloc[0][column], errors="coerce")
            for column in ("ep_costs", "maximum")
        }
        if any(
            pd.isna(value) or not np.isfinite(float(value))
            for value in parameters.values()
        ):
            raise ValueError(
                f"The centralized template has invalid renewable parameters for {label}."
            )
        if float(parameters["ep_costs"]) < 0 or float(parameters["maximum"]) <= 0:
            raise ValueError(
                f"The centralized template has invalid renewable parameters for {label}."
            )
        result[label] = {
            column: float(value) for column, value in parameters.items()
        }
    return result
def _scenario_pv_is_enabled(scenario_file: str) -> bool:
    try:
        renewables = pd.read_excel(scenario_file, sheet_name="renewables")
        transformers = pd.read_excel(scenario_file, sheet_name="transformers")
        renewable_labels = renewables["label"].astype(str).str.replace(
            r"_X$", "", regex=True
        )
        transformer_labels = transformers["label"].astype(str).str.replace(
            r"_X$", "", regex=True
        )
        renewable_active = pd.to_numeric(
            renewables["active"], errors="coerce"
        ).fillna(0).eq(1)
        transformer_active = pd.to_numeric(
            transformers["active"], errors="coerce"
        ).fillna(0).eq(1)
        return bool(
            (renewable_labels.isin(PV_RENEWABLE_LABELS) & renewable_active).any()
            and (
                transformer_labels.isin(PV_TRANSFORMER_LABELS)
                & transformer_active
            ).any()
        )
    except Exception:
        return False


def _scenario_pv_feed_in_tariff(scenario_file: str) -> float:
    """Return the positive PV feed-in tariff represented as a negative cost."""
    feed_in = pd.read_excel(scenario_file, sheet_name="feed_in_trafo")
    required = {"label", "variable input costs"}
    if not required.issubset(feed_in.columns):
        raise ValueError(
            "The feed_in_trafo sheet must contain label and variable input costs columns."
        )
    labels = feed_in["label"].astype(str).str.replace(r"_X$", "", regex=True)
    pv_rows = feed_in.loc[labels.isin(PV_FEED_IN_LABELS)].copy()
    if pv_rows.empty:
        raise ValueError("No PV feed-in component was found.")

    # New centralized templates use one shared feed-in component for every PV
    # plant category. Prefer it when reading files that also retain legacy
    # categorized rows.
    generic_rows = pv_rows.loc[labels.loc[pv_rows.index].eq(PV_FEED_IN_LABEL)]
    if not generic_rows.empty:
        pv_rows = generic_rows

    costs = pd.to_numeric(pv_rows["variable input costs"], errors="coerce")
    if "active" in pv_rows.columns:
        active = pd.to_numeric(pv_rows["active"], errors="coerce").fillna(0).eq(1)
        active_costs = costs.loc[active & costs.notna()]
        if not active_costs.empty:
            return max(-float(active_costs.iloc[0]), 0.0)
    valid_costs = costs.loc[costs.notna()]
    if valid_costs.empty or not np.isfinite(float(valid_costs.iloc[0])):
        raise ValueError("The PV feed-in tariff in the scenario file is invalid.")
    return max(-float(valid_costs.iloc[0]), 0.0)


def _configure_scenario_pv(
    scenario_file: str,
    roof_area_m2: float,
    enabled: bool = False,
    feed_in_tariff_eur_per_kwh: float | None = None,
) -> dict:
    """Apply the roof limit and activate its category only when PV is selected."""
    roof_area = max(float(roof_area_m2 or 0), 0.0)
    profile_source = _ensure_scenario_pv_profile(scenario_file) if enabled else None
    peak_kw_per_m2 = _scenario_pv_peak_kw_per_m2(scenario_file)
    peak_kw = roof_area * peak_kw_per_m2
    # The PV transformer category bounds describe incident-radiation input
    # capacity. Electrical peak output is 20% of that value.
    category_input_kw = (
        peak_kw / PV_ELECTRICAL_EFFICIENCY
        if PV_ELECTRICAL_EFFICIENCY > 0 else peak_kw
    )
    renewables = pd.read_excel(scenario_file, sheet_name="renewables")
    transformers = pd.read_excel(scenario_file, sheet_name="transformers")
    feed_in = pd.read_excel(scenario_file, sheet_name="feed_in_trafo")

    renewable_labels = renewables["label"].astype(str).str.replace(
        r"_X$", "", regex=True
    )
    transformer_labels = transformers["label"].astype(str).str.replace(
        r"_X$", "", regex=True
    )
    feed_in_labels = feed_in["label"].astype(str).str.replace(
        r"_X$", "", regex=True
    )
    # A generic row replaces the old four category-specific feed-in rows. If a
    # template accidentally contains that row more than once, retain the first
    # definition so only one oemof component can be activated.
    generic_feed_in_indices = feed_in.index[
        feed_in_labels.eq(PV_FEED_IN_LABEL)
    ].tolist()
    if len(generic_feed_in_indices) > 1:
        feed_in = feed_in.drop(index=generic_feed_in_indices[1:]).reset_index(
            drop=True
        )
        feed_in_labels = feed_in["label"].astype(str).str.replace(
            r"_X$", "", regex=True
        )
    pv_renewable_mask = renewable_labels.isin(PV_RENEWABLE_LABELS)
    pv_transformer_mask = transformer_labels.isin(PV_TRANSFORMER_LABELS)
    all_pv_feed_in_mask = feed_in_labels.isin(PV_FEED_IN_LABELS)
    generic_feed_in_mask = feed_in_labels.eq(PV_FEED_IN_LABEL)
    pv_feed_in_mask = (
        generic_feed_in_mask
        if generic_feed_in_mask.any()
        else feed_in_labels.isin(PV_LEGACY_FEED_IN_LABELS)
    )
    if (
        not pv_renewable_mask.any()
        or not pv_transformer_mask.any()
        or not pv_feed_in_mask.any()
    ):
        raise ValueError("This scenario template does not contain the centralized PV components.")

    if feed_in_tariff_eur_per_kwh is not None:
        feed_in_tariff = float(feed_in_tariff_eur_per_kwh)
        if not np.isfinite(feed_in_tariff) or feed_in_tariff < 0:
            raise ValueError("The PV feed-in tariff must be a finite non-negative value.")
        # oemof minimizes costs, so revenue is represented as a negative
        # variable cost. Also update legacy rows so older scenario files retain
        # the user-defined tariff if their PV category changes.
        feed_in.loc[all_pv_feed_in_mask, "variable input costs"] = -feed_in_tariff
    else:
        feed_in_tariff = _scenario_pv_feed_in_tariff(scenario_file)

    # Scenario creation and the generation-technology editor both rewrite the
    # transformer sheet. Reapply PV investment costs from the source template
    # during the final PV configuration so those rewrites cannot leave NaN
    # objective coefficients in the optimization model.
    template_parameters = _pv_transformer_parameters_from_template()
    for transformer_label, parameters in template_parameters.items():
        matching_rows = transformer_labels.eq(transformer_label)
        for parameter in ("minimum", "maximum", "ep_costs"):
            transformers.loc[matching_rows, parameter] = parameters[parameter]
    for renewable_label, parameters in _pv_renewable_parameters_from_template().items():
        matching_rows = renewable_labels.eq(renewable_label)
        renewables.loc[matching_rows, "ep_costs"] = parameters["ep_costs"]
        renewables.loc[matching_rows, "maximum"] = parameters["maximum"]

    renewables.loc[pv_renewable_mask, "active"] = 0
    transformers.loc[pv_transformer_mask, "active"] = 0
    feed_in.loc[all_pv_feed_in_mask, "active"] = 0
    # Older scenario files can contain PV feed-in rows without their emission
    # credit. An active NaN emission factor propagates through oemof's integral
    # emission expression and makes the total CO2 result NaN.
    feed_in.loc[all_pv_feed_in_mask, "emission_factor"] = pd.to_numeric(
        feed_in.loc[all_pv_feed_in_mask, "emission_factor"], errors="coerce"
    ).fillna(-0.34)
    transformers.loc[
        pv_transformer_mask, "efficiency"
    ] = PV_ELECTRICAL_EFFICIENCY
    renewables.loc[pv_renewable_mask, "label"] = renewable_labels[pv_renewable_mask]
    transformers.loc[pv_transformer_mask, "label"] = transformer_labels[pv_transformer_mask]
    feed_in.loc[all_pv_feed_in_mask, "label"] = feed_in_labels[
        all_pv_feed_in_mask
    ]

    selected_transformer = None
    selected_renewable = None
    selected_feed_in = None
    maximum_collector_area = roof_area
    if peak_kw > 0:
        candidates = transformers.loc[pv_transformer_mask].copy()
        candidates["_minimum"] = pd.to_numeric(candidates["minimum"], errors="coerce").fillna(0)
        candidates["_maximum"] = pd.to_numeric(
            candidates["maximum"], errors="coerce"
        ).fillna(np.inf)
        selected = candidates.loc[
            candidates["_minimum"].le(category_input_kw)
            & candidates["_maximum"].gt(category_input_kw)
        ]
        if selected.empty:
            selected = candidates.sort_values("_maximum").tail(1)
        selected_transformer = str(selected.iloc[0]["label"])
        selected_renewable = selected_transformer.replace("PV_plant_", "PV_")
        if generic_feed_in_mask.any():
            matching_feed_in = feed_in_labels.loc[generic_feed_in_mask]
        else:
            capacity_category = selected_transformer.rsplit("_", 1)[-1]
            matching_feed_in = feed_in_labels.loc[
                pv_feed_in_mask
                & feed_in_labels.str.rsplit("_", n=1).str[-1].eq(
                    capacity_category
                )
            ]
        if matching_feed_in.empty:
            raise ValueError(
                "No PV feed-in component was found for the selected PV plant."
            )
        selected_feed_in = str(matching_feed_in.iloc[0])
        selected_feed_in_index = matching_feed_in.index[0]
        if enabled:
            transformers.loc[
                transformer_labels.eq(selected_transformer), "active"
            ] = 1
            renewables.loc[
                renewable_labels.eq(selected_renewable), "active"
            ] = 1
            feed_in.loc[selected_feed_in_index, "active"] = 1
            # Collector investment is area in m² and is limited only by the
            # footprint-derived roof availability. Apply scenario-specific
            # bounds only when PV is actually selected; otherwise all inactive
            # category rows retain their clean template values.
            renewables.loc[
                renewable_labels.eq(selected_renewable), "maximum"
            ] = maximum_collector_area
            transformers.loc[
                transformer_labels.eq(selected_transformer), "minimum"
            ] = 0.0
            transformers.loc[
                transformer_labels.eq(selected_transformer), "maximum"
            ] = category_input_kw

    maximum_peak_kw = maximum_collector_area * peak_kw_per_m2

    with pd.ExcelWriter(
        scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
    ) as writer:
        renewables.to_excel(writer, sheet_name="renewables", index=False)
        transformers.to_excel(writer, sheet_name="transformers", index=False)
        feed_in.to_excel(writer, sheet_name="feed_in_trafo", index=False)

    pv_profile_sum = None
    workbook = openpyxl.load_workbook(scenario_file)
    if "time_series" in workbook.sheetnames:
        time_series = workbook["time_series"]
        headers = {
            str(time_series.cell(1, column).value): column
            for column in range(1, time_series.max_column + 1)
        }
        pv_column = headers.get(PV_PROFILE_COLUMN)
        timestamp_column = headers.get("timestamp")
        if pv_column is not None and timestamp_column is not None:
            timestamp_rows = [
                row
                for row in range(2, time_series.max_row + 1)
                if time_series.cell(row, timestamp_column).value is not None
            ]
            values = [
                time_series.cell(row, pv_column).value
                for row in timestamp_rows
            ]
            normalized = _fit_pv_profile(values, len(values))
            for row, value in zip(timestamp_rows, normalized):
                time_series.cell(row, pv_column, float(value))
            timestamp_row_set = set(timestamp_rows)
            for row in range(2, time_series.max_row + 1):
                if row not in timestamp_row_set:
                    time_series.cell(row, pv_column).value = None
            pv_profile_sum = float(normalized.sum())
            workbook.save(scenario_file)

    update_meta_sheet(
        scenario_file,
        {
            "Maximum PV Roof Area (m2)": roof_area,
            "Maximum PV Collector Area (m2)": maximum_collector_area,
            "Maximum PV Peak Capacity (kWp)": maximum_peak_kw,
            "Maximum PV Radiation Input Capacity (kW)": category_input_kw,
            "PV Roof Peak Potential (kWp)": peak_kw,
            "PV Enabled": str(bool(enabled and peak_kw > 0)),
            "Selected PV Renewable": selected_renewable,
            "Selected PV Transformer": selected_transformer,
            "Active PV Renewable": selected_renewable if enabled and peak_kw > 0 else None,
            "Active PV Transformer": selected_transformer if enabled and peak_kw > 0 else None,
            "Selected PV Feed-in Transformer": selected_feed_in,
            "Active PV Feed-in Transformer": selected_feed_in if enabled and peak_kw > 0 else None,
            "PV Roof Area Basis": "40% of included building footprint/ground area",
            "PV Peak Power Density (kWp/m2)": peak_kw_per_m2,
            "PV Profile Source": (
                "DWD project solar profile"
                if profile_source == "DWD"
                else (
                    "Berlin fallback from PV_COP.xlsx"
                    if profile_source == "Berlin fallback" else None
                )
            ),
            "PV Peak Method": (
                "maximum local hourly DWD irradiation x 20% efficiency"
                if profile_source == "DWD"
                else (
                    "maximum hourly Berlin fallback irradiation x 20% efficiency; "
                    "used because project DWD solar data are unavailable"
                    if profile_source == "Berlin fallback"
                    else "defined from the available irradiation profile x 20% efficiency"
                )
            ),
            "PV Electrical Efficiency": PV_ELECTRICAL_EFFICIENCY,
            "PV Feed-in Tariff (EUR/kWh)": feed_in_tariff,
            "PV Irradiation Profile Sum (kWh/m2)": pv_profile_sum,
            "PV Electrical Yield at 20% (kWh/m2)": (
                pv_profile_sum * PV_ELECTRICAL_EFFICIENCY
                if pv_profile_sum is not None else None
            ),
        },
    )
    return {
        "roof_area_m2": roof_area,
        "maximum_collector_area_m2": maximum_collector_area,
        "peak_kw": maximum_peak_kw,
        "category_input_kw": category_input_kw,
        "peak_kw_per_m2": peak_kw_per_m2,
        "enabled": bool(enabled and peak_kw > 0),
        "renewable": selected_renewable,
        "transformer": selected_transformer,
        "feed_in_transformer": selected_feed_in,
        "feed_in_tariff_eur_per_kwh": feed_in_tariff,
    }


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
        # Representative-city files remain the fallback for COP values, but
        # their PV profiles must not stand in for project DWD solar data.
        weather_df = weather_df.drop(columns=[PV_PROFILE_COLUMN], errors="ignore")

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
        if dwd_weather.get("pv_profile") is not None:
            weather_df[PV_PROFILE_COLUMN] = _fit_pv_profile(
                dwd_weather["pv_profile"], target_len
            )
    pv_profile_source = None
    if PV_PROFILE_COLUMN in weather_df:
        pv_profile_source = "DWD"
    elif _scenario_pv_is_enabled(scenario_file):
        weather_df[PV_PROFILE_COLUMN] = _berlin_pv_profile(target_len)
        pv_profile_source = "Berlin fallback"
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
    if PV_PROFILE_COLUMN not in weather_df and PV_PROFILE_COLUMN in headers:
        old_pv_column = headers[PV_PROFILE_COLUMN]
        for row_index in range(2, sheet.max_row + 1):
            sheet.cell(row_index, old_pv_column).value = None
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
        "Ground Temperature Depth (cm)": dwd_weather.get("ground_temperature_depth_cm") if dwd_weather else None,
        "Ground Temperature Method": dwd_weather.get("ground_method") if dwd_weather else None,
        "DWD Soil Station": dwd_weather.get("soil_station_name") if dwd_weather else None,
        "DWD Soil Station ID": dwd_weather.get("soil_station_id") if dwd_weather else None,
        "DWD Soil Station Distance (km)": dwd_weather.get("soil_station_distance_km") if dwd_weather else None,
        "DWD Complete Years Available": dwd_weather.get("available_complete_year_count") if dwd_weather else None,
        "DWD Profile Years Used": (
            ", ".join(map(str, dwd_weather.get("profile_years_used", []))) if dwd_weather else None
        ),
        "DWD Temperature Profile Method": dwd_weather.get("profile_method") if dwd_weather else None,
        "DWD Solar Station": dwd_weather.get("solar_station_name") if dwd_weather else None,
        "DWD Solar Station ID": dwd_weather.get("solar_station_id") if dwd_weather else None,
        "DWD Solar Station Latitude": dwd_weather.get("solar_station_latitude") if dwd_weather else None,
        "DWD Solar Station Longitude": dwd_weather.get("solar_station_longitude") if dwd_weather else None,
        "DWD Solar Station Distance (km)": dwd_weather.get("solar_station_distance_km") if dwd_weather else None,
        "DWD Solar Weather Year": dwd_weather.get("solar_year") if dwd_weather else None,
        "DWD Solar Profile Years Used": (
            ", ".join(map(str, dwd_weather.get("solar_profile_years_used", [])))
            if dwd_weather else None
        ),
        "DWD Solar Profile Method": dwd_weather.get("solar_profile_method") if dwd_weather else None,
        "PV Profile Source": (
            "DWD"
            if pv_profile_source == "DWD"
            else (
                "Berlin fallback from PV_COP.xlsx"
                if pv_profile_source == "Berlin fallback" else None
            )
        ),
        "PV Irradiation Profile Sum (kWh/m2)": (
            float(pd.to_numeric(weather_df.get(PV_PROFILE_COLUMN), errors="coerce").sum())
            if PV_PROFILE_COLUMN in weather_df else None
        ),
        "PV Electrical Yield at 20% (kWh/m2)": (
            float(pd.to_numeric(weather_df.get(PV_PROFILE_COLUMN), errors="coerce").sum())
            * PV_ELECTRICAL_EFFICIENCY
            if PV_PROFILE_COLUMN in weather_df else None
        ),
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
    if _scenario_system_type(scenario_file) == "District heating (centralized)":
        _apply_network_temperature_assumptions(scenario_file)
    elif _scenario_system_type(scenario_file) == "Decentralized solution":
        _apply_decentral_configuration(scenario_file)
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
    yes_col, no_col = st.columns([1, 1])
    if yes_col.button(
        "Update current scenario weather",
        key=f"update_weather_{project_name}_{scenario}",
        use_container_width=True,
    ):
        try:
            weather = _update_scenario_weather_data(get_scenario_file(scenario, project_name))
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
    return sorted(
        (f[:-5] for f in os.listdir(scen_dir) if f.lower().endswith(".xlsx")),
        key=natural_sort_key,
    )

def get_scenario_file(name: str, project_name: str | None = None):
    """Get the full path to a scenario file for the given (or current) project."""
    proj = project_name or get_current_project()
    if not proj:
        raise ValueError("No project selected.")
    scen_dir = get_scenario_dir_for_project(proj)
    return os.path.join(scen_dir, f"{name}.xlsx")


def _scenario_name_validation_error(name: str) -> str | None:
    """Return a user-facing error for names unsafe as Windows filenames."""
    value = str(name or "").strip()
    if not value:
        return "Enter a name for the duplicated scenario."
    if value.lower().endswith(".xlsx"):
        return "Enter the scenario name without the .xlsx extension."
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        return "The scenario name contains a character that is not allowed in a filename."
    if value.endswith((" ", ".")):
        return "The scenario name cannot end with a space or full stop."
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if value.split(".", 1)[0].upper() in reserved:
        return "Choose another scenario name; this name is reserved by Windows."
    return None


def duplicate_scenario(
    source_name: str, new_name: str, project_name: str | None = None
) -> str:
    """Copy one complete scenario workbook under an independent new name."""
    validation_error = _scenario_name_validation_error(new_name)
    if validation_error:
        raise ValueError(validation_error)
    project = project_name or get_current_project()
    if not project:
        raise ValueError("No project is currently open.")
    source_path = get_scenario_file(source_name, project_name=project)
    target_path = get_scenario_file(new_name.strip(), project_name=project)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"Scenario '{source_name}' does not exist.")
    existing_names = {name.casefold() for name in list_scenarios(project)}
    if new_name.strip().casefold() in existing_names or os.path.exists(target_path):
        raise FileExistsError(f"Scenario '{new_name.strip()}' already exists.")

    shutil.copy2(source_path, target_path)
    update_meta_sheet(
        target_path,
        {
            "Scenario Name": new_name.strip(),
            "Duplicated From Scenario": source_name,
        },
    )
    _clear_deleted_scenario_state(target_path, new_name.strip())
    return target_path


def _capture_duplicate_scenario_form_values() -> None:
    """Persist submitted form values before Streamlit reruns the page body."""
    st.session_state["_duplicate_scenario_name_draft"] = str(
        st.session_state.get("duplicate_scenario_name") or ""
    )
    st.session_state["_duplicate_scenario_source_draft"] = str(
        st.session_state.get("duplicate_scenario_source") or ""
    )


def _remove_file_with_retry(path: str, attempts: int = 6, delay_seconds: float = 0.2):
    """Remove a file, allowing short-lived Windows readers time to release it."""
    last_error = None
    for attempt in range(max(int(attempts), 1)):
        try:
            os.remove(path)
            return
        except PermissionError as exc:
            last_error = exc
            gc.collect()
            if attempt + 1 < attempts:
                time.sleep(max(float(delay_seconds), 0.0) * (attempt + 1))
    raise last_error


def _clear_deleted_scenario_state(path: str, scenario_name: str) -> None:
    """Remove drafts/widgets that must not survive scenario-name reuse."""
    absolute_path = os.path.abspath(path)
    basename = os.path.basename(path)
    stem = Path(path).stem
    explicit_keys = {
        f"_pending_lt_demand_{stem}",  # legacy stem-only key
        f"_pending_lt_demand_{absolute_path}",
        f"_unsaved_scenario_sections_{absolute_path}",
        f"_meaningful_demand_dirty_{absolute_path}",
        f"components_{basename}",
        f"components_{basename}_file_mtime",
        f"sources_{basename}",
        f"sources_{basename}_file_mtime",
        f"tech_selection_{basename}",
        f"tech_selection_{basename}_file_mtime",
    }
    protected_form_keys = {
        "duplicate_scenario_name",
        "duplicate_scenario_source",
        "_duplicate_scenario_name_draft",
        "_duplicate_scenario_source_draft",
    }
    for key in list(st.session_state.keys()):
        if key in protected_form_keys:
            continue
        text = str(key)
        scenario_widget_key = (
            basename in text
            or text.endswith(f"_{stem}")
            or f"_{stem}_" in text
        )
        if key in explicit_keys or absolute_path in text or scenario_widget_key:
            st.session_state.pop(key, None)
    for selection_key in (
        "current_scenario_file", "last_selected_scenario", "scenario_selectbox"
    ):
        value = st.session_state.get(selection_key)
        if isinstance(value, (str, Path)) and str(value) in {
            path, absolute_path, basename, stem, scenario_name
        }:
            st.session_state.pop(selection_key, None)


def delete_scenario(name: str, project_name: str | None = None) -> bool:
    """Delete a scenario, reporting persistent Windows file locks cleanly."""
    path = get_scenario_file(name, project_name=project_name)
    if os.path.exists(path):
        try:
            _remove_file_with_retry(path)
        except PermissionError:
            st.error(
                f"Scenario '{name}' could not be deleted because its Excel file is still in use. "
                "Wait for any running simulation to finish and close the file in Excel or another "
                "program, then try again."
            )
            return False
        except OSError as exc:
            st.error(f"Scenario '{name}' could not be deleted: {exc}")
            return False
        _clear_deleted_scenario_state(path, name)
        st.success(f"Deleted scenario: {name}")
        return True
    else:
        st.error(f"Scenario '{name}' does not exist.")
        return False

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

def _normalized_dhw_profile_values(target_len, existing_values=None):
    """Load the hourly DHW shape without replacing saved data by a flat profile."""
    profile_df = st.session_state.get("dhw_load_profile")
    values = None

    if isinstance(profile_df, pd.DataFrame) and not profile_df.empty:
        if "dhw_heat_kWh" in profile_df.columns:
            values = pd.to_numeric(profile_df["dhw_heat_kWh"], errors="coerce").fillna(0).to_numpy(dtype=float)
        elif "dhw_heat_kW" in profile_df.columns:
            values = pd.to_numeric(profile_df["dhw_heat_kW"], errors="coerce").fillna(0).to_numpy(dtype=float)

    if values is None or len(values) == 0 or float(np.nansum(values)) <= 0:
        project = get_current_project()
        if project:
            try:
                persisted = pd.read_excel(
                    Path(get_project_dir(project)) / "project_data.xlsx",
                    sheet_name="dhw_profile",
                )
                column = next(
                    (
                        name for name in ("dhw_heat_kWh", "dhw_heat_kW")
                        if name in persisted.columns
                    ),
                    None,
                )
                if column:
                    persisted_values = pd.to_numeric(
                        persisted[column], errors="coerce"
                    ).fillna(0).to_numpy(dtype=float)
                    if float(np.nansum(persisted_values)) > 0:
                        values = persisted_values
            except Exception:
                pass

    if values is None or len(values) == 0 or float(np.nansum(values)) <= 0:
        existing = pd.to_numeric(
            pd.Series(existing_values, dtype="object"), errors="coerce"
        ).fillna(0).to_numpy(dtype=float)
        if len(existing) and float(np.nansum(existing)) > 0:
            values = existing

    if values is None or len(values) == 0 or float(np.nansum(values)) <= 0:
        values = np.ones(target_len, dtype=float)

    values = np.asarray(values, dtype=float)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)

    if len(values) < target_len:
        values = np.resize(values, target_len)
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
    target_col = None
    for col_idx in range(1, ws.max_column + 1):
        if str(ws.cell(row=1, column=col_idx).value or "").strip() == DHW_PROFILE_COLUMN:
            target_col = col_idx
            break
    if target_col is None:
        target_col = ws.max_column + 1

    target_len = last_timestamp_row - 1
    existing_values = [
        ws.cell(row=row_idx, column=target_col).value
        for row_idx in timestamp_rows
    ]
    values = _normalized_dhw_profile_values(
        target_len, existing_values=existing_values
    )

    ws.cell(row=1, column=target_col, value=DHW_PROFILE_COLUMN)
    for row_offset, value in enumerate(values, start=2):
        ws.cell(row=row_offset, column=target_col, value=float(value))

    for row_idx in range(len(values) + 2, ws.max_row + 1):
        ws.cell(row=row_idx, column=target_col, value=None)

    wb.save(scenario_file)


WINTER_SIMULTANEITY_MAX_BUILDINGS = 1500


def winter_simultaneity_factor(building_count) -> float:
    """Winter et al. factor, held constant above the supported building count."""
    try:
        count = max(float(building_count), 1.0)
    except (TypeError, ValueError):
        return 1.0
    if count <= 1:
        return 1.0
    # Do not extrapolate the fitted curve indefinitely. Large projects use
    # the factor at 1,500 connected buildings as a constant minimum.
    count = min(count, float(WINTER_SIMULTANEITY_MAX_BUILDINGS))
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
    if target <= feasible_minimum * (1.0 + 1e-12):
        # At the exact mathematical minimum, the former iterative cap could
        # approach a sum of one without reaching it because of rounding.
        # Return the only feasible minimum-peak profile directly.
        adjusted = np.zeros_like(profile)
        adjusted[positive] = feasible_minimum
        return adjusted
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


_SCENARIO_META_CACHE = {}


def _scenario_meta_values(scenario_file) -> dict:
    path = os.path.abspath(str(scenario_file))
    try:
        stat = os.stat(path)
        version = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        version = None
    cached = _SCENARIO_META_CACHE.get(path)
    if cached is not None and cached[0] == version:
        return dict(cached[1])
    try:
        meta = pd.read_excel(path, sheet_name="meta")
        if {"Key", "Value"}.issubset(meta.columns):
            values = dict(zip(meta["Key"].astype(str), meta["Value"]))
            _SCENARIO_META_CACHE[path] = (version, values)
            return dict(values)
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
    """Update SH profiles; peak adjustment is only used for centralized supply."""
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
    is_centralized = preview["system_type"] == "District heating (centralized)"
    if is_centralized:
        selected_peak = float(peak_load_kw) if peak_load_kw is not None else preview["estimated_peak_kw"]
        selected_peak = max(selected_peak, annual_sh / max(int((base_sh > 0).sum()), 1))
        applied_peak_factor = (
            selected_peak / preview["unconstrained_peak_kw"]
            if preview["unconstrained_peak_kw"] > 0
            else 1.0
        )
        adjusted_sh = _profile_with_peak_limit(base_sh, selected_peak / annual_sh)
    else:
        selected_peak = preview["unconstrained_peak_kw"]
        applied_peak_factor = 1.0
        adjusted_sh = base_sh
        user_edited = False

    headers = {str(sheet.cell(1, col).value): col for col in range(1, sheet.max_column + 1)}
    sh_col = headers.get("Last_SH.fix", sheet.max_column + 1)
    sheet.cell(1, sh_col, "Last_SH.fix")
    for row, value in zip(timestamp_rows, adjusted_sh):
        sheet.cell(row, sh_col, float(value))

    if is_centralized and "Loss.fix" in template_ts.columns:
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
            "Profile Adjustment Input": (
                "peak load" if user_edited
                else "building count" if is_centralized
                else "not applied (decentralized)"
            ),
            "SH Profile Method": (
                f"{profile_info.get('method', 'unknown base profile')}; "
                + (
                    "constrained by annual energy and coincident peak"
                    if is_centralized
                    else "no centralized peak adjustment"
                )
            ),
            "BDEW Building Mix": profile_info.get("mix"),
            "Loss Profile Method": (
                "70% template seasonal pattern + 30% adjusted SH load pattern"
                if is_centralized else "not applicable"
            ),
        },
    )
    preview["selected_peak_kw"] = selected_peak
    preview["applied_peak_factor"] = applied_peak_factor
    return preview


def _meta_bool(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _local_network_is_enabled(scenario_file: str) -> bool:
    return _meta_bool(
        _scenario_meta_values(scenario_file).get(
            "LT Network With Local Boosters", False
        )
    )


def _local_network_base_demand(scenario_file: str) -> pd.DataFrame:
    """Return the user-defined demand rows before LC expansion."""
    try:
        with pd.ExcelFile(scenario_file) as workbook:
            sheets = workbook.sheet_names
        sheet_name = (
            LOCAL_DEMAND_BASE_SHEET
            if LOCAL_DEMAND_BASE_SHEET in sheets
            else "demand"
        )
        demand = pd.read_excel(scenario_file, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame(
            columns=[
                "label", "active", "from", "nominal value",
                "Demand in MWh/a", "Demand in GWh/a",
            ]
        )
    if "label" not in demand.columns:
        return demand
    labels = demand["label"].fillna("").astype(str).str.strip()
    generated = labels.str.match(r"^(?:DHW|HT)_LC\d{2}$", case=False)
    return demand.loc[
        ~labels.isin({HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL})
        & ~generated
    ].copy()


def _temperature_mix(demand: pd.DataFrame) -> tuple[bool, bool]:
    buses = demand.get("from", pd.Series(dtype=str)).fillna("").astype(str)
    return bool(buses.eq("b_th_LT").any()), bool(buses.eq("b_th_HT").any())


def _save_local_network_base_demand(
    scenario_file: str, demand: pd.DataFrame
) -> None:
    with pd.ExcelWriter(
        scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
    ) as writer:
        demand.to_excel(writer, sheet_name=LOCAL_DEMAND_BASE_SHEET, index=False)


def _local_profile(values, target_len: int) -> np.ndarray:
    series = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0).clip(lower=0)
    if len(series) < target_len:
        series = pd.Series(
            np.pad(series.to_numpy(float), (0, target_len - len(series)))
        )
    else:
        series = series.iloc[:target_len].reset_index(drop=True)
    total = float(series.sum())
    if total <= 0:
        return np.full(target_len, 1.0 / max(target_len, 1), dtype=float)
    return series.to_numpy(float) / total


def _scenario_local_dhw_profile(
    scenario_file: str, time_series: pd.DataFrame, target_len: int,
    space_heating_profile: np.ndarray,
) -> np.ndarray:
    """Use saved DHW data; fall back to SH with simultaneity factor one."""
    scenario_values = None
    if DHW_PROFILE_COLUMN in time_series.columns:
        values = pd.to_numeric(
            time_series[DHW_PROFILE_COLUMN], errors="coerce"
        ).fillna(0)
        scenario_values = values
        if float(values.sum()) > 0 and values.nunique(dropna=True) > 1:
            return _local_profile(values, target_len)
    project = get_current_project()
    if project:
        project_data = Path(get_project_dir(project)) / "project_data.xlsx"
        try:
            dhw = pd.read_excel(project_data, sheet_name="dhw_profile")
            column = next(
                (name for name in ("dhw_heat_kWh", "dhw_heat_kW") if name in dhw),
                None,
            )
            if column:
                values = pd.to_numeric(dhw[column], errors="coerce").fillna(0)
                if float(values.sum()) > 0:
                    return _local_profile(values, target_len)
        except Exception:
            pass
    if scenario_values is not None and float(scenario_values.sum()) > 0:
        return _local_profile(scenario_values, target_len)
    return np.asarray(space_heating_profile, dtype=float)


def _project_local_demand_inputs() -> pd.DataFrame:
    """Load per-building SH, DHW and peak values used for imported demand rows."""
    project = get_current_project()
    if not project:
        return pd.DataFrame()
    path = Path(get_project_dir(project)) / "project_data.xlsx"
    try:
        buildings = pd.read_excel(path, sheet_name="buildings_dataset")
    except Exception:
        return pd.DataFrame()
    if "included" in buildings:
        buildings = buildings.loc[
            _to_bool_series(buildings["included"], default=True)
        ].copy()
    result = pd.DataFrame(index=buildings.index)
    result["bidx"] = buildings.get("bidx", buildings.index).astype(str)
    result["name"] = buildings.get(
        "name", pd.Series("", index=buildings.index)
    ).fillna("").astype(str)
    result["space_heating_kwh"] = pd.to_numeric(
        buildings.get("demand_kwh_final"), errors="coerce"
    ).fillna(0).clip(lower=0)
    result["space_heating_peak_kw"] = pd.to_numeric(
        buildings.get("peak_kw_final"), errors="coerce"
    ).fillna(0).clip(lower=0)
    result["dhw_kwh"] = 0.0
    try:
        dhw = pd.read_excel(path, sheet_name="dhw_estimates")
        if "bidx" in dhw and "annual_dhw_demand_kWh" in dhw:
            dhw = dhw.copy()
            dhw.index = dhw["bidx"].astype(str)
            dhw_map = pd.to_numeric(
                dhw["annual_dhw_demand_kWh"], errors="coerce"
            ).fillna(0).to_dict()
            result["dhw_kwh"] = result["bidx"].map(dhw_map).fillna(0).clip(lower=0)
    except Exception:
        pass
    return result.reset_index(drop=True)


def _local_users_from_base_demand(
    scenario_file: str, base_demand: pd.DataFrame
) -> tuple[list[dict], pd.DataFrame, int]:
    """Expand only local HT-side demand; central LT demand is not grouped."""
    time_series = pd.read_excel(scenario_file, sheet_name="time_series")
    target_len = int(
        pd.to_numeric(time_series.get("timestamp"), errors="coerce").notna().sum()
    )
    if target_len <= 0:
        raise ValueError("The scenario time_series sheet has no timestamps.")
    time_series = time_series.iloc[:target_len].copy()
    sh_profile = np.asarray(
        _scenario_base_sh_profile(scenario_file, target_len=target_len), dtype=float
    )
    sh_profile = _local_profile(sh_profile, target_len)
    dhw_profile = _scenario_local_dhw_profile(
        scenario_file, time_series, target_len, sh_profile
    )
    # Persist a repaired project profile into older scenarios whose saved
    # Last_DHW.fix column was replaced by the former uniform fallback.
    time_series[DHW_PROFILE_COLUMN] = dhw_profile
    buildings = _project_local_demand_inputs()
    users: dict[str, dict] = {}

    def add_component(
        key: str, member: str, name: str, kind: str, annual_kwh: float,
        profile: np.ndarray, peak_hint_kw: float = 0.0,
    ):
        annual = max(float(annual_kwh or 0), 0.0)
        if annual <= 0:
            return
        user = users.setdefault(
            key,
            {"members": [], "name": name, "components": [], "peak_hint_kw": 0.0},
        )
        if member not in user["members"]:
            user["members"].append(member)
        user["components"].append(
            {"kind": kind, "annual_kwh": annual, "profile": profile}
        )
        user["peak_hint_kw"] = max(
            float(user["peak_hint_kw"]), max(float(peak_hint_kw or 0), 0.0)
        )

    ht_rows = base_demand.loc[
        base_demand.get("from", pd.Series("", index=base_demand.index))
        .fillna("").astype(str).eq("b_th_HT")
    ].copy()
    for row_index, row in ht_rows.iterrows():
        label = str(row.get("label") or "Heat demand").strip()
        annual = parse_number(row.get("nominal value", 0))
        is_dhw = _is_dhw_demand_label(label)
        is_imported_sh = label == SPACE_HEATING_DEMAND_LABEL
        is_imported_dhw = label == DHW_DEMAND_LABEL
        building_column = "dhw_kwh" if is_imported_dhw else "space_heating_kwh"
        if (is_imported_sh or is_imported_dhw) and not buildings.empty:
            source_values = pd.to_numeric(
                buildings[building_column], errors="coerce"
            ).fillna(0).clip(lower=0)
            source_total = float(source_values.sum())
            if source_total > 0:
                scale = float(annual) / source_total
                for idx, building in buildings.iterrows():
                    allocated = float(source_values.loc[idx]) * scale
                    if allocated <= 0:
                        continue
                    bidx = str(building["bidx"])
                    name = str(building.get("name") or f"Building {bidx}")
                    add_component(
                        f"building:{bidx}", bidx, name,
                        "DHW" if is_imported_dhw else "HT",
                        allocated,
                        dhw_profile if is_imported_dhw else sh_profile,
                        (
                            float(building.get("space_heating_peak_kw", 0)) * scale
                            if is_imported_sh else 0.0
                        ),
                    )
                continue

        profile_column = f"{label}.fix"
        if profile_column in time_series:
            profile = _local_profile(time_series[profile_column], target_len)
        else:
            profile = dhw_profile if is_dhw else sh_profile
        add_component(
            f"manual:{row_index}:{label}", label, label,
            "DHW" if is_dhw else "HT", annual, profile,
        )
    return list(users.values()), time_series, target_len


def _group_local_users(users: list[dict], maximum_groups: int = 20) -> list[list[dict]]:
    """Group all local users into peak-similar, size-balanced clusters.

    Users are first sorted by their unshaved local HT-side peak. Adjacent users
    therefore have the most similar peak loads. All users are grouped, normally
    in clusters of three to five; exceptionally large projects remain capped at
    ``maximum_groups`` modeled clusters.
    """
    prepared = []
    for user in users:
        load = sum(
            float(component["annual_kwh"]) * np.asarray(component["profile"], dtype=float)
            for component in user["components"]
        )
        user = dict(user)
        user["calculated_peak_kw"] = float(np.max(load)) if len(load) else 0.0
        user["selection_peak_kw"] = max(
            user["calculated_peak_kw"], float(user.get("peak_hint_kw", 0.0))
        )
        prepared.append(user)
    prepared.sort(key=lambda item: item["selection_peak_kw"])
    count = len(prepared)
    if count == 0:
        return []

    # ceil(count / 5) groups keeps the normal cluster size at no more than five.
    # For six or more consumers, at least two groups are used so that the sorted
    # peak ranges remain reasonably narrow. Counts below three necessarily form
    # one small cluster because no three-consumer group is possible.
    target_groups = min(maximum_groups, max(1, int(np.ceil(count / 5))))
    quotient, remainder = divmod(count, target_groups)
    sizes = [
        quotient + (1 if index < remainder else 0)
        for index in range(target_groups)
    ]

    groups = []
    position = 0
    for size in sizes:
        groups.append(prepared[position:position + size])
        position += size
    return groups


def _aggregate_local_groups(
    grouped_users: list[list[dict]], target_len: int
) -> list[dict]:
    """Aggregate local HT users without applying a simultaneity reduction."""
    groups = []
    for number, members in enumerate(grouped_users, start=1):
        suffix = f"LC{number:02d}"
        kind_loads = {
            "HT": np.zeros(target_len, dtype=float),
            "DHW": np.zeros(target_len, dtype=float),
        }
        for member in members:
            for component in member["components"]:
                kind = component["kind"]
                kind_loads[kind] += (
                    float(component["annual_kwh"])
                    * np.asarray(component["profile"], dtype=float)
                )
        combined = kind_loads["HT"] + kind_loads["DHW"]
        calculated_group_peak = float(np.max(combined)) if len(combined) else 0.0
        summed_member_peaks = sum(
            float(member.get("selection_peak_kw", 0.0)) for member in members
        )
        design_peak = max(summed_member_peaks, calculated_group_peak)
        groups.append({
            "suffix": suffix,
            "members": [item for member in members for item in member["members"]],
            # This is a virtual model-size reduction, not a physical network
            # aggregation. Therefore the simultaneity factor is exactly 1 and
            # original local HT-side peaks are added without shaving. DHW is
            # included because it is connected to the same local HT bus.
            "peak_kw": design_peak,
            "sum_original_consumer_peaks_kw": summed_member_peaks,
            "profile_coincident_peak_kw": calculated_group_peak,
            "simultaneity_factor": 1.0,
            # Keep the price category of the largest original small user when
            # several users are aggregated; do not promote the group category.
            "category_peak_kw": max(
                (float(member["selection_peak_kw"]) for member in members),
                default=0.0,
            ),
            "ht_load": kind_loads["HT"],
            "dhw_load": kind_loads["DHW"],
            "annual_ht_kwh": float(kind_loads["HT"].sum()),
            "annual_dhw_kwh": float(kind_loads["DHW"].sum()),
        })
    return groups


def _select_local_ashp_category(hp: pd.DataFrame, peak_kw: float) -> str:
    labels = hp.get("label", pd.Series(dtype=str)).astype(str)
    candidates = hp.loc[
        labels.str.match(r"^ASHP_\d+_X$", case=False, na=False)
    ].copy()
    candidates["_minimum"] = pd.to_numeric(candidates["minimum"], errors="coerce").fillna(0)
    candidates["_maximum"] = pd.to_numeric(candidates["maximum"], errors="coerce").fillna(np.inf)
    candidates["_category"] = candidates["label"].astype(str).str.extract(
        r"^ASHP_(\d+)_X$", expand=False
    )
    selected = candidates.loc[
        candidates["_minimum"].le(float(peak_kw))
        & candidates["_maximum"].gt(float(peak_kw))
    ]
    if selected.empty:
        selected = candidates.sort_values("_maximum").tail(1)
    if selected.empty:
        raise ValueError("No central ASHP category bounds were found in the template.")
    return str(selected.iloc[0]["_category"])


def _set_central_hp_temperature_level(
    hp: pd.DataFrame, time_series: pd.DataFrame, low_temperature: bool
) -> pd.DataFrame:
    result = hp.copy()
    labels = result.get("label", pd.Series("", index=result.index)).astype(str)
    active = pd.to_numeric(
        result.get("active", pd.Series(0, index=result.index)), errors="coerce"
    ).fillna(0).eq(1)
    central = ~labels.str.contains(r"_LC(?:_|\d)", case=False, regex=True)
    for index in result.index[active & central]:
        hp_type = str(result.at[index, "type"] or "")
        if low_temperature and hp_type.endswith("_HT"):
            candidate = hp_type[:-3] + "_NT"
            if f"{candidate}.fix" not in time_series.columns:
                raise ValueError(
                    f"The active central heat pump '{result.at[index, 'label']}' "
                    f"requires the missing COP profile '{candidate}.fix'."
                )
            result.at[index, "type"] = candidate
            result.at[index, "to 1"] = "b_th_LT"
        elif not low_temperature and hp_type.endswith("_NT"):
            candidate = hp_type[:-3] + "_HT"
            if f"{candidate}.fix" in time_series.columns:
                result.at[index, "type"] = candidate
                result.at[index, "to 1"] = "b_th_HT"
    return result


def _apply_local_booster_configuration(scenario_file: str) -> dict:
    """Materialize or remove local consumers and their isolated components."""
    if _scenario_system_type(scenario_file) != "District heating (centralized)":
        return {"enabled": False, "local_consumers": 0}
    meta = _scenario_meta_values(scenario_file)
    enabled = _meta_bool(meta.get("LT Network With Local Boosters"))
    base = _local_network_base_demand(scenario_file)
    buses = pd.read_excel(scenario_file, sheet_name="buses")
    transformers = pd.read_excel(scenario_file, sheet_name="transformers")
    hp = pd.read_excel(scenario_file, sheet_name="hp")
    storages = pd.read_excel(scenario_file, sheet_name="storages")

    time_series = pd.read_excel(scenario_file, sheet_name="time_series")

    buses = buses.loc[
        ~buses.get("label", pd.Series("", index=buses.index)).astype(str)
        .str.match(r"^b_th_HT_LC\d{2}$", case=False, na=False)
    ].copy()
    transformers = transformers.loc[
        ~transformers.get("label", pd.Series("", index=transformers.index)).astype(str)
        .str.match(r"^.*_LC\d{2}$", case=False, na=False)
    ].copy()
    hp = hp.loc[
        ~hp.get("label", pd.Series("", index=hp.index)).astype(str)
        .str.match(r"^.*_LC\d{2}$", case=False, na=False)
    ].copy()
    storages = storages.loc[
        ~storages.get("label", pd.Series("", index=storages.index)).astype(str)
        .str.match(r"^Storage_th_LC\d{2}$", case=False, na=False)
    ].copy()
    local_columns = [
        column for column in time_series.columns
        if LOCAL_PROFILE_RE.search(str(column))
    ]
    time_series = time_series.drop(columns=local_columns, errors="ignore")
    hp = _set_central_hp_temperature_level(hp, time_series, enabled)

    if not enabled:
        demand = base.copy()
        if not demand.empty and {"label", "from", "nominal value"}.issubset(
            demand.columns
        ):
            demand = demand.loc[
                ~demand["label"].astype(str).isin(
                    {HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL}
                )
            ].copy()
            thermal = demand["from"].astype(str).str.match(r"^b_th_", na=False)
            thermal_total = float(
                pd.to_numeric(
                    demand.loc[thermal, "nominal value"], errors="coerce"
                ).fillna(0).sum()
            )
            if thermal_total > 0:
                demand = pd.concat(
                    [
                        demand,
                        pd.DataFrame(
                            [_demand_row(
                                HEAT_LOSS_DEMAND_LABEL,
                                thermal_total * 0.05,
                                from_bus="b_th_HT",
                            )]
                        ),
                    ],
                    ignore_index=True,
                )
        updated_meta = {
            **meta,
            "Local Consumer Count": 0,
            "Local Consumers Before Grouping": 0,
        }
        with pd.ExcelWriter(
            scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
        ) as writer:
            demand.to_excel(writer, sheet_name="demand", index=False)
            buses.to_excel(writer, sheet_name="buses", index=False)
            transformers.to_excel(writer, sheet_name="transformers", index=False)
            hp.to_excel(writer, sheet_name="hp", index=False)
            storages.to_excel(writer, sheet_name="storages", index=False)
            time_series.to_excel(writer, sheet_name="time_series", index=False)
            pd.DataFrame().to_excel(writer, sheet_name=LOCAL_CONSUMERS_SHEET, index=False)
            pd.DataFrame(
                list(updated_meta.items()), columns=["Key", "Value"]
            ).to_excel(writer, sheet_name="meta", index=False)
        return {"enabled": False, "local_consumers": 0}

    users, source_time_series, target_len = _local_users_from_base_demand(
        scenario_file, base
    )
    source_time_series = source_time_series.drop(
        columns=[
            column for column in source_time_series.columns
            if LOCAL_PROFILE_RE.search(str(column))
        ],
        errors="ignore",
    )
    grouped_users = _group_local_users(users, maximum_groups=20)
    groups = _aggregate_local_groups(grouped_users, target_len)
    technologies = {
        item.strip().upper()
        for item in str(meta.get("Local Booster Technologies") or "").split(",")
        if item.strip()
    }
    if groups and "ASHP" in technologies and "ASHP_HT.fix" not in source_time_series.columns:
        raise ValueError(
            "Local ASHPs require the missing COP profile 'ASHP_HT.fix'. "
            "Load the scenario weather data before saving local boosters."
        )
    allow_local_storage = _meta_bool(meta.get("Local Storage Enabled"))
    storage_mode = str(
        meta.get("Local Storage Capacity Mode") or "Fixed capacity per local consumer"
    )
    fixed_storage = pd.to_numeric(
        meta.get("Local Storage Max Capacity per Consumer (kWh)"), errors="coerce"
    )
    fixed_storage = float(fixed_storage) if pd.notna(fixed_storage) else 0.0

    bus_template = buses.loc[buses["label"].astype(str).eq("b_th_HT")]
    if bus_template.empty:
        raise ValueError("The centralized template has no b_th_HT bus to copy.")
    p2h_templates = transformers.loc[
        transformers["label"].astype(str).str.contains(
            r"^P2H_LC_100_[Xx]$", regex=True, na=False
        )
    ]
    storage_templates = storages.loc[
        storages["label"].astype(str).eq("Storage_th")
        & storages["bus"].astype(str).eq("b_th_HT")
    ]
    local_rows = []
    demand_rows = base.loc[
        ~base.get("from", pd.Series("", index=base.index)).fillna("")
        .astype(str).eq("b_th_HT")
    ].copy()

    # In an LT network, losses occur on the LT flow transported from the plant;
    # locally generated HT energy is not counted as district-network throughput.
    lt_annual = float(
        pd.to_numeric(
            demand_rows.loc[
                demand_rows.get("from", pd.Series("", index=demand_rows.index))
                .astype(str).eq("b_th_LT"),
                "nominal value",
            ],
            errors="coerce",
        ).fillna(0).sum()
    )
    if lt_annual > 0:
        demand_rows = pd.concat([
            demand_rows,
            pd.DataFrame([_demand_row(
                HEAT_LOSS_DEMAND_LABEL, lt_annual * 0.05, from_bus="b_th_LT"
            )]),
        ], ignore_index=True)

    for group in groups:
        suffix = group["suffix"]
        local_bus = f"b_th_HT_{suffix}"
        bus_row = bus_template.iloc[0].copy()
        bus_row["label"] = local_bus
        buses = pd.concat([buses, pd.DataFrame([bus_row])], ignore_index=True)

        for kind, annual_key, load_key in (
            ("HT", "annual_ht_kwh", "ht_load"),
            ("DHW", "annual_dhw_kwh", "dhw_load"),
        ):
            annual = float(group[annual_key])
            if annual <= 0:
                continue
            label = f"{kind}_{suffix}"
            demand_rows = pd.concat([
                demand_rows,
                pd.DataFrame([_demand_row(label, annual, from_bus=local_bus)]),
            ], ignore_index=True)
            source_time_series[f"{label}.fix"] = (
                np.asarray(group[load_key], dtype=float) / annual
            )

        category = _select_local_ashp_category(hp, group["category_peak_kw"])
        group["ashp_category"] = category
        capacity_limit = max(float(group["peak_kw"]), 1e-6)
        if "P2H" in technologies:
            if p2h_templates.empty:
                raise ValueError("The template has no P2H_LC_100_X row.")
            row = p2h_templates.iloc[0].copy()
            row["label"] = f"P2H_LC_100_{suffix}"
            row["active"] = 1
            row["to"] = local_bus
            row["minimum"] = 0.0
            row["maximum"] = capacity_limit
            transformers = pd.concat(
                [transformers, pd.DataFrame([row])], ignore_index=True
            )
        if "ASHP" in technologies:
            mask = hp["label"].astype(str).str.contains(
                rf"^ASHP_LC_{re.escape(category)}_X$", case=False, regex=True, na=False
            )
            template_rows = hp.loc[mask]
            if template_rows.empty:
                raise ValueError(f"The template has no ASHP_LC_{category}_X row.")
            row = template_rows.iloc[0].copy()
            row["label"] = f"ASHP_LC_{category}_{suffix}"
            row["active"] = 1
            row["type"] = "ASHP_HT"
            row["to 1"] = local_bus
            row["minimum"] = 0.0
            row["maximum"] = capacity_limit
            hp = pd.concat([hp, pd.DataFrame([row])], ignore_index=True)

        storage_max = 0.0
        if allow_local_storage:
            if "three days" in storage_mode.lower():
                storage_max = 3.0 * float(group["annual_dhw_kwh"]) / 365.0
            else:
                storage_max = max(fixed_storage, 0.0) * max(
                    len(group["members"]), 1
                )
            if storage_max > 0:
                if storage_templates.empty:
                    raise ValueError("The template has no HT Storage_th row to copy.")
                row = storage_templates.iloc[0].copy()
                row["label"] = f"Storage_th_{suffix}"
                row["active"] = 1
                row["bus"] = local_bus
                row["max capacity"] = storage_max
                storages = pd.concat(
                    [storages, pd.DataFrame([row])], ignore_index=True
                )
        group["storage_max_kwh"] = storage_max
        local_rows.append({
            "local consumer": suffix,
            "members": ", ".join(map(str, group["members"])),
            "local HT peak, simultaneity 1 (kW)": group["peak_kw"],
            "sum of original consumer peaks (kW)": group[
                "sum_original_consumer_peaks_kw"
            ],
            "profile-coincident local HT peak (kW)": group["profile_coincident_peak_kw"],
            "local simultaneity factor": group["simultaneity_factor"],
            "category peak (kW)": group["category_peak_kw"],
            "annual HT demand (kWh/a)": group["annual_ht_kwh"],
            "annual DHW demand (kWh/a)": group["annual_dhw_kwh"],
            "ASHP category": category if "ASHP" in technologies else None,
            "local storage maximum (kWh)": storage_max,
        })

    hp = _set_central_hp_temperature_level(hp, source_time_series, True)
    local_meta_updates = {
        "Network Supply Temperature": "LT",
        "Local Consumer Count": len(groups),
        "Local Consumers Before Grouping": len(users),
        "Local Consumer Grouping Applied": str(len(groups) < len(users)),
        "Local Consumer Grouping Method": (
            "all consumers sorted by unshaved local HT-side peak and grouped "
            "into balanced peak-similar clusters"
        ),
        "Local Consumer Simultaneity Factor": 1.0,
        "Local Peak Demand Scope": "HT and DHW demand on local HT buses only",
        "Local Generator Sizing Peak Method": (
            "sum of original unshaved consumer peaks; no coincidence or "
            "simultaneity reduction"
        ),
    }
    updated_meta = {**meta, **local_meta_updates}
    with pd.ExcelWriter(
        scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
    ) as writer:
        demand_rows.to_excel(writer, sheet_name="demand", index=False)
        buses.to_excel(writer, sheet_name="buses", index=False)
        transformers.to_excel(writer, sheet_name="transformers", index=False)
        hp.to_excel(writer, sheet_name="hp", index=False)
        storages.to_excel(writer, sheet_name="storages", index=False)
        source_time_series.to_excel(writer, sheet_name="time_series", index=False)
        pd.DataFrame(local_rows).to_excel(
            writer, sheet_name=LOCAL_CONSUMERS_SHEET, index=False
        )
        pd.DataFrame(
            list(updated_meta.items()), columns=["Key", "Value"]
        ).to_excel(writer, sheet_name="meta", index=False)
    return {
        "enabled": True,
        "local_consumers": len(groups),
        "original_consumers": len(users),
        "technologies": sorted(technologies),
    }


def _decentral_base_demand(scenario_file: str) -> pd.DataFrame:
    """Return decentralized user demand before cluster materialization."""
    try:
        with pd.ExcelFile(scenario_file) as workbook:
            sheet = (
                DECENTRAL_DEMAND_BASE_SHEET
                if DECENTRAL_DEMAND_BASE_SHEET in workbook.sheet_names
                else "demand"
            )
        demand = pd.read_excel(scenario_file, sheet_name=sheet)
    except Exception:
        return pd.DataFrame()
    labels = demand.get("label", pd.Series("", index=demand.index)).astype(str)
    return demand.loc[
        ~labels.isin({HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL})
        & ~labels.str.contains(r"_DC\d+$", case=False, regex=True, na=False)
    ].copy()


def _save_decentral_base_demand(scenario_file: str, demand: pd.DataFrame) -> None:
    with pd.ExcelWriter(
        scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
    ) as writer:
        demand.to_excel(writer, sheet_name=DECENTRAL_DEMAND_BASE_SHEET, index=False)


def _decentral_space_heating_ht_share(
    scenario_file: str, base_demand: pd.DataFrame | None = None
) -> float:
    """Return the decentralized space-heating share supplied at HT."""
    saved = pd.to_numeric(
        _scenario_meta_values(scenario_file).get(
            "Decentral Space Heating HT Share (%)"
        ),
        errors="coerce",
    )
    if pd.notna(saved):
        return min(max(float(saved) / 100.0, 0.0), 1.0)
    demand = (
        base_demand
        if isinstance(base_demand, pd.DataFrame)
        else _decentral_base_demand(scenario_file)
    )
    if not demand.empty and "label" in demand.columns:
        space_heating = demand.loc[
            demand["label"].astype(str).eq(SPACE_HEATING_DEMAND_LABEL)
        ]
        if not space_heating.empty:
            return (
                0.0
                if str(space_heating.iloc[0].get("from") or "").endswith("_LT")
                else 1.0
            )
    return 1.0


def _decentral_users_from_base_demand(
    scenario_file: str, base_demand: pd.DataFrame
) -> tuple[list[dict], pd.DataFrame, int]:
    """Expand decentralized HT and NT demand into individual modeled users."""
    time_series = pd.read_excel(scenario_file, sheet_name="time_series")
    target_len = int(
        pd.to_numeric(time_series.get("timestamp"), errors="coerce").notna().sum()
    )
    if target_len <= 0:
        target_len = len(time_series)
    time_series = time_series.iloc[:target_len].copy()
    sh_profile = _local_profile(
        _scenario_base_sh_profile(scenario_file, target_len=target_len), target_len
    )
    dhw_profile = _scenario_local_dhw_profile(
        scenario_file, time_series, target_len, sh_profile
    )
    time_series[DHW_PROFILE_COLUMN] = dhw_profile
    buildings = _project_local_demand_inputs()
    space_heating_ht_share = _decentral_space_heating_ht_share(
        scenario_file, base_demand
    )
    users: dict[str, dict] = {}

    def add_component(
        key, member, name, temperature, annual_kwh, profile, peak_hint_kw=0.0,
        demand_kind=None,
    ):
        annual = max(float(annual_kwh or 0), 0.0)
        if annual <= 0:
            return
        user = users.setdefault(
            key,
            {
                "members": [], "name": name, "temperature": temperature,
                "components": [], "peak_hint_kw": 0.0,
            },
        )
        if member not in user["members"]:
            user["members"].append(member)
        user["components"].append({
            "kind": temperature,
            "demand_kind": demand_kind or temperature,
            "annual_kwh": annual,
            "profile": profile,
        })
        user["peak_hint_kw"] = max(
            float(user["peak_hint_kw"]), max(float(peak_hint_kw or 0), 0.0)
        )

    for row_index, row in base_demand.iterrows():
        from_bus = str(row.get("from") or "b_th_HT")
        temperature = "NT" if from_bus.endswith("_LT") else "HT"
        label = str(row.get("label") or "Heat demand").strip()
        annual = parse_number(row.get("nominal value", 0))
        is_dhw = _is_dhw_demand_label(label)
        imported_sh = label == SPACE_HEATING_DEMAND_LABEL
        imported_dhw = label == DHW_DEMAND_LABEL
        building_column = "dhw_kwh" if imported_dhw else "space_heating_kwh"

        if (imported_sh or imported_dhw) and not buildings.empty:
            source_values = pd.to_numeric(
                buildings[building_column], errors="coerce"
            ).fillna(0).clip(lower=0)
            source_total = float(source_values.sum())
            if source_total > 0:
                scale = float(annual) / source_total
                for index, building in buildings.iterrows():
                    allocated = float(source_values.loc[index]) * scale
                    if allocated <= 0:
                        continue
                    bidx = str(building["bidx"])
                    portions = (
                        [("HT", 1.0)]
                        if imported_dhw else
                        [
                            ("HT", space_heating_ht_share),
                            ("NT", 1.0 - space_heating_ht_share),
                        ]
                    )
                    for portion_temperature, portion in portions:
                        if portion <= 0:
                            continue
                        add_component(
                            f"{portion_temperature}:building:{bidx}",
                            bidx,
                            str(building.get("name") or f"Building {bidx}"),
                            portion_temperature,
                            allocated * portion,
                            dhw_profile if imported_dhw else sh_profile,
                            (
                                float(building.get("space_heating_peak_kw", 0))
                                * scale * portion
                                if imported_sh else 0.0
                            ),
                            "DHW" if imported_dhw else "SH",
                        )
                continue

        profile_column = f"{label}.fix"
        profile = (
            _local_profile(time_series[profile_column], target_len)
            if profile_column in time_series
            else dhw_profile if is_dhw else sh_profile
        )
        portions = (
            [
                ("HT", space_heating_ht_share),
                ("NT", 1.0 - space_heating_ht_share),
            ]
            if imported_sh else [(temperature, 1.0)]
        )
        for portion_temperature, portion in portions:
            if portion <= 0:
                continue
            add_component(
                f"{portion_temperature}:manual:{row_index}:{label}",
                label,
                label,
                portion_temperature,
                annual * portion,
                profile,
                demand_kind="DHW" if is_dhw else "SH",
            )
    return list(users.values()), time_series, target_len


def _decentral_groups(
    users: list[dict], target_len: int,
    selected: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Create one cluster per distinct decentralized technology category.

    HT and NT portions belonging to the same original consumers use the same
    category and cluster suffix.  This keeps a split consumer on a paired HT/LT
    bus and avoids creating several identical technology components merely
    because a category contains more than five consumers.
    """
    result = []
    peak_boundaries = _decentral_common_peak_boundaries()

    prepared_users = [
        member
        for members in _group_local_users(
            users, maximum_groups=max(len(users), 1)
        )
        for member in members
    ]
    # A split space-heating consumer appears once at each temperature level.
    # Use its combined unsplit peak for the common category so both portions
    # are guaranteed to receive the same DC suffix.
    peaks_by_member: dict[str, float] = {}
    for user in prepared_users:
        members = [str(member) for member in user.get("members", [])]
        if not members:
            continue
        share = float(user.get("selection_peak_kw", 0.0)) / len(members)
        for member in members:
            peaks_by_member[member] = peaks_by_member.get(member, 0.0) + share

    selected_tags = sorted({
        tech_sheet_map[technology]
        for technologies in (selected or {}).values()
        for technology in technologies
        if technology != "PV plant" and technology in tech_sheet_map
    })
    users_by_temperature_and_category: dict[tuple[str, tuple], list[dict]] = {}
    category_order: dict[tuple, float] = {}
    for user in prepared_users:
        member_peaks = [
            peaks_by_member.get(str(member), 0.0)
            for member in user.get("members", [])
        ]
        category_peak = max(
            member_peaks,
            default=float(user.get("selection_peak_kw", 0.0)),
        )
        category_index, category_label = _decentral_common_peak_category(
            category_peak, peak_boundaries
        )
        decentral_signature = tuple(
            (
                sheet_name,
                tag,
                re.sub(
                    r"_[xX]$", "",
                    str(
                        _decentral_category_template(
                            sheet_name, tag, category_peak
                        )["label"]
                    ).strip(),
                ),
            )
            for sheet_name, tag in selected_tags
        )
        # With no selected generator, retain the central category partition.
        # Otherwise, adjacent central bands that resolve to exactly the same
        # decentralized template rows are one modeled component cluster.
        category_key = decentral_signature or (("central", category_index),)
        user = dict(user)
        user["category_selection_peak_kw"] = category_peak
        user["common_peak_category"] = category_label
        temperature = str(user.get("temperature"))
        users_by_temperature_and_category.setdefault(
            (temperature, category_key), []
        ).append(user)
        category_order[category_key] = min(
            category_order.get(category_key, category_peak), category_peak
        )

    ordered_category_keys = sorted(category_order, key=category_order.get)
    suffix_by_category = {
        category_key: f"DC{number:02d}"
        for number, category_key in enumerate(ordered_category_keys, start=1)
    }
    for temperature in ("HT", "NT"):
        category_keys = sorted(
            (
                category_key
                for candidate_temperature, category_key
                in users_by_temperature_and_category
                if candidate_temperature == temperature
            ),
            key=category_order.get,
        )
        for category_key in category_keys:
            members = users_by_temperature_and_category[
                (temperature, category_key)
            ]
            load = np.zeros(target_len, dtype=float)
            dhw_load = np.zeros(target_len, dtype=float)
            for member in members:
                for component in member["components"]:
                    component_load = float(
                        component["annual_kwh"]
                    ) * np.asarray(component["profile"], dtype=float)
                    load += component_load
                    if component.get("demand_kind") == "DHW":
                        dhw_load += component_load
            annual = float(load.sum())
            if annual <= 0:
                continue
            original_peaks = [
                float(member.get("selection_peak_kw", 0.0))
                for member in members
            ]
            category_peaks = [
                float(member.get("category_selection_peak_kw", 0.0))
                for member in members
            ]
            category_descriptions = list(dict.fromkeys(
                str(member.get("common_peak_category", "unknown"))
                for member in members
            ))
            summed_original_peaks = sum(original_peaks)
            profile_coincident_peak = float(np.max(load)) if len(load) else 0.0
            result.append({
                "suffix": suffix_by_category[category_key],
                "temperature": temperature,
                "members": [
                    item for member in members for item in member["members"]
                ],
                "annual_kwh": annual,
                "annual_dhw_kwh": float(dhw_load.sum()),
                "load": load,
                "dhw_load": dhw_load,
                "peak_kw": max(summed_original_peaks, profile_coincident_peak),
                "sum_original_consumer_peaks_kw": summed_original_peaks,
                "profile_coincident_peak_kw": profile_coincident_peak,
                "category_peak_kw": max(category_peaks, default=0.0),
                "minimum_original_peak_kw": min(original_peaks, default=0.0),
                "maximum_original_peak_kw": max(original_peaks, default=0.0),
                "common_peak_category": "; ".join(category_descriptions),
            })
    return result


def _decentral_common_peak_boundaries() -> list[float]:
    """Build common consumer peak bands from centralized category limits."""
    boundaries = {0.0}
    sheets: dict[str, pd.DataFrame] = {}
    for sheet_name, tag in dict.fromkeys(tech_sheet_map.values()):
        if sheet_name not in sheets:
            sheets[sheet_name] = pd.read_excel(
                TEMPLATE_CEN_FILE, sheet_name=sheet_name
            )
        frame = sheets[sheet_name]
        labels = frame.get("label", pd.Series("", index=frame.index)).astype(str)
        rows = frame.loc[
            labels.str.startswith(tag)
            & ~labels.str.contains(r"_LC(?:_|\d)", case=False, regex=True)
        ].copy()
        # Only labeled capacity categories participate. Internal helper rows
        # and components without a numeric category are deliberately excluded.
        category_number = pd.to_numeric(
            rows.get("label", pd.Series("", index=rows.index))
            .astype(str)
            .str.replace(r"_[xX]$", "", regex=True)
            .str.extract(r"_(\d+)$", expand=False),
            errors="coerce",
        )
        rows = rows.loc[category_number.notna()]
        for column in ("minimum", "maximum"):
            values = pd.to_numeric(rows.get(column), errors="coerce")
            for value in values.dropna():
                value = float(value)
                # Template values at or above 1e9 are open-ended sentinels,
                # not meaningful consumer-category boundaries.
                if np.isfinite(value) and 0 <= value < 1e9:
                    boundaries.add(value)
    return sorted(boundaries)


def _decentral_common_peak_category(
    peak_kw: float, boundaries: list[float]
) -> tuple[int, str]:
    """Classify one original consumer peak into a shared half-open band."""
    peak = max(float(peak_kw or 0.0), 0.0)
    limits = sorted({float(value) for value in boundaries if value >= 0})
    if not limits:
        return 0, "all peak loads"
    category_index = max(int(np.searchsorted(limits, peak, side="right")) - 1, 0)
    lower = limits[category_index]
    upper = limits[category_index + 1] if category_index + 1 < len(limits) else None
    label = (
        f"{lower:g} to <{upper:g} kW"
        if upper is not None else f"at least {lower:g} kW"
    )
    return category_index, label


def _decentral_category_template(sheet_name: str, tag: str, peak_kw: float) -> pd.Series:
    """Pick a category row exclusively from the decentralized template."""
    decentralized = pd.read_excel(TEMPLATE_DEC_FILE, sheet_name=sheet_name)
    labels = decentralized["label"].astype(str).str.strip()
    candidates = decentralized.loc[
        labels.str.startswith(tag)
        & ~labels.str.contains(r"_LC(?:_|\d)", case=False, regex=True)
    ].copy()
    if candidates.empty:
        raise ValueError(f"No decentral template category exists for {tag}.")
    helper_columns = ["_base", "_category"]
    candidates["_base"] = candidates["label"].astype(str).str.replace(
        r"_[xX]$", "", regex=True
    )
    candidates["_category"] = pd.to_numeric(
        candidates["_base"].str.extract(r"_(\d+)$", expand=False),
        errors="coerce",
    )
    if len(candidates) == 1:
        return candidates.iloc[0].drop(labels=helper_columns, errors="ignore").copy()

    candidates = candidates.sort_values("_category").reset_index(drop=True)
    categories = candidates["_category"].to_numpy(float)
    for index in range(len(candidates) - 1):
        boundary = 0.8 * categories[index + 1]
        if float(peak_kw) < boundary:
            return candidates.iloc[index].drop(
                labels=helper_columns, errors="ignore"
            )
    return candidates.iloc[-1].drop(labels=helper_columns, errors="ignore")


def _decentral_selected_technologies(scenario_file: str) -> dict[str, list[str]]:
    meta = _scenario_meta_values(scenario_file)
    try:
        selected = json.loads(str(meta.get("Decentral Technology Selection JSON") or "{}"))
        if isinstance(selected, dict):
            return {
                str(source): [str(technology) for technology in technologies]
                for source, technologies in selected.items()
                if isinstance(technologies, list)
            }
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    existing = read_existing_technologies(scenario_file, tech_sheet_map)
    try:
        sources = pd.read_excel(scenario_file, sheet_name="sources")
        labels_by_bus = {
            str(row.get("to") or ""): str(row.get("label") or "")
            for _, row in sources.iterrows()
        }
    except Exception:
        labels_by_bus = {}
    labels_by_bus["b_el"] = "el"
    inferred = {}
    for bus, technologies in existing.items():
        source = labels_by_bus.get(str(bus))
        if source and technologies:
            inferred[source] = list(technologies)
    return inferred


def _apply_decentral_configuration(scenario_file: str) -> dict:
    """Materialize isolated decentralized buses, demand, plants and storage."""
    if _scenario_system_type(scenario_file) != "Decentralized solution":
        return {"enabled": False, "clusters": 0}

    base = _decentral_base_demand(scenario_file)
    with pd.ExcelFile(scenario_file) as workbook:
        has_base_demand = DECENTRAL_DEMAND_BASE_SHEET in workbook.sheet_names
    if not has_base_demand:
        _save_decentral_base_demand(scenario_file, base)
    users, time_series, target_len = _decentral_users_from_base_demand(
        scenario_file, base
    )
    selected = _decentral_selected_technologies(scenario_file)
    groups = _decentral_groups(users, target_len, selected=selected)
    source_df = pd.read_excel(scenario_file, sheet_name="sources")
    source_bus = dict(zip(source_df["label"].astype(str), source_df["to"].astype(str)))
    source_bus["el"] = "b_el"

    buses = pd.read_excel(scenario_file, sheet_name="buses")
    demand_records = []
    transformers = pd.read_excel(scenario_file, sheet_name="transformers")
    chp = pd.read_excel(scenario_file, sheet_name="chp")
    hp = pd.read_excel(scenario_file, sheet_name="hp")
    storages = pd.read_excel(scenario_file, sheet_name="storages")

    transformers = transformers.loc[
        ~transformers["label"].astype(str).str.match(
            r"^z_HT_LT_DC\d+$", case=False, na=False
        )
    ].copy()

    buses = buses.loc[
        ~buses["label"].astype(str).str.contains(r"_DC\d+$", case=False, regex=True)
    ].copy()
    time_series = time_series.drop(
        columns=[column for column in time_series if DECENTRAL_PROFILE_RE.search(str(column))],
        errors="ignore",
    )

    tags_by_sheet = {}
    for _, (sheet_name, tag) in tech_sheet_map.items():
        tags_by_sheet.setdefault(sheet_name, set()).add(tag)
    scenario_sheets = {"transformers": transformers, "chp": chp, "hp": hp}
    for sheet_name, frame in scenario_sheets.items():
        labels = frame["label"].astype(str)
        technology_rows = pd.Series(False, index=frame.index)
        for tag in tags_by_sheet.get(sheet_name, set()):
            technology_rows |= labels.str.startswith(tag)
        canonical = pd.read_excel(TEMPLATE_DEC_FILE, sheet_name=sheet_name)
        canonical_labels = canonical["label"].astype(str)
        canonical_rows = pd.Series(False, index=canonical.index)
        for tag in tags_by_sheet.get(sheet_name, set()):
            canonical_rows |= canonical_labels.str.startswith(tag)
        scenario_sheets[sheet_name] = pd.concat(
            [frame.loc[~technology_rows], canonical.loc[canonical_rows]],
            ignore_index=True,
        )
    transformers, chp, hp = (
        scenario_sheets["transformers"], scenario_sheets["chp"], scenario_sheets["hp"]
    )

    active_storage = pd.to_numeric(
        storages.get("active", 0), errors="coerce"
    ).fillna(0).eq(1)
    meta = _scenario_meta_values(scenario_file)
    storage_enabled = _meta_bool(meta.get("Decentral Storage Enabled"))
    storage_mode = str(
        meta.get("Decentral Storage Capacity Mode")
        or "Fixed capacity per consumer"
    )
    storage_capacity = pd.to_numeric(
        meta.get(
            "Decentral Storage Max Capacity per Consumer (kWh)",
            meta.get("Decentral Storage Max Capacity per Cluster (kWh)"),
        ),
        errors="coerce",
    )
    if pd.isna(storage_capacity) and active_storage.any():
        storage_capacity = parse_number(
            storages.loc[active_storage, "max capacity"].iloc[0]
        )
        storage_enabled = float(storage_capacity) > 0
    storage_capacity = float(storage_capacity) if pd.notna(storage_capacity) else 0.0
    storages = storages.loc[
        ~active_storage
        & ~storages["label"].astype(str).str.contains(
            r"_DC\d+$", case=False, regex=True
        )
    ].copy()

    bus_templates = {
        temperature: buses.loc[buses["label"].astype(str).eq(f"b_th_{temperature}")]
        for temperature in ("HT", "LT")
    }
    storage_templates = pd.read_excel(TEMPLATE_DEC_FILE, sheet_name="storages")
    link_templates = pd.read_excel(TEMPLATE_DEC_FILE, sheet_name="transformers")
    link_template = link_templates.loc[
        link_templates["label"].astype(str).str.fullmatch(
            "z_HT_LT", case=False, na=False
        )
    ]
    temperatures_by_suffix: dict[str, set[str]] = {}
    for group in groups:
        temperatures_by_suffix.setdefault(group["suffix"], set()).add(
            group["temperature"]
        )
    split_suffixes = {
        suffix
        for suffix, temperatures in temperatures_by_suffix.items()
        if {"HT", "NT"}.issubset(temperatures)
    }
    storage_temperature_by_suffix = {
        suffix: "HT" if "HT" in temperatures else "NT"
        for suffix, temperatures in temperatures_by_suffix.items()
    }
    records = []
    for group in groups:
        suffix = group["suffix"]
        profile_level = group["temperature"]
        bus_level = "LT" if profile_level == "NT" else "HT"
        bus = f"b_th_{bus_level}_{suffix}"
        template_bus = bus_templates[bus_level]
        if template_bus.empty:
            raise ValueError(f"The decentral template has no b_th_{bus_level} bus.")
        bus_row = template_bus.iloc[0].copy()
        bus_row["label"] = bus
        buses = pd.concat([buses, pd.DataFrame([bus_row])], ignore_index=True)

        # DHW remains part of the HT cluster demand. Its annual amount is kept
        # separately in the cluster metadata for storage sizing and review.
        demand_label = f"Demand_{profile_level}_{suffix}"
        demand_records.append(
            _demand_row(demand_label, group["annual_kwh"], from_bus=bus)
        )
        time_series[f"{demand_label}.fix"] = (
            group["load"] / group["annual_kwh"]
        )

        # A paired LT demand is supplied from its HT bus through z_HT_LT.  It
        # must not receive a second copy of every generator technology.
        add_generation = not (
            profile_level == "NT" and suffix in split_suffixes
        )
        for source, technologies in (
            selected.items() if add_generation else []
        ):
            for technology in technologies:
                if technology == "PV plant" or technology not in tech_sheet_map:
                    continue
                sheet_name, tag = tech_sheet_map[technology]
                row = _decentral_category_template(
                    sheet_name, tag, group["category_peak_kw"]
                ).copy()
                base_label = re.sub(r"_[xX]$", "", str(row["label"]).strip())
                biomass_suffix = {"LPM": "LPM", "BM_W": "W", "BM_P": "P"}.get(source)
                if biomass_suffix:
                    base_label = f"{base_label}_{biomass_suffix}"
                row["label"] = f"{base_label}_{suffix}"
                row["active"] = 1
                if "from" in row.index:
                    row["from"] = source_bus.get(source, row.get("from"))
                if sheet_name == "transformers":
                    row["to"] = bus
                    transformers = pd.concat(
                        [transformers, pd.DataFrame([row])], ignore_index=True
                    )
                elif sheet_name == "chp":
                    row["to 1"] = bus
                    row["to 2"] = "b_el_CHP"
                    chp = pd.concat([chp, pd.DataFrame([row])], ignore_index=True)
                elif sheet_name == "hp":
                    row["to 1"] = bus
                    hp_type = re.sub(r"_(?:HT|NT)$", "", str(row.get("type") or tag))
                    row["type"] = f"{hp_type}_{profile_level}"
                    profile_column = f"{row['type']}.fix"
                    if profile_column not in time_series.columns:
                        raise ValueError(
                            f"The selected decentral heat pump requires missing COP profile "
                            f"'{profile_column}'."
                        )
                    hp = pd.concat([hp, pd.DataFrame([row])], ignore_index=True)

        storage_max = 0.0
        add_storage = (
            storage_enabled
            and profile_level == storage_temperature_by_suffix.get(suffix)
        )
        if add_storage:
            if "three days" in storage_mode.lower():
                storage_max = 3.0 * float(group["annual_dhw_kwh"]) / 365.0
            else:
                storage_max = storage_capacity * max(
                    len(group["members"]), 1
                )
        if storage_max > 0:
            storage_template = storage_templates.loc[
                storage_templates["bus"].astype(str).eq(f"b_th_{bus_level}")
            ]
            if storage_template.empty:
                storage_template = storage_templates.iloc[[0]]
            storage_row = storage_template.iloc[0].copy()
            storage_row["label"] = f"Storage_th_{suffix}"
            storage_row["active"] = 1
            storage_row["bus"] = bus
            storage_row["max capacity"] = storage_max
            storages = pd.concat(
                [storages, pd.DataFrame([storage_row])], ignore_index=True
            )

        records.append({
            "consumer cluster": suffix,
            "temperature level": profile_level,
            "members": ", ".join(map(str, group["members"])),
            "annual demand (kWh/a)": group["annual_kwh"],
            "annual DHW demand (kWh/a)": group["annual_dhw_kwh"],
            "unshaved peak (kW)": group["peak_kw"],
            "sum of original consumer peaks (kW)": group[
                "sum_original_consumer_peaks_kw"
            ],
            "profile-coincident cluster peak (kW)": group[
                "profile_coincident_peak_kw"
            ],
            "common original-consumer peak category": group[
                "common_peak_category"
            ],
            "minimum original consumer peak (kW)": group[
                "minimum_original_peak_kw"
            ],
            "maximum original consumer peak (kW)": group[
                "maximum_original_peak_kw"
            ],
            "generator category reference peak (kW)": group[
                "category_peak_kw"
            ],
            "storage maximum (kWh)": storage_max,
        })

    if split_suffixes and link_template.empty:
        raise ValueError("The decentral template has no z_HT_LT row.")
    for suffix in sorted(split_suffixes):
        row = link_template.iloc[0].copy()
        row["label"] = f"z_HT_LT_{suffix}"
        row["active"] = 1
        row["from"] = f"b_th_HT_{suffix}"
        row["to"] = f"b_th_LT_{suffix}"
        transformers = pd.concat(
            [transformers, pd.DataFrame([row])], ignore_index=True
        )

    demand = pd.DataFrame(demand_records)
    if demand.empty:
        demand = pd.DataFrame(columns=base.columns)
    else:
        ordered_columns = list(dict.fromkeys([*base.columns, *demand.columns]))
        demand = demand.reindex(columns=ordered_columns)

    decentral_meta_updates = {
        "Decentral Consumer Count": len({group["suffix"] for group in groups}),
        "Decentral Consumers Before Grouping": len(users),
        "Decentral Consumer Grouping Applied": str(len(groups) < len(users)),
        "Decentral Consumer Simultaneity Factor": 1.0,
        "Decentral Generator Sizing Peak Method": (
            "sum of original unshaved consumer peaks; no coincidence or "
            "simultaneity reduction; decentralized template capacity limits "
            "are retained unchanged, and original consumer peak is used only "
            "for cost-category selection"
        ),
        "Decentral Consumer Grouping Method": (
            "HT and NT portions classified by common original-consumer peak "
            "bands derived from centralized generator minimum/maximum limits, "
            "then grouped by the actual selected decentralized-template "
            "technology-row combination so adjacent bands with identical "
            "decentralized categories share one cluster; "
            "split HT and NT portions share a cluster suffix and are connected "
            "by a decentralized-template z_HT_LT component; generator cost "
            "category selected from an original consumer peak, never the "
            "summed cluster peak"
        ),
        "Decentral Common Peak Category Boundaries (kW)": json.dumps(
            _decentral_common_peak_boundaries()
        ),
        "Decentral Space Heating HT Share (%)": (
            100.0 * _decentral_space_heating_ht_share(scenario_file, base)
        ),
    }
    updated_meta = {**meta, **decentral_meta_updates}
    with pd.ExcelWriter(
        scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
    ) as writer:
        buses.to_excel(writer, sheet_name="buses", index=False)
        demand.to_excel(writer, sheet_name="demand", index=False)
        transformers.to_excel(writer, sheet_name="transformers", index=False)
        chp.to_excel(writer, sheet_name="chp", index=False)
        hp.to_excel(writer, sheet_name="hp", index=False)
        storages.to_excel(writer, sheet_name="storages", index=False)
        time_series.to_excel(writer, sheet_name="time_series", index=False)
        pd.DataFrame(records).to_excel(
            writer, sheet_name=DECENTRAL_CONSUMERS_SHEET, index=False
        )
        pd.DataFrame(
            list(updated_meta.items()), columns=["Key", "Value"]
        ).to_excel(writer, sheet_name="meta", index=False)
    _configure_chp_feed_in(scenario_file)
    return {
        "enabled": True,
        "clusters": len({group["suffix"] for group in groups}),
        "original_consumers": len(users),
    }


def _local_booster_validation_error(scenario_file: str) -> str | None:
    if _scenario_system_type(scenario_file) != "District heating (centralized)":
        return None
    base = _local_network_base_demand(scenario_file)
    has_lt, has_ht = _temperature_mix(base)
    meta = _scenario_meta_values(scenario_file)
    if has_lt and has_ht and not _meta_bool(meta.get("LT Temperature Decision Saved")):
        return "choose whether the mixed HT/LT demand uses an LT network with local boosters"
    if _meta_bool(meta.get("LT Network With Local Boosters")) and has_ht:
        technologies = {
            item.strip().upper()
            for item in str(meta.get("Local Booster Technologies") or "").split(",")
            if item.strip()
        }
        if not technologies.intersection(LOCAL_BOOSTER_OPTIONS):
            return "select at least one local booster technology (P2H or ASHP)"
    return None


def _save_demand_and_apply_temperature_configuration(
    scenario_file: str, demand: pd.DataFrame
) -> dict:
    """Persist user rows, maintain the LT decision, and rebuild generated rows."""
    is_central = (
        _scenario_system_type(scenario_file) == "District heating (centralized)"
    )
    if not is_central:
        if _scenario_system_type(scenario_file) == "Decentralized solution":
            _save_decentral_base_demand(scenario_file, demand)
            # The caller refreshes the annual profiles and materializes the
            # decentralized clusters once afterwards. Doing it here as well
            # rebuilt and rewrote the full time-series sheet twice per save.
            return {"enabled": True, "clusters": 0, "deferred": True}
        with pd.ExcelWriter(
            scenario_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
        ) as writer:
            demand.to_excel(writer, sheet_name="demand", index=False)
        return {"enabled": False, "local_consumers": 0}

    has_lt, has_ht = _temperature_mix(demand)
    meta = _scenario_meta_values(scenario_file)
    decision_saved = _meta_bool(meta.get("LT Temperature Decision Saved"))
    if has_lt and has_ht:
        # Keep an already saved choice, but require a choice for a newly mixed
        # scenario. The demand page renders the decision immediately below.
        updates = {
            "LT Temperature Decision Saved": str(decision_saved),
        }
    elif has_lt:
        updates = {
            "LT Network With Local Boosters": "True",
            "LT Temperature Decision Saved": "True",
            "Network Supply Temperature": "LT",
        }
    else:
        updates = {
            "LT Network With Local Boosters": "False",
            "LT Temperature Decision Saved": "True",
            "Network Supply Temperature": "HT",
        }
    update_meta_sheet(scenario_file, updates)
    _save_local_network_base_demand(scenario_file, demand)
    return _apply_local_booster_configuration(scenario_file)


def _pending_lt_demand_key(scenario_file: str) -> str:
    return f"_pending_lt_demand_{os.path.abspath(scenario_file)}"


def _refresh_profiles_after_demand_save(scenario_file: str) -> None:
    if _scenario_system_type(scenario_file) == "Decentralized solution":
        # Decentralized profiles are not peak-shaved by building count. Their
        # existing weather-derived SH shape can therefore be reused directly;
        # only the newly saved annual demand and HT/LT split need clustering.
        _apply_decentral_configuration(scenario_file)
        return
    meta = _scenario_meta_values(scenario_file)
    count_raw = pd.to_numeric(meta.get("Building Count"), errors="coerce")
    profile_count = int(count_raw) if pd.notna(count_raw) and count_raw > 0 else None
    peak_was_edited = _meta_bool(meta.get("Peak Load User Edited"))
    stored_peak = pd.to_numeric(
        meta.get("Space Heating Peak Load (kW)"), errors="coerce"
    )
    _update_scenario_load_profiles(
        scenario_file,
        building_count=profile_count,
        peak_load_kw=(
            float(stored_peak)
            if peak_was_edited and pd.notna(stored_peak)
            else None
        ),
        user_edited=peak_was_edited,
    )


def _render_lt_network_choice(
    scenario_file: str, pending_demand: pd.DataFrame | None = None
) -> None:
    """Ask for the mixed-temperature network choice only when it is relevant."""
    if _scenario_system_type(scenario_file) != "District heating (centralized)":
        return
    base = (
        pending_demand.copy()
        if isinstance(pending_demand, pd.DataFrame)
        else _local_network_base_demand(scenario_file)
    )
    has_lt, has_ht = _temperature_mix(base)
    if not (has_lt and has_ht):
        return
    meta = _scenario_meta_values(scenario_file)
    current_enabled = _meta_bool(meta.get("LT Network With Local Boosters"))
    decision_saved = _meta_bool(meta.get("LT Temperature Decision Saved"))
    has_pending_draft = isinstance(pending_demand, pd.DataFrame)
    explanation = (
        "This centralized scenario contains both LT and HT demand. The heating "
        "center can either supply an LT network with local booster plants for HT "
        "consumers, or supply the whole network at HT."
        + (
            " The demand configuration has not been written to the scenario file yet."
            if has_pending_draft
            else ""
        )
    )
    options = [
        "Use an LT network and local booster plants",
        "Supply the whole network at HT",
    ]
    default = 0 if current_enabled else 1

    def render_choice_form():
        with st.form(f"lt_network_choice_{os.path.abspath(scenario_file)}"):
            selected_choice = st.radio(
                "How should the mixed-temperature demand be supplied?",
                options,
                index=default,
            )
            submitted_choice = st.form_submit_button(
                "Save network temperature choice"
                if decision_saved else "Confirm choice"
            )
        return selected_choice, submitted_choice

    st.divider()
    if decision_saved and not has_pending_draft:
        saved_label = (
            "LT network with local booster plants"
            if current_enabled else "HT network"
        )
        with st.expander(
            f"Network supply temperature: {saved_label}", expanded=False
        ):
            st.caption(explanation)
            choice, save_choice = render_choice_form()
    else:
        st.subheader("Network supply temperature")
        st.warning(explanation)
        choice, save_choice = render_choice_form()
    if save_choice:
        use_boosters = choice.startswith("Use an LT")
        demand = base.copy()
        if not use_boosters:
            demand.loc[
                demand.get("from", pd.Series("", index=demand.index))
                .fillna("").astype(str).eq("b_th_LT"),
                "from",
            ] = "b_th_HT"
        try:
            _save_local_network_base_demand(scenario_file, demand)
            update_meta_sheet(
                scenario_file,
                {
                    "LT Network With Local Boosters": str(use_boosters),
                    "LT Temperature Decision Saved": "True",
                    "Network Supply Temperature": "LT" if use_boosters else "HT",
                },
            )
            _apply_local_booster_configuration(scenario_file)
            _refresh_profiles_after_demand_save(scenario_file)
            _apply_network_temperature_assumptions(scenario_file)
        except Exception as exc:
            st.error(f"The demand configuration could not be saved: {exc}")
            return
        st.session_state.pop(_pending_lt_demand_key(scenario_file), None)
        _set_scenario_section_dirty(scenario_file, "Demand", False)
        st.success(
            "LT network with local boosters selected."
            if use_boosters
            else "All thermal demand is now supplied as HT demand."
        )
        _clear_demand_editor_state(scenario_file, defer_widget_clear=True)
        st.rerun()

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
    st.success(f"✅ The scenario-specific file for '{name}' was created.")

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

    # Load the representative-city template as the documented COP fallback.
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
    # Representative-city data continue to provide offline COP profiles. PV
    # is added only from project DWD observations or, once PV is selected, the
    # explicit Berlin fallback.
    weather_df = weather_df.drop(columns=[PV_PROFILE_COLUMN], errors="ignore")

    # Prefer coordinate-based DWD temperature and solar observations. The
    # representative-city file remains the offline fallback.
    dwd_weather = st.session_state.get("dwd_weather")
    if (
        not isinstance(dwd_weather, dict)
        or dwd_weather.get("pv_profile_unit") != "kWh/m2 global irradiation"
    ):
        dwd_weather = None
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
        if dwd_weather.get("pv_profile") is not None:
            weather_df[PV_PROFILE_COLUMN] = _fit_pv_profile(
                dwd_weather["pv_profile"], len(weather_df)
            )
        st.session_state["design_outdoor_temperature_c"] = dwd_weather[
            "design_outdoor_temperature_c"
        ]
        st.session_state["ground_temperature_c"] = dwd_weather["ground_temperature_c"]
        st.session_state["scenario_creation_message"] = (
            f"DWD hourly weather from {dwd_weather['station_name']} "
            f"(station {dwd_weather['station_id']}; {dwd_weather['profile_year_count']} averaged years: "
            f"{', '.join(map(str, dwd_weather['profile_years_used']))}; chronology {dwd_weather['year']}) "
            "is used for BDEW space-heating profile generation and heat-pump COP. "
            + (
                f"DWD solar data from {dwd_weather['solar_station_name']} are used for PV."
                if dwd_weather.get("pv_profile") is not None
                else (
                    "No DWD solar profile is available. If PV is selected, "
                    "the Berlin fallback profile will be generated."
                )
            )
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
            if (
                PV_PROFILE_COLUMN not in weather_df
                and PV_PROFILE_COLUMN in existing_columns
            ):
                pv_col_idx = existing_columns[PV_PROFILE_COLUMN]
                for row_idx in range(2, ws.max_row + 1):
                    ws.cell(row=row_idx, column=pv_col_idx).value = None

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
        meta_ws["A23"] = "DWD Solar Station"
        meta_ws["B23"] = dwd_weather.get("solar_station_name") if dwd_weather else None
        meta_ws["A24"] = "DWD Solar Station ID"
        meta_ws["B24"] = dwd_weather.get("solar_station_id") if dwd_weather else None
        meta_ws["A25"] = "DWD Solar Weather Year"
        meta_ws["B25"] = dwd_weather.get("solar_year") if dwd_weather else None
        meta_ws["A26"] = "DWD Solar Profile Years Used"
        meta_ws["B26"] = (
            ", ".join(map(str, dwd_weather.get("solar_profile_years_used", [])))
            if dwd_weather else None
        )
        meta_ws["A27"] = "DWD Solar Profile Method"
        meta_ws["B27"] = dwd_weather.get("solar_profile_method") if dwd_weather else None
        meta_ws["A28"] = "PV Irradiation Profile Sum (kWh/m2)"
        meta_ws["B28"] = (
            float(pd.to_numeric(weather_df[PV_PROFILE_COLUMN], errors="coerce").sum())
            if PV_PROFILE_COLUMN in weather_df else None
        )
        meta_ws["A29"] = "PV Electrical Yield at 20% (kWh/m2)"
        meta_ws["B29"] = (
            float(pd.to_numeric(weather_df[PV_PROFILE_COLUMN], errors="coerce").sum())
            * PV_ELECTRICAL_EFFICIENCY
            if PV_PROFILE_COLUMN in weather_df else None
        )
        meta_ws["A30"] = "DWD Solar Station Latitude"
        meta_ws["B30"] = dwd_weather.get("solar_station_latitude") if dwd_weather else None
        meta_ws["A31"] = "DWD Solar Station Longitude"
        meta_ws["B31"] = dwd_weather.get("solar_station_longitude") if dwd_weather else None
        meta_ws["A32"] = "DWD Solar Station Distance (km)"
        meta_ws["B32"] = dwd_weather.get("solar_station_distance_km") if dwd_weather else None

        # Record the physical roof potential in the metadata that is already
        # being written during scenario creation. Do not run the PV component
        # configurator here: no generation technology has been selected yet,
        # so the copied template rows must remain untouched.
        if solution_type == "District heating (centralized)":
            pv_roof_area = _default_pv_roof_area_m2(current_project)
            pv_profile = pd.to_numeric(
                weather_df[PV_PROFILE_COLUMN], errors="coerce"
            ).clip(lower=0) if PV_PROFILE_COLUMN in weather_df else pd.Series(dtype=float)
            irradiation_peak = pv_profile.max()
            pv_peak_kw_per_m2 = (
                float(irradiation_peak) * PV_ELECTRICAL_EFFICIENCY
                if pd.notna(irradiation_peak) and float(irradiation_peak) > 0
                else float(_berlin_pv_profile(weather_target_len).max())
                * PV_ELECTRICAL_EFFICIENCY
            )
            meta_ws["A33"] = "Maximum PV Roof Area (m2)"
            meta_ws["B33"] = pv_roof_area
            meta_ws["A34"] = "Maximum PV Collector Area (m2)"
            meta_ws["B34"] = pv_roof_area
            meta_ws["A35"] = "Maximum PV Peak Capacity (kWp)"
            meta_ws["B35"] = pv_roof_area * pv_peak_kw_per_m2
            meta_ws["A36"] = "PV Roof Peak Potential (kWp)"
            meta_ws["B36"] = pv_roof_area * pv_peak_kw_per_m2
            meta_ws["A37"] = "PV Enabled"
            meta_ws["B37"] = "False"
            meta_ws["A38"] = "PV Roof Area Basis"
            meta_ws["B38"] = "40% of included building footprint/ground area"
            meta_ws["A39"] = "PV Peak Power Density (kWp/m2)"
            meta_ws["B39"] = pv_peak_kw_per_m2
            meta_ws["A40"] = "PV Peak Method"
            meta_ws["B40"] = (
                "maximum local hourly DWD irradiation x 20% efficiency"
                if PV_PROFILE_COLUMN in weather_df
                else (
                    "maximum hourly Berlin fallback irradiation x 20% efficiency; "
                    "the fallback profile will be written only if PV is selected"
                )
            )
            meta_ws["A41"] = "PV Electrical Efficiency"
            meta_ws["B41"] = PV_ELECTRICAL_EFFICIENCY

        meta_ws["A42"] = "Ground Temperature Depth (cm)"
        meta_ws["B42"] = dwd_weather.get("ground_temperature_depth_cm") if dwd_weather else None
        meta_ws["A43"] = "Ground Temperature Method"
        meta_ws["B43"] = dwd_weather.get("ground_method") if dwd_weather else None
        meta_ws["A44"] = "DWD Soil Station"
        meta_ws["B44"] = dwd_weather.get("soil_station_name") if dwd_weather else None
        meta_ws["A45"] = "DWD Soil Station ID"
        meta_ws["B45"] = dwd_weather.get("soil_station_id") if dwd_weather else None
        meta_ws["A46"] = "DWD Soil Station Distance (km)"
        meta_ws["B46"] = dwd_weather.get("soil_station_distance_km") if dwd_weather else None

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


def _clear_demand_editor_state(excel_file: str, defer_widget_clear: bool = False) -> None:
    """Discard cached demand rows and their position-based widget values."""
    scenario_key = f"components_{os.path.basename(excel_file)}"
    reset_key = f"_reset_widgets_{scenario_key}"
    st.session_state.pop(scenario_key, None)
    st.session_state.pop(f"{scenario_key}_file_mtime", None)
    if defer_widget_clear:
        st.session_state[reset_key] = True
        return
    st.session_state.pop(reset_key, None)
    st.session_state.pop(
        f"decentral_sh_temperature_mode_{scenario_key}", None
    )
    st.session_state.pop(f"decentral_sh_split_enabled_{scenario_key}", None)
    st.session_state.pop(f"decentral_sh_ht_share_{scenario_key}", None)
    widget_prefixes = ("d_label_", "d_value_", "d_type_", "d_type_dhw_locked_")
    widget_suffix = f"_{scenario_key}"
    for key in list(st.session_state.keys()):
        if key.startswith(widget_prefixes) and key.endswith(widget_suffix):
            st.session_state.pop(key, None)
            st.session_state.pop(f"_last_valid_number_{key}", None)
            st.session_state.pop(f"_number_draft_{key}", None)


def _scenario_dirty_state_key(excel_file: str) -> str:
    return f"_unsaved_scenario_sections_{os.path.abspath(excel_file)}"


def _meaningful_demand_dirty_key(excel_file: str) -> str:
    return f"_meaningful_demand_dirty_{os.path.abspath(excel_file)}"


def _set_scenario_section_dirty(excel_file: str, section: str, dirty: bool = True) -> None:
    key = _scenario_dirty_state_key(excel_file)
    sections = set(st.session_state.get(key, []))
    if dirty:
        sections.add(section)
    else:
        sections.discard(section)
    if sections:
        st.session_state[key] = sorted(sections)
    else:
        st.session_state.pop(key, None)


def _scenario_unsaved_sections(excel_file: str) -> list[str]:
    sections = list(st.session_state.get(_scenario_dirty_state_key(excel_file), []))
    meaningful_demand_dirty = st.session_state.get(
        _meaningful_demand_dirty_key(excel_file)
    )
    if (
        "Demand" in sections
        and meaningful_demand_dirty is False
    ):
        sections.remove("Demand")
        _set_scenario_section_dirty(excel_file, "Demand", False)
    return sections


def _clear_source_editor_state(excel_file: str, defer_widget_clear: bool = False) -> None:
    scenario_key = f"sources_{os.path.basename(excel_file)}"
    reset_key = f"_reset_widgets_{scenario_key}"
    st.session_state.pop(scenario_key, None)
    st.session_state.pop(f"{scenario_key}_file_mtime", None)
    if defer_widget_clear:
        st.session_state[reset_key] = True
        return
    st.session_state.pop(reset_key, None)
    prefixes = (
        "src_label_", "src_costs_", "src_emission_", "src_nominal_",
        "src_am_", "src_full_",
    )
    suffix = f"_{scenario_key}"
    for key in list(st.session_state):
        if key.startswith(prefixes) and key.endswith(suffix):
            st.session_state.pop(key, None)
            st.session_state.pop(f"_last_valid_number_{key}", None)
            st.session_state.pop(f"_number_draft_{key}", None)


def _source_values_equal(left, right, numeric: bool) -> bool:
    if numeric:
        left_missing = left is None or pd.isna(left)
        right_missing = right is None or pd.isna(right)
        if left_missing or right_missing:
            return left_missing and right_missing
        return bool(np.isclose(parse_number(left), parse_number(right), rtol=0, atol=1e-12))
    return str(left or "").strip() == str(right or "").strip()


def _save_source_rows(excel_file: str, existing: pd.DataFrame, updated: pd.DataFrame) -> None:
    """Preserve untouched source cells; replace the sheet only for structural edits."""
    columns = [
        "label", "active", "variable costs", "emission factor",
        "nominal value", "full load time", "annual max", "to",
    ]
    old_labels = existing.get("label", pd.Series(dtype=str)).fillna("").astype(str).str.strip().tolist()
    new_labels = updated.get("label", pd.Series(dtype=str)).fillna("").astype(str).str.strip().tolist()
    if old_labels != new_labels:
        with pd.ExcelWriter(
            excel_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
        ) as writer:
            updated.to_excel(writer, sheet_name="sources", index=False)
        return

    workbook = openpyxl.load_workbook(excel_file)
    sheet = workbook["sources"]
    headers = {
        str(sheet.cell(1, column).value).strip(): column
        for column in range(1, sheet.max_column + 1)
        if sheet.cell(1, column).value is not None
    }
    numeric_columns = {
        "active", "variable costs", "emission factor",
        "nominal value", "full load time", "annual max",
    }
    for index, (_, new_row) in enumerate(updated.iterrows(), start=2):
        old_row = existing.iloc[index - 2]
        for column in columns:
            if column not in headers:
                continue
            old_value = old_row.get(column)
            new_value = new_row.get(column)
            if _source_values_equal(old_value, new_value, column in numeric_columns):
                continue
            if column in numeric_columns:
                new_value = None if pd.isna(new_value) else float(new_value)
            sheet.cell(index, headers[column], new_value)
    workbook.save(excel_file)


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
    """Update scenario metadata with one workbook load and at most one save."""

    def excel_value(value):
        if isinstance(value, np.generic):
            value = value.item()
        if value is pd.NA:
            return None
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return value

    def values_equal(left, right):
        if left is None or right is None:
            return left is None and right is None
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return bool(np.isclose(float(left), float(right), rtol=0, atol=1e-12))
        return left == right

    workbook = openpyxl.load_workbook(scenario_file)
    try:
        changed = False
        if "meta" not in workbook.sheetnames:
            sheet = workbook.create_sheet("meta")
            sheet.append(["Key", "Value"])
            changed = True
        else:
            sheet = workbook["meta"]

        headers = {
            str(sheet.cell(1, column).value or "").strip(): column
            for column in range(1, sheet.max_column + 1)
        }
        if "Key" not in headers or "Value" not in headers:
            workbook.remove(sheet)
            sheet = workbook.create_sheet("meta")
            sheet.append(["Key", "Value"])
            headers = {"Key": 1, "Value": 2}
            changed = True

        key_column = headers["Key"]
        value_column = headers["Value"]
        key_rows = {
            str(sheet.cell(row, key_column).value): row
            for row in range(2, sheet.max_row + 1)
            if sheet.cell(row, key_column).value is not None
        }
        for raw_key, raw_value in new_meta_dict.items():
            key = str(raw_key)
            value = excel_value(raw_value)
            row = key_rows.get(key)
            if row is None:
                row = sheet.max_row + 1
                sheet.cell(row, key_column, key)
                sheet.cell(row, value_column, value)
                key_rows[key] = row
                changed = True
            elif not values_equal(sheet.cell(row, value_column).value, value):
                sheet.cell(row, value_column, value)
                changed = True

        if changed:
            workbook.save(scenario_file)
    finally:
        workbook.close()

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

def dot_number_input(
        label: str,
        value: float | int | str,
        key: str,
        help: str | None = None,
        decimals: int | None = None,
        on_change=None,
        args=None,
):
    """
    Text-based numeric input that always displays a dot-decimal.
    Returns a float. Accepts both "." and "," from the user; displays ".".
    """
    try:
        number = float(str(value).replace(',', '.'))
        init = f"{number:.{decimals}f}" if decimals is not None else f"{number:.12g}"
    except Exception:
        number = 0.0
        init = f"{0:.{decimals}f}" if decimals is not None else "0"
    shadow_key = f"_number_draft_{key}"
    if shadow_key not in st.session_state:
        st.session_state[shadow_key] = number

    # Streamlit can retain the widget key while clearing its text during a
    # rerun. Restore it from a non-widget shadow value before instantiating the
    # widget, so the visible field never becomes blank.
    current_raw = st.session_state.get(key)
    try:
        current_number = float(str(current_raw).replace(",", "."))
        current_is_valid = str(current_raw).strip() != ""
    except (TypeError, ValueError):
        current_number = float(st.session_state[shadow_key])
        current_is_valid = False
    if not current_is_valid:
        current_number = float(st.session_state[shadow_key])
        st.session_state[key] = (
            f"{current_number:.{decimals}f}"
            if decimals is not None
            else f"{current_number:.12g}"
        )
    elif decimals is not None:
        st.session_state[key] = f"{current_number:.{decimals}f}"

    raw = st.text_input(
        label, key=key, help=help, on_change=on_change, args=args
    )
    # live-normalize commas to dots for display-then-parse
    normalized = raw.replace(",", ".")
    try:
        parsed = float(normalized)
        st.session_state[shadow_key] = parsed
        st.session_state[f"_last_valid_number_{key}"] = parsed
        return parsed
    except Exception:
        return float(st.session_state.get(shadow_key, number))

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
            labels = df["label"].astype(str)
            df = df[
                (df["active"] == 1)
                & (~labels.str.contains("z_"))
                & (~labels.str.contains(r"_LC(?:_|\d)", case=False, regex=True))
                & (labels.str.startswith(tag))
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
    if source == PV_SOURCE_DISPLAY:
        try:
            renewables = pd.read_excel(excel_file, sheet_name="renewables")
            transformers = pd.read_excel(excel_file, sheet_name="transformers")
            renewable_labels = renewables.get(
                "label", pd.Series(dtype=str)
            ).astype(str).str.replace(r"_X$", "", regex=True)
            transformer_labels = transformers.get(
                "label", pd.Series(dtype=str)
            ).astype(str).str.replace(r"_X$", "", regex=True)
            if (
                renewable_labels.isin(PV_RENEWABLE_LABELS).any()
                and transformer_labels.isin(PV_TRANSFORMER_LABELS).any()
            ):
                return ["PV plant"]
        except Exception:
            pass
        return []
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
            central_labels = labels.loc[
                ~labels.str.contains(r"_LC(?:_|\d)", case=False, regex=True)
            ]
            if central_labels.str.startswith(tag).any():
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
        If the heat demand is unknown, use
        <span class="page-demand"> Building Heat Demand</span> to estimate space-heating and DHW demand.
        </p>

        <p class="start-copy">
        In <span class="page-scenarios"> Heat Supply Scenarios</span>, you can specify available fuels and
        energy sources, select heat generation technologies, and define storage capacities to create
        and compare multiple system configurations.
        </p>

        <p class="start-copy">
        Finally, use <span class="page-run">Project Review & Simulation</span> to review the project and submit it
        for simulation and optimization.
        </p>

        <p class="start-copy">
        The optimization may take a few minutes.
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

    with st.expander("Duplicate this project", expanded=False):
        st.caption(
            "Create an independent copy with a new name. Project inputs, scenarios, and existing simulation "
            "results are copied. The new project will be opened automatically."
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
                    location_info = reverse(
                        coords,
                        exactly_one=True,
                        zoom=14,
                        addressdetails=True,
                        language="de",
                    )
                    if location_info is None:
                        raise ValueError(
                            "The reverse geocoder returned no administrative address."
                        )
                    raw_location = location_info.raw if location_info else {}
                    address = raw_location.get("address", {})
                    display_fallback = str(
                        raw_location.get("display_name", "")
                    ).split(",", maxsplit=1)[0]
                    city_name = _detailed_reverse_location_name(
                        address,
                        fallback=display_fallback
                        or f"{float(lat):.5f}, {float(lon):.5f}",
                    )
                    st.session_state["project_city_name"] = city_name
                    st.session_state.pop("location_geocoding_warning", None)
                except Exception as exc:
                    st.session_state["project_city_name"] = (
                        f"{float(lat):.5f}, {float(lon):.5f}"
                    )
                    st.session_state["location_geocoding_warning"] = (
                        f"{type(exc).__name__}: {str(exc).strip() or type(exc).__name__}"
                    )

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
            if st.session_state.get("location_geocoding_warning"):
                st.caption(
                    "The precise administrative name could not be loaded, so "
                    "the clicked coordinates are shown instead. "
                    f"Details: {st.session_state['location_geocoding_warning']}"
                )

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
        run_building_heat_demand_page(
            scenario_profile_updater=_update_scenario_load_profiles,
        )
    except Exception as e:
        st.error("❌ The building estimation module could not be loaded.")
        with st.expander("Show technical details"):
            st.exception(e)
        return

def _network_length_from_buildings(area_wkt: str, buildings, excluded_ids=None):
    """Estimate route length, reducing effective area when buildings are excluded."""
    polygon = _wkt.loads(area_wkt)
    if buildings is None or getattr(buildings, "empty", True):
        raise ValueError("No OSM building footprints were found in the selected area.")

    all_buildings = buildings.copy()
    b = all_buildings.copy()
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

    included_footprint = pd.to_numeric(b["area_m2"], errors="coerce").fillna(0)
    total_floor_area = float((included_footprint * levels).sum())

    area_gdf = gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326")
    try:
        metric_crs = area_gdf.estimate_utm_crs()
        selected_area_m2 = float(area_gdf.to_crs(metric_crs).geometry.iloc[0].area)
    except Exception:
        selected_area_m2 = float(area_gdf.to_crs(3857).geometry.iloc[0].area)
    if selected_area_m2 <= 0:
        raise ValueError("The selected polygon has no measurable area.")

    all_footprint = pd.to_numeric(
        all_buildings.get("area_m2", pd.Series(0.0, index=all_buildings.index)),
        errors="coerce",
    ).fillna(0)
    all_footprint_m2 = float(all_footprint.sum())
    included_footprint_m2 = float(included_footprint.sum())
    if excluded_ids:
        if all_footprint_m2 > 0:
            included_area_share = included_footprint_m2 / all_footprint_m2
        else:
            included_area_share = len(b) / max(len(all_buildings), 1)
    else:
        included_area_share = 1.0
    included_area_share = min(max(float(included_area_share), 0.0), 1.0)
    effective_area_m2 = selected_area_m2 * included_area_share

    if effective_area_m2 <= 0 or total_floor_area <= 0:
        return 0.0, b, effective_area_m2, total_floor_area

    plot_ratio = total_floor_area / effective_area_m2
    route_length = 16.171 * (plot_ratio ** 0.1495) * 1000 * effective_area_m2 / 1_000_000
    return float(route_length), b, effective_area_m2, total_floor_area


def _network_length_from_polygon(area_wkt: str, excluded_ids=None):
    """Load OSM buildings once, then estimate route length."""
    buildings = _rehydrate_osm_buildings_from_polygons([area_wkt])
    return _network_length_from_buildings(area_wkt, buildings, excluded_ids)


def _scenario_system_type(scenario_file):
    return str(_scenario_meta_values(scenario_file).get("System Type", ""))


def _scenario_route_length(scenario_file):
    value = _scenario_meta_values(scenario_file).get("Route Length (rm)")
    value = pd.to_numeric(value, errors="coerce")
    return float(value) if pd.notna(value) and float(value) > 0 else None


def _network_supply_temperature_c(scenario_file: str) -> float:
    """Return the modeled district-network supply temperature in degrees C."""
    level = str(
        _scenario_meta_values(scenario_file).get("Network Supply Temperature") or "HT"
    ).strip().upper()
    return 40.0 if level in {"LT", "NT"} else 60.0


def _apply_network_temperature_assumptions(scenario_file: str) -> None:
    """Scale network losses and refresh NT/HT pipe sizing assumptions."""
    if _scenario_system_type(scenario_file) != "District heating (centralized)":
        return

    meta = _scenario_meta_values(scenario_file)
    supply_temperature_c = _network_supply_temperature_c(scenario_file)
    ground_temperature_c = pd.to_numeric(
        meta.get("Ground Boundary Temperature (C)"), errors="coerce"
    )
    if pd.isna(ground_temperature_c):
        ground_temperature_c = pd.to_numeric(
            st.session_state.get("ground_temperature_c"), errors="coerce"
        )

    loss_factor = 1.0
    if pd.notna(ground_temperature_c):
        ht_difference = max(60.0 - float(ground_temperature_c), 1e-6)
        supply_difference = max(
            supply_temperature_c - float(ground_temperature_c), 0.0
        )
        loss_factor = supply_difference / ht_difference

    try:
        time_series = pd.read_excel(scenario_file, sheet_name="time_series")
        if "Loss.fix" in time_series.columns and not time_series.empty:
            # Normalize first so repeated saves never compound the reduction.
            base_loss = _normalized_profile(
                time_series["Loss.fix"], len(time_series)
            )
            time_series["Loss.fix"] = base_loss * float(loss_factor)
            with pd.ExcelWriter(
                scenario_file,
                engine="openpyxl",
                mode="a",
                if_sheet_exists="replace",
            ) as writer:
                time_series.to_excel(writer, sheet_name="time_series", index=False)
    except (FileNotFoundError, ValueError, KeyError):
        pass

    update_meta_sheet(
        scenario_file,
        {
            "Network Supply Temperature (C)": supply_temperature_c,
            "Network Heat Loss Reference Temperature (C)": 60.0,
            "Network Heat Loss Ground Temperature (C)": (
                float(ground_temperature_c)
                if pd.notna(ground_temperature_c) else None
            ),
            "Network Heat Loss Scaling Factor": float(loss_factor),
            "Network Heat Loss Method": (
                "Loss.fix normalized HT profile multiplied by "
                "(network supply temperature - annual mean ground temperature) / "
                "(60 C - annual mean ground temperature)"
            ),
        },
    )

    route_length = _scenario_route_length(scenario_file)
    if route_length is not None:
        _apply_network_length_to_scenario(scenario_file, route_length)


def _apply_network_length_to_scenario(scenario_file, route_length):
    """Persist a project network length in a centralized scenario workbook."""
    if not scenario_file or not os.path.exists(scenario_file):
        return
    route_length = float(route_length)
    if _scenario_system_type(scenario_file) != "District heating (centralized)":
        update_meta_sheet(scenario_file, {"Route Length (rm)": route_length})
        return

    total_heat_demand = 0.0
    network_level = str(
        _scenario_meta_values(scenario_file).get("Network Supply Temperature") or "HT"
    ).strip().upper()
    try:
        demand = (
            _local_network_base_demand(scenario_file)
            if network_level in {"LT", "NT"}
            else pd.read_excel(scenario_file, sheet_name="demand")
        )
        labels = demand.get("label", pd.Series("", index=demand.index)).astype(str)
        from_bus = demand.get("from", pd.Series("", index=demand.index)).astype(str)
        thermal = (
            from_bus.eq("b_th_LT")
            if network_level in {"LT", "NT"}
            else from_bus.str.match(r"^b_th_", na=False)
        )
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
            "Network Pipe Sizing Demand (kWh/a)": total_heat_demand,
            "Network Pipe Sizing Scope": (
                "LT/NT demand transported by the central network; local HT booster demand excluded"
                if network_level in {"LT", "NT"}
                else "all centralized thermal demand"
            ),
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
    network_scenario_key = os.path.basename(scenario_file) if scenario_file else "project"
    if system_type and system_type != "District heating (centralized)":
        st.info("This is a decentral scenario, so a district-heating network length is not required.")
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
        st.divider()
    else:
        st.warning(
            "This centralized scenario has no network length. Enter a known value below or estimate one "
            "from a map area."
        )

    mode = st.radio(
        "How should the network length be set?",
        ["Enter a known length", "Estimate from an OSM area"],
        horizontal=True,
        key=f"network_length_mode_{network_scenario_key}",
    )

    if mode == "Enter a known length":
        with st.form(f"network_length_form_{network_scenario_key}"):
            known_length = st.number_input(
                "Network trench/route length (m)",
                min_value=0.0,
                value=float(current_length or 0.0),
                step=10.0,
                key=f"network_length_manual_m_{network_scenario_key}",
                help=(
                    "Enter the one-way trench/route length. Do not add supply and return together; "
                    "the model doubles this value automatically."
                ),
            )
            save_known_length = st.form_submit_button("Save network length")
        length_changed = not np.isclose(
            float(known_length), float(scenario_length or 0.0), rtol=0, atol=1e-6
        )
        _set_scenario_section_dirty(
            scenario_file, "Network length", length_changed
        )
        if save_known_length and known_length <= 0:
            st.warning("Enter a network length greater than 0 m.")
        if save_known_length and known_length > 0:
            st.session_state["route_length"] = float(known_length)
            st.session_state["route_length_source"] = "user input"
            save_project_meta_from_session(curr)
            _apply_network_length_to_scenario(scenario_file, known_length)
            _set_scenario_section_dirty(scenario_file, "Network length", False)
            st.success("Network length saved for this project.")
            st.rerun()
        return

    st.info(
        "The estimation area does not have to match the project area exactly. If local map data are "
        "incomplete, you can search for and draw a comparable area with similar building density."
    )
    search_col, button_col = st.columns([4, 1])
    with search_col:
        network_search = st.text_input(
            "Search for a place or comparable area",
            key="network_length_search_query",
            placeholder="e.g. a similar neighbourhood or village",
        ).strip()
    with button_col:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        search_clicked = st.button(
            "Search",
            key="network_length_search_button",
            use_container_width=True,
        )
    if search_clicked:
        if not network_search:
            st.warning("Enter a place or area name to search.")
        else:
            try:
                geocoder = Nominatim(user_agent="district_planning_tool_network_length")
                location = geocoder.geocode(network_search, timeout=10)
                if location is None:
                    st.warning("The searched place could not be found.")
                else:
                    st.session_state["network_length_map_center"] = [
                        float(location.latitude),
                        float(location.longitude),
                    ]
                    st.session_state["network_length_map_rev"] = int(
                        st.session_state.get("network_length_map_rev", 0)
                    ) + 1
                    st.rerun()
            except Exception as exc:
                st.warning(f"The place search failed: {exc}")

    coords = (
        st.session_state.get("network_length_map_center")
        or get_valid_coords()
        or (52.52, 13.405)
    )
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
        _set_scenario_section_dirty(
            scenario_file,
            "Network length",
            not np.isclose(
                float(estimate), float(scenario_length or 0.0), rtol=0, atol=1e-6
            ),
        )
        st.markdown(
            f"Estimated trench/route length: **{float(estimate):,.0f} m**  "
            f"(supply + return pipes: **{2 * float(estimate):,.0f} m**)"
        )
        st.caption(
            "This uses the same plot-density equation as the Building Heat Demand page. "
            "OSM levels are used when available; otherwise height/3 m or three levels is assumed. "
            "When buildings are excluded, the effective selected area is reduced in proportion to "
            "the included building footprint."
        )
        if st.button(
            "Use this estimated network length",
            key=f"save_estimated_network_length_{network_scenario_key}",
            disabled=float(estimate) <= 0,
        ):
            st.session_state["route_length"] = float(estimate)
            st.session_state["route_length_source"] = "OSM area estimate"
            save_project_meta_from_session(curr)
            _apply_network_length_to_scenario(scenario_file, estimate)
            _set_scenario_section_dirty(scenario_file, "Network length", False)
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
    if "show_duplicate_scenario" not in st.session_state:
        st.session_state["show_duplicate_scenario"] = False

    # --- Button to trigger scenario creation ---
    scenario_forms_collapsed = (
        not st.session_state["show_create_scenario"]
        and not st.session_state["show_duplicate_scenario"]
    )
    if scenarios and scenario_forms_collapsed:
        if st.button("➕ Create New Scenario"):
            st.session_state["show_create_scenario"] = True

    if scenarios and scenario_forms_collapsed:
        if st.button("📋 Duplicate Scenario", key="open_duplicate_scenario"):
            # Mount the form on a fresh run. Rendering it for the first time in
            # the opener button's event run can occasionally leave the browser
            # and Streamlit with different initial widget state, so the first
            # submitted name arrives as an empty string.
            st.session_state.pop("duplicate_scenario_name", None)
            st.session_state.pop("_duplicate_scenario_name_draft", None)
            st.session_state.pop("_duplicate_scenario_source_draft", None)
            st.session_state["show_duplicate_scenario"] = True
            st.rerun()

    # --- Scenario creation form (conditionally shown) ---
    if st.session_state["show_create_scenario"]:
        with st.expander("🆕 Create a New Scenario", expanded=True):
            st.markdown("**Scenario Configuration**")
            solution_type = st.radio(
                "Choose system type:",
                ["District heating (centralized)", "Decentralized solution"],
                key="new_scenario_solution_type",
                format_func=_system_type_display_name,
            )

            with st.form("create_scenario_form", clear_on_submit=False):
                st.markdown("**Scenario Name**")
                new_name = st.text_input("Enter the scenario name here", key="new_scenario_name")

                sh_value = st.session_state.get("total_heat_demand")
                dhw_value = st.session_state.get("total_dhw_demand")
                tool_demand_available = sh_value is not None and dhw_value is not None
                connected_building_count = _connected_building_count_from_session()
                building_count_available = (
                    connected_building_count is not None
                    and connected_building_count > 0
                )
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
                if not building_count_available:
                    st.session_state["new_scenario_use_building_count"] = False
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
                use_connected_building_count = False
                if solution_type == "District heating (centralized)":
                    use_connected_building_count = st.checkbox(
                        (
                            f"Use connected building number from **Building Heat Demand** "
                            f"({connected_building_count} buildings)"
                            if building_count_available
                            else "Use connected building number from **Building Heat Demand**"
                        ),
                        value=building_count_available,
                        key="new_scenario_use_building_count",
                        disabled=not building_count_available,
                    )
                    st.caption(
                        "The connected building number includes only buildings whose final annual "
                        "space-heating demand is greater than 0 kWh/a. Buildings with zero or missing "
                        "demand are not counted."
                    )
                else:
                    st.session_state["new_scenario_use_building_count"] = False

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
                use_building_data = bool(
                    use_connected_building_count and building_count_available
                )
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
                            if solution_type == "Decentralized solution":
                                _save_decentral_base_demand(
                                    scenario_file, df_dem
                                )
                                _apply_decentral_configuration(scenario_file)
                            st.success("Space heating and DHW demand were automatically saved to the new scenario.")

                        elif copy_existing_demand and demand_source_scenario:
                            source_file = get_scenario_file(demand_source_scenario)
                            _copy_scenario_demand(source_file, scenario_file)
                            source_meta = _scenario_meta_values(source_file)
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
                                building_count=scenario_building_count,
                                peak_load_kw=source_peak,
                                user_edited=source_peak_edited,
                            )
                            if solution_type == "Decentralized solution":
                                _apply_decentral_configuration(scenario_file)
                            st.success(f"Demand was copied from scenario '{demand_source_scenario}'.")

                        st.session_state[
                            _meaningful_demand_dirty_key(scenario_file)
                        ] = False
                        _set_scenario_section_dirty(
                            scenario_file, "Demand", False
                        )
                        _clear_demand_editor_state(scenario_file)
                        st.session_state["last_selected_scenario"] = clean_name
                        st.session_state["current_scenario_file"] = scenario_file
                        st.rerun()

    if scenarios and st.session_state["show_duplicate_scenario"]:
        duplicate_name_draft = str(
            st.session_state.get("_duplicate_scenario_name_draft") or ""
        )
        if (
            duplicate_name_draft
            and not str(
                st.session_state.get("duplicate_scenario_name") or ""
            ).strip()
        ):
            # Restore the separately captured draft before the widget is
            # instantiated. Assigning it after text_input would be illegal.
            st.session_state["duplicate_scenario_name"] = duplicate_name_draft
        with st.expander("Duplicate an existing scenario", expanded=True):
            st.caption(
                "Create an independent copy of the complete saved scenario configuration. "
                "Existing simulation results are not duplicated for the new scenario."
            )
            with st.form("duplicate_scenario_form", clear_on_submit=False):
                source_scenario = st.selectbox(
                    "Scenario to duplicate",
                    scenarios,
                    key="duplicate_scenario_source",
                )
                duplicate_name = st.text_input(
                    "Name of the duplicated scenario",
                    key="duplicate_scenario_name",
                    placeholder=f"Copy of {source_scenario}",
                )
                duplicate_col, cancel_col = st.columns([1, 1])
                duplicate_clicked = duplicate_col.form_submit_button(
                    "Duplicate scenario",
                    use_container_width=True,
                    on_click=_capture_duplicate_scenario_form_values,
                )
                cancel_duplicate = cancel_col.form_submit_button(
                    "Cancel", use_container_width=True
                )

            if cancel_duplicate:
                st.session_state["show_duplicate_scenario"] = False
                st.session_state.pop("_duplicate_scenario_name_draft", None)
                st.session_state.pop("_duplicate_scenario_source_draft", None)
                st.rerun()

            if duplicate_clicked:
                # The submit callback captures the browser values before this
                # page body reruns. Use that durable copy first, then both
                # Streamlit representations as fallbacks.
                clean_duplicate_name = str(
                    st.session_state.get("_duplicate_scenario_name_draft")
                    or duplicate_name
                    or st.session_state.get("duplicate_scenario_name")
                    or ""
                ).strip()
                submitted_source_scenario = str(
                    st.session_state.get("_duplicate_scenario_source_draft")
                    or source_scenario
                )
                validation_error = _scenario_name_validation_error(
                    clean_duplicate_name
                )
                if validation_error:
                    st.warning(validation_error)
                elif clean_duplicate_name.casefold() in {
                    scenario.casefold() for scenario in scenarios
                }:
                    st.warning(
                        f"Scenario '{clean_duplicate_name}' already exists."
                    )
                else:
                    try:
                        duplicated_file = duplicate_scenario(
                            submitted_source_scenario,
                            clean_duplicate_name,
                            project_name=current_project,
                        )
                        st.session_state["scenario_just_duplicated"] = (
                            submitted_source_scenario,
                            clean_duplicate_name,
                        )
                        st.session_state["last_selected_scenario"] = (
                            clean_duplicate_name
                        )
                        st.session_state["current_scenario_file"] = (
                            duplicated_file
                        )
                        st.session_state["show_duplicate_scenario"] = False
                        st.session_state.pop(
                            "_duplicate_scenario_name_draft", None
                        )
                        st.session_state.pop(
                            "_duplicate_scenario_source_draft", None
                        )
                        st.rerun()
                    except (FileNotFoundError, FileExistsError, ValueError) as exc:
                        st.warning(str(exc))
                    except PermissionError:
                        st.error(
                            "The scenario could not be duplicated because its "
                            "Excel file is currently in use. Close it in Excel "
                            "and try again."
                        )
                    except OSError as exc:
                        st.error(f"The scenario could not be duplicated: {exc}")

    # --- Scenario creation feedback ---
    if "scenario_just_created" in st.session_state:
        created_name = st.session_state.pop("scenario_just_created")
        st.success(f"Scenario '{created_name}' opened.")

    if "scenario_just_duplicated" in st.session_state:
        source_name, duplicated_name = st.session_state.pop(
            "scenario_just_duplicated"
        )
        st.success(
            f"Scenario '{source_name}' was duplicated as "
            f"'{duplicated_name}' and opened."
        )

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
                    solar_station = meta.get("DWD Solar Station")
                    solar_station_id = meta.get("DWD Solar Station ID")
                    solar_years = meta.get("DWD Solar Profile Years Used")
                    solution_type = meta.get("System Type", "Unknown")

                    with st.expander(
                        "📁 Project metadata for this scenario",
                        expanded=False,
                    ):
                        st.markdown(f"- **Project Name**: {project_name}")
                        if dwd_station:
                            st.markdown(
                                f"- **DWD weather:** {dwd_station} (station {dwd_station_id}); "
                                f"profile years {dwd_years or 'not recorded'}"
                            )
                        else:
                            st.markdown(
                                f"- **Weather fallback:** {city} template "
                                f"(legacy zone {climate_zone})"
                            )
                        if solar_station:
                            st.markdown(
                                f"- **DWD solar radiation:** {solar_station} "
                                f"(station {solar_station_id}); profile years "
                                f"{solar_years or 'not recorded'}"
                            )
                        st.markdown(
                            f"- **System Type**: "
                            f"{_system_type_display_name(solution_type)}"
                        )
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
            st.caption(
                "Changes in the editor are not saved until you click its Save button. "
                "If you navigate away without saving, those draft values may be discarded."
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
    if _scenario_system_type(excel_file) != "District heating (centralized)":
        return
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
    if (
            method == "Use building count"
            and count_preview["system_type"] == "District heating (centralized)"
            and building_count > WINTER_SIMULTANEITY_MAX_BUILDINGS
    ):
        controls.caption(
            f"For more than {WINTER_SIMULTANEITY_MAX_BUILDINGS:,} connected buildings, "
            f"the simultaneity factor is held at its minimum value for "
            f"{WINTER_SIMULTANEITY_MAX_BUILDINGS:,} buildings."
        )

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
        controls.markdown(f"- **Simultaneity factor:** {count_preview['simultaneity_factor']:.2f}")
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
        if _scenario_system_type(excel_file) == "Decentralized solution":
            _apply_decentral_configuration(excel_file)
        st.session_state[pending_peak_key] = result.get("selected_peak_kw", peak_load)
        st.success(
            f"Load profiles updated. Applied space-heating peak: "
            f"{result.get('selected_peak_kw', peak_load):,.1f} kW."
        )
        st.rerun()


def _render_demand_import_expander(excel_file: str, project: str | None) -> None:
    with st.expander("Import latest building demand estimation", expanded=False):
        estimated_demand_available = (
            st.session_state.get("total_heat_demand") is not None
            and st.session_state.get("total_dhw_demand") is not None
        )
        if not estimated_demand_available:
            st.info(
                "After estimating space-heating and DHW demand on **Building Heat Demand**, "
                "you can import the results here."
            )
            return

        st.caption(
            "Import and save the latest space-heating and DHW totals directly to this scenario."
        )
        file_id = os.path.basename(excel_file)
        import_intent_key = f"_import_demand_intent_{file_id}"
        if st.button(
            "Import latest space-heating and DHW estimation",
            key=f"import_project_demand_{file_id}",
        ):
            st.session_state[import_intent_key] = True

        if not st.session_state.get(import_intent_key):
            return

        st.markdown("##### Confirm demand import")
        current_demand_rows = _scenario_demand_rows_without_loss(excel_file)
        keep_existing = False
        if not current_demand_rows.empty:
            import_action = st.radio(
                "This scenario already contains demand components. What should happen to them?",
                [
                    "Replace the current demand components",
                    "Keep the current demand components and add the imported results after them",
                ],
                key=f"import_demand_action_{file_id}",
            )
            keep_existing = import_action.startswith("Keep")

        estimated_building_count = _connected_building_count_from_session()
        import_building_count = False
        if _scenario_system_type(excel_file) == "District heating (centralized)":
            import_building_count = st.checkbox(
                (
                    f"Import the connected building count ({estimated_building_count})"
                    if estimated_building_count is not None
                    else "Import the connected building count"
                ),
                value=estimated_building_count is not None,
                disabled=estimated_building_count is None,
                key=f"import_building_count_{file_id}",
                help="When cleared, the building count already stored in this scenario is retained.",
            )
            import_building_count = bool(
                estimated_building_count is not None and import_building_count
            )

        confirm_col, cancel_col = st.columns([1, 1])
        confirm_import = confirm_col.button(
            "Confirm import", key=f"confirm_project_demand_import_{file_id}"
        )
        cancel_import = cancel_col.button(
            "Cancel", key=f"cancel_project_demand_import_{file_id}"
        )
        if cancel_import:
            st.session_state.pop(import_intent_key, None)
            st.rerun()
        if confirm_import:
            totals = _import_latest_project_demand(
                excel_file,
                keep_existing=keep_existing,
                import_building_count=import_building_count,
            )
            st.session_state.pop(import_intent_key, None)
            st.session_state[_meaningful_demand_dirty_key(excel_file)] = False
            _set_scenario_section_dirty(excel_file, "Demand", False)
            _clear_demand_editor_state(excel_file)
            st.success(
                f"Imported and saved {totals['space_heating_kwh']:,.0f} kWh/a space heating and "
                f"{totals['dhw_kwh']:,.0f} kWh/a DHW to this scenario."
            )
            st.rerun()


def run_demand_page(excel_file):
    scenario_key = f"components_{os.path.basename(excel_file)}"
    if st.session_state.pop(f"_reset_widgets_{scenario_key}", False):
        _clear_demand_editor_state(excel_file)

    st.markdown("""
        On this page, you can define the demand data for consumers.

        There are three possible demand types:  
        - **HT** – High-temperature heat demand (50–70 °C)  
        - **LT** – Low-temperature heat demand (30–50 °C)  
        - **el** – Electricity demand

        **Note:** DHW is fixed to **HT** because domestic hot water requires temperatures above 60 °C.
        The type of space-heating and other demand rows can still be adjusted when needed.

        To add more consumers, click **Add another component**.
        Click **Save** after entering or modifying data.
        To delete a component, simply clear its name field.  
    """)

    project = get_current_project()
    if project:
        _load_saved_project_totals(project)
    _render_demand_import_expander(excel_file, project)

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

    version_key = f"{scenario_key}_file_mtime"
    scenario_system_type = _scenario_system_type(excel_file)
    is_decentral = scenario_system_type == "Decentralized solution"

    # ---- Load current demand sheet
    df_existing = (
        _local_network_base_demand(excel_file)
        if _local_network_is_enabled(excel_file)
        else (
            _decentral_base_demand(excel_file)
            if is_decentral
            else pd.read_excel(excel_file, sheet_name=sheet_name)
        )
    )
    if "label" in df_existing.columns:
        df_existing = df_existing.loc[
            ~df_existing["label"].astype(str).str.strip().isin(HIDDEN_LEGACY_SOURCES)
        ].copy()
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
    is_decentral = not is_central and str(solution_type_meta) == "Decentralized solution"
    if is_central and not _local_network_is_enabled(excel_file):
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
        st.session_state.pop(
            f"decentral_sh_split_enabled_{scenario_key}", None
        )
        st.session_state.pop(f"decentral_sh_ht_share_{scenario_key}", None)
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

    saved_ht_share_percent = (
        100.0 * _decentral_space_heating_ht_share(excel_file, df_existing)
        if is_decentral else 100.0
    )
    effective_ht_share_percent = saved_ht_share_percent
    saved_split_space_heating = bool(
        is_decentral
        and 1e-9 < saved_ht_share_percent < 100.0 - 1e-9
    )

    with st.form(f"demand_form_{os.path.basename(excel_file)}"):
        new_components = []
        split_controls_rendered = False
        explicit_sh_temperature_choice = None

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
                    key=f"d_value_{i}_{scenario_key}",
                    decimals=2,
                )
            with col3:
                if _is_dhw_demand_label(label):
                    ctype = "HT"
                    st.selectbox(
                        "Type",
                        ["HT"],
                        index=0,
                        key=f"d_type_dhw_locked_{i}_{scenario_key}",
                        disabled=True,
                        help="DHW is fixed to HT because it requires temperatures above 60 °C.",
                    )
                else:
                    type_options = (
                        ["HT", "LT"]
                        if is_decentral and label == SPACE_HEATING_DEMAND_LABEL
                        else ["HT", "LT", "el"]
                    )
                    saved_type = comp["type"] if comp["type"] in type_options else "HT"
                    if (
                        is_decentral
                        and label == SPACE_HEATING_DEMAND_LABEL
                        and saved_split_space_heating
                    ):
                        selected_type = st.selectbox(
                            "Type",
                            type_options,
                            index=None,
                            key=f"d_type_split_override_{i}_{scenario_key}",
                            placeholder="Keep the HT/LT percentage split",
                            help=(
                                "Selecting HT or LT replaces the percentage split "
                                "with a 100% HT or 100% LT configuration when saved."
                            ),
                        )
                        if selected_type is None:
                            ctype = saved_type
                        else:
                            ctype = selected_type
                            explicit_sh_temperature_choice = selected_type
                    else:
                        ctype = st.selectbox(
                            "Type",
                            type_options,
                            index=type_options.index(saved_type),
                            key=f"d_type_{i}_{scenario_key}"
                        )

            if (
                is_decentral
                and label == SPACE_HEATING_DEMAND_LABEL
                and not split_controls_rendered
            ):
                with st.expander(
                    "Optionally split space-heating demand between HT and LT",
                    expanded=False,
                ):
                    split_space_heating = st.checkbox(
                        "Split space-heating demand by HT percentage",
                        value=saved_split_space_heating,
                        key=f"decentral_sh_split_enabled_{scenario_key}",
                    )
                    entered_ht_share = st.number_input(
                        "HT share of space-heating demand (%)",
                        min_value=0.0,
                        max_value=100.0,
                        value=(
                            float(saved_ht_share_percent)
                            if saved_split_space_heating else 50.0
                        ),
                        step=1.0,
                        key=f"decentral_sh_ht_share_{scenario_key}",
                    )
                    st.caption(
                        "When enabled, the percentage defines the HT/LT split. "
                        "Selecting HT or LT in the Type field replaces the split "
                        "with 100% of the selected type when the demand is saved. "
                        "DHW remains entirely HT."
                    )
                if explicit_sh_temperature_choice is not None:
                    effective_ht_share_percent = (
                        100.0
                        if explicit_sh_temperature_choice == "HT"
                        else 0.0
                    )
                else:
                    effective_ht_share_percent = (
                        float(entered_ht_share)
                        if split_space_heating
                        else (100.0 if ctype == "HT" else 0.0)
                    )
                split_controls_rendered = True

            from_bus = "b_el" if ctype == "el" else f"b_th_{ctype}"
            new_components.append({
                "label": label,
                "active": 1,
                "from": from_bus,
                "nominal value": value,
                "Demand in MWh/a": value / 1000,
                "Demand in GWh/a": value / 1e6
            })

        draft_components = [
            comp for comp in new_components
            if comp["label"].strip()
            and comp["label"].strip() not in {
                HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL
            }
        ]
        draft_df = pd.DataFrame(
            draft_components,
            columns=[
                "label", "active", "from", "nominal value",
                "Demand in MWh/a", "Demand in GWh/a",
            ],
        )
        if is_decentral and not draft_df.empty:
            draft_sh = draft_df["label"].astype(str).eq(
                SPACE_HEATING_DEMAND_LABEL
            )
            draft_df.loc[draft_sh, "from"] = (
                "b_th_LT"
                if effective_ht_share_percent <= 0 else "b_th_HT"
            )
        compare_columns = ["label", "active", "from", "nominal value"]
        old_compare = df_existing.copy()
        if not old_compare.empty and "label" in old_compare.columns:
            old_compare = old_compare.loc[
                ~old_compare["label"].astype(str).isin(
                    {HEAT_LOSS_DEMAND_LABEL, OLD_HEAT_LOSS_DEMAND_LABEL}
                )
            ]
        for frame in (old_compare, draft_df):
            for column in compare_columns:
                if column not in frame.columns:
                    frame[column] = np.nan
            frame["label"] = frame["label"].fillna("").astype(str).str.strip()
            frame["from"] = frame["from"].fillna("").astype(str).str.strip()
            frame["active"] = pd.to_numeric(
                frame["active"], errors="coerce"
            ).fillna(0).astype(int)
            frame["nominal value"] = pd.to_numeric(
                frame["nominal value"], errors="coerce"
            ).round(2)
        old_compare = old_compare[compare_columns].sort_values(
            compare_columns, kind="stable"
        ).reset_index(drop=True)
        new_compare = draft_df[compare_columns].sort_values(
            compare_columns, kind="stable"
        ).reset_index(drop=True)
        temperature_changed = bool(
            is_decentral
            and not np.isclose(
                effective_ht_share_percent,
                saved_ht_share_percent,
                rtol=0,
                atol=1e-9,
            )
        )
        demand_changed = not old_compare.equals(new_compare) or temperature_changed
        st.session_state[_meaningful_demand_dirty_key(excel_file)] = demand_changed
        _set_scenario_section_dirty(excel_file, "Demand", demand_changed)

        c1, c2 = st.columns([1, 1])
        add_clicked = c1.form_submit_button("Add another component")
        save_clicked = c2.form_submit_button(
            "Save",
        )

        if add_clicked:
            if not demand_changed:
                st.session_state[_meaningful_demand_dirty_key(excel_file)] = False
                _set_scenario_section_dirty(excel_file, "Demand", False)
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

            if is_decentral:
                sh_rows = to_write.get(
                    "label", pd.Series("", index=to_write.index)
                ).astype(str).eq(SPACE_HEATING_DEMAND_LABEL)
                to_write.loc[sh_rows, "from"] = (
                    "b_th_LT"
                    if effective_ht_share_percent <= 0 else "b_th_HT"
                )
                update_meta_sheet(
                    excel_file,
                    {
                        "Decentral Space Heating HT Share (%)": float(
                            effective_ht_share_percent
                        ),
                        "Decentral Space Heating Temperature Mode": (
                            "HT"
                            if effective_ht_share_percent >= 100 else
                            "LT"
                            if effective_ht_share_percent <= 0 else "Mixed"
                        ),
                    },
                )

            has_lt, has_ht = _temperature_mix(to_write)
            if is_central and has_lt and has_ht:
                pending_key = _pending_lt_demand_key(excel_file)
                st.session_state[pending_key] = to_write.copy()
                st.session_state[_meaningful_demand_dirty_key(excel_file)] = True
                _set_scenario_section_dirty(excel_file, "Demand", True)
                st.rerun()

            st.session_state.pop(_pending_lt_demand_key(excel_file), None)
            _save_demand_and_apply_temperature_configuration(excel_file, to_write)
            _refresh_profiles_after_demand_save(excel_file)
            _apply_network_temperature_assumptions(excel_file)

            _set_scenario_section_dirty(excel_file, "Demand", False)
            st.session_state[_meaningful_demand_dirty_key(excel_file)] = False

            st.success("Demand data and network temperature configuration saved.")

            # Reseed from file
            _clear_demand_editor_state(excel_file, defer_widget_clear=True)
            st.rerun()

    pending_demand = st.session_state.get(_pending_lt_demand_key(excel_file))
    _render_lt_network_choice(excel_file, pending_demand=pending_demand)
    _render_scenario_peak_load_editor(excel_file)


def _render_pv_roof_area_editor(excel_file: str) -> None:
    st.divider()
    st.subheader("Photovoltaic potential")
    is_decentralized = (
        _scenario_system_type(excel_file) == "Decentralized solution"
    )

    meta = _scenario_meta_values(excel_file)
    stored = pd.to_numeric(
        meta.get("Maximum PV Roof Area (m2)"), errors="coerce"
    )
    default_area = _default_pv_roof_area_m2()
    peak_kw_per_m2 = _scenario_pv_peak_kw_per_m2(excel_file)
    current_area = float(stored) if pd.notna(stored) and stored >= 0 else default_area
    try:
        current_feed_in_tariff = _scenario_pv_feed_in_tariff(excel_file)
    except (FileNotFoundError, KeyError, ValueError):
        current_feed_in_tariff = _scenario_pv_feed_in_tariff(TEMPLATE_CEN_FILE)
    scenario_id = Path(excel_file).stem
    with st.form(f"pv_roof_area_form_{scenario_id}"):
        roof_area = st.number_input(
            "Maximum roof area available for PV (m²)",
            min_value=0.0,
            value=float(current_area),
            step=10.0,
            format="%.2f",
            key=f"pv_roof_area_{scenario_id}",
        )
        st.caption(
            f"The initial value is 40% of the included building footprint "
            f"(ground area). Under local conditions according to "
            f"DWD data, the entered area would result in about "
            f"{float(roof_area) * peak_kw_per_m2:,.1f} kWp."
        )
        if is_decentralized:
            st.info(
                "For decentral solutions, the available PV areas are "
                "aggregated and modeled as one shared PV system rather than "
                "separately for each consumer. This simplification can "
                "slightly overestimate PV self-consumption."
            )
        feed_in_tariff = st.number_input(
            "PV feed-in tariff (€/kWh)",
            min_value=0.0,
            value=float(current_feed_in_tariff),
            step=0.001,
            format="%.3f",
            key=f"pv_feed_in_tariff_{scenario_id}",
            help=(
                "Enter the revenue received per kWh exported to the grid. "
                "It is stored in the optimization model as a negative variable cost."
            ),
        )
        pv_changed = not np.isclose(
            float(roof_area), float(current_area), rtol=0, atol=1e-6
        )
        tariff_changed = not np.isclose(
            float(feed_in_tariff), float(current_feed_in_tariff), rtol=0, atol=1e-9
        )
        _set_scenario_section_dirty(
            excel_file, "Sources", pv_changed or tariff_changed
        )
        save_pv = st.form_submit_button(
            "Save PV configuration",
        )

    if save_pv:
        try:
            result = _configure_scenario_pv(
                excel_file,
                roof_area,
                enabled=_scenario_pv_is_enabled(excel_file),
                feed_in_tariff_eur_per_kwh=feed_in_tariff,
            )
            category = result["transformer"] or "none (PV disabled)"
            st.success(
                f"PV configuration saved: {result['roof_area_m2']:,.1f} m², "
                f"maximum installable collector area "
                f"{result['maximum_collector_area_m2']:,.1f} m² "
                f"({result['peak_kw']:,.1f} kWp). "
                f"Feed-in tariff {result['feed_in_tariff_eur_per_kwh']:.3f} €/kWh. "
                f"(PV is {'enabled' if result['enabled'] else 'not yet selected on **Generation Techs** page'}.)"
            )
            _set_scenario_section_dirty(excel_file, "Sources", False)
        except Exception as exc:
            st.error(f"PV configuration could not be saved: {exc}")


def run_sources_page(excel_file):
    scenario_key = f"sources_{os.path.basename(excel_file)}"
    if st.session_state.pop(f"_reset_widgets_{scenario_key}", False):
        _clear_source_editor_state(excel_file)

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
        
        For limited renewable heat pump sources (e.g. ground source, waste water), maximum capacity and annual consumption can be defined on this page.
        
        Since the maximum capacity, full load hours, and annual maximum amount are interrelated, these values will be adjusted automatically based on the inputs:
        **Annual maximum amount = maximum capacity × full load hours**
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
                            src_df = src_df.loc[
                                src_df["label"].ne("")
                                & ~src_df["label"].isin(HIDDEN_LEGACY_SOURCES)
                            ].copy()

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
                        _set_scenario_section_dirty(excel_file, "Sources", False)
                        _clear_source_editor_state(excel_file, defer_widget_clear=True)
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

    with st.form(f"source_form_{os.path.basename(excel_file)}"):
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
                nv = dot_number_input("Max. capacity (kW)", src["nominal value"], key=f"src_nominal_{i}_{scenario_key}")
            with col5:
                annual_max_value = src.get("annual max")
                am_str = st.text_input(
                    "Annual max. amount (kWh) (optional)",
                    value=(
                        ""
                        if annual_max_value is None or pd.isna(annual_max_value)
                        else str(annual_max_value)
                    ),
                    key=f"src_am_{i}_{scenario_key}"
                )
                am = parse_number(am_str) if am_str.strip() else None
            flt_val = src.get("full load time", None)
            if (flt_val is None or np.isnan(flt_val)) and nv and am:
                flt_val = am / nv
            if flt_val is None or np.isnan(flt_val):
                flt_val = 8760.0
            with col6:
                flt = dot_number_input("Max. full load hours (h)", src["full load time"], key=f"src_full_{i}_{scenario_key}")

            capacity_inputs_changed = any(
                (
                    not _source_values_equal(
                        original, current, numeric=True
                    )
                )
                for original, current in (
                    (src.get("nominal value"), nv),
                    (src.get("full load time"), flt),
                    (src.get("annual max"), am),
                )
            )
            if capacity_inputs_changed:
                nv, flt, am = reconcile_nv_flt_am(nv, flt, am)
            else:
                # Do not normalize or recalculate these linked fields merely
                # because a cost or emission value caused the page to rerun.
                nv = src.get("nominal value")
                flt = src.get("full load time")
                am = src.get("annual max")

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

        # Keep the complete draft independent of Streamlit's widget lifecycle.
        # This prevents unrelated cells from falling back to blank/zero when
        # one text field triggers a rerun.
        st.session_state[scenario_key] = [
            {
                "label": source["label"],
                "variable costs": source["variable costs"],
                "emission factor": source["emission factor"],
                "nominal value": source["nominal value"],
                "full load time": source["full load time"],
                "annual max": source["annual max"],
                "to": source["to"],
            }
            for source in new_sources
        ]

        c1, c2 = st.columns([1, 1])
        source_columns = [
            "label", "active", "variable costs", "emission factor",
            "nominal value", "full load time", "annual max", "to",
        ]
        source_draft = pd.DataFrame(
            [src for src in new_sources if src["label"].strip()],
            columns=source_columns,
        )
        existing_compare = df_existing.reindex(columns=source_columns).copy()
        draft_compare = source_draft.reindex(columns=source_columns).copy()
        for frame in (existing_compare, draft_compare):
            for column in source_columns:
                if column in {"label", "to"}:
                    frame[column] = frame[column].fillna("").astype(str).str.strip()
                else:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
        _set_scenario_section_dirty(
            excel_file,
            "Sources",
            not existing_compare.reset_index(drop=True).equals(
                draft_compare.reset_index(drop=True)
            ),
        )

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

            df_sources = pd.DataFrame(cleaned_sources, columns=source_columns)

            # Force numeric types (REAL numbers in Excel, not strings)
            num_cols = ["variable costs", "emission factor", "nominal value", "full load time", "annual max"]
            for c in num_cols:
                if c in df_sources.columns:
                    df_sources[c] = pd.to_numeric(df_sources[c], errors="coerce")

            # Optional: keep active as int
            if "active" in df_sources.columns:
                df_sources["active"] = pd.to_numeric(df_sources["active"], errors="coerce").fillna(0).astype(int)

            # Preserve every untouched workbook cell when only source
            # properties were edited. Structural changes still replace the
            # sheet so additions and deletions remain correct.
            _save_source_rows(excel_file, df_existing, df_sources)

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
            if not new_buses.empty:
                updated_buses = pd.concat([existing_buses, new_buses], ignore_index=True)
                with pd.ExcelWriter(
                    excel_file, engine="openpyxl", mode="a", if_sheet_exists="replace"
                ) as writer:
                    updated_buses.to_excel(writer, sheet_name="buses", index=False)

            st.success("Sources updated.")
            _set_scenario_section_dirty(excel_file, "Sources", False)
            _clear_source_editor_state(excel_file, defer_widget_clear=True)
            st.rerun()

    _render_pv_roof_area_editor(excel_file)

def run_transformers_page(excel_file, scenario_name):
    # st.title("Generation Technologies")
    st.markdown("""
    On this page, you can assign generation technologies to energy sources defined on the **Sources** page.

    Click **Save** after making your selections. To remove a technology, simply clear its name field.

    The following abbreviations are used for the predefined sources:  
    - **Solar**: Solar radiation
    - **el**:   Electricity  
    - **LPM**: Biomass (Wood, local available)  
    - **BM_W**: Biomass (Wood, from market)  
    - **BM_P**: Biomass (Pellets, from market)  
    - **BG**:   Biogas  
    - **NG**:   Natural Gas
    
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

    # PV is a generation-page-only source. Place it before
    # electricity because its plant feeds the electricity bus.
    if _scenario_system_type(excel_file) in {
        "District heating (centralized)",
        "Decentralized solution",
    }:
        source_df_all = source_df_all.loc[
            source_df_all["label"].ne(PV_SOURCE_DISPLAY)
        ].reset_index(drop=True)
        electricity_positions = source_df_all.index[
            source_df_all["label"].eq("el")
        ].tolist()
        insert_at = electricity_positions[0] if electricity_positions else len(source_df_all)
        source_df_all = pd.concat(
            [
                source_df_all.iloc[:insert_at],
                pd.DataFrame([{"label": PV_SOURCE_DISPLAY, "to": ""}]),
                source_df_all.iloc[insert_at:],
            ],
            ignore_index=True,
        )

    # Hide internal HP sources and the removed legacy DHS option from the UI.
    source_df = source_df_all[
        ~source_df_all["label"].isin(INTERNAL_HP_SOURCES | HIDDEN_LEGACY_SOURCES)
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
    if _scenario_pv_is_enabled(excel_file):
        existing_techs[PV_SOURCE_DISPLAY] = ["PV plant"]

    # Always initialize the selection for every source from Excel active techs (or empty list)
    for source in source_df["label"]:
        if source not in st.session_state[session_key]:
            source_bus = source_bus_map.get(source, "")
            # get active techs or empty list if none found
            active_techs = (
                existing_techs.get(PV_SOURCE_DISPLAY, [])
                if source == PV_SOURCE_DISPLAY
                else existing_techs.get(source_bus, [])
            )
            st.session_state[session_key][source] = active_techs

    local_boosters_visible = False
    is_decentralized = (
        _scenario_system_type(excel_file) == "Decentralized solution"
    )
    saved_local_boosters = []
    if _local_network_is_enabled(excel_file):
        local_base = _local_network_base_demand(excel_file)
        _, local_has_ht = _temperature_mix(local_base)
        local_boosters_visible = bool(local_has_ht)
        if local_boosters_visible:
            local_meta = _scenario_meta_values(excel_file)
            saved_local_boosters = [
                item.strip().upper()
                for item in str(
                    local_meta.get("Local Booster Technologies") or ""
                ).split(",")
                if item.strip().upper() in LOCAL_BOOSTER_OPTIONS
            ]
    selected_local_boosters = list(saved_local_boosters)

    # --- Live editor ---
    with st.form(f"technology_form_{scenario_id}", border=False):
        central_plant_box = st.container(border=True)
        central_plant_box.subheader(
            "Decentral plants" if is_decentralized else "Central heating plant"
        )
        for source in source_df["label"]:
            central_plant_box.subheader(f"Source: {source}")
            if source == PV_SOURCE_DISPLAY:
                central_plant_box.caption(
                    "The maximum roof area available for PV can be defined on "
                    "the **Sources** subpage."
                )
            configured_options = tech_options.get(source, [])
            options = available_technology_options(excel_file, source)
            display_options = [display_names[opt] for opt in options]

            unavailable = [tech for tech in configured_options if tech not in options]
            if unavailable:
                unavailable_names = ", ".join(display_names.get(tech, tech) for tech in unavailable)
                central_plant_box.caption(
                    "Not available for this scenario's system type: " + unavailable_names
                )

            prev_selection = st.session_state[session_key].get(source, [])
            prev_display_selection = [
                display_names.get(tech, tech) for tech in prev_selection if
                display_names.get(tech, tech) in display_options
            ]

            widget_key = f"tech_{scenario_id}_{source}"
            selected_display = central_plant_box.multiselect(
                f"Select technologies for {source}",
                options=display_options,
                default=prev_display_selection,
                key=widget_key
            )

            selected = [rev_display_names[disp] for disp in selected_display]
            st.session_state[session_key][source] = selected

        if local_boosters_visible:
            local_plant_box = st.container(border=True)
            local_plant_box.subheader("Local booster plants")
            local_plant_box.caption(
                "Select the technologies available to the local HT consumer "
                "clusters. Virtual grouping uses a simultaneity factor of 1, "
                "while LT demand remains supplied by the central plant."
            )
            selected_local_boosters = []
            for local_source, local_options in (
                LOCAL_BOOSTER_TECHNOLOGIES_BY_SOURCE.items()
            ):
                local_plant_box.subheader(f"Source: {local_source}")
                source_defaults = [
                    item for item in saved_local_boosters
                    if item in local_options
                ]
                selected_local_boosters.extend(
                    local_plant_box.multiselect(
                        f"Select local technologies for {local_source}",
                        list(local_options),
                        default=source_defaults,
                        format_func=lambda item: (
                            "Power-to-heat (P2H)"
                            if item == "P2H"
                            else "Air-source heat pump (ASHP)"
                        ),
                        key=(
                            f"local_booster_technologies_"
                            f"{scenario_id}_{local_source}"
                        ),
                    )
                )

        tech_changed = False
        for source in source_df["label"]:
            source_bus = source_bus_map.get(source, "")
            saved = (
                existing_techs.get(PV_SOURCE_DISPLAY, [])
                if source == PV_SOURCE_DISPLAY
                else existing_techs.get(source_bus, [])
            )
            current = st.session_state[session_key].get(source, [])
            if set(current) != set(saved):
                tech_changed = True
                break
        if local_boosters_visible and (
            set(selected_local_boosters) != set(saved_local_boosters)
        ):
            tech_changed = True
        _set_scenario_section_dirty(excel_file, "Generation technologies", tech_changed)
        submitted = st.form_submit_button(
            "Save all technologies",
        )

    # --- Save Logic ---
    if submitted:
        updated_dfs = {}
        sheets_to_clean = set(sheet for sheet, _ in tech_sheet_map.values())

        # Read and clean all relevant sheets
        for sheet_name in sheets_to_clean:
            try:
                existing_df = pd.read_excel(excel_file, sheet_name=sheet_name)
                labels = existing_df["label"].astype(str).str.replace(
                    r"_X$", "", regex=True
                )
                protected_pv = labels.isin(PV_TRANSFORMER_LABELS)
                cleaned_df = existing_df[
                    ~(
                            (existing_df["active"] == 1) &
                            (~existing_df["label"].astype(str).str.contains("z_")) &
                            (~protected_pv)
                    )
                ]
                if "label" in cleaned_df.columns and "active" in cleaned_df.columns:
                    lab = cleaned_df["label"].astype(str).str.strip()
                    lab = lab.str.replace(
                        r"_[xX]_[xX]$", "_X", regex=True
                    )
                    cleaned_df.loc[:, "label"] = lab
                    act = pd.to_numeric(cleaned_df["active"], errors="coerce").fillna(0).astype(int)
                    pv_rows = lab.str.replace(r"_X$", "", regex=True).isin(
                        PV_TRANSFORMER_LABELS
                    )

                    # inactive example rows should KEEP "_X" (append if missing)
                    mask_inactive = (
                        (act == 0)
                        & (~lab.str.lower().str.endswith("_x"))
                        & (~pv_rows)
                    )
                    cleaned_df.loc[mask_inactive, "label"] = lab[mask_inactive] + "_X"
                updated_dfs[sheet_name] = [cleaned_df]

            except Exception as e:
                st.error(f"Error reading sheet '{sheet_name}': {e}")

        for source, selected_techs in st.session_state[session_key].items():
            source_bus = source_bus_map.get(source)

            for tech in selected_techs:
                if tech == "PV plant":
                    continue
                sheet_name, tag = tech_sheet_map[tech]

                try:
                    existing_df = pd.read_excel(excel_file, sheet_name=sheet_name)
                    is_local_template = existing_df["label"].astype(str).str.contains(
                        r"_LC(?:_|\d)", case=False, regex=True, na=False
                    )
                    template_rows = existing_df[
                        (existing_df["label"].astype(str).str.startswith(tag)) &
                        (existing_df["active"] == 0) &
                        (~is_local_template)
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
                        if orig_label.lower().endswith("_x"):
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

        if _scenario_system_type(excel_file) == "District heating (centralized)":
            try:
                meta = _scenario_meta_values(excel_file)
                roof_area = pd.to_numeric(
                    meta.get("Maximum PV Roof Area (m2)"), errors="coerce"
                )
                if pd.isna(roof_area):
                    roof_area = _default_pv_roof_area_m2()
                pv_selected = "PV plant" in st.session_state[session_key].get(
                    PV_SOURCE_DISPLAY, []
                )
                _configure_scenario_pv(
                    excel_file, float(roof_area), enabled=pv_selected
                )
            except Exception as exc:
                st.error(f"PV technology could not be updated: {exc}")
            if _local_network_is_enabled(excel_file):
                try:
                    if local_boosters_visible:
                        update_meta_sheet(
                            excel_file,
                            {
                                "Local Booster Technologies": ",".join(
                                    selected_local_boosters
                                )
                            },
                        )
                    _apply_local_booster_configuration(excel_file)
                except Exception as exc:
                    st.error(f"Local booster components could not be updated: {exc}")
        elif is_decentralized:
            try:
                selection = {
                    source: list(technologies)
                    for source, technologies in st.session_state[session_key].items()
                    if technologies
                }
                update_meta_sheet(
                    excel_file,
                    {
                        "Decentral Technology Selection JSON": json.dumps(
                            selection, ensure_ascii=False, sort_keys=True
                        )
                    },
                )
                _apply_decentral_configuration(excel_file)
                meta = _scenario_meta_values(excel_file)
                roof_area = pd.to_numeric(
                    meta.get("Maximum PV Roof Area (m2)"), errors="coerce"
                )
                if pd.isna(roof_area):
                    roof_area = _default_pv_roof_area_m2()
                pv_selected = "PV plant" in st.session_state[session_key].get(
                    PV_SOURCE_DISPLAY, []
                )
                _configure_scenario_pv(
                    excel_file, float(roof_area), enabled=pv_selected
                )
            except Exception as exc:
                st.error(
                    f"Decentral consumer clusters could not be updated: {exc}"
                )

        try:
            _configure_chp_feed_in(excel_file)
        except Exception as exc:
            st.error(f"CHP feed-in component could not be updated: {exc}")

        _set_scenario_section_dirty(excel_file, "Generation technologies", False)
        st.success("Technologies updated.")

    # --- Summary Display ---
    st.divider()
    st.write("### Saved technologies")
    st.markdown(
        "#### Decentral plants" if is_decentralized
        else "#### Central heating plant"
    )
    for source in source_df["label"]:
        if is_decentralized and source == PV_SOURCE_DISPLAY:
            continue
        techs = st.session_state[session_key].get(source, [])
        if techs:
            tech_names = [display_names.get(t, t) for t in techs]
            st.markdown(f"**{source}** → {', '.join(tech_names)}")
    if is_decentralized:
        pv_technologies = st.session_state[session_key].get(
            PV_SOURCE_DISPLAY, []
        )
        if pv_technologies:
            st.markdown("#### Aggregated PV system")
            pv_names = [display_names.get(t, t) for t in pv_technologies]
            st.markdown(
                f"**{PV_SOURCE_DISPLAY}** → {', '.join(pv_names)}"
            )
    if local_boosters_visible:
        st.markdown("#### Local booster plants")
        for local_source, local_options in (
            LOCAL_BOOSTER_TECHNOLOGIES_BY_SOURCE.items()
        ):
            local_names = [
                "Power-to-heat (P2H)" if item == "P2H"
                else "Air-source heat pump (ASHP)"
                for item in selected_local_boosters
                if item in local_options
            ]
            st.markdown(
                f"**{local_source}** → "
                + (", ".join(local_names) if local_names else "_none selected_")
            )


def run_storage_page(excel_file):
    is_decentralized = (
        _scenario_system_type(excel_file) == "Decentralized solution"
    )
    local_storage_section_available = False
    if _local_network_is_enabled(excel_file):
        local_base = _local_network_base_demand(excel_file)
        _, local_has_ht = _temperature_mix(local_base)
        local_storage_section_available = bool(local_has_ht)
    st.markdown(
        "Configure optional thermal storage separately for each decentral "
        "consumer cluster."
        if is_decentralized else
        "Configure optional thermal storage for the central heating plant and, "
        "where applicable, for local consumer clusters."
    )
    scenario_id = os.path.basename(excel_file)

    # Load storage sheet and extract any active configuration
    try:
        df = pd.read_excel(excel_file, sheet_name="storages")
        active_row = df[(df["label"] == "Storage_th") & (df["active"] == 1)]
        storage_exists = not active_row.empty
        default_kwh = parse_number(active_row.iloc[0]["max capacity"]) if storage_exists else 0.0
    except Exception:
        storage_exists = False
        default_kwh = 0.0
    decentral_storage_modes = [
        "Fixed capacity per consumer",
        "Three days of DHW demand",
    ]
    saved_decentral_mode = decentral_storage_modes[0]
    if is_decentralized:
        decentral_meta = _scenario_meta_values(excel_file)
        storage_exists = _meta_bool(
            decentral_meta.get("Decentral Storage Enabled")
        )
        saved_decentral_capacity = pd.to_numeric(
            decentral_meta.get(
                "Decentral Storage Max Capacity per Consumer (kWh)",
                decentral_meta.get(
                    "Decentral Storage Max Capacity per Cluster (kWh)"
                ),
            ),
            errors="coerce",
        )
        default_kwh = (
            float(saved_decentral_capacity)
            if pd.notna(saved_decentral_capacity) else 0.0
        )
        saved_decentral_mode = str(
            decentral_meta.get("Decentral Storage Capacity Mode")
            or decentral_storage_modes[0]
        )
        if saved_decentral_mode not in decentral_storage_modes:
            saved_decentral_mode = decentral_storage_modes[0]

    storage_key = f"use_storage_{scenario_id}"
    draft_key = f"storage_draft_kwh_{scenario_id}"
    if draft_key not in st.session_state:
        st.session_state[draft_key] = float(default_kwh)
    if storage_key not in st.session_state:
        st.session_state[storage_key] = storage_exists

    central_box = st.container(border=True)
    with central_box:
        st.subheader(
            "Decentral consumer thermal storage"
            if is_decentralized else
            "Central heating plant thermal storage"
            if local_storage_section_available else "Thermal storage"
        )
        st.caption(
            "Choose a fixed limit per consumer or use "
            "three days of its assigned DHW demand as maximum capacity."
            if is_decentralized else
            "This storage is connected to the central heating plant."
        )
        use_storage = st.checkbox(
            "Allow decentral thermal storage"
            if is_decentralized else "Allow central thermal storage",
            key=storage_key,
        )
        decentral_storage_mode = (
            st.radio(
                "Decentral storage maximum capacity method",
                decentral_storage_modes,
                index=decentral_storage_modes.index(saved_decentral_mode),
                disabled=not use_storage,
                key=f"decentral_storage_mode_{scenario_id}",
            )
            if is_decentralized else decentral_storage_modes[0]
        )
        fixed_decentral_storage = (
            not is_decentralized
            or decentral_storage_mode == decentral_storage_modes[0]
        )
        unit = st.radio(
            "Storage input unit" if is_decentralized else "Central storage input unit",
            ["kWh", "m³"],
            key=f"storage_unit_{scenario_id}",
            disabled=not use_storage or not fixed_decentral_storage,
        )
        last_unit_key = f"storage_last_unit_{scenario_id}"
        if st.session_state.get(last_unit_key) != unit:
            target_key = (
                f"storage_cap_kwh_{scenario_id}"
                if unit == "kWh" else f"storage_cap_m3_{scenario_id}"
            )
            st.session_state.pop(target_key, None)
            st.session_state.pop(f"_last_valid_number_{target_key}", None)
            st.session_state.pop(f"_number_draft_{target_key}", None)
            st.session_state[last_unit_key] = unit
        max_kwh = 0.0
        draft_kwh = float(st.session_state.get(draft_key, default_kwh))
        if use_storage and fixed_decentral_storage and unit == "kWh":
            max_kwh = max(
                0.0,
                dot_number_input(
                    "Maximum capacity per original consumer (kWh)"
                    if is_decentralized else
                    "Central storage maximum capacity (kWh)",
                    draft_kwh,
                    key=f"storage_cap_kwh_{scenario_id}"
                )
            )
        elif use_storage and fixed_decentral_storage:
            default_m3 = draft_kwh / 1.163
            max_m3 = max(
                0.0,
                dot_number_input(
                    "Maximum volume per original consumer (m³)"
                    if is_decentralized else
                    "Central storage maximum volume (m³)",
                    default_m3,
                    key=f"storage_cap_m3_{scenario_id}"
                )
            )
            max_kwh = max_m3 * 1.163
        if use_storage and fixed_decentral_storage:
            st.session_state[draft_key] = float(max_kwh)
    effective_use_storage = bool(
        use_storage
        and (
            (
                is_decentralized
                and decentral_storage_mode == decentral_storage_modes[1]
            )
            or max_kwh > 0
        )
    )
    storage_changed = (
        effective_use_storage != bool(storage_exists)
        or (
            is_decentralized
            and decentral_storage_mode != saved_decentral_mode
        )
        or (
            effective_use_storage
            and (
                not is_decentralized
                or decentral_storage_mode == decentral_storage_modes[0]
            )
            and not np.isclose(
                float(max_kwh), float(default_kwh), rtol=0, atol=1e-6
            )
        )
    )

    local_enabled = False
    local_effective_enabled = False
    local_mode = "Fixed capacity per local consumer"
    local_capacity = 0.0
    local_changed = False
    if local_storage_section_available:
        local_meta = _scenario_meta_values(excel_file)
        local_enabled = _meta_bool(local_meta.get("Local Storage Enabled"))
        local_modes = [
            "Fixed capacity per local consumer",
            "Three days of each local consumer's summer DHW demand",
        ]
        saved_local_mode = str(
            local_meta.get("Local Storage Capacity Mode") or local_modes[0]
        )
        if saved_local_mode not in local_modes:
            saved_local_mode = local_modes[0]
        saved_local_capacity = pd.to_numeric(
            local_meta.get("Local Storage Max Capacity per Consumer (kWh)"),
            errors="coerce",
        )
        saved_local_capacity = (
            float(saved_local_capacity)
            if pd.notna(saved_local_capacity) else 0.0
        )
        local_box = st.container(border=True)
        with local_box:
            st.subheader("Local consumer thermal storage")
            st.caption(
                "This storage is connected separately to each local consumer/consumer-cluster."
            )
            use_local_storage = st.checkbox(
                "Allow local thermal storage",
                value=local_enabled,
                key=f"use_local_storage_{scenario_id}",
            )
            local_mode = st.radio(
                "Local storage maximum capacity method",
                local_modes,
                index=local_modes.index(saved_local_mode),
                disabled=not use_local_storage,
                key=f"local_storage_mode_{scenario_id}",
            )
            local_capacity = st.number_input(
                "Local storage maximum capacity per consumer (kWh)",
                min_value=0.0,
                value=saved_local_capacity,
                disabled=(
                    not use_local_storage or local_mode != local_modes[0]
                ),
                key=f"local_storage_capacity_{scenario_id}",
            )
        local_effective_enabled = bool(
            use_local_storage
            and (local_mode == local_modes[1] or float(local_capacity) > 0)
        )
        local_changed = (
            local_effective_enabled != local_enabled
            or local_mode != saved_local_mode
            or not np.isclose(
                float(local_capacity), saved_local_capacity, rtol=0, atol=1e-6
            )
        )

    _set_scenario_section_dirty(
        excel_file, "Storage", storage_changed or local_changed
    )
    save_storage = st.button(
        "Save storage configurations",
        key=f"save_all_storage_{scenario_id}",
    )

    if save_storage:
        try:
            df = pd.read_excel(excel_file, sheet_name="storages")

            # Clean existing active non-template row
            df = df[~((df["label"] == "Storage_th") & (df["active"] == 1))]

            if effective_use_storage and not is_decentralized:
                central_bus = (
                    "b_th_LT" if _local_network_is_enabled(excel_file)
                    else "b_th_HT"
                )
                template_row = df[
                    (df["label"] == "Storage_th")
                    & (df["active"] == 0)
                    & (df["bus"].astype(str) == central_bus)
                ]
                if template_row.empty:
                    template_row = df[
                        (df["label"] == "Storage_th") & (df["active"] == 0)
                    ]

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

            saved_fixed_capacity = float(
                max_kwh
                if not is_decentralized or fixed_decentral_storage
                else default_kwh
            )
            st.session_state[draft_key] = saved_fixed_capacity
            if is_decentralized:
                update_meta_sheet(
                    excel_file,
                    {
                        "Decentral Storage Enabled": str(effective_use_storage),
                        "Decentral Storage Capacity Mode": decentral_storage_mode,
                        "Decentral Storage Max Capacity per Consumer (kWh)": float(
                            saved_fixed_capacity
                        ),
                    },
                )
                _apply_decentral_configuration(excel_file)
            elif local_storage_section_available:
                update_meta_sheet(
                    excel_file,
                    {
                        "Local Storage Enabled": str(local_effective_enabled),
                        "Local Storage Capacity Mode": local_mode,
                        "Local Storage Max Capacity per Consumer (kWh)": float(
                            local_capacity
                        ),
                    },
                )
                _apply_local_booster_configuration(excel_file)
            _set_scenario_section_dirty(excel_file, "Storage", False)
            st.success(
                "Decentral storage configuration saved."
                if is_decentralized else
                "Central and local storage configurations saved."
                if local_storage_section_available
                else "Storage configuration saved."
            )
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
    missing_network_lengths = _project_missing_network_lengths(active_project)
    local_configuration_errors = {}
    unsaved_by_scenario = {}
    for scenario in list_scenarios(project_name=active_project):
        scenario_file = get_scenario_file(scenario, project_name=active_project)
        local_error = _local_booster_validation_error(scenario_file)
        if local_error:
            local_configuration_errors[scenario] = local_error
        sections = _scenario_unsaved_sections(scenario_file)
        if sections:
            unsaved_by_scenario[scenario] = sections

    if missing_network_lengths:
        st.warning(
            "Enter or estimate a network length before submitting these centralized scenarios: "
            f"**{', '.join(missing_network_lengths)}**. Open each scenario under "
            "**Heat Supply Scenarios → Network Length**."
        )
    if unsaved_by_scenario:
        details = "; ".join(
            f"{scenario}: {', '.join(sections)}"
            for scenario, sections in unsaved_by_scenario.items()
        )
        st.warning(
            f"Unsaved changes: **{details}**. Save them if they should be included in the simulation. "
            "If you do not want to make or save these changes, you can ignore this warning and submit "
            "the project using the previously saved values."
        )
    if local_configuration_errors:
        st.warning(
            "Complete the LT-network configuration before submitting: "
            + "; ".join(
                f"**{scenario}**: {error}"
                for scenario, error in local_configuration_errors.items()
            )
        )

    if run_state == "queued":
        st.info("Simulation is queued. This page will update automatically when it starts.")
    elif run_state == "running":
        scenario = run_status.get("scenario")
        index = run_status.get("scenario_index")
        count = run_status.get("scenario_count")
        progress = f" ({index} of {count})" if index and count else ""
        scenario_line = f"🖥️ Current simulated scenario{progress}: **{scenario}**" if scenario else "🖥️ Preparing the first scenario."
        st.info(
            f"{scenario_line}\n\n"
            "Simulation is running. Please keep this webpage open; it will update automatically.\n\n"
            "Depending on its complexity, one scenario can take seconds or several minutes to simulate."
        )
    elif run_state == "failed":
        failed_scenario = run_status.get("scenario")
        scenario_text = f" for scenario **{failed_scenario}**" if failed_scenario else ""
        st.error(
            f"The last simulation failed{scenario_text}: "
            f"{run_status.get('error', 'Unknown error')}"
        )
    elif run_state == "interrupted":
        st.warning(
            "The previous simulation was interrupted before completion. "
            "You can submit the project again to start a new simulation."
        )
    elif run_state == "completed" and results_current:
        st.success("Simulation completed.")
        if st.session_state.get("navigate_to_results_when_complete") == active_project:
            st.session_state.pop("navigate_to_results_when_complete", None)
            st.session_state["results_project"] = active_project
            st.session_state["results_project_selector"] = active_project
            st.session_state["requested_page"] = "Simulation Results"
            st.rerun()

    col_a, col_b = st.columns([1, 1])
    with col_a:
        if missing_network_lengths or local_configuration_errors:
            st.button(
                "Submit this project",
                use_container_width=True,
                disabled=True,
                help=(
                    "Add a network length and complete every LT-network configuration "
                    "before submitting."
                ),
                key=f"pm_submit_missing_network_{active_project}",
            )
        elif has_results and results_current and not is_running and not unsaved_by_scenario:
            with st.popover("Submit this project", use_container_width=True):
                st.info("No input changes have been made since the current results were produced.")
                if st.button("Yes, submit again", type="primary", key=f"pm_confirm_unchanged_submit_{active_project}"):
                    if submit_project(active_project):
                        st.session_state["navigate_to_results_when_complete"] = active_project
                        st.rerun()
        elif st.button(
            "Submit this project",
            use_container_width=True,
            disabled=is_running,
            help="A simulation is already running." if is_running else None,
            key=f"pm_submit_{active_project}",
        ):
            if submit_project(active_project):
                st.session_state["navigate_to_results_when_complete"] = active_project
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
            solar_station = meta.get("dwd_solar_station") or "_Not loaded_"
            solar_station_id = meta.get("dwd_solar_station_id") or "unknown ID"
            solar_years = (
                (meta.get("dwd_weather") or {}).get("solar_profile_years_used")
                or meta.get("dwd_solar_profile_years_used")
                or []
            )
            coords = meta.get("project_coords") or None
            saved_weather = _load_project_weather(active_project, coords)
            if saved_weather:
                solar_station = (
                    saved_weather.get("solar_station_name") or solar_station
                )
                solar_station_id = (
                    saved_weather.get("solar_station_id") or solar_station_id
                )
                solar_years = (
                    saved_weather.get("solar_profile_years_used") or solar_years
                )
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
            st.markdown(
                f"- **DWD solar radiation:** {solar_station} (station {solar_station_id}); "
                f"profile years {', '.join(map(str, solar_years)) or '_Not loaded_'}"
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
                    round(values["space_heating_kwh"], 2),
                    round(values["dhw_kwh"], 2),
                )
                for values in scenario_demands.values()
            }
            common_demand = len(demand_signatures) == 1
            if common_demand:
                _render_demand_summary_row(next(iter(scenario_demands.values())), "Demand used by all scenarios")

            for sc in scenarios:
                with st.expander(f"Scenario: {sc}", expanded=True):
                    scenario_file = get_scenario_file(sc, project_name=active_project)
                    unsaved_sections = _scenario_unsaved_sections(scenario_file)
                    if unsaved_sections:
                        st.warning(
                            "Unsaved changes: "
                            f"**{', '.join(unsaved_sections)}**. Return to this scenario on "
                            "**Heat Supply Scenarios** and click the relevant Save button if these changes "
                            "should be included. If you do not want to make or save the changes, you can "
                            "ignore this warning."
                        )
                    render_scenario_summary(active_project, sc, show_demand=not common_demand)

            # Bulk delete scenarios
            with st.expander("Delete scenarios", expanded=False):
                project_run_state = read_run_status(active_project).get("state")
                deletion_disabled = project_run_state in {"queued", "running"}
                if deletion_disabled:
                    st.info(
                        "Scenarios cannot be deleted while this project is queued or being simulated."
                    )
                with st.form(
                    f"pm_delete_scenarios_form_{active_project}",
                    clear_on_submit=True,
                ):
                    to_delete = st.multiselect(
                        "Select scenarios to delete",
                        scenarios,
                        key=f"pm_delete_scenarios_{active_project}",
                        disabled=deletion_disabled,
                    )
                    st.warning(
                        "The selected scenarios will be permanently deleted when "
                        "you click the button below."
                    )
                    delete_selected = st.form_submit_button(
                        "🗑️ Delete selected scenarios",
                        type="primary",
                        use_container_width=True,
                        disabled=deletion_disabled,
                    )

                if delete_selected:
                    if not to_delete:
                        st.warning("Select at least one scenario to delete.")
                    else:
                        deleted_names = []
                        failed_names = []
                        for name in to_delete:
                            if delete_scenario(name, project_name=active_project):
                                deleted_names.append(name)
                                # Clear cached UI keyed by the deleted workbook.
                                scenario_prefix = f"{name}.xlsx"
                                keys = [
                                    k for k in st.session_state.keys()
                                    if scenario_prefix in k
                                ]
                                for k in keys:
                                    del st.session_state[k]
                            else:
                                failed_names.append(name)
                        if deleted_names and not failed_names:
                            st.rerun()
                        elif deleted_names:
                            st.warning(
                                "Deleted: " + ", ".join(deleted_names)
                                + ". Still locked: " + ", ".join(failed_names) + "."
                            )
                        elif failed_names:
                            st.warning(
                                "The following scenarios are still locked and "
                                "could not be deleted: " + ", ".join(failed_names) + "."
                            )

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
    projects = sorted(list_projects(), key=natural_sort_key)
    if not projects:
        st.info("No projects are available yet.")
        return

    preferred = st.session_state.get("results_project") or get_current_project()
    selector_key = "results_project_selector"
    if st.session_state.get(selector_key) not in projects:
        st.session_state[selector_key] = preferred if preferred in projects else projects[0]
    project = st.selectbox("Project", projects, key=selector_key)
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
        scenario_line = f"🖥️ Current simulated scenario{progress}: **{scenario}**" if scenario else "🖥️ Preparing the first scenario."
        st.info(
            f"{scenario_line}\n\n"
            f"Simulation is {run_state}. Please keep this webpage open; it will update automatically.\n\n"
            "Depending on its complexity, one scenario can take seconds or several minutes to simulate."
        )
        import time
        time.sleep(3)
        st.rerun()
    if run_state == "failed":
        failed_scenario = run_status.get("scenario")
        scenario_text = f" for scenario **{failed_scenario}**" if failed_scenario else ""
        st.error(
            f"The last simulation failed{scenario_text}: "
            f"{run_status.get('error', 'Unknown error')}"
        )
    if run_state == "interrupted":
        st.warning(
            "The previous simulation was interrupted before completion. "
            "Return to **Project Review & Simulation** to submit it again."
        )
    if not has_results:
        st.info(
            "No completed simulation results are available for this project. Submit the project on "
            "the **Project Review & Simulation** page to run simulation and optimization and create results."
        )
        return

    result_files = sorted(results_dir.rglob("results.xlsx"))
    results_signature = tuple(
        (
            str(path),
            path.stat().st_mtime_ns,
            path.stat().st_size,
        )
        for path in result_files
    )
    viewer_cache_key = f"_loaded_results_viewer_{project}"
    cached_viewer = st.session_state.get(viewer_cache_key)
    results_already_loaded = bool(
        isinstance(cached_viewer, dict)
        and cached_viewer.get("signature") == results_signature
        and cached_viewer.get("viewer") is not None
    )

    results_current = results_match_current_inputs(project, run_status)
    if not results_current:
        st.warning(
            "Changes have been made to the input data or project/scenario settings since this simulation was run. "
            "These results are therefore not up to date. Submit the project again to create current results."
        )
        if not results_already_loaded:
            st.info(
                "The outdated result files have not been loaded. Load them only if "
                "you want to inspect the previous simulation."
            )
            if not st.button(
                "Show old results",
                key=f"show_outdated_results_{project}",
            ):
                return
    else:
        st.success("These results match the current project inputs.")

    if results_already_loaded:
        viewer = cached_viewer["viewer"]
    else:
        from logic.show_results import MultiScenarioViewer
        viewer = MultiScenarioViewer(project)
        st.session_state[viewer_cache_key] = {
            "signature": results_signature,
            "viewer": viewer,
        }
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
