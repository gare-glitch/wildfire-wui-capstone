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


def _polygonal(geom) -> object:
    """Normalize an intersection result to polygonal geometry.

    Polygon/MultiPolygon pass through; GeometryCollections are reduced to
    their polygonal parts (unioned); lineal/punctual touch artifacts and
    empties become None (they carry no area).
    """
    if geom is None or geom.is_empty:
        return None
    t = geom.geom_type
    if t in _POLYGON_TYPES:
        return geom
    if t == "GeometryCollection":
        parts = shapely.get_parts(geom)
        poly_parts = [g for g in parts if g.geom_type in _POLYGON_TYPES]
        return shapely.union_all(poly_parts) if poly_parts else None
    return None  # Line/Point touch artifact


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
    # Input contract: processed fires carry derived lowercase columns
    # (fire_id, fire_name, fire_type, year); the output table renames the
    # event key to FIRE_ID, the name used by all county-level products.
    fire_cols = ["fire_id", "fire_name", "fire_type", "year", "geometry"]
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

    # Normalize degenerate outputs: keep polygonal parts, drop zero-area
    # touch artifacts (boundary-only contacts produce lines/points).
    inter = np.asarray([_polygonal(g) for g in inter], dtype=object)

    # Drop fragments below the sliver threshold (keep None handling simple:
    # shapely.area(None) would fail, so mask Nones out first).
    nonnone = np.array([g is not None for g in inter], dtype=bool)
    n_degenerate = int(len(inter) - nonnone.sum())
    if n_degenerate:
        log.info(
            "Dropped %d boundary-touch/degenerate intersection results.",
            n_degenerate,
        )
    area_m2 = np.zeros(len(inter), dtype=float)
    if nonnone.any():
        area_m2[nonnone] = shapely.area(inter[nonnone])
    keep = (area_m2 * KM2_PER_M2) >= min_intersection_area_km2

    log.info(
        "Kept %d fire-county fragments >= %.6f km2 (dropped %d slivers/touches).",
        int(keep.sum()),
        min_intersection_area_km2,
        int((~keep).sum()),
    )

    kept_geoms = [g for g, k in zip(inter, keep) if k]
    out = gpd.GeoDataFrame(
        {
            "GEOID": pairs.loc[keep, "GEOID"].to_numpy(),
            "FIRE_ID": pairs.loc[keep, "fire_id"].to_numpy(),
            "fire_name": pairs.loc[keep, "fire_name"].to_numpy(),
            "fire_type": pairs.loc[keep, "fire_type"].to_numpy(),
            "year": pairs.loc[keep, "year"].to_numpy(),
        },
        geometry=kept_geoms,
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