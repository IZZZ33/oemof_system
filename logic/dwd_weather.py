"""Coordinate-based DWD weather data and heat-pump COP helpers.

The downloader uses the open Climate Data Center (CDC) hourly air-temperature
station observations. Downloads are cached because the historical station
archives can contain several decades of data.
"""

from __future__ import annotations

import io
import math
import re
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd


DWD_HOURLY_BASE = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    "climate/hourly/air_temperature/historical"
)
DWD_SOIL_BASE = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    "climate/hourly/soil_temperature/historical"
)
CACHE_DIR = Path(__file__).resolve().parents[1] / "cache" / "dwd"
STATION_FILE = "TU_Stundenwerte_Beschreibung_Stationen.txt"
DIN_REFERENCE_YEARS = 20


def _download(url: str, target: Path, max_age_days: int | None = None) -> bytes:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (
        max_age_days is None
        or time.time() - target.stat().st_mtime <= max_age_days * 86400
    ):
        return target.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "oemof-system/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = response.read()
    target.write_bytes(payload)
    return payload


def _station_metadata() -> pd.DataFrame:
    payload = _download(
        f"{DWD_HOURLY_BASE}/{STATION_FILE}",
        CACHE_DIR / STATION_FILE,
        max_age_days=30,
    )
    text = payload.decode("cp1252", errors="replace")
    rows = []
    pattern = re.compile(
        r"^\s*(\d{5})\s+(\d{8})\s+(\d{8})\s+(-?\d+)\s+"
        r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(.+?)\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        station_id, start, end, height, lat, lon, label = match.groups()
        station_name = re.split(r"\s{2,}", label.strip(), maxsplit=1)[0]
        rows.append(
            {
                "station_id": station_id,
                "start": pd.to_datetime(start, format="%Y%m%d", errors="coerce"),
                "end": pd.to_datetime(end, format="%Y%m%d", errors="coerce"),
                "height_m": float(height),
                "latitude": float(lat),
                "longitude": float(lon),
                "station_name": station_name,
            }
        )
    if not rows:
        raise ValueError("DWD station metadata could not be parsed.")
    return pd.DataFrame(rows)


def _distance_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    p1 = np.radians(float(lat1))
    p2 = np.radians(pd.to_numeric(lat2, errors="coerce").to_numpy(float))
    dp = p2 - p1
    dl = np.radians(pd.to_numeric(lon2, errors="coerce").to_numpy(float) - float(lon1))
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return radius * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def _nearest_station(latitude: float, longitude: float) -> dict:
    stations = _station_metadata().dropna(subset=["latitude", "longitude", "start", "end"]).copy()
    latest_complete_year = pd.Timestamp.utcnow().year - 1
    # Prefer a station with enough history for a DIN/TS-like design-temperature
    # statistic and data through the latest complete calendar year.
    eligible = stations.loc[
        (stations["start"].dt.year <= latest_complete_year - DIN_REFERENCE_YEARS + 1)
        & (stations["end"].dt.year >= latest_complete_year)
    ].copy()
    if eligible.empty:
        eligible = stations.loc[stations["end"].dt.year >= latest_complete_year].copy()
    if eligible.empty:
        eligible = stations.copy()
    eligible["distance_km"] = _distance_km(
        latitude, longitude, eligible["latitude"], eligible["longitude"]
    )
    return eligible.sort_values("distance_km").iloc[0].to_dict()


def _historical_directory() -> str:
    payload = _download(
        f"{DWD_HOURLY_BASE}/",
        CACHE_DIR / "historical_index.html",
        max_age_days=7,
    )
    return payload.decode("utf-8", errors="replace")


def _station_archive_name(station_id: str) -> str:
    listing = _historical_directory()
    pattern = re.compile(
        rf'href=["\'](stundenwerte_TU_{re.escape(station_id)}_\d{{8}}_\d{{8}}_hist\.zip)["\']',
        flags=re.IGNORECASE,
    )
    matches = pattern.findall(listing)
    if not matches:
        raise FileNotFoundError(f"No DWD historical hourly archive found for station {station_id}.")
    return sorted(matches)[-1]


def _station_hourly_data(station_id: str) -> pd.DataFrame:
    archive_name = _station_archive_name(station_id)
    payload = _download(
        f"{DWD_HOURLY_BASE}/{archive_name}", CACHE_DIR / archive_name
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        product_names = [
            name for name in archive.namelist()
            if Path(name).name.lower().startswith("produkt_tu_stunde")
        ]
        if not product_names:
            raise ValueError(f"DWD archive {archive_name} has no hourly temperature product.")
        with archive.open(product_names[0]) as source:
            data = pd.read_csv(source, sep=";", dtype=str)
    data.columns = [str(column).strip() for column in data.columns]
    if "MESS_DATUM" not in data or "TT_TU" not in data:
        raise ValueError("DWD hourly product is missing MESS_DATUM or TT_TU.")
    timestamp = pd.to_datetime(data["MESS_DATUM"].str.strip(), format="%Y%m%d%H", errors="coerce")
    temperature = pd.to_numeric(data["TT_TU"].str.strip(), errors="coerce").replace(-999, np.nan)
    out = pd.DataFrame({"temperature_c": temperature.to_numpy()}, index=timestamp)
    out = out.loc[out.index.notna()].sort_index()
    return out.loc[~out.index.duplicated(keep="last")]


def _complete_hourly_year(data: pd.DataFrame, requested_year: int):
    """Return one 8760-hour year, or None when data completeness is below 95%."""
    year = int(requested_year)
    start = pd.Timestamp(year, 1, 1)
    end = pd.Timestamp(year + 1, 1, 1)
    expected = pd.date_range(start, end, freq="h", inclusive="left")
    # Scenario templates always contain 8760 hours. Remove 29 February for
    # leap years so timestamps and optimization input remain aligned.
    expected = expected[~((expected.month == 2) & (expected.day == 29))]
    values = data.reindex(expected)["temperature_c"]
    if values.notna().mean() < 0.95:
        return None
    values = values.interpolate(limit=12, limit_direction="both").ffill().bfill()
    return pd.Series(values.to_numpy(float), index=expected, name="temperature_c")


def _multi_year_temperature_profile(
    data: pd.DataFrame, requested_year: int | None = None, max_years: int = 5
):
    """Average annual order statistics and restore a reference-year chronology.

    Averaging temperatures at the same calendar hour would smooth cold and hot
    events that occur on different dates. Instead, each complete year is sorted,
    equal ranks are averaged, and those averaged ranks are assigned to the
    timestamps holding the corresponding ranks in the latest/reference year.
    """
    complete = {}
    candidates = sorted(set(data.index.year), reverse=True)
    for year in candidates:
        profile = _complete_hourly_year(data, int(year))
        if profile is not None:
            complete[int(year)] = profile

    if not complete:
        raise ValueError("The selected DWD station has no sufficiently complete hourly calendar year.")
    if requested_year is not None:
        if int(requested_year) not in complete:
            raise ValueError(f"DWD station data for {requested_year} are not at least 95% complete.")
        reference_year = int(requested_year)
        eligible_years = [year for year in sorted(complete, reverse=True) if year <= reference_year]
    else:
        reference_year = max(complete)
        eligible_years = sorted(complete, reverse=True)
    used_years = eligible_years[: max(int(max_years), 1)]
    reference = complete[reference_year]
    sorted_matrix = np.vstack(
        [np.sort(complete[year].to_numpy(float), kind="mergesort") for year in used_years]
    )
    mean_sorted = np.mean(sorted_matrix, axis=0)
    reference_values = reference.to_numpy(float)
    reference_order = np.argsort(reference_values, kind="mergesort")
    composite = np.empty_like(mean_sorted)
    composite[reference_order] = mean_sorted
    return (
        reference_year,
        pd.Series(composite, index=reference.index, name="temperature_c"),
        sorted(complete),
        sorted(used_years),
    )


def _legacy_complete_hourly_year(data: pd.DataFrame, requested_year: int | None = None):
    """Backward-compatible single-year selector retained for external callers."""
    candidates = [requested_year] if requested_year is not None else sorted(set(data.index.year), reverse=True)
    for year in candidates:
        if year is None:
            continue
        profile = _complete_hourly_year(data, int(year))
        if profile is not None:
            return int(year), profile
    raise ValueError("The selected DWD station has no sufficiently complete hourly calendar year.")


def _design_parameters(data: pd.DataFrame):
    latest_year = int(data.index.year.max())
    start_year = latest_year - DIN_REFERENCE_YEARS + 1
    reference = data.loc[(data.index.year >= start_year) & (data.index.year <= latest_year)].copy()
    daily = reference["temperature_c"].resample("D").mean().dropna()
    two_day_means = daily.rolling(2).mean().dropna()
    if two_day_means.empty:
        raise ValueError("Not enough DWD observations to derive design parameters.")
    # Count independent cold spells, rather than adjacent rolling windows from
    # the same event. DIN/TS 12831-1 describes a two-day mean reached or
    # undercut ten times in twenty years. A four-day separation prevents one
    # prolonged cold spell from occupying several of those ten occurrences.
    occurrences = []
    for timestamp, value in two_day_means.sort_values().items():
        if all(abs((timestamp - prior).days) > 3 for prior, _ in occurrences):
            occurrences.append((timestamp, float(value)))
        if len(occurrences) == 10:
            break
    if not occurrences:
        raise ValueError("No independent two-day cold events found in DWD observations.")
    design_outdoor_c = float(occurrences[-1][1])
    ground_temperature_c = float(reference["temperature_c"].mean())
    return design_outdoor_c, ground_temperature_c, start_year, latest_year


def _soil_temperature_mean(station_id: str, start_year: int, end_year: int):
    """Return the best available DWD soil-temperature mean and its depth."""
    listing = _download(
        f"{DWD_SOIL_BASE}/", CACHE_DIR / "soil_historical_index.html", max_age_days=7
    ).decode("utf-8", errors="replace")
    pattern = re.compile(
        rf'href=["\'](stundenwerte_EB_{re.escape(station_id)}_\d{{8}}_\d{{8}}_hist\.zip)["\']',
        flags=re.IGNORECASE,
    )
    matches = pattern.findall(listing)
    if not matches:
        return None
    archive_name = sorted(matches)[-1]
    payload = _download(f"{DWD_SOIL_BASE}/{archive_name}", CACHE_DIR / archive_name)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        products = [
            name for name in archive.namelist()
            if Path(name).name.lower().startswith("produkt_eb_stunde")
        ]
        if not products:
            return None
        with archive.open(products[0]) as source:
            data = pd.read_csv(source, sep=";", dtype=str)
    data.columns = [str(column).strip() for column in data.columns]
    timestamp = pd.to_datetime(
        data.get("MESS_DATUM", pd.Series(dtype=str)).str.strip(),
        format="%Y%m%d%H",
        errors="coerce",
    )
    depth_column = next(
        (column for column in ("V_TE100", "V_TE050", "V_TE020", "V_TE010") if column in data.columns),
        None,
    )
    if depth_column is None:
        return None
    values = pd.to_numeric(data[depth_column].str.strip(), errors="coerce").replace(-999, np.nan)
    mask = timestamp.dt.year.between(int(start_year), int(end_year))
    selected = values.loc[mask].dropna()
    if selected.empty:
        return None
    depth_cm = int(re.sub(r"\D", "", depth_column))
    return float(selected.mean()), depth_cm


def get_location_weather(latitude: float, longitude: float, year: int | None = None) -> dict:
    """Return nearest-station DWD weather and derived TEASER boundary values."""
    latitude, longitude = float(latitude), float(longitude)
    if not (47.0 <= latitude <= 55.5 and 5.0 <= longitude <= 16.0):
        raise ValueError("DWD station weather is currently supported for locations in Germany.")
    station = _nearest_station(latitude, longitude)
    hourly_all = _station_hourly_data(station["station_id"])
    weather_year, hourly, available_years, used_years = _multi_year_temperature_profile(
        hourly_all, requested_year=year, max_years=5
    )
    design_c, ground_c, reference_start, reference_end = _design_parameters(hourly_all)
    ground_method = "20-year mean air temperature used as ground-boundary proxy"
    try:
        observed_ground = _soil_temperature_mean(
            station["station_id"], reference_start, reference_end
        )
        if observed_ground is not None and math.isfinite(observed_ground[0]):
            ground_c, ground_depth_cm = observed_ground
            ground_method = (
                f"DWD mean observed soil temperature at {ground_depth_cm} cm "
                "(latest 20-year period)"
            )
    except Exception:
        pass
    return {
        "temperature_c": hourly,
        "year": weather_year,
        "available_complete_years": available_years,
        "available_complete_year_count": len(available_years),
        "profile_years_used": used_years,
        "profile_year_count": len(used_years),
        "profile_method": (
            "mean of sorted annual temperature profiles mapped to the rank chronology "
            f"of reference year {weather_year}"
        ),
        "design_outdoor_temperature_c": design_c,
        "ground_temperature_c": ground_c,
        "design_reference_start": reference_start,
        "design_reference_end": reference_end,
        "station_id": station["station_id"],
        "station_name": station["station_name"],
        "station_latitude": float(station["latitude"]),
        "station_longitude": float(station["longitude"]),
        "station_distance_km": float(station["distance_km"]),
        "source": "DWD Climate Data Center hourly air-temperature observations",
        "design_method": "10th independent two-day cold-event mean in the latest available 20-year station period",
        "ground_method": ground_method,
    }


def heat_pump_cop_profiles(temperature_c, ground_temperature_c: float) -> pd.DataFrame:
    """Calculate transparent hourly COP series from source/sink temperatures.

    A practical Carnot-efficiency model is used because DWD supplies source
    temperature, not manufacturer performance maps. Project-specific equipment
    curves can still replace these scenario time-series later.
    """
    air = pd.to_numeric(pd.Series(temperature_c), errors="coerce").interpolate().ffill().bfill()
    annual_ground = float(ground_temperature_c)
    collector = air.rolling(24 * 30, min_periods=1, center=True).mean()
    collector = 0.65 * collector + 0.35 * annual_ground
    borehole = pd.Series(annual_ground, index=air.index, dtype=float)

    def carnot(source_c, sink_c, quality=0.50, apply_defrost=False):
        source = pd.to_numeric(pd.Series(source_c), errors="coerce").to_numpy(float)
        sink_k = float(sink_c) + 273.15
        lift = np.maximum(float(sink_c) - source, 5.0)
        cop = quality * sink_k / lift
        # Air-source evaporators can frost in cold ambient conditions. Apply
        # the requested simplified defrost derating only to ASHPs; ground and
        # wastewater heat pumps do not have this ambient-air defrost penalty.
        if apply_defrost:
            cop = np.where(source < 5.0, 0.80 * cop, cop)
        return np.clip(cop, 1.1, 10.0)

    profiles = {}
    for suffix, sink in (("HT", 60.0), ("NT", 40.0)):
        profiles[f"ASHP_{suffix}.fix"] = carnot(air, sink, apply_defrost=True)
        profiles[f"GSHP_C_{suffix}.fix"] = carnot(collector, sink)
        profiles[f"GSHP_B_{suffix}.fix"] = carnot(borehole, sink)
    # The model currently has one high-temperature wastewater HP type. In the
    # absence of a project-specific wastewater measurement, use the stable
    # long-term ground/air mean as its source-temperature proxy.
    profiles["WWSHP.fix"] = carnot(borehole, 60.0)
    return pd.DataFrame(profiles)
