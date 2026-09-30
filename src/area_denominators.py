"""Census land-area denominators and EQUI7 ground-area scale.

`county_area_km2` / `place_area_km2` are full legal polygons (land + water)
measured in EPSG:5070. WUI exposure is easier to interpret against Census
land area (`ALAND` / 1e6). Both denominators are kept.

The Schug et al. 2023 North America mosaic is EQUI7 (EPSG:27705), an
azimuthal equidistant CRS, not an equal-area CRS. Nominal 10 m pixels are
100 m² on that map and slightly less on the ground. Nominal areas stay in
`nature_*_area_km2`. Ground-corrected companions multiply by

    polygon area in EPSG:5070 / polygon area in EPSG:27705

evaluated on the county polygon (one scale per county, not a new raster pass).
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PROCESSED_DIR,
    RAW_DIR,
    TABLES_DIR,
    WUI_DIR,
)

SQ_M_PER_SQ_KM = 1_000_000.0
# Tile CRS reported by rasterio for the Schug 2023 NA GeoTIFFs.
EQUI7_NA_EPSG = 27705


def census_km2(square_meters: pd.Series) -> pd.Series:
    """Census ALAND/AWATER are square meters. Return km²."""
    return square_meters.astype("float64") / SQ_M_PER_SQ_KM


def equi7_ground_over_nominal(geometries_5070: gpd.GeoSeries) -> pd.Series:
    """Ground area (EPSG:5070) / map area (EPSG:27705) for each geometry.

    Less than 1 away from the EQUI7 North America center (52°N, 97.5°W).
    Multiplying a nominal pixel area by this scale estimates ground area.
    """
    if geometries_5070.crs is None or geometries_5070.crs.to_epsg() != 5070:
        raise ValueError(
            f"equi7 scale expects EPSG:5070 geometries; got {geometries_5070.crs}"
        )
    ground = geometries_5070.area.to_numpy()
    nominal = geometries_5070.to_crs(EQUI7_NA_EPSG).area.to_numpy()
    scale = np.divide(ground, nominal, out=np.full(len(ground), np.nan), where=nominal > 0)
    return pd.Series(scale, index=geometries_5070.index, name="equi7_ground_over_nominal")


def _pct(numer: pd.Series, denom: pd.Series) -> pd.Series:
    out = numer.astype("float64") / denom.astype("float64") * 100.0
    return out.where(denom.astype("float64") > 0)


def county_denominator_frame(counties: gpd.GeoDataFrame) -> pd.DataFrame:
    """Land, water, and EQUI7 scale keyed by GEOID. Does not alter raw ALAND."""
    if "ALAND" not in counties.columns or "AWATER" not in counties.columns:
        raise ValueError("counties layer is missing Census ALAND/AWATER")
    scale = equi7_ground_over_nominal(counties.geometry)
    return pd.DataFrame({
        "GEOID": counties["GEOID"].astype(str).to_numpy(),
        "county_land_area_km2": census_km2(counties["ALAND"]).to_numpy(),
        "county_water_area_km2": census_km2(counties["AWATER"]).to_numpy(),
        "equi7_ground_over_nominal": scale.to_numpy(),
    })


def attach_silvis_denominators(county_wui: pd.DataFrame, denom: pd.DataFrame) -> pd.DataFrame:
    """Add land/water area and both WUI percentages. Keeps pct_county_wui."""
    out = county_wui.drop(
        columns=[c for c in ("county_land_area_km2", "county_water_area_km2",
                             "pct_total_county_area_wui", "pct_county_land_area_wui")
                 if c in county_wui.columns],
        errors="ignore",
    )
    out = out.merge(
        denom[["GEOID", "county_land_area_km2", "county_water_area_km2"]],
        on="GEOID", how="left",
    )
    # pct_county_wui is the legal-polygon percentage (land + water). The
    # explicit alias makes that denominator unambiguous next to the land %.
    out["pct_total_county_area_wui"] = out["pct_county_wui"]
    out["pct_county_land_area_wui"] = _pct(out["wui_area_km2"], out["county_land_area_km2"])
    return out


def attach_nature_denominators(nature: pd.DataFrame, denom: pd.DataFrame) -> pd.DataFrame:
    """Add land/water denominators and ground-corrected Nature areas.

    Nominal `nature_*_area_km2` values are not replaced or clipped.
    """
    drop = [
        "county_land_area_km2", "county_water_area_km2", "equi7_ground_over_nominal",
        "nature_wui_ground_km2", "nature_classified_ground_km2",
        "pct_total_county_area_nature_wui", "pct_county_land_area_nature_wui",
        "pct_total_county_area_nature_wui_ground", "pct_county_land_area_nature_wui_ground",
    ]
    out = nature.drop(columns=[c for c in drop if c in nature.columns], errors="ignore")
    out = out.merge(denom, on="GEOID", how="left")
    scale = out["equi7_ground_over_nominal"]
    out["nature_wui_ground_km2"] = out["nature_wui_area_km2"] * scale
    out["nature_classified_ground_km2"] = out["nature_classified_area_km2"] * scale
    out["pct_total_county_area_nature_wui"] = out["pct_county_nature_wui"]
    out["pct_county_land_area_nature_wui"] = _pct(
        out["nature_wui_area_km2"], out["county_land_area_km2"]
    )
    out["pct_total_county_area_nature_wui_ground"] = _pct(
        out["nature_wui_ground_km2"], out["county_area_km2"]
    )
    out["pct_county_land_area_nature_wui_ground"] = _pct(
        out["nature_wui_ground_km2"], out["county_land_area_km2"]
    )
    return out


def attach_place_denominators(place_wui: pd.DataFrame, places_area: pd.DataFrame) -> pd.DataFrame:
    """Add Census land area and both place WUI percentages. Keeps pct_place_wui."""
    drop = [
        "place_land_area_km2", "place_water_area_km2",
        "pct_total_place_area_wui", "pct_place_land_area_wui",
    ]
    out = place_wui.drop(columns=[c for c in drop if c in place_wui.columns], errors="ignore")
    out = out.merge(
        places_area[["GEOID", "place_land_area_km2", "place_water_area_km2"]],
        on="GEOID", how="left",
    )
    out["pct_total_place_area_wui"] = out["pct_place_wui"]
    out["pct_place_land_area_wui"] = _pct(out["wui_area_km2"], out["place_land_area_km2"])
    return out


def read_place_aland(raw_dir: Path | None = None) -> pd.DataFrame:
    """Read GEOID, ALAND, AWATER from the 49 CONUS TIGER place zips.

    Attribute-only. Raw archives are not modified.
    """
    import pyogrio

    raw_dir = raw_dir or (RAW_DIR / "census_places")
    frames = []
    zips = sorted(raw_dir.glob("tl_*_*_place.zip"))
    if not zips:
        raise FileNotFoundError(f"No TIGER place zips in {raw_dir}")
    for zpath in zips:
        stem = zpath.stem
        frame = None
        last = None
        for path in (
            f"/vsizip/{zpath}/{stem}.shp",
            f"zip://{zpath}!{stem}.shp",
        ):
            try:
                frame = pyogrio.read_dataframe(
                    path, columns=["GEOID", "ALAND", "AWATER"], read_geometry=False,
                )
                break
            except Exception as exc:  # try the other GDAL zip form
                last = exc
        if frame is None:
            raise RuntimeError(f"Could not read ALAND from {zpath}: {last}")
        frames.append(frame)
    out = pd.concat(frames, ignore_index=True)
    out["GEOID"] = out["GEOID"].astype(str)
    if out["GEOID"].duplicated().any():
        raise ValueError("Duplicate place GEOIDs while reading ALAND")
    out["place_land_area_km2"] = census_km2(out["ALAND"])
    out["place_water_area_km2"] = census_km2(out["AWATER"])
    return out[["GEOID", "ALAND", "AWATER", "place_land_area_km2", "place_water_area_km2"]]


def _write_table(df: pd.DataFrame, parquet_path: Path, csv_name: str | None) -> None:
    df.to_parquet(parquet_path, index=False)
    if csv_name is not None:
        df.to_csv(TABLES_DIR / csv_name, index=False)


def apply_to_existing_outputs() -> dict:
    """Attach denominator columns to existing Stage 2 tables. No raster pass."""
    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    denom = county_denominator_frame(counties)
    counties = counties.drop(
        columns=[c for c in ("county_land_area_km2", "county_water_area_km2")
                 if c in counties.columns],
        errors="ignore",
    )
    counties = counties.merge(
        denom[["GEOID", "county_land_area_km2", "county_water_area_km2"]],
        on="GEOID", how="left",
    )
    counties.to_parquet(PROCESSED_DIR / "counties_conus.geoparquet", index=False)

    county_wui = pd.read_parquet(PROCESSED_DIR / "county_wui_by_vintage.parquet")
    county_wui = attach_silvis_denominators(county_wui, denom)
    _write_table(county_wui, PROCESSED_DIR / "county_wui_by_vintage.parquet",
                 "county_wui_by_vintage.csv")

    nature = pd.read_parquet(PROCESSED_DIR / "county_nature_wui_2020.parquet")
    nature = attach_nature_denominators(nature, denom)
    _write_table(nature, PROCESSED_DIR / "county_nature_wui_2020.parquet",
                 "county_nature_wui_2020.csv")

    year = pd.read_parquet(PROCESSED_DIR / "county_year_wui_fire.parquet")
    year = year.drop(
        columns=[c for c in ("county_land_area_km2", "county_water_area_km2")
                 if c in year.columns],
        errors="ignore",
    )
    year = year.merge(
        denom[["GEOID", "county_land_area_km2", "county_water_area_km2"]],
        on="GEOID", how="left",
    )
    _write_table(year, PROCESSED_DIR / "county_year_wui_fire.parquet",
                 "county_year_wui_fire.csv")

    places_area = read_place_aland()
    places = gpd.read_parquet(WUI_DIR / "places_conus.geoparquet")
    places = places.drop(
        columns=[c for c in ("ALAND", "AWATER", "place_land_area_km2", "place_water_area_km2")
                 if c in places.columns],
        errors="ignore",
    )
    places = places.merge(places_area, on="GEOID", how="left")
    places.to_parquet(WUI_DIR / "places_conus.geoparquet", index=False)

    for parquet_name, csv_name in (
        ("place_wui_by_vintage.parquet", "place_wui_by_vintage.csv"),
        ("place_wui_2020.parquet", "place_wui_2020.csv"),
    ):
        path = WUI_DIR / parquet_name
        table = pd.read_parquet(path)
        table = attach_place_denominators(table, places_area)
        _write_table(table, path, csv_name)

    return {
        "n_counties": int(len(denom)),
        "n_places": int(len(places_area)),
        "silvis_land_pct_max": float(county_wui["pct_county_land_area_wui"].max()),
        "nature_nominal_over_total_max": float(nature["pct_county_nature_wui"].max()),
        "nature_ground_over_total_max": float(
            nature["pct_total_county_area_nature_wui_ground"].max()
        ),
    }


if __name__ == "__main__":
    stats = apply_to_existing_outputs()
    for k, v in stats.items():
        print(f"{k}: {v}")
