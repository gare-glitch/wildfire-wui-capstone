"""Preprocess the MTBS national burned-area-boundary shapefile.

Steps (schema inspection BEFORE processing, AGENTS.md #2):
1. Load the raw shapefile (read-only; raw data never modified).
2. Print + save a full schema report (fields, dtypes, CRS, geometry types,
   bounds) to outputs/tables/schema_mtbs_raw.{txt,json}.
3. Assert expected fields and CRS against the values observed at build time
   (NAD83 geographic, EPSG:4269).
4. Derive analysis fields without altering originals:
     fire_id     <- event_id  (canonical event identifier of this product;
                  the FIRE_ID field belongs to the MTBS fire-occurrence
                  points product and does not exist here)
     fire_name   <- incid_name ('UNNAMED' preserved)
     fire_type   <- incid_type
     year        <- ig_date year, cross-checked against the event_id date
                  suffix (they agree for all 30,933 rows at build time)
     ignition_date, state_abbr_event_id
5. Repair invalid geometries ONLY if present, with counts logged (AGENTS.md).
6. Write all events (native CRS) to data/interim/mtbs_all_events.geoparquet.
7. Filter to the analysis scope and write
   data/processed/mtbs_wildfires_conus.geoparquet plus a filter report
   (counts at every filtering step) to data/processed/mtbs_filter_report.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    TABLES_DIR,
    ensure_dirs,
    get_logger,
    load_analysis_config,
)

log = get_logger("preprocess_mtbs")

# Field names exactly as read from the 2026-09-30 national archive
# (the DBF stores lowercase names; the FGDC metadata documents mixed case).
EXPECTED_FIELDS = [
    "event_id", "irwinid", "incid_name", "incid_type", "map_id", "map_prog",
    "asmnt_type", "burnbndac", "burnbndlat", "burnbndlon", "ig_date",
    "pre_id", "post_id", "perim_id", "dnbr_offst", "dnbr_stddv", "nodata_t",
    "incgreen_t", "low_t", "mod_t", "high_t", "comment",
]
EXPECTED_CRS_EPSG = 4269  # NAD83 geographic, per .prj and FGDC metadata

NON_CONUS_STATE_PREFIXES = {"AK", "HI", "PR"}
# event_id = <2-letter state abbr><lat6><lon7><YYYYMMDD> per FGDC metadata.


def find_raw_shapefile() -> Path:
    """Locate the extracted MTBS shapefile via the provenance manifest."""
    prov = RAW_DIR / "provenance.json"
    with open(prov, "r", encoding="utf-8") as fh:
        extraction_dir = json.load(fh)["datasets"]["mtbs_perimeter"]["extraction_dir"]
    shp_dir = Path(extraction_dir)
    shps = sorted(shp_dir.glob("*.shp"))
    if len(shps) != 1:
        raise FileNotFoundError(f"Expected exactly one .shp in {shp_dir}, found {shps}")
    return shps[0]


def write_schema_report(gdf: gpd.GeoDataFrame, label: str) -> dict:
    """Human + machine schema snapshot (AGENTS.md #2)."""
    report = {
        "label": label,
        "n_features": int(len(gdf)),
        "crs": str(gdf.crs),
        "geometry_types": {k: int(v) for k, v in gdf.geom_type.value_counts().items()},
        "total_bounds": [float(x) for x in gdf.total_bounds],
        "columns": {c: str(gdf[c].dtype) for c in gdf.columns},
    }
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    with open(TABLES_DIR / f"schema_{label}.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(TABLES_DIR / f"schema_{label}.txt", "w", encoding="utf-8") as fh:
        fh.write(f"Schema report: {label}\n")
        fh.write("=" * (14 + len(label)) + "\n")
        fh.write(f"n_features: {report['n_features']}\n")
        fh.write(f"crs: {report['crs']}\n")
        fh.write(f"geometry types: {report['geometry_types']}\n")
        fh.write(f"total bounds (xmin, ymin, xmax, ymax): {report['total_bounds']}\n\n")
        fh.write("Columns (name: dtype):\n")
        for col, dt in report["columns"].items():
            fh.write(f"  {col}: {dt}\n")
    return report


def derive_analysis_fields(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add derived fields; never mutate original attributes."""
    gdf = gdf.copy()

    ig = pd.to_datetime(gdf["ig_date"], errors="coerce")
    n_bad_dates = int(ig.isna().sum())
    if n_bad_dates:
        raise ValueError(f"{n_bad_dates} rows have unparsable ig_date; inspect raw data.")

    gdf["ignition_date"] = ig.dt.date
    gdf["year"] = ig.dt.year.astype("int64")

    # Cross-check year against the event_id date suffix (producer-embedded).
    yr_from_id = gdf["event_id"].str[-8:-4].astype(int)
    mismatch = gdf["year"] != yr_from_id
    n_mismatch = int(mismatch.sum())
    if n_mismatch:
        log.warning(
            "%d rows where ig_date year != event_id year; keeping ig_date year.",
            n_mismatch,
        )

    gdf["fire_id"] = gdf["event_id"].astype(str)
    gdf["fire_name"] = gdf["incid_name"].astype(str)
    gdf["fire_type"] = gdf["incid_type"].astype(str)
    gdf["state_abbr_event_id"] = gdf["event_id"].astype(str).str[:2]
    return gdf


def geometry_hygiene(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, dict]:
    """Empty-geometry removal and validity repair, fully counted.

    Returns (gdf, stats). Rows with null/empty geometry are dropped (a fire
    perimeter without geometry cannot participate in a spatial overlay);
    the drop count is recorded in the filter report.
    """
    stats: dict = {}
    n_null = int(gdf.geometry.isna().sum())
    n_empty = int(gdf.geometry.is_empty.sum())
    stats["n_null_geometry"] = n_null
    stats["n_empty_geometry"] = n_empty
    if n_null or n_empty:
        keep = gdf.geometry.notna() & ~gdf.geometry.is_empty
        gdf = gdf[keep].reset_index(drop=True)
        stats["n_dropped_missing_geometry"] = n_null + n_empty
        log.warning("Dropped %d rows with null/empty geometry.", n_null + n_empty)

    n_invalid = int((~gdf.geometry.is_valid).sum())
    stats["n_invalid_geometry_before_repair"] = n_invalid
    if n_invalid:
        log.warning("Repairing %d invalid geometries with make_valid().", n_invalid)
        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.make_valid()
        n_still_invalid = int((~gdf.geometry.is_valid).sum())
        stats["n_invalid_geometry_after_repair"] = n_still_invalid
        if n_still_invalid:
            raise ValueError(
                f"{n_still_invalid} geometries remain invalid after make_valid."
            )
    stats["n_after_geometry_clean"] = int(len(gdf))
    return gdf, stats


def build_filter_report(gdf_all: gpd.GeoDataFrame, gdf_kept: gpd.GeoDataFrame,
                        cfg: dict, geom_stats: dict) -> dict:
    include_types = list(cfg["fire_types"]["include"])
    min_year, max_year = int(cfg["years"]["min_year"]), int(cfg["years"]["max_year"])

    conus_mask = ~gdf_all["state_abbr_event_id"].isin(NON_CONUS_STATE_PREFIXES)
    type_mask = gdf_all["fire_type"].isin(include_types)
    year_mask = gdf_all["year"].between(min_year, max_year)

    return {
        "n_raw_total": int(len(gdf_all)) - int(geom_stats.get("n_dropped_missing_geometry", 0)),
        "n_unique_event_id_raw": int(gdf_all["event_id"].nunique()),
        "by_fire_type": {k: int(v) for k, v in
                         gdf_all["fire_type"].value_counts().items()},
        "year_range_raw": [int(gdf_all["year"].min()), int(gdf_all["year"].max())],
        "min_year": min_year,
        "max_year": max_year,
        "fire_types_included": include_types,
        "n_conus": int(conus_mask.sum()),
        "n_excluded_by_type": {
            t: int((gdf_all["fire_type"] == t).sum())
            for t in cfg["fire_types"]["exclude"]
        },
        "n_excluded_by_year": {
            str(y): int((gdf_all["year"] == y).sum())
            for y in sorted(gdf_all.loc[~year_mask, "year"].unique())
        },
        "n_retained": int(len(gdf_kept)),
        **{k: int(v) if not isinstance(v, (dict, list)) else v
           for k, v in geom_stats.items()},
    }


def main(argv: list[str] | None = None) -> int:
    cfg = load_analysis_config()
    ensure_dirs()

    shp_path = find_raw_shapefile()
    log.info("Loading MTBS shapefile %s ...", shp_path)
    gdf = gpd.read_file(shp_path, engine="pyogrio")
    log.info("Loaded %d fire perimeter records.", len(gdf))

    # --- schema inspection (always before processing) ----------------------
    write_schema_report(gdf, "mtbs_raw")

    missing = [f for f in EXPECTED_FIELDS if f not in gdf.columns]
    if missing:
        log.error("MTBS shapefile is missing expected fields: %s", missing)
        return 1
    epsg = gdf.crs.to_epsg() if gdf.crs else None
    if epsg != EXPECTED_CRS_EPSG:
        log.error("MTBS CRS is EPSG:%s; expected EPSG:%s (NAD83). "
                  "Do NOT compute areas in this CRS.", epsg, EXPECTED_CRS_EPSG)
        return 1

    # --- derived fields -----------------------------------------------------
    gdf = derive_analysis_fields(gdf)
    log.info(
        "Derived fields. Year range %d-%d; fire types: %s",
        gdf["year"].min(), gdf["year"].max(),
        gdf["fire_type"].value_counts().to_dict(),
    )

    # --- geometry hygiene ----------------------------------------------------
    gdf, geom_stats = geometry_hygiene(gdf)
    log.info("Geometry stats: %s", geom_stats)

    # --- all-events interim artifact (native CRS, every original field) ------
    all_path = INTERIM_DIR / "mtbs_all_events.geoparquet"
    gdf.to_parquet(all_path, index=False)
    log.info("Wrote %d all events (native CRS) to %s", len(gdf), all_path)

    # --- analysis-scope filters ---------------------------------------------
    include_types = list(cfg["fire_types"]["include"])
    min_year, max_year = int(cfg["years"]["min_year"]), int(cfg["years"]["max_year"])

    conus_mask = ~gdf["state_abbr_event_id"].isin(NON_CONUS_STATE_PREFIXES)
    type_mask = gdf["fire_type"].isin(include_types)
    year_mask = gdf["year"].between(min_year, max_year)

    n_after_type = int(type_mask.sum())
    n_after_year = int((type_mask & year_mask).sum())
    n_retained = int((type_mask & year_mask & conus_mask).sum())
    log.info(
        "Filter chain: %d raw -> %d after fire_type -> %d after years "
        "%d-%d -> %d after CONUS restriction.",
        len(gdf), n_after_type, n_after_year, min_year, max_year, n_retained,
    )

    kept = gdf[type_mask & year_mask & conus_mask].reset_index(drop=True)
    excluded_nonconus = gdf[type_mask & year_mask & ~conus_mask].reset_index(drop=True)

    # --- outputs -------------------------------------------------------------
    out_path = PROCESSED_DIR / "mtbs_wildfires_conus.geoparquet"
    kept.to_parquet(out_path, index=False)
    log.info("Wrote %d CONUS wildfire perimeters to %s", len(kept), out_path)

    excl_path = INTERIM_DIR / "mtbs_qualifying_nonconus.geoparquet"
    excluded_nonconus.to_parquet(excl_path, index=False)
    log.info(
        "Preserved %d qualifying non-CONUS events (AK/HI/PR) at %s for a "
        "future stage with jurisdiction-appropriate projections.",
        len(excluded_nonconus), excl_path,
    )

    report = build_filter_report(gdf, kept, cfg, geom_stats)
    report["n_excluded_nonconus_of_qualifying"] = int(len(excluded_nonconus))
    report["n_after_type_filter"] = n_after_type
    report["n_after_year_filter"] = n_after_year
    report_path = PROCESSED_DIR / "mtbs_filter_report.json"
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    log.info("Filter report written to %s", report_path)
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("preprocess_mtbs.log")
    sys.exit(main())