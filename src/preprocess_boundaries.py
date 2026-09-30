"""Preprocess the Census TIGER/Line national county file.

Steps (schema inspection BEFORE processing, AGENTS.md #2):
1. Load tl_2025_us_county.shp (read-only; raw data never modified).
2. Print + save schema report to outputs/tables/schema_counties_raw.{txt,json}.
3. Assert expected fields, CRS (NAD83, EPSG:4269), GEOID uniqueness, and
   Virginia independent-city presence BEFORE any transformation.
4. Restrict to CONUS states + DC (config/datasets.yaml scope).
5. Repair geometries only if necessary, with counts logged.
6. Reproject to EPSG:5070 (CONUS Albers, meters) and compute county_area_km2.
7. Cross-check computed areas against the published ALAND + AWATER values.
8. Write data/processed/counties_conus.geoparquet with derived fields:
   county_name (NAMELSAD legal name), state_fips, state_name, county_area_km2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PROCESSED_DIR,
    RAW_DIR,
    TABLES_DIR,
    ensure_dirs,
    get_logger,
    load_analysis_config,
    resolve_stored_path,
)
from preprocess_mtbs import write_schema_report  # noqa: E402  (shared helper)

log = get_logger("preprocess_boundaries")

# Field names exactly as read from tl_2025_us_county.shp (uppercase in DBF).
EXPECTED_FIELDS = [
    "STATEFP", "COUNTYFP", "COUNTYNS", "GEOID", "GEOIDFQ", "NAME", "NAMELSAD",
    "LSAD", "CLASSFP", "MTFCC", "CSAFP", "CBSAFP", "METDIVFP", "FUNCSTAT",
    "ALAND", "AWATER", "INTPTLAT", "INTPTLON",
]
EXPECTED_CRS_EPSG = 4269  # NAD83 geographic, per TIGER/Line documentation
VA_STATEFP = "51"
VA_INDEPENDENT_CITY_MIN_COUNTYFP = 500  # Census convention: VA independent
# cities carry COUNTYFP >= 500 (verified: 38 cities in TIGER 2025).


def find_raw_shapefile() -> Path:
    """Locate the extracted county shapefile.

    Primary path: the provenance manifest's extraction_dir; fallback: the
    standard extraction location (download failures leave no record).
    """
    shp_dir: Path | None = None
    prov = RAW_DIR / "provenance.json"
    if prov.exists():
        with open(prov, "r", encoding="utf-8") as fh:
            rec = json.load(fh).get("datasets", {}).get("census_counties", {})
        if rec.get("extraction_dir"):
            shp_dir = resolve_stored_path(rec["extraction_dir"])
    if shp_dir is None or not shp_dir.exists():
        shp_dir = RAW_DIR / "census_counties" / "extracted"
    shps = sorted(shp_dir.glob("*.shp"))
    if len(shps) != 1:
        raise FileNotFoundError(
            f"Expected exactly one .shp in {shp_dir}, found {shps}. "
            "Run the download stage first."
        )
    return shps[0]


def assert_county_integrity(gdf: gpd.GeoDataFrame) -> None:
    """Fail loudly on structural problems in the county source."""
    n, nu = len(gdf), gdf["GEOID"].nunique()
    if n != nu:
        raise ValueError(f"GEOID not unique in county source: {n} rows, {nu} GEOIDs")
    bad = gdf[~gdf["GEOID"].astype(str).str.fullmatch(r"\d{5}")]
    if len(bad):
        raise ValueError(f"{len(bad)} malformed GEOIDs in county source")

    va = gdf[gdf["STATEFP"] == VA_STATEFP]
    va_cities = va[va["COUNTYFP"].astype(int) >= VA_INDEPENDENT_CITY_MIN_COUNTYFP]
    n_cities = len(va_cities)
    all_named_city = bool(
        va_cities["NAMELSAD"].str.lower().str.endswith("city").all()
    ) if n_cities else False
    log.info(
        "Virginia: %d county equivalents, %d independent cities "
        "(COUNTYFP>=%d), all named '<name> city': %s",
        len(va), n_cities, VA_INDEPENDENT_CITY_MIN_COUNTYFP, all_named_city,
    )
    if n_cities < 30 or not all_named_city:
        raise ValueError(
            "Virginia independent cities missing or misclassified "
            f"({n_cities} found, all_named_city={all_named_city})"
        )


def main(argv: list[str] | None = None) -> int:
    cfg = load_analysis_config()
    ensure_dirs()

    shp_path = find_raw_shapefile()
    log.info("Loading county shapefile %s ...", shp_path)
    counties = gpd.read_file(shp_path, engine="pyogrio")
    log.info("Loaded %d county-equivalent records (national).", len(counties))

    # --- schema inspection ---------------------------------------------------
    write_schema_report(counties, "counties_raw")

    missing = [f for f in EXPECTED_FIELDS if f not in counties.columns]
    if missing:
        log.error("County shapefile is missing expected fields: %s", missing)
        return 1
    epsg = counties.crs.to_epsg() if counties.crs else None
    if epsg != EXPECTED_CRS_EPSG:
        log.error("County CRS is EPSG:%s; expected EPSG:%s (NAD83).",
                  epsg, EXPECTED_CRS_EPSG)
        return 1

    # --- structural assertions (before any transformation) --------------------
    try:
        assert_county_integrity(counties)
    except ValueError as exc:
        log.error("County integrity check failed: %s", exc)
        return 1

    # --- CONUS restriction ----------------------------------------------------
    conus_fips = [str(f) for f in cfg["scope"]["conus_state_fips"]]
    n_national = len(counties)
    counties = counties[counties["STATEFP"].isin(conus_fips)].reset_index(drop=True)
    log.info(
        "Restricted to CONUS (48 states + DC): %d of %d national county "
        "equivalents retained; excluded AK/HI/PR/territories.",
        len(counties), n_national,
    )

    # --- geometry hygiene ------------------------------------------------------
    n_null = int(counties.geometry.isna().sum())
    n_empty = int(counties.geometry.is_empty.sum())
    if n_null or n_empty:
        raise ValueError(f"Counties contain {n_null} null / {n_empty} empty geometries")
    n_invalid = int((~counties.geometry.is_valid).sum())
    if n_invalid:
        log.warning("Repairing %d invalid county geometries with make_valid().",
                    n_invalid)
        counties["geometry"] = counties.geometry.make_valid()
        n_still = int((~counties.geometry.is_valid).sum())
        if n_still:
            log.error("%d county geometries remain invalid after repair.", n_still)
            return 1
    else:
        log.info("All %d CONUS county geometries valid; no repairs needed.", len(counties))

    # --- projection + area (EPSG:5070, never lat/lon) -------------------------
    counties = counties.to_crs(int(cfg["crs"]["conus_equal_area"].split(":")[1]))
    counties["county_area_km2"] = counties.geometry.area * 1e-6
    # Census published land and water (m² → km²). Kept beside the legal-polygon
    # area so WUI percentages can use either denominator. ALAND/AWATER are
    # attributes, not a replacement for the projected polygon area.
    counties["county_land_area_km2"] = counties["ALAND"].astype("float64") * 1e-6
    counties["county_water_area_km2"] = counties["AWATER"].astype("float64") * 1e-6

    # Cross-check against published ALAND+AWATER (m^2) — TIGER polygons are
    # slightly generalized, so small differences are expected and recorded.
    published_km2 = (counties["ALAND"] + counties["AWATER"]) * 1e-6
    diff = (counties["county_area_km2"] - published_km2).abs()
    rel_diff = (diff / published_km2 * 100.0)
    log.info(
        "Computed area vs ALAND+AWATER: max abs diff %.2f km2, max rel diff "
        "%.4f%% (TIGER line generalization).",
        float(diff.max()), float(rel_diff.max()),
    )

    # --- derived fields --------------------------------------------------------
    state_names = cfg["aggregation"]["state_fips_names"]
    counties["county_name"] = counties["NAMELSAD"].astype(str)
    counties["state_fips"] = counties["STATEFP"].astype(str)
    counties["state_name"] = counties["state_fips"].map(
        lambda f: state_names.get(f, {}).get("name", None)
    )
    n_unmapped = int(counties["state_name"].isna().sum())
    if n_unmapped:
        log.error("%d counties could not be mapped to a state name.", n_unmapped)
        return 1

    # --- output -----------------------------------------------------------------
    out_path = PROCESSED_DIR / "counties_conus.geoparquet"
    counties.to_parquet(out_path, index=False)
    log.info(
        "Wrote %d CONUS counties (CRS %s) to %s",
        len(counties), counties.crs, out_path,
    )
    log.info(
        "Area range: %.1f km2 (%s) to %.1f km2 (%s).",
        float(counties["county_area_km2"].min()),
        counties.loc[counties["county_area_km2"].idxmin(), "county_name"],
        float(counties["county_area_km2"].max()),
        counties.loc[counties["county_area_km2"].idxmax(), "county_name"],
    )
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("preprocess_boundaries.log")
    sys.exit(main())