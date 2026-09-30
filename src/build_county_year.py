"""Aggregate fire-county intersections into county-year and all-years tables.

Outputs (data/processed/):
- county_year_wildfire.parquet (+ .csv)
    One row per GEOID x year **with at least one qualifying fire**:
      fire_count                distinct FIRE_IDs touching the county that year
      total_burned_area_km2     sum of intersected fragment areas
      county_area_km2           full county area (EPSG:5070)
      pct_county_burned_in_year total_burned_area_km2 / county_area_km2 * 100

- county_all_years_wildfire_summary.parquet (+ .csv)
    One row per CONUS county (zero-filled where no fire was recorded):
      number_of_fire_years               years with >=1 fire
      unique_fire_count                  distinct FIRE_IDs, all years
      cumulative_burned_area_km2         sum of intersected areas, all years
        (can exceed county_area_km2 — land can reburn)
      unique_historical_burn_footprint_km2  area of the county covered by the
        DISSOLVED union of all historical perimeters (rebuilt land counted once)
      pct_unique_historical_burn_footprint

Cumulative burned area vs unique footprint: cumulative sums perimeters per
event, so land burned 3 times contributes 3x; the footprint dissolves all
perimeters first, so that land contributes once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    TABLES_DIR,
    ensure_dirs,
    get_logger,
)

log = get_logger("aggregate")

KM2_PER_M2 = 1e-6


# ---------------------------------------------------------------------------
# Pure functions (unit-tested in tests/test_aggregation.py)
# ---------------------------------------------------------------------------
def aggregate_county_year(intersections: pd.DataFrame) -> pd.DataFrame:
    """One row per GEOID x year; counts DISTINCT fires, sums fragment areas."""
    cy = (
        intersections.groupby(["GEOID", "year"], as_index=False)
        .agg(
            fire_count=("FIRE_ID", "nunique"),
            total_burned_area_km2=("intersection_area_km2", "sum"),
        )
        .reset_index(drop=True)
    )
    cy["fire_count"] = cy["fire_count"].astype("int64")
    return cy


def attach_county_attrs(county_year: pd.DataFrame, counties: gpd.GeoDataFrame) -> pd.DataFrame:
    """Attach county name/state/area and compute annual burned percentage."""
    attrs = counties[["GEOID", "county_name", "state_fips", "state_name", "county_area_km2"]]
    out = county_year.merge(attrs, on="GEOID", how="left", validate="many_to_one")
    out["pct_county_burned_in_year"] = (
        out["total_burned_area_km2"] / out["county_area_km2"] * 100.0
    )
    return out


def summarize_all_years(intersections: pd.DataFrame) -> pd.DataFrame:
    """One row per GEOID over the full record (counties with >=1 fire)."""
    summary = (
        intersections.groupby("GEOID", as_index=False)
        .agg(
            number_of_fire_years=("year", "nunique"),
            unique_fire_count=("FIRE_ID", "nunique"),
            cumulative_burned_area_km2=("intersection_area_km2", "sum"),
        )
        .reset_index(drop=True)
    )
    summary["number_of_fire_years"] = summary["number_of_fire_years"].astype("int64")
    summary["unique_fire_count"] = summary["unique_fire_count"].astype("int64")
    return summary


def attach_county_attrs_all_years(
    summary: pd.DataFrame, counties: gpd.GeoDataFrame
) -> pd.DataFrame:
    attrs = counties[["GEOID", "county_name", "state_fips", "state_name", "county_area_km2"]]
    return summary.merge(attrs, on="GEOID", how="left", validate="many_to_one")


def compute_footprint_by_county(
    fires: gpd.GeoDataFrame,
    intersections: pd.DataFrame,
    counties: gpd.GeoDataFrame,
    log_every: int = 200,
) -> pd.DataFrame:
    """Unique historical burn footprint per county (km2 and % of county).

    For each county: dissolve (union) the FULL perimeters of every distinct
    fire that intersects it, intersect that union with the county polygon, and
    measure the area. Repeatedly burned land is therefore counted once.

    Uses full fire perimeters (not the per-county fragments) so the union is
    geometrically identical to dissolving the national fire layer.
    """
    fire_geom = dict(zip(fires["FIRE_ID"].tolist(), fires.geometry.tolist()))
    county_geom = dict(zip(counties["GEOID"].tolist(), counties.geometry.tolist()))
    county_area = dict(zip(counties["GEOID"].tolist(), counties["county_area_km2"].tolist()))
    fire_ids_by_geoid = intersections.groupby("GEOID")["FIRE_ID"].unique().to_dict()

    records = []
    geoids = intersections["GEOID"].unique()
    for i, geoid in enumerate(geoids):
        fire_ids = fire_ids_by_geoid[geoid]
        union = shapely.union_all([fire_geom[fid] for fid in fire_ids])
        footprint = shapely.intersection(union, county_geom[geoid])
        area_km2 = shapely.area(footprint) * KM2_PER_M2
        records.append(
            {
                "GEOID": geoid,
                "unique_historical_burn_footprint_km2": area_km2,
                "pct_unique_historical_burn_footprint": area_km2 / county_area[geoid] * 100.0,
            }
        )
        if (i + 1) % log_every == 0:
            log.info("  footprint: %d / %d counties", i + 1, len(geoids))
    return pd.DataFrame(records)


def finalize_summary(
    counties: gpd.GeoDataFrame,
    summary: pd.DataFrame,
    footprint: pd.DataFrame,
) -> pd.DataFrame:
    """All-years table over every CONUS county, zero-filled where no fires.

    Starts from the full county list (left merges keep counties absent from
    the fire summary), so untouched counties appear with zeros.
    """
    import pandas as pd  # local import keeps pure-function signature clean

    base = pd.DataFrame(
        counties[["GEOID", "county_name", "state_fips", "state_name", "county_area_km2"]]
    )
    metrics = summary[
        ["GEOID", "number_of_fire_years", "unique_fire_count", "cumulative_burned_area_km2"]
    ]
    final = base.merge(metrics, on="GEOID", how="left", validate="one_to_one")
    final = final.merge(footprint, on="GEOID", how="left", validate="one_to_one")
    for col in (
        "number_of_fire_years",
        "unique_fire_count",
        "cumulative_burned_area_km2",
        "unique_historical_burn_footprint_km2",
        "pct_unique_historical_burn_footprint",
    ):
        final[col] = final[col].fillna(0)
    final["number_of_fire_years"] = final["number_of_fire_years"].astype("int64")
    final["unique_fire_count"] = final["unique_fire_count"].astype("int64")
    return final.sort_values("GEOID").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ensure_dirs()

    fires = gpd.read_parquet(PROCESSED_DIR / "mtbs_wildfires_conus.geoparquet")
    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    intersections = gpd.read_parquet(
        INTERIM_DIR / "fires_county_intersections.geoparquet"
    )
    log.info(
        "Aggregating %d intersection fragments (%d fires, %d counties).",
        len(intersections),
        fires["FIRE_ID"].nunique(),
        len(counties),
    )

    # -- county-year table -------------------------------------------------
    county_year = attach_county_attrs(
        aggregate_county_year(intersections), counties
    )
    county_year = county_year.sort_values(["GEOID", "year"]).reset_index(drop=True)

    # -- all-years summary + unique footprint -------------------------------
    log.info("Computing unique historical burn footprints per county...")
    footprint = compute_footprint_by_county(fires, intersections, counties)
    final = finalize_summary(counties, summarize_all_years(intersections), footprint)
    final = final.sort_values("GEOID").reset_index(drop=True)

    # -- write outputs ------------------------------------------------------
    cy_parquet = PROCESSED_DIR / "county_year_wildfire.parquet"
    county_year.to_parquet(cy_parquet, index=False)
    county_year.to_csv(TABLES_DIR / "county_year_wildfire.csv", index=False)

    final_parquet = PROCESSED_DIR / "county_all_years_wildfire_summary.parquet"
    final.to_parquet(final_parquet, index=False)
    final.to_csv(TABLES_DIR / "county_all_years_wildfire_summary.csv", index=False)
    log.info(
        "Wrote %d county-year rows (%s) and %d county summary rows (%s).",
        len(county_year),
        cy_parquet.name,
        len(final),
        final_parquet.name,
    )

    log.info(
        "Counties with >=1 qualifying fire: %d of %d CONUS counties; "
        "max annual pct burned: %.2f%%; max cumulative: %.2f km2; "
        "max footprint pct: %.2f%%.",
        int((final["unique_fire_count"] > 0).sum()),
        len(final),
        float(county_year["pct_county_burned_in_year"].max()),
        float(final["cumulative_burned_area_km2"].max()),
        float(final["pct_unique_historical_burn_footprint"].max()),
    )
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("aggregate.log")
    sys.exit(main())