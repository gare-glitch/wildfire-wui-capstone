"""Geometric intersection of MTBS wildfire perimeters with county polygons.

Performs a true geometric overlay (not centroid assignment):
- spatial join to find candidate fire x county pairs,
- vectorized shapely intersection for each pair,
- area of each intersected fragment in km^2.

Both layers are brought to EPSG:5070 (CONUS Albers, meters) before any area
math. A fire spanning multiple counties contributes its intersected area to
each county. `fire_count` aggregation downstream counts DISTINCT FIRE_IDs, so
multi-county fires are never counted as multiple events.

Output: data/interim/fires_county_intersections.geoparquet
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    ensure_dirs,
    get_logger,
    load_analysis_config,
)

log = get_logger("overlay")

KM2_PER_M2 = 1e-6

# Polygonal geometry types retained from intersections.
_POLYGON_TYPES = ("Polygon", "MultiPolygon")


def intersect_fires_counties(
    fires: gpd.GeoDataFrame,
    counties: gpd.GeoDataFrame,
    min_intersection_area_km2: float,
) -> gpd.GeoDataFrame:
    """Geometrically intersect fire perimeters with county polygons.

    Args:
        fires: GeoDataFrame with FIRE_ID, fire_name, fire_type, year,
            geometry (any CRS; reprojected here to the counties' CRS).
        counties: CONUS counties in EPSG:5070 with GEOID, geometry.
        min_intersection_area_km2: drop fragments below this area
            (floating-point slivers, not meaningful burned area).

    Returns:
        One row per (fire, county) pair with intersection geometry and
        intersection_area_km2.
    """
    fires = fires.reset_index(drop=True)
    counties = counties.reset_index(drop=True)

    # --- Guard: same CRS, projected, EPSG:5070 for CONUS area math -------
    if counties.crs is None or fires.crs is None:
        raise ValueError("Both layers must have a defined CRS.")
    if counties.crs.to_epsg() != 5070:
        raise ValueError(
            f"Counties must be in EPSG:5070 for CONUS area math; got {counties.crs}"
        )
    if fires.crs.to_epsg() != 5070:
        log.info("Reprojecting fires %s -> EPSG:5070", fires.crs)
        fires = fires.to_crs(5070)

    # --- Guard: valid geometries -----------------------------------------
    n_invalid_fires = int((~fires.geometry.is_valid).sum())
    n_invalid_counties = int((~counties.geometry.is_valid).sum())
    if n_invalid_fires or n_invalid_counties:
        log.warning(
            "Invalid geometries at overlay time: %d fires, %d counties; "
            "applying make_valid (preprocessing should have handled this).",
            n_invalid_fires,
            n_invalid_counties,
        )
        if n_invalid_fires:
            fires["geometry"] = fires.geometry.make_valid()
        if n_invalid_counties:
            counties["geometry"] = counties.geometry.make_valid()

    # --- 1) candidate pairs via spatial join ----------------------------
    fire_cols = ["FIRE_ID", "fire_name", "fire_type", "year", "geometry"]
    pairs = gpd.sjoin(
        fires[fire_cols],
        counties[["GEOID", "geometry"]],
        how="inner",
        predicate="intersects",
    )
    log.info(
        "%d candidate (fire, county) pairs from %d fires x %d counties",
        len(pairs),
        len(fires),
        len(counties),
    )

    # --- 2) vectorized geometric intersection ----------------------------
    fire_geom_arr = np.asarray(fires.geometry.values, dtype=object)
    county_pos = {g: i for i, g in enumerate(counties["GEOID"])}
    county_geom_arr = np.asarray(counties.geometry.values, dtype=object)

    fire_idx = pairs.index.to_numpy()
    county_idx = pairs["GEOID"].map(county_pos).to_numpy()
    inter = shapely.intersection(
        fire_geom_arr[fire_idx], county_geom_arr[county_idx]
    )

    # Keep only polygonal results (degenerate pairs can yield empty/lineal
    # outputs when perimeters only touch county boundaries).
    if hasattr(shapely, "collection_extract"):
        inter = shapely.collection_extract(inter, [3])  # 3 = polygonal parts
    area_m2 = shapely.area(inter)
    keep = (area_m2 * KM2_PER_M2) >= min_intersection_area_km2
    geom = gpd.GeoSeries(inter, crs=counties.crs)
    type_ok = geom.geom_type.isin(_POLYGON_TYPES)
    n_non_polygonal = int((~type_ok & keep).sum())
    if n_non_polygonal:
        log.warning("Dropping %d non-polygonal intersection results.", n_non_polygonal)
    keep = keep & type_ok.to_numpy()

    log.info(
        "Kept %d fire-county fragments >= %.6f km2 (dropped %d slivers/touches).",
        int(keep.sum()),
        min_intersection_area_km2,
        int((~keep).sum()),
    )

    out = gpd.GeoDataFrame(
        {
            "GEOID": pairs.loc[keep, "GEOID"].to_numpy(),
            "FIRE_ID": pairs.loc[keep, "FIRE_ID"].to_numpy(),
            "fire_name": pairs.loc[keep, "fire_name"].to_numpy(),
            "fire_type": pairs.loc[keep, "fire_type"].to_numpy(),
            "year": pairs.loc[keep, "year"].to_numpy(),
        },
        geometry=geom[keep].geometry.values,
        crs=counties.crs,
    )
    out["intersection_area_km2"] = (
        shapely.area(np.asarray(out.geometry.values, dtype=object)) * KM2_PER_M2
    )
    return out


def main(argv: list[str] | None = None) -> int:
    cfg = load_analysis_config()
    ensure_dirs()

    fires = gpd.read_parquet(PROCESSED_DIR / "mtbs_wildfires_conus.geoparquet")
    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    log.info(
        "Loaded %d wildfire perimeters and %d counties.", len(fires), len(counties)
    )

    intersections = intersect_fires_counties(
        fires,
        counties,
        min_intersection_area_km2=float(cfg["intersection"]["min_intersection_area_km2"]),
    )

    out_path = INTERIM_DIR / "fires_county_intersections.geoparquet"
    intersections.to_parquet(out_path, index=False)
    log.info("Wrote %d intersection fragments to %s", len(intersections), out_path)

    # Diagnostics: multi-county fires, per-year fragment counts.
    counties_per_fire = intersections.groupby("FIRE_ID")["GEOID"].nunique()
    log.info(
        "Fires intersecting >1 county: %d of %d (max %d counties for one fire).",
        int((counties_per_fire > 1).sum()),
        len(counties_per_fire),
        int(counties_per_fire.max()) if len(counties_per_fire) else 0,
    )
    by_year = intersections.groupby("year")["FIRE_ID"].nunique()
    log.info(
        "Distinct fires per year (min/median/max): %d / %d / %d",
        int(by_year.min()),
        int(by_year.median()),
        int(by_year.max()),
    )
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("overlay.log")
    sys.exit(main())