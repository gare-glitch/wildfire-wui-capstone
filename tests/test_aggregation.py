"""Tests for deterministic aggregation logic (build_county_year.py).

Synthetic geometries use EPSG:5070 coordinates directly so areas are exact:
- County A = [0,10] km x [0,10] km  (100 km2)
- County B = [10,20] km x [0,10] km (100 km2)
- Fires span/overlap these counties with hand-derivable fragment areas.
"""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

import build_county_year as bcy

CRS = "EPSG:5070"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_counties() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "GEOID": ["01001", "01002"],
            "county_name": ["County A", "County B"],
            "state_fips": ["01", "01"],
            "state_name": ["Alabama", "Alabama"],
            "geometry": [box(0, 0, 10_000, 10_000), box(10_000, 0, 20_000, 10_000)],
        },
        crs=CRS,
    ).assign(county_area_km2=lambda df: df.geometry.area * 1e-6)


def make_fires() -> gpd.GeoDataFrame:
    """F1 (2000): x[1,11],y[1,11] -> A:81, B:9
    F2 (2001): x[2,12],y[2,8]  -> A:48, B:12
    F3 (2001): x[0,10],y[0,10] -> A:100 (fully inside A, overlaps F2)
    """
    return gpd.GeoDataFrame(
        {
            "FIRE_ID": ["F1", "F2", "F3"],
            "fire_name": ["Alpha", "Bravo", "Charlie"],
            "fire_type": ["Wildfire"] * 3,
            "year": [2000, 2001, 2001],
            "geometry": [
                box(1_000, 1_000, 11_000, 11_000),
                box(2_000, 2_000, 12_000, 8_000),
                box(0, 0, 10_000, 10_000),
            ],
        },
        crs=CRS,
    )


def make_intersections() -> pd.DataFrame:
    """Fragment table consistent with make_fires() geometry."""
    rows = [
        {"GEOID": "01001", "FIRE_ID": "F1", "year": 2000, "intersection_area_km2": 81.0},
        {"GEOID": "01001", "FIRE_ID": "F2", "year": 2001, "intersection_area_km2": 48.0},
        {"GEOID": "01002", "FIRE_ID": "F2", "year": 2001, "intersection_area_km2": 12.0},
        {"GEOID": "01001", "FIRE_ID": "F3", "year": 2001, "intersection_area_km2": 100.0},
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# county-year aggregation
# ---------------------------------------------------------------------------
def test_aggregate_county_year_counts_distinct_fires_not_fragments():
    inter = make_intersections()
    # simulate an extra geometry fragment for the same fire-county pair
    inter = pd.concat(
        [inter, inter.iloc[[1]].assign(intersection_area_km2=0.5)], ignore_index=True
    )
    cy = bcy.aggregate_county_year(inter)
    a_2001 = cy[(cy.GEOID == "01001") & (cy.year == 2001)].iloc[0]
    assert a_2001["fire_count"] == 2  # F2 + F3, not 3 fragments
    assert a_2001["total_burned_area_km2"] == pytest.approx(148.5)


def test_aggregate_county_year_shape_and_years():
    cy = bcy.aggregate_county_year(make_intersections())
    assert set(cy.columns) >= {"GEOID", "year", "fire_count", "total_burned_area_km2"}
    assert len(cy) == 3  # (A,2000), (A,2001), (B,2001)
    assert not cy.duplicated(subset=["GEOID", "year"]).any()


def test_attach_county_attrs_and_pct():
    counties = make_counties()
    cy = bcy.aggregate_county_year(make_intersections())
    cy = bcy.attach_county_attrs(cy, counties)
    a_2000 = cy[(cy.GEOID == "01001") & (cy.year == 2000)].iloc[0]
    assert a_2000["county_name"] == "County A"
    assert a_2000["county_area_km2"] == pytest.approx(100.0)
    assert a_2000["pct_county_burned_in_year"] == pytest.approx(81.0)
    b_2001 = cy[(cy.GEOID == "01002") & (cy.year == 2001)].iloc[0]
    assert b_2001["pct_county_burned_in_year"] == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# all-years summary
# ---------------------------------------------------------------------------
def test_summarize_all_years_metrics():
    counties = make_counties()
    summary = bcy.summarize_all_years(make_intersections())
    summary = bcy.attach_county_attrs_all_years(summary, counties)

    a = summary[summary.GEOID == "01001"].iloc[0]
    assert a["number_of_fire_years"] == 2
    assert a["unique_fire_count"] == 3
    assert a["cumulative_burned_area_km2"] == pytest.approx(229.0)  # 81+48+100
    b = summary[summary.GEOID == "01002"].iloc[0]
    assert b["unique_fire_count"] == 1
    assert b["cumulative_burned_area_km2"] == pytest.approx(12.0)


def test_cumulative_can_exceed_county_area_via_reburn():
    """Reburned land legitimately drives cumulative area > county area."""
    counties = make_counties()
    rows = [
        {"GEOID": "01001", "FIRE_ID": f"R{i}", "year": 2000 + i,
         "intersection_area_km2": 90.0}
        for i in range(3)
    ]
    summary = bcy.attach_county_attrs_all_years(
        bcy.summarize_all_years(pd.DataFrame(rows)), counties
    )
    a = summary[summary.GEOID == "01001"].iloc[0]
    assert a["cumulative_burned_area_km2"] == 270.0 > a["county_area_km2"]


# ---------------------------------------------------------------------------
# unique historical burn footprint
# ---------------------------------------------------------------------------
def test_footprint_dedupes_overlapping_fires():
    """Two fully overlapping fires -> footprint == one fire's area, not two."""
    counties = make_counties()
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["X1", "X2"],
            "year": [2000, 2005],
            "geometry": [box(1_000, 1_000, 11_000, 11_000)] * 2,
        },
        crs=CRS,
    )
    inter = pd.DataFrame(
        [
            {"GEOID": "01001", "FIRE_ID": "X1", "year": 2000, "intersection_area_km2": 81.0},
            {"GEOID": "01001", "FIRE_ID": "X2", "year": 2005, "intersection_area_km2": 81.0},
        ]
    )
    fp = bcy.compute_footprint_by_county(fires, inter, counties)
    a = fp[fp.GEOID == "01001"].iloc[0]
    assert a["unique_historical_burn_footprint_km2"] == pytest.approx(81.0)  # not 162
    assert a["pct_unique_historical_burn_footprint"] == pytest.approx(81.0)


def test_footprint_partial_overlap_between_max_and_sum():
    """Partial overlap: footprint strictly between max(single) and sum."""
    counties = make_counties()
    # P1: x[1,11],y[1,11] -> A:81, B:9 ; P2: x[0.5,8.5],y[0.5,8.5] -> A:64, B:0
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["P1", "P2"],
            "year": [2000, 2001],
            "geometry": [
                box(1_000, 1_000, 11_000, 11_000),
                box(500, 500, 8_500, 8_500),
            ],
        },
        crs=CRS,
    )
    inter = pd.DataFrame(
        [
            {"GEOID": "01001", "FIRE_ID": "P1", "year": 2000, "intersection_area_km2": 81.0},
            {"GEOID": "01001", "FIRE_ID": "P2", "year": 2001, "intersection_area_km2": 64.0},
            {"GEOID": "01002", "FIRE_ID": "P1", "year": 2000, "intersection_area_km2": 9.0},
        ]
    )
    fp = bcy.compute_footprint_by_county(fires, inter, counties)
    a = fp[fp.GEOID == "01001"].iloc[0]["unique_historical_burn_footprint_km2"]
    # union(P1,P2) within A = 81 + 64 - 56.25 = 88.75
    assert a == pytest.approx(88.75)
    assert a > 81.0 and a < 145.0  # strictly between max and sum
    b = fp[fp.GEOID == "01002"].iloc[0]["unique_historical_burn_footprint_km2"]
    assert b == pytest.approx(9.0)


def test_footprint_never_exceeds_county_area():
    """Even with many heavily overlapping fires, footprint <= county area."""
    counties = make_counties()
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": [f"Y{i}" for i in range(10)],
            "year": [2000 + i for i in range(10)],
            "geometry": [box(0, 0, 10_000, 10_000)] * 10,
        },
        crs=CRS,
    )
    inter = pd.DataFrame(
        [
            {"GEOID": "01001", "FIRE_ID": f"Y{i}", "year": 2000 + i,
             "intersection_area_km2": 100.0}
            for i in range(10)
        ]
    )
    fp = bcy.compute_footprint_by_county(fires, inter, counties)
    a = fp[fp.GEOID == "01001"].iloc[0]
    assert a["unique_historical_burn_footprint_km2"] == pytest.approx(100.0)
    assert a["pct_unique_historical_burn_footprint"] == pytest.approx(100.0)
    # cumulative, by contrast, is 10x the county area (reburn)
    summary = bcy.summarize_all_years(inter)
    assert summary[summary.GEOID == "01001"].iloc[0]["cumulative_burned_area_km2"] == 1000.0


def test_footprint_uses_full_fire_geometry_not_fragments():
    """A county's footprint must include parts of fires that also cover other
    counties — footprint is computed from full perimeters, not fragments."""
    counties = make_counties()
    # fire spans the whole A+B strip; B fragment list only has this fire
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["S1", "S2"],
            "year": [2000, 2002],
            "geometry": [
                box(2_000, 2_000, 12_000, 8_000),   # A:48, B:12
                box(4_000, 2_000, 14_000, 8_000),   # A:36, B:24; overlaps S1 in B by 12
            ],
        },
        crs=CRS,
    )
    inter = pd.DataFrame(
        [
            {"GEOID": "01002", "FIRE_ID": "S1", "year": 2000, "intersection_area_km2": 12.0},
            {"GEOID": "01002", "FIRE_ID": "S2", "year": 2002, "intersection_area_km2": 24.0},
        ]
    )
    fp = bcy.compute_footprint_by_county(fires, inter, counties)
    b = fp[fp.GEOID == "01002"].iloc[0]["unique_historical_burn_footprint_km2"]
    # union(S1,S2) within B: x[10,12]u[10,14] -> x[10,14] x y[2,8] = 4*6 = 24
    assert b == pytest.approx(24.0)


def test_finalize_summary_zero_fills_counties_without_fires():
    counties = make_counties()
    fires = gpd.GeoDataFrame(
        {"FIRE_ID": ["Z1"], "year": [2000], "geometry": [box(0, 0, 10_000, 10_000)]},
        crs=CRS,
    )
    inter = pd.DataFrame(
        [{"GEOID": "01001", "FIRE_ID": "Z1", "year": 2000, "intersection_area_km2": 100.0}]
    )
    fp = bcy.compute_footprint_by_county(fires, inter, counties)
    final = bcy.finalize_summary(
        counties, bcy.summarize_all_years(inter), fp
    )
    assert len(final) == 2
    no_fire = final[final.GEOID == "01002"].iloc[0]
    assert no_fire["unique_fire_count"] == 0
    assert no_fire["number_of_fire_years"] == 0
    assert no_fire["unique_historical_burn_footprint_km2"] == 0.0
    assert no_fire["pct_unique_historical_burn_footprint"] == 0.0