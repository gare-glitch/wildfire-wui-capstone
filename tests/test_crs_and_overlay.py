"""Tests for CRS handling and area-measurement conventions.

Verifies that EPSG:5070 is a projected, meter-based CRS suitable for CONUS
area math, and that area helpers never operate in geographic coordinates.
"""

import geopandas as gpd
import pytest
from shapely.geometry import box

import spatial_overlay as ov

CRS_CONUS = "EPSG:5070"


def test_epsg5070_is_projected_meters():
    crs = gpd.GeoSeries([], crs=CRS_CONUS).crs
    assert crs is not None
    assert not crs.is_geographic
    units = crs.axis_info[0].unit_name
    assert units in ("metre", "meter", "m")
    assert crs.to_epsg() == 5070


def test_epsg5070_is_equal_area_class():
    """Conus Albers is an Albers Equal Area projection (EPSG registry method)."""
    import pyproj

    crs = pyproj.CRS.from_epsg(5070)
    op = crs.coordinate_operation
    assert op is not None
    # EPSG registry operation name is 'Conus Albers'; the projection METHOD is
    # the authoritative signal for the equal-area property.
    assert "albers" in op.name.lower()
    assert "albers" in op.method_name.lower()
    assert "equal" in op.method_name.lower()


def test_area_in_projected_crs_matches_nominal_km2():
    """A 10x10 km square defined in EPSG:5070 measures 100 km2 exactly."""
    g = gpd.GeoDataFrame(geometry=[box(0, 0, 10_000, 10_000)], crs=CRS_CONUS)
    assert g.geometry.area.sum() * ov.KM2_PER_M2 == pytest.approx(100.0)


def test_overlay_rejects_geographic_county_crs():
    """The overlay engine must refuse to measure areas in latitude/longitude."""
    counties = gpd.GeoDataFrame(
        {"GEOID": ["01001"], "geometry": [box(-90.0, 30.0, -89.0, 31.0)]},
        crs="EPSG:4269",  # NAD83 geographic (degrees)
    )
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["F1"],
            "fire_name": ["X"],
            "fire_type": ["Wildfire"],
            "year": [2000],
            "geometry": [box(-90.0, 30.0, -89.5, 30.5)],
        },
        crs="EPSG:4269",
    )
    with pytest.raises(ValueError, match="EPSG:5070"):
        ov.intersect_fires_counties(fires, counties, min_intersection_area_km2=1e-4)


def test_overlay_rejects_missing_crs():
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["F1"],
            "fire_name": ["X"],
            "fire_type": ["Wildfire"],
            "year": [2000],
            "geometry": [box(0, 0, 1, 1)],
        }
    )
    counties = gpd.GeoDataFrame(
        {"GEOID": ["01001"], "geometry": [box(0, 0, 2, 2)]}, crs=CRS_CONUS
    )
    with pytest.raises(ValueError, match="CRS"):
        ov.intersect_fires_counties(fires, counties, min_intersection_area_km2=1e-4)


def test_intersect_fires_counties_basic_math():
    """Fire spanning two counties contributes the correct fragment area to each."""
    counties = gpd.GeoDataFrame(
        {
            "GEOID": ["01001", "01002"],
            "geometry": [box(0, 0, 10_000, 10_000), box(10_000, 0, 20_000, 10_000)],
        },
        crs=CRS_CONUS,
    )
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["F1"],
            "fire_name": ["Border Fire"],
            "fire_type": ["Wildfire"],
            "year": [2005],
            "geometry": [box(1_000, 1_000, 11_000, 11_000)],
        },
        crs=CRS_CONUS,
    )
    out = ov.intersect_fires_counties(fires, counties, min_intersection_area_km2=1e-6)
    assert len(out) == 2
    by_geoid = out.set_index("GEOID")["intersection_area_km2"]
    assert by_geoid["01001"] == pytest.approx(81.0)
    assert by_geoid["01002"] == pytest.approx(9.0)
    assert out["FIRE_ID"].nunique() == 1  # one fire, two fragments


def test_intersect_drops_boundary_touch_slivers():
    """A fire that only touches a county boundary contributes zero area."""
    counties = gpd.GeoDataFrame(
        {
            "GEOID": ["01001", "01002"],
            "geometry": [box(0, 0, 10_000, 10_000), box(10_000, 0, 20_000, 10_000)],
        },
        crs=CRS_CONUS,
    )
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["F1"],
            "fire_name": ["Left Fire"],
            "fire_type": ["Wildfire"],
            "year": [2005],
            "geometry": [box(0, 0, 10_000, 10_000)],  # inside A, touches B on edge
        },
        crs=CRS_CONUS,
    )
    out = ov.intersect_fires_counties(fires, counties, min_intersection_area_km2=1e-6)
    assert list(out["GEOID"]) == ["01001"]
    assert out["intersection_area_km2"].iloc[0] == pytest.approx(100.0)


def test_intersect_repairs_invalid_geometry_instead_of_failing():
    """Self-intersecting (bowtie) fire geometry is repaired, not dropped silently.

    The bowtie (0,0)-(12000,12000)-(12000,0)-(0,12000) has zero signed area;
    make_valid retains the positively-oriented lobe, whose intersection with
    the county box measures 50 km2 (GEOS behavior at the pinned version).
    """
    from shapely.geometry import Polygon

    bowtie = Polygon([(0, 0), (12_000, 12_000), (12_000, 0), (0, 12_000)])
    counties = gpd.GeoDataFrame(
        {"GEOID": ["01001"], "geometry": [box(0, 0, 10_000, 10_000)]},
        crs=CRS_CONUS,
    )
    fires = gpd.GeoDataFrame(
        {
            "FIRE_ID": ["B1"],
            "fire_name": ["Bowtie"],
            "fire_type": ["Wildfire"],
            "year": [2005],
            "geometry": [bowtie],
        },
        crs=CRS_CONUS,
    )
    out = ov.intersect_fires_counties(fires, counties, min_intersection_area_km2=1e-6)
    assert len(out) == 1
    assert out["intersection_area_km2"].iloc[0] == pytest.approx(50.0)
    assert out.geometry.is_valid.all()