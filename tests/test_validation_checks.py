"""Tests for validation check functions (validate_data.py) on synthetic data."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box, Polygon

import validate_data as vd

CRS = "EPSG:5070"


def _counties(n=3, with_area=True):
    df = gpd.GeoDataFrame(
        {
            "GEOID": [f"0100{i}" for i in range(1, n + 1)],
            "STATEFP": ["01"] * n,
            "COUNTYFP": [f"00{i}" for i in range(1, n + 1)],
            "NAMELSAD": [f"County {i}" for i in range(1, n + 1)],
            "county_area_km2": [1000.0 * i for i in range(1, n + 1)],
            "geometry": [box(0, 0, 1000 * i, 1000) for i in range(1, n + 1)],
        },
        crs=CRS,
    )
    return df


# ---------------------------------------------------------------------------
def test_geoid_uniqueness_pass_and_fail():
    ok = vd.check_geoid_uniqueness(_counties())
    assert ok.status == vd.PASS
    dup = pd.concat([_counties(), _counties().head(1)], ignore_index=True)
    bad = vd.check_geoid_uniqueness(dup)
    assert bad.status == vd.FAIL


def test_geoid_format():
    ok = vd.check_county_geoid_format(_counties())
    assert ok.status == vd.PASS
    df = _counties()
    df.loc[0, "GEOID"] = "1001"  # 4 chars — malformed
    assert vd.check_county_geoid_format(df).status == vd.FAIL


def test_county_area_bounds():
    ok = vd.check_county_areas(_counties(), min_km2=5, max_km2=55000)
    assert ok.status == vd.PASS
    df = _counties()
    df.loc[0, "county_area_km2"] = 2.0  # implausibly small
    assert vd.check_county_areas(df, min_km2=5, max_km2=55000).status == vd.FAIL
    df.loc[0, "county_area_km2"] = 99_999.0
    assert vd.check_county_areas(df, min_km2=5, max_km2=55000).status == vd.FAIL


def test_conus_state_coverage_missing_state_fails():
    counties = _counties()
    assert vd.check_conus_state_coverage(counties, ["01"]).status == vd.PASS
    assert vd.check_conus_state_coverage(counties, ["01", "06"]).status == vd.FAIL


def test_va_independent_cities():
    # Build a synthetic Virginia: 1 county + 38 independent cities (the actual
    # count in TIGER/Line; the check requires >=30 for robustness).
    rows = [
        {"GEOID": "51001", "STATEFP": "51", "COUNTYFP": "001",
         "NAMELSAD": "Accomack County", "county_area_km2": 3000.0,
         "geometry": box(0, 0, 30000, 10000)},
    ]
    city_names = [
        "Alexandria city", "Bristol city", "Buena Vista city",
        "Charlottesville city", "Chesapeake city", "Colonial Heights city",
        "Covington city", "Danville city", "Emporia city", "Fairfax city",
        "Falls Church city", "Franklin city", "Fredericksburg city",
        "Galax city", "Hampton city", "Harrisonburg city", "Hopewell city",
        "Lexington city", "Lynchburg city", "Manassas city",
        "Manassas Park city", "Martinsville city", "Newport News city",
        "Norfolk city", "Norton city", "Petersburg city", "Poquoson city",
        "Portsmouth city", "Radford city", "Richmond city", "Roanoke city",
        "Salem city", "Staunton city", "Suffolk city", "Virginia Beach city",
        "Waynesboro city", "Williamsburg city", "Winchester city",
    ]
    assert len(city_names) == 38
    for i, nm in enumerate(city_names):
        rows.append(
            {"GEOID": f"51{i + 500:03d}", "STATEFP": "51",
             "COUNTYFP": f"{i + 500:03d}", "NAMELSAD": nm,
             "county_area_km2": 50.0,
             "geometry": box(0, 100000 + i * 5000, 5000, 103000 + i * 5000)}
        )
    va = gpd.GeoDataFrame(rows, crs=CRS)
    res = vd.check_va_independent_cities(va)
    assert res.status == vd.PASS
    assert res.metrics["n_va_independent_cities"] == 38

    no_cities = va[va.COUNTYFP == "001"]
    assert vd.check_va_independent_cities(no_cities).status == vd.FAIL


def test_missing_crs_detection():
    good = {"a": gpd.GeoDataFrame({"geometry": [box(0, 0, 1, 1)]}, crs=CRS)}
    assert vd.check_missing_crs(good).status == vd.PASS
    bad_gdf = gpd.GeoDataFrame({"geometry": [box(0, 0, 1, 1)]})
    bad = {"a": bad_gdf, "b": gpd.GeoDataFrame({"geometry": [box(0, 0, 1, 1)]}, crs=CRS)}
    res = vd.check_missing_crs(bad)
    assert res.status == vd.FAIL
    assert res.metrics["n_missing_crs"] == 1


def test_conus_crs_is_5070():
    g5070 = {"x": gpd.GeoDataFrame({"geometry": [box(0, 0, 1, 1)]}, crs=CRS)}
    assert vd.check_conus_crs_is_5070(g5070).status == vd.PASS
    g4326 = {"x": gpd.GeoDataFrame({"geometry": [box(0, 0, 1, 1)]}, crs="EPSG:4326")}
    assert vd.check_conus_crs_is_5070(g4326).status == vd.FAIL


def test_invalid_geometries_detected():
    valid = {"v": gpd.GeoDataFrame({"geometry": [box(0, 0, 1, 1)]}, crs=CRS)}
    assert vd.check_invalid_geometries(valid).status == vd.PASS
    # classic "bowtie" self-intersecting polygon
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    invalid = {"i": gpd.GeoDataFrame({"geometry": [bowtie]}, crs=CRS)}
    assert vd.check_invalid_geometries(invalid).status == vd.FAIL


def _fires():
    return gpd.GeoDataFrame(
        {
            "FIRE_ID": ["F1", "F2"],
            "fire_type": ["Wildfire", "Wildland Fire Use"],
            "year": [1984, 2024],
            "geometry": [box(0, 0, 1, 1), box(2, 2, 3, 3)],
        },
        crs=CRS,
    )


def test_mtbs_year_range():
    assert vd.check_mtbs_year_range(_fires(), 1984, 2024).status == vd.PASS
    late = _fires()
    late.loc[1, "year"] = 2025
    assert vd.check_mtbs_year_range(late, 1984, 2024).status == vd.FAIL


def test_missing_fire_id():
    assert vd.check_missing_fire_id(_fires()).status == vd.PASS
    f = _fires()
    f.loc[0, "FIRE_ID"] = None
    assert vd.check_missing_fire_id(f).status == vd.FAIL


def test_duplicate_fire_id():
    f = pd.concat([_fires(), _fires().head(1)], ignore_index=True)
    assert vd.check_duplicate_fire_id(f).status == vd.FAIL
    assert vd.check_duplicate_fire_id(_fires()).status == vd.PASS


def test_fire_type_domain():
    assert vd.check_fire_type_domain(_fires(), ["Wildfire", "Wildland Fire Use"]).status == vd.PASS
    f = _fires()
    f.loc[0, "fire_type"] = "Prescribed Fire"
    assert vd.check_fire_type_domain(f, ["Wildfire", "Wildland Fire Use"]).status == vd.FAIL


def test_duplicate_county_year_rows():
    cy = pd.DataFrame(
        {"GEOID": ["01001", "01001", "01002"], "year": [2000, 2001, 2000],
         "fire_count": [1, 1, 1], "total_burned_area_km2": [1.0, 2.0, 3.0],
         "county_area_km2": [100.0, 100.0, 200.0]}
    )
    assert vd.check_duplicate_county_year(cy).status == vd.PASS
    dup = pd.concat([cy, cy.head(1)], ignore_index=True)
    assert vd.check_duplicate_county_year(dup).status == vd.FAIL


def test_negative_areas_fail():
    def cy(v):
        return pd.DataFrame(
            {"GEOID": ["01001"], "year": [2000], "fire_count": [1],
             "total_burned_area_km2": [v], "county_area_km2": [100.0],
             "pct_county_burned_in_year": [v / 100.0 * 100]}
        )
    def ay(v):
        return pd.DataFrame(
            {"GEOID": ["01001"], "cumulative_burned_area_km2": [v],
             "unique_historical_burn_footprint_km2": [1.0]}
        )
    def inter(v):
        return pd.DataFrame({"GEOID": ["01001"], "intersection_area_km2": [v]})

    assert vd.check_negative_areas(cy(1.0), ay(1.0), inter(1.0)).status == vd.PASS
    assert vd.check_negative_areas(cy(-1.0), ay(1.0), inter(1.0)).status == vd.FAIL
    assert vd.check_negative_areas(cy(1.0), ay(-1.0), inter(1.0)).status == vd.FAIL
    assert vd.check_negative_areas(cy(1.0), ay(1.0), inter(-0.5)).status == vd.FAIL


def test_annual_pct_bounds():
    def cy(pct):
        return pd.DataFrame(
            {"GEOID": ["01001", "01002"], "year": [2000, 2000],
             "pct_county_burned_in_year": pct}
        )
    assert vd.check_annual_pct_bounds(cy([10.0, 99.0])).status == vd.PASS
    # >100 is WARN (investigate overlapping perimeters), not blind failure
    res = vd.check_annual_pct_bounds(cy([150.0, 20.0]))
    assert res.status == vd.WARN
    assert res.metrics["n_over_100"] == 1
    # negative is impossible -> FAIL
    assert vd.check_annual_pct_bounds(cy([-5.0, 20.0])).status == vd.FAIL


def test_footprint_pct_bounds():
    def ay(p):
        return pd.DataFrame({"GEOID": ["01001"], "pct_unique_historical_burn_footprint": p})
    assert vd.check_footprint_pct_bounds(ay([99.9])).status == vd.PASS
    assert vd.check_footprint_pct_bounds(ay([100.5])).status == vd.FAIL
    assert vd.check_footprint_pct_bounds(ay([-1.0])).status == vd.FAIL


def test_footprint_le_cumulative():
    def ay(fp, cum):
        return pd.DataFrame(
            {"GEOID": ["01001"],
             "unique_historical_burn_footprint_km2": [fp],
             "cumulative_burned_area_km2": [cum]}
        )
    assert vd.check_footprint_le_cumulative(ay(50.0, 60.0)).status == vd.PASS
    assert vd.check_footprint_le_cumulative(ay(61.0, 60.0)).status == vd.FAIL


def test_cumulative_vs_county_area_reburn_is_not_failure():
    def ay(cum):
        return pd.DataFrame(
            {"GEOID": ["01001"], "cumulative_burned_area_km2": [cum],
             "county_area_km2": [100.0]}
        )
    res = vd.check_cumulative_vs_county_area(ay(250.0))
    assert res.status == vd.INFO_STATUS  # reburn, recorded not failed
    assert res.metrics["n_over_100pct"] == 1


def test_count_consistency():
    cy = pd.DataFrame(
        {"GEOID": ["01001", "01002"], "year": [2000, 2000],
         "fire_count": [2, 1], "total_burned_area_km2": [10.0, 5.0]}
    )
    ay = pd.DataFrame(
        {"GEOID": ["01001"], "unique_fire_count": [3]}
    )
    # sum of county-year counts (3) >= distinct fires overall (3)
    assert vd.check_count_consistency(cy, ay).status == vd.PASS
    bad_cy = cy.assign(fire_count=[2, 1], total_burned_area_km2=[0.0, 5.0])
    assert vd.check_count_consistency(bad_cy, ay).status == vd.FAIL
    bad_cy2 = cy.assign(fire_count=[0, 1])
    assert vd.check_count_consistency(bad_cy2, ay).status == vd.FAIL


def test_intersection_vs_county_area():
    # _counties() areas: GEOID 01001 -> 1000 km2, 01002 -> 2000, 01003 -> 3000
    counties = _counties()
    inter = pd.DataFrame(
        {"GEOID": ["01001", "01002"], "intersection_area_km2": [500.0, 1500.0]}
    )
    assert vd.check_intersection_vs_county_area(inter, counties).status == vd.PASS
    bad = pd.DataFrame({"GEOID": ["01001"], "intersection_area_km2": [5000.0]})
    assert vd.check_intersection_vs_county_area(bad, counties).status == vd.FAIL


def test_event_count_accounting():
    report = {
        "n_raw_total": 100,
        "n_conus": 60,
        "n_retained": 55,
        "by_fire_type": {"Wildfire": 40, "Prescribed Fire": 20},
        "fire_types_included": ["Wildfire", "Wildland Fire Use"],
        "min_year": 1984,
        "max_year": 2024,
    }
    assert vd.check_event_counts(report).status == vd.PASS
    bad = dict(report, n_retained=70)
    assert vd.check_event_counts(bad).status == vd.FAIL


def test_provenance(tmp_path):
    import json

    # missing file -> FAIL
    missing = vd.check_provenance(tmp_path / "nope.json")
    assert missing.status == vd.FAIL

    good = tmp_path / "provenance.json"
    payload = {
        "datasets": {
            "mtbs_perimeter": {
                "url": "https://example.gov/x.zip",
                "retrieval_utc": "2026-09-30T00:00:00Z",
                "sha256": "0" * 64,
                "size_bytes": 123,
                "archive_valid": True,
            },
            "census_counties": {
                "url": "https://example.gov/y.zip",
                "retrieval_utc": "2026-09-30T00:00:00Z",
                "sha256": "1" * 64,
                "size_bytes": 456,
                "archive_valid": True,
            },
        }
    }
    good.write_text(json.dumps(payload))
    assert vd.check_provenance(good).status == vd.PASS

    payload["datasets"]["census_counties"]["archive_valid"] = False
    good.write_text(json.dumps(payload))
    assert vd.check_provenance(good).status == vd.FAIL