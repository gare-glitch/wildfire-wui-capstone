"""Integration tests over built Stage 1 outputs.

These run only after `python run_pipeline.py` has produced its artifacts
(raw-data-independent unit tests live in the other test modules).
"""

from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESSED = REPO_ROOT / "data" / "processed"
INTERIM = REPO_ROOT / "data" / "interim"

EXPECTED_COUNTY_YEARS_COLS = {
    "GEOID", "year", "fire_count", "total_burned_area_km2", "county_name",
    "state_fips", "state_name", "county_area_km2", "pct_county_burned_in_year",
}
EXPECTED_SUMMARY_COLS = {
    "GEOID", "county_name", "state_fips", "state_name", "number_of_fire_years",
    "unique_fire_count", "cumulative_burned_area_km2", "county_area_km2",
    "unique_historical_burn_footprint_km2", "pct_unique_historical_burn_footprint",
}

pytestmark = pytest.mark.skipif(
    not (PROCESSED / "county_year_wildfire.parquet").exists(),
    reason="Stage 1 outputs not built yet (run `python run_pipeline.py`)",
)


@pytest.fixture(scope="module")
def county_year() -> pd.DataFrame:
    return pd.read_parquet(PROCESSED / "county_year_wildfire.parquet")


@pytest.fixture(scope="module")
def summary() -> pd.DataFrame:
    return pd.read_parquet(PROCESSED / "county_all_years_wildfire_summary.parquet")


def test_county_year_schema_and_uniqueness(county_year):
    assert EXPECTED_COUNTY_YEARS_COLS.issubset(county_year.columns)
    assert not county_year.duplicated(subset=["GEOID", "year"]).any()
    assert county_year["year"].between(1984, 2024).all()
    assert (county_year["fire_count"] >= 1).all()
    assert (county_year["total_burned_area_km2"] > 0).all()


def test_county_year_pct_recomputes(county_year):
    recomputed = (
        county_year["total_burned_area_km2"] / county_year["county_area_km2"] * 100.0
    )
    pd.testing.assert_series_equal(
        county_year["pct_county_burned_in_year"],
        recomputed,
        check_names=False,
        rtol=1e-9,
    )


def test_summary_schema_and_completeness(summary):
    assert EXPECTED_SUMMARY_COLS.issubset(summary.columns)
    assert len(summary) == 3109  # all CONUS county equivalents
    assert summary["GEOID"].nunique() == len(summary)


def test_summary_virginia_independent_cities_present(summary):
    va = summary[summary["state_fips"] == "51"]
    assert len(va) == 133
    va_cities = va[va["GEOID"].str[2:5].astype(int) >= 500]
    assert len(va_cities) == 38
    assert va_cities["county_name"].str.lower().str.endswith("city").all()


def test_summary_footprint_bounds(summary):
    assert (summary["pct_unique_historical_burn_footprint"] >= 0).all()
    assert (
        summary["pct_unique_historical_burn_footprint"] <= 100.0 + 1e-6
    ).all()
    assert (
        summary["unique_historical_burn_footprint_km2"]
        <= summary["cumulative_burned_area_km2"] + 1e-6
    ).all()


def test_summary_zero_counties_have_zero_metrics(summary):
    no_fire = summary[summary["unique_fire_count"] == 0]
    assert (no_fire["number_of_fire_years"] == 0).all()
    assert (no_fire["cumulative_burned_area_km2"] == 0).all()
    assert (no_fire["pct_unique_historical_burn_footprint"] == 0).all()


def test_intersections_artifact_exists_with_expected_columns():
    import geopandas as gpd

    path = INTERIM / "fires_county_intersections.geoparquet"
    assert path.exists()
    inter = gpd.read_parquet(path)
    assert {"GEOID", "FIRE_ID", "year", "intersection_area_km2"}.issubset(
        inter.columns
    )
    assert (inter["intersection_area_km2"] > 0).all()
    assert inter.crs is not None and inter.crs.to_epsg() == 5070


def test_validation_report_exists_and_no_critical_failures():
    path = REPO_ROOT / "outputs" / "tables" / "data_validation_report.csv"
    assert path.exists()
    report = pd.read_csv(path)
    critical = report[report["severity"] == "CRITICAL"]
    assert (critical["status"] == "PASS").all(), critical[
        critical["status"] != "PASS"
    ].to_string()


def test_maps_generated():
    figures = REPO_ROOT / "outputs" / "figures"
    for name in (
        "map_conus_cumulative_burned_area.png",
        "map_conus_wildfire_count.png",
        "map_conus_pct_unique_burn_footprint.png",
    ):
        assert (figures / name).exists(), name