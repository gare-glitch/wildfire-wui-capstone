"""Tests for Stage 2 (WUI) functions on synthetic data.

Covers the pure processing functions of preprocess_wui, build_wui_fire,
wui_change, nature_wui, compare_wui, places_wui, validate_stage2, and the
Stage 2 downloader path — including regression tests for bugs found during
implementation (see names ending _regression).
"""

import json
import os
import re
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

import build_wui_fire as bwf
import common
import compare_wui as cmp_wui
import download_data as dl
import make_maps_stage2 as maps2
import nature_wui as nat
import places_wui as plw
import preprocess_wui as pw
import validate_stage2 as v2
import wui_change as wch
from validate_data import FAIL, PASS, WARN

CRS5070 = "EPSG:5070"
KM2_PER_M2 = 1e-6


def _s2():
    """Minimal stage-2 config mirroring config/analysis.yaml stage2."""
    return {
        "wui_vintages": [1990, 2000, 2010, 2020],
        "vintage_periods": {
            "1990": {"min_year": 1990, "max_year": 1999},
            "2000": {"min_year": 2000, "max_year": 2009},
            "2010": {"min_year": 2010, "max_year": 2019},
            "2020": {"min_year": 2020, "max_year": 2024},
        },
        "pre_wui_baseline": {
            "min_year": 1984, "max_year": 1989,
            "label": "pre_1990_no_wui_baseline",
        },
        "silvis": {
            "intermix_classes": ["Low_Dens_Intermix"],
            "interface_classes": ["Low_Dens_Interface"],
        },
        "nature": {"valid_values": [1, 2, 3, 4, 5, 6, 7, 8]},
        "validation": {"max_pct_county_wui": 100.0},
        "mapping": {"dpi": 72},
    }


# ---------------------------------------------------------------------------
# Temporal alignment (build_wui_fire.assign_vintage)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "year,expected",
    [(1984, "pre_1990_no_wui_baseline"), (1989, "pre_1990_no_wui_baseline"),
     (1990, 1990), (1995, 1990), (1999, 1990),
     (2000, 2000), (2005, 2000), (2009, 2000),
     (2010, 2010), (2015, 2010), (2019, 2010),
     (2020, 2020), (2024, 2020)],
)
def test_assign_vintage_mapping(year, expected):
    assert bwf.assign_vintage(year, _s2()) == expected


@pytest.mark.parametrize("year", [1983, 2025])
def test_assign_vintage_outside_config_raises(year):
    with pytest.raises(ValueError):
        bwf.assign_vintage(year, _s2())


def test_vintage_of_period():
    assert bwf.vintage_of_period("1990-1999", _s2()) == 1990
    assert bwf.vintage_of_period("2020-2024", _s2()) == 2020
    with pytest.raises(ValueError):
        bwf.vintage_of_period("1991-1999", _s2())


def test_no_2020_vintage_on_earlier_decades():
    """Regression guard: early fire years must never map to 2020."""
    s2 = _s2()
    for year in range(1990, 2020):
        assert bwf.assign_vintage(year, s2) != 2020


# ---------------------------------------------------------------------------
# wui_change pure functions
# ---------------------------------------------------------------------------
def test_pct_change_basic_and_zero_base():
    assert wch.pct_change(110.0, 100.0) == pytest.approx(10.0)
    assert np.isnan(wch.pct_change(10.0, 0.0))
    assert np.isnan(wch.pct_change(0.0, 0.0))


def _county_wui_table():
    """Two counties x two vintages (1990, 2020 kept minimal but realistic)."""
    rows = []
    for y in (1990, 2000, 2010, 2020):
        for geoid, name, area in (("01001", "A", 1000.0), ("01002", "B", 500.0)):
            wui = 100.0 if geoid == "01001" else 0.0
            rows.append({
                "GEOID": geoid, "county_name": name, "state_fips": "01",
                "state_name": "Alabama", "wui_year": y,
                "county_area_km2": area,
                "wui_area_km2": wui,
                "interface_wui_area_km2": wui * 0.25,
                "intermix_wui_area_km2": wui * 0.75,
                "hu_in_wui": 10.0 if geoid == "01001" else 0.0,
            })
    return pd.DataFrame(rows)


def test_build_change_table_transitions_and_math():
    change = wch.build_change_table(_county_wui_table())
    got = set(zip(change["base_year"], change["end_year"]))
    assert got == {(1990, 2000), (2000, 2010), (2010, 2020), (1990, 2020)}
    a = change[(change["GEOID"] == "01001") & (change["transition"] == "1990->2020")]
    assert a["wui_area_change_km2"].iloc[0] == 0.0
    # interface + intermix change sums to total change
    comp = (a["interface_wui_area_change_km2"] + a["intermix_wui_area_change_km2"]
            - a["wui_area_change_km2"]).abs()
    assert comp.iloc[0] < 1e-9
    # zero-base county: pct NaN but absolute change defined
    b = change[(change["GEOID"] == "01002") & (change["transition"] == "1990->2020")]
    assert b["wui_area_change_km2"].iloc[0] == 0.0


# ---------------------------------------------------------------------------
# preprocess_wui pure functions
# ---------------------------------------------------------------------------
def test_allocate_housing_preserves_totals():
    df = pd.DataFrame({
        "BLK20": ["b1", "b1", "b2"],
        "fragment_area_km2": [25.0, 75.0, 40.0],
        "hu_1990": [10.0, 10.0, 7.0],
        "hu_2020": [20.0, 20.0, 9.0],
    })
    out = pw.allocate_housing(df)
    assert out.loc[0, "hu_1990"] == pytest.approx(2.5)
    assert out.loc[1, "hu_1990"] == pytest.approx(7.5)
    assert out.loc[2, "hu_1990"] == pytest.approx(7.0)
    # block totals preserved exactly
    assert out[out["BLK20"] == "b1"]["hu_2020"].sum() == pytest.approx(20.0)


def _fragments():
    """Two WUI fragments in county A, none in B; class differs by vintage."""
    return pd.DataFrame({
        "BLK20": ["b1", "b2"],
        "GEOID": ["01001", "01001"],
        "fragment_area_km2": [100.0, 50.0],
        "wui_class_1990": ["Low_Dens_Intermix", None],
        "wui_class_2000": ["Low_Dens_Interface", "Low_Dens_Intermix"],
        "wui_class_2010": ["Low_Dens_Intermix", "Low_Dens_Intermix"],
        "wui_class_2020": ["Low_Dens_Interface", None],
        "hu_1990": [10.0, 4.0],
        "hu_2000": [11.0, 5.0],
        "hu_2010": [12.0, 6.0],
        "hu_2020": [13.0, 7.0],
    })


def _counties():
    return gpd.GeoDataFrame(
        {
            "GEOID": ["01001", "01002"],
            "county_name": ["A", "B"],
            "state_fips": ["01", "01"],
            "state_name": ["Alabama", "Alabama"],
            "county_area_km2": [1000.0, 500.0],
        },
        geometry=[box(0, 0, 1000, 1000), box(0, 1000, 500, 2000)],
        crs=CRS5070,
    )


def test_county_wui_metrics_vintage_specific():
    """Regression: county housing totals must be vintage-specific per row."""
    hu_totals = {
        "1990": {"01001": 100.0, "01002": 40.0},
        "2000": {"01001": 110.0, "01002": 45.0},
        "2010": {"01001": 120.0, "01002": 50.0},
        "2020": {"01001": 130.0, "01002": 55.0},
    }
    metrics = pw.county_wui_metrics(_fragments(), _counties(), hu_totals, _s2())
    # full grid: 2 counties x 4 vintages, unique keys
    assert len(metrics) == 8
    assert not metrics.duplicated(["GEOID", "wui_year"]).any()
    # county B (no WUI) is zero-filled but present in every vintage
    b = metrics[metrics["GEOID"] == "01002"]
    assert (b["wui_area_km2"] == 0).all() and len(b) == 4
    # vintage-specific areas: 1990 has only b1, 2000 has both
    a = metrics.set_index(["GEOID", "wui_year"])
    assert a.loc[("01001", 1990), "wui_area_km2"] == 100.0
    assert a.loc[("01001", 2000), "wui_area_km2"] == 150.0
    # interface/intermix split sums to the total
    for _, r in metrics.iterrows():
        if r["wui_area_km2"] > 0:
            assert (r["interface_wui_area_km2"] + r["intermix_wui_area_km2"]
                    == pytest.approx(r["wui_area_km2"]))
    # regression: hu_total_county differs by vintage on the SAME county
    assert a.loc[("01001", 1990), "hu_total_county"] == 100.0
    assert a.loc[("01001", 2020), "hu_total_county"] == 130.0


# ---------------------------------------------------------------------------
# build_wui_fire: county-year and county-period tables
# ---------------------------------------------------------------------------
def _pieces():
    """Fire x WUI intersection pieces (EPSG:5070 boxes inside county A)."""
    return gpd.GeoDataFrame(
        {
            "fire_id": ["F1", "F1", "F2"],
            "year": [1995, 2021, 2021],
            "GEOID": ["01001", "01001", "01001"],
            "BLK20": ["b1", "b1", "b2"],
            "fragment_area_km2": [100.0, 100.0, 50.0],
            "wui_class_1990": ["Low_Dens_Intermix", None, None],
            "wui_class_2000": [None, None, None],
            "wui_class_2010": [None, None, None],
            "wui_class_2020": [None, "Low_Dens_Interface", "Low_Dens_Intermix"],
            "intersection_area_km2": [10.0, 20.0, 5.0],
        },
        geometry=[box(0, 0, 10, 1), box(0, 0, 20, 1), box(0, 2, 5, 3)],
        crs=CRS5070,
    )


def _county_year_stage1():
    """Stage 1 county-year schema (columns as produced by build_county_year)."""
    return pd.DataFrame({
        "GEOID": ["01001"] * 3,
        "year": [1985, 1995, 2021],
        "fire_count": [1, 1, 2],
        "total_burned_area_km2": [30.0, 50.0, 80.0],
        "county_name": ["A"] * 3,
        "state_fips": ["01"] * 3,
        "state_name": ["Alabama"] * 3,
        "county_area_km2": [1000.0] * 3,
        "pct_county_burned_in_year": [3.0, 5.0, 8.0],
    })


def _county_wui():
    return pd.DataFrame({
        "GEOID": ["01001", "01001", "01001", "01001"],
        "wui_year": [1990, 2000, 2010, 2020],
        "wui_area_km2": [100.0, 150.0, 150.0, 150.0],
        "interface_wui_area_km2": [25.0, 62.5, 112.5, 112.5],
        "intermix_wui_area_km2": [75.0, 87.5, 37.5, 37.5],
        "hu_in_wui": [10.0, 16.0, 18.0, 13.0],
    })


def _fire_county_intersections():
    """Stage 1 fire x county fragments (for distinct-fire counting)."""
    return pd.DataFrame({
        "GEOID": ["01001", "01001"],
        "FIRE_ID": ["F1", "F2"],
        "year": [1995, 2021],
    })


def test_build_county_year_wui_fire_alignment():
    out = bwf.build_county_year_wui_fire(
        _pieces(), _county_year_stage1(), _county_wui(), _s2()
    )
    out = out.drop_duplicates(["GEOID", "year"])
    by_year = out.set_index("year")
    # 1985: null integer vintage, explicit pre-baseline label, null WUI exposure
    assert pd.isna(by_year.loc[1985, "assigned_wui_vintage"])
    assert by_year.loc[1985, "assigned_wui_vintage_label"] == "pre_1990_no_wui_baseline"
    assert pd.isna(by_year.loc[1985, "wui_area_km2"])
    assert pd.isna(by_year.loc[1985, "wui_burned_area_km2"])
    # 1995: 1990 vintage; only the 1995 piece counts
    assert by_year.loc[1995, "assigned_wui_vintage"] == 1990
    assert by_year.loc[1995, "assigned_wui_vintage_label"] == "1990"
    assert by_year.loc[1995, "wui_burned_area_km2"] == pytest.approx(10.0)
    assert by_year.loc[1995, "wui_area_km2"] == pytest.approx(100.0)
    assert by_year.loc[1995, "pct_wui_burned_in_year"] == pytest.approx(10.0)
    assert by_year.loc[1995, "pct_burned_area_that_was_wui"] == pytest.approx(20.0)
    # 2021: 2020 vintage; pieces from both blocks
    assert by_year.loc[2021, "assigned_wui_vintage"] == 2020
    assert by_year.loc[2021, "assigned_wui_vintage_label"] == "2020"
    assert by_year.loc[2021, "wui_burned_area_km2"] == pytest.approx(25.0)
    assert by_year.loc[2021, "interface_wui_burned_km2"] == pytest.approx(20.0)
    assert by_year.loc[2021, "intermix_wui_burned_km2"] == pytest.approx(5.0)


def test_build_county_period_labels_and_footprints():
    """Regression: period labels derive from configured spans (not bare keys)."""
    out = bwf.build_county_period_wui_fire(
        _pieces(), _county_year_stage1(), _county_wui(), _s2(),
        fire_county_intersections=_fire_county_intersections(),
    )
    assert set(out["period"]) == {"1990-1999", "2000-2009", "2010-2019", "2020-2024"}
    assert not out.duplicated(["GEOID", "period"]).any()
    p1990 = out[out["period"] == "1990-1999"].iloc[0]
    p2020 = out[out["period"] == "2020-2024"].iloc[0]
    assert p1990["wui_burned_area_km2"] == pytest.approx(10.0)
    assert p2020["wui_burned_area_km2"] == pytest.approx(25.0)
    # unique footprint never exceeds cumulative burn
    assert p2020["unique_wui_burned_footprint_km2"] <= \
        p2020["wui_burned_area_km2"] + 1e-9
    # footprint <= vintage-matched WUI area
    assert p2020["unique_wui_burned_footprint_km2"] <= p2020["wui_area_km2"]


# ---------------------------------------------------------------------------
# nature_wui county table
# ---------------------------------------------------------------------------
def test_build_county_nature_table():
    info = {"pixel_area_km2": 0.0001}
    # 1 county, 2 pixels each of classes 1, 2, 3, 4 and 5 pixels of class 5
    accum = {"01001": {1: 2, 2: 2, 3: 2, 4: 2, 5: 5}}
    counties = _counties()
    df = nat.build_county_nature_table(accum, info, counties)
    row = df.iloc[0]
    px = 0.0001
    assert row["nature_wui_area_km2"] == pytest.approx(8 * px)
    assert row["nature_intermix_wui_area_km2"] == pytest.approx(4 * px)   # 1 + 3
    assert row["nature_interface_wui_area_km2"] == pytest.approx(4 * px)  # 2 + 4
    assert row["nature_classified_area_km2"] == pytest.approx(13 * px)
    assert row["pct_of_nature_wui_that_is_interface"] == pytest.approx(50.0)
    # county with no raster pixels at all
    assert df[df["GEOID"] == "01002"]["nature_wui_area_km2"].iloc[0] == 0.0


# Representative real VRT metadata from the inspected Schug 2023 NA mosaic
# (NA/mosaic/WUI.vrt): VRTDataset rasterXSize/rasterYSize (NOT sizeX/sizeY),
# GeoTransform, band NoDataValue 255, ComplexSource with SourceProperties
# RasterXSize/RasterYSize, SrcRect, DstRect, per-source NODATA.
_REAL_NATURE_VRT = """<VRTDataset rasterXSize="1110000" rasterYSize="900000">
  <SRS dataAxisToSRSAxisMapping="1,2">PROJCS["Azimuthal_Equidistant"]</SRS>
  <GeoTransform>  9.0000000000000000e+05,  1.0000000000000000e+01,  0.0000000000000000e+00,  9.1000000000000000e+06,  0.0000000000000000e+00, -1.0000000000000000e+01</GeoTransform>
  <VRTRasterBand dataType="Byte" band="1">
    <NoDataValue>255</NoDataValue>
    <ColorInterp>Gray</ColorInterp>
    <ComplexSource>
      <SourceFilename relativeToVRT="1">../X0086_Y0054/WUI.tif</SourceFilename>
      <SourceBand>1</SourceBand>
      <SourceProperties RasterXSize="10000" RasterYSize="10000" DataType="Byte" BlockXSize="10000" BlockYSize="1" />
      <SrcRect xOff="0" yOff="0" xSize="10000" ySize="10000" />
      <DstRect xOff="770000" yOff="460000" xSize="10000" ySize="10000" />
      <NODATA>255</NODATA>
    </ComplexSource>
  </VRTRasterBand>
</VRTDataset>
"""


def _vrt_zip(tmp_path, vrt_xml: str):
    import zipfile

    zpath = tmp_path / "NA.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("NA/mosaic/WUI.vrt", vrt_xml)
    return zpath


def test_parse_vrt_tiles_regression(tmp_path):
    """Regression: Nature VRT uses rasterXSize/rasterYSize (not sizeX/sizeY),
    relative ../X####_Y####/WUI.tif members, SrcRect/DstRect xOff semantics,
    and band NoDataValue 255. int(None) from the wrong attribute names was
    the production crash."""
    info, tiles = nat.parse_vrt_tiles(_vrt_zip(tmp_path, _REAL_NATURE_VRT),
                                      "NA/mosaic/WUI.vrt")
    assert info["raster_xsize"] == 1110000
    assert info["raster_ysize"] == 900000
    assert info["n_tiles"] == 1
    assert info["nodata_value"] == 255
    assert info["band_datatype"] == "Byte"
    assert tiles[0]["member"] == "NA/X0086_Y0054/WUI.tif"
    assert tiles[0]["xoff"] == 770000
    assert tiles[0]["yoff"] == 460000
    assert tiles[0]["nx"] == 10000
    assert tiles[0]["ny"] == 10000
    assert info["geotransform"][0] == pytest.approx(900000.0)
    assert info["geotransform"][1] == pytest.approx(10.0)
    assert info["geotransform"][5] == pytest.approx(-10.0)


def test_parse_vrt_rejects_sizeX_sizeY_dataset_attrs(tmp_path):
    """The original bug: reading sizeX/sizeY (absent) yielded None -> int(None).
    A VRT that only has those wrong names must fail loudly, not guess."""
    bad = _REAL_NATURE_VRT.replace("rasterXSize", "sizeX").replace(
        "rasterYSize", "sizeY"
    )
    with pytest.raises(ValueError, match="rasterXSize"):
        nat.parse_vrt_tiles(_vrt_zip(tmp_path, bad), "NA/mosaic/WUI.vrt")


def test_parse_vrt_rejects_missing_dstrect_attrs(tmp_path):
    bad = _REAL_NATURE_VRT.replace("xOff=\"770000\"", "")
    with pytest.raises(ValueError, match="xOff"):
        nat.parse_vrt_tiles(_vrt_zip(tmp_path, bad), "NA/mosaic/WUI.vrt")


def test_parse_vrt_rejects_src_dst_size_mismatch(tmp_path):
    bad = _REAL_NATURE_VRT.replace(
        '<SrcRect xOff="0" yOff="0" xSize="10000" ySize="10000" />',
        '<SrcRect xOff="0" yOff="0" xSize="5000" ySize="10000" />',
    )
    with pytest.raises(ValueError, match="size mismatch"):
        nat.parse_vrt_tiles(_vrt_zip(tmp_path, bad), "NA/mosaic/WUI.vrt")


def test_parse_vrt_rejects_partial_source_window(tmp_path):
    bad = _REAL_NATURE_VRT.replace(
        '<SrcRect xOff="0" yOff="0" xSize="10000" ySize="10000" />',
        '<SrcRect xOff="10" yOff="0" xSize="10000" ySize="10000" />',
    )
    with pytest.raises(ValueError, match="source window"):
        nat.parse_vrt_tiles(_vrt_zip(tmp_path, bad), "NA/mosaic/WUI.vrt")


# ---------------------------------------------------------------------------
# compare_wui
# ---------------------------------------------------------------------------
def _silvis_2020():
    return pd.DataFrame({
        "GEOID": ["01001", "01002", "01003"],
        "county_name": ["A", "B", "C"],
        "state_fips": ["01"] * 3, "state_name": ["Alabama"] * 3,
        "county_area_km2": [1000.0, 500.0, 800.0],
        "wui_area_km2": [100.0, 0.0, 400.0],
        "pct_county_wui": [10.0, 0.0, 50.0],
        "interface_wui_area_km2": [25.0, 0.0, 100.0],
        "intermix_wui_area_km2": [75.0, 0.0, 300.0],
        "pct_of_wui_that_is_interface": [25.0, np.nan, 25.0],
        "hu_in_wui": [10.0, 0.0, 40.0],
    })


def _nature_2020():
    return pd.DataFrame({
        "GEOID": ["01001", "01002", "01003"],
        "nature_wui_area_km2": [120.0, 30.0, 400.0],
        "pct_county_nature_wui": [12.0, 6.0, 50.0],
        "nature_interface_wui_area_km2": [60.0, 15.0, 100.0],
        "nature_intermix_wui_area_km2": [60.0, 15.0, 300.0],
        "pct_of_nature_wui_that_is_interface": [50.0, 50.0, 25.0],
        "pct_county_covered_by_nature_raster": [100.0, 100.0, 100.0],
    })


def test_build_comparison_and_summary():
    comp = cmp_wui.build_comparison(_silvis_2020(), _nature_2020())
    assert len(comp) == 3
    a = comp[comp["GEOID"] == "01001"].iloc[0]
    assert a["wui_area_diff_km2"] == pytest.approx(20.0)
    assert a["wui_pct_point_diff"] == pytest.approx(2.0)
    assert a["wui_area_pct_diff"] == pytest.approx(20.0)
    # presence disagreement: SILVIS says B has no WUI, Nature says it does
    assert comp[comp["GEOID"] == "01002"]["wui_presence_disagreement"].iloc[0] is True \
        or bool(comp[comp["GEOID"] == "01002"]["wui_presence_disagreement"].iloc[0])
    summary = cmp_wui.summarize_comparison(comp)
    assert summary["conus_silvis_wui_km2"] == pytest.approx(500.0)
    assert summary["conus_nature_wui_km2"] == pytest.approx(550.0)
    assert 0.9 < summary["area_correlation_all_counties"]["pearson"] <= 1.0
    assert summary["methodological_note"]
    # join mismatch must raise, never silently drop counties
    with pytest.raises(ValueError):
        cmp_wui.build_comparison(_silvis_2020(), _nature_2020().head(2))


# ---------------------------------------------------------------------------
# places_wui
# ---------------------------------------------------------------------------
def _places():
    return gpd.GeoDataFrame(
        {
            "GEOID": ["0112345", "0167890"],
            "NAME": ["P1", "P2"], "NAMELSAD": ["P1 city", "P2 CDP"],
            "STATEFP": ["01", "01"], "PLACEFP": ["12345", "67890"],
            "LSAD": ["25", "57"], "FUNCSTAT": ["A", "A"],
            "place_area_km2": [100.0, 50.0],
        },
        # EPSG:5070 units are meters: 10 km x 10 km = 100 km2; 5 km x 10 km = 50 km2
        geometry=[box(0, 0, 10_000, 10_000), box(0, 20_000, 5_000, 30_000)],
        crs=CRS5070,
    )


def _fragments_geoparquet(tmp_path):
    frag = gpd.GeoDataFrame(
        {
            "BLK20": ["b1", "b2"],
            "GEOID": ["01001", "01001"],
            "fragment_area_km2": [25.0, 12.0],
            "wui_class_1990": ["Low_Dens_Intermix", None],
            "wui_class_2000": [None, None],
            "wui_class_2010": [None, "Low_Dens_Interface"],
            "wui_class_2020": ["Low_Dens_Interface", "Low_Dens_Intermix"],
        },
        # b1: 5 km x 5 km = 25 km2 inside P1; b2: 3 km x 4 km = 12 km2 inside P2
        geometry=[box(0, 0, 5_000, 5_000), box(0, 20_000, 3_000, 24_000)],
        crs=CRS5070,
    )
    out = tmp_path / "frag.geoparquet"
    frag.to_parquet(out, index=False)
    return out


def test_place_wui_by_vintage(tmp_path):
    places = _places()
    out = plw.build_place_wui_by_vintage(
        places, _fragments_geoparquet(tmp_path), _s2()
    )
    assert len(out) == 8  # 2 places x 4 vintages
    a = out[(out["GEOID"] == "0112345") & (out["wui_year"] == 1990)].iloc[0]
    # fragment b1 (25 km2) lies fully inside place P1 (100 km2 box)
    assert a["wui_area_km2"] == pytest.approx(25.0)
    assert a["pct_place_wui"] == pytest.approx(25.0)
    assert a["intermix_wui_area_km2"] == pytest.approx(25.0)
    # P1 has no WUI overlap in 2000 (b1 not WUI then, b2 elsewhere)
    a2000 = out[(out["GEOID"] == "0112345") & (out["wui_year"] == 2000)].iloc[0]
    assert a2000["wui_area_km2"] == 0.0


def test_place_exposure_prebaseline_and_hierarchy(tmp_path):
    places = _places()
    place_wui = pd.DataFrame({
        "GEOID": ["0112345"] * 4,
        "wui_year": [1990, 2000, 2010, 2020],
        "wui_area_km2": [25.0, 25.0, 25.0, 25.0],
    })
    fire_summary = pd.DataFrame({
        "GEOID": ["0112345", "0112345", "0167890"],
        "NAME": ["P1", "P1", "P2"],
        "NAMELSAD": ["P1 city", "P1 city", "P2 CDP"],
        "STATEFP": ["01", "01", "01"], "LSAD": ["25", "25", "57"],
        "FUNCSTAT": ["A", "A", "A"],
        "place_area_km2": [100.0, 100.0, 50.0],
        "year": [1986, 2021, 2021],
        "fire_count": [1, 1, 1],
        "burned_area_km2": [30.0, 20.0, 10.0],
    })
    pieces = _pieces()
    pieces_path = tmp_path / "pieces.geoparquet"
    pieces.to_parquet(pieces_path, index=False)
    out = plw.build_place_wui_fire_exposure(
        pieces_path, places, fire_summary, place_wui, _s2()
    )
    pre = out[out["year"] == 1986].iloc[0]
    assert pd.isna(pre["assigned_wui_vintage"])
    assert pre["assigned_wui_vintage_label"] == "pre_1990_no_wui_baseline"
    assert pd.isna(pre["wui_burned_area_km2"])
    assert pd.isna(pre["wui_area_km2"])
    y2021 = out[(out["GEOID"] == "0112345") & (out["year"] == 2021)].iloc[0]
    assert y2021["assigned_wui_vintage"] == 2020
    assert y2021["assigned_wui_vintage_label"] == "2020"
    # WUI burn (clipped to the place) can never exceed total place burn
    assert y2021["wui_burned_area_km2"] <= y2021["burned_area_km2"] + 1e-9


def test_place_wui_burn_share_weighted():
    cp = pd.DataFrame({
        "GEOID": ["a", "a", "b"],
        "period": ["1990-1999", "2020-2024", "2020-2024"],
        "unique_wui_burned_footprint_km2": [10.0, 20.0, 5.0],
        "wui_area_km2": [100.0, 200.0, 50.0],
    })
    share = maps2._wui_burn_share(cp)
    assert share.loc["a"] == pytest.approx(100 * 30.0 / 300.0)
    assert share.loc["b"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# validate_stage2 checks (synthetic frames)
# ---------------------------------------------------------------------------
def _county_wui_frame():
    df = _county_wui_table()
    df["pct_county_wui"] = df["wui_area_km2"] / df["county_area_km2"] * 100
    df["pct_wui_interface"] = df["interface_wui_area_km2"] / df["county_area_km2"] * 100
    df["pct_wui_intermix"] = df["intermix_wui_area_km2"] / df["county_area_km2"] * 100
    df["pct_of_wui_that_is_interface"] = np.where(
        df["wui_area_km2"] > 0, df["interface_wui_area_km2"] / df["wui_area_km2"] * 100, np.nan)
    df["pct_of_wui_that_is_intermix"] = np.where(
        df["wui_area_km2"] > 0, df["intermix_wui_area_km2"] / df["wui_area_km2"] * 100, np.nan)
    df["pct_county_hu_in_wui"] = np.where(
        df["wui_area_km2"] > 0, 50.0, np.nan)
    df["hu_total_county"] = 100.0
    return df


def test_check_wui_areas_and_composition():
    cw = _county_wui_frame()
    assert v2.check_wui_area_nonnegative(cw).status == PASS
    assert v2.check_wui_area_le_county(cw).status == PASS
    assert v2.check_wui_pct_bounds(cw, 100.0).status == PASS
    assert v2.check_wui_composition_consistency(cw).status == PASS
    assert v2.check_county_vintage_uniqueness(cw, _s2()).status == PASS
    bad = cw.copy()
    bad.loc[0, "wui_area_km2"] = bad.loc[0, "county_area_km2"] * 2
    assert v2.check_wui_area_le_county(bad).status == FAIL
    bad2 = cw.copy()
    bad2.loc[0, "wui_area_km2"] = -1.0
    assert v2.check_wui_area_nonnegative(bad2).status == FAIL
    bad3 = cw.copy()
    bad3 = pd.concat([bad3, bad3.head(1)], ignore_index=True)
    assert v2.check_county_vintage_uniqueness(bad3, _s2()).status == FAIL


def _county_year_wui_frame():
    df = pd.DataFrame({
        "GEOID": ["01001"] * 3,
        "year": [1986, 1995, 2021],
        "assigned_wui_vintage": pd.Series([pd.NA, 1990, 2020], dtype="Int64"),
        "assigned_wui_vintage_label": [
            "pre_1990_no_wui_baseline", "1990", "2020",
        ],
        "fire_count": [1, 1, 1],
        "burned_area_km2": [30.0, 50.0, 80.0],
        "wui_area_km2": [np.nan, 100.0, 150.0],
        "wui_burned_area_km2": [np.nan, 10.0, 25.0],
        "interface_wui_burned_km2": [np.nan, 10.0, 20.0],
        "intermix_wui_burned_km2": [np.nan, 0.0, 5.0],
    })
    df["pct_wui_burned_in_year"] = np.where(
        df["wui_area_km2"] > 0, df["wui_burned_area_km2"] / df["wui_area_km2"] * 100, np.nan)
    df["pct_burned_area_that_was_wui"] = np.where(
        df["burned_area_km2"] > 0, df["wui_burned_area_km2"] / df["burned_area_km2"] * 100, np.nan)
    return df


def test_check_temporal_alignment():
    cy = _county_year_wui_frame()
    s2 = _s2()
    assert v2.check_vintage_matches_year(cy, s2).status == PASS
    assert v2.check_no_2020_wui_on_earlier_years(cy).status == PASS
    assert v2.check_pre_baseline_nulls(cy, s2).status == PASS
    assert v2.check_vintage_wui_area_matches(
        cy, _county_wui()[["GEOID", "wui_year", "wui_area_km2"]]
    ).status == PASS
    # 2020 vintage on a 1995 fire year must FAIL loudly
    bad = cy.copy()
    bad.loc[bad["year"] == 1995, "assigned_wui_vintage"] = 2020
    bad.loc[bad["year"] == 1995, "assigned_wui_vintage_label"] = "2020"
    assert v2.check_no_2020_wui_on_earlier_years(bad).status == FAIL
    # A string "2020" in the vintage column is the same violation.
    bad_str = cy.copy()
    bad_str["assigned_wui_vintage"] = bad_str["assigned_wui_vintage"].map(
        lambda v: "" if pd.isna(v) else str(int(v))
    )
    bad_str.loc[bad_str["year"] == 1995, "assigned_wui_vintage"] = "2020"
    assert v2.check_no_2020_wui_on_earlier_years(bad_str).status == FAIL
    assert v2.check_vintage_matches_year(bad, s2).status == FAIL
    # pre-baseline rows with non-null exposure must FAIL
    bad2 = cy.copy()
    bad2.loc[bad2["year"] == 1986, "wui_burned_area_km2"] = 5.0
    assert v2.check_pre_baseline_nulls(bad2, s2).status == FAIL


def test_county_year_and_period_parquet_roundtrip(tmp_path):
    """Regression: Parquet rejected assigned_wui_vintage when 1984-1989 rows
    carried the pre-baseline label beside integer vintages.

    The built county-year table (pre-baseline year AND an aligned year) and
    the county-period table must each round-trip through Parquet with one
    dtype per vintage column.
    """
    s2 = _s2()
    county_year = bwf.build_county_year_wui_fire(
        _pieces(), _county_year_stage1(), _county_wui(), s2
    )
    year_path = tmp_path / "county_year_wui_fire.parquet"
    county_year.to_parquet(year_path, index=False)
    back = pd.read_parquet(year_path)
    by_year = back.set_index("year")
    assert back["assigned_wui_vintage"].dtype == "Int64"
    assert pd.isna(by_year.loc[1985, "assigned_wui_vintage"])
    assert by_year.loc[1985, "assigned_wui_vintage_label"] == "pre_1990_no_wui_baseline"
    assert int(by_year.loc[1995, "assigned_wui_vintage"]) == 1990
    assert by_year.loc[1995, "assigned_wui_vintage_label"] == "1990"
    assert int(by_year.loc[2021, "assigned_wui_vintage"]) == 2020
    assert by_year.loc[2021, "assigned_wui_vintage_label"] == "2020"
    assert pd.isna(by_year.loc[1985, "wui_burned_area_km2"])

    county_period = bwf.build_county_period_wui_fire(
        _pieces(), _county_year_stage1(), _county_wui(), s2,
        fire_county_intersections=_fire_county_intersections(),
    )
    period_path = tmp_path / "county_period_wui_fire.parquet"
    county_period.to_parquet(period_path, index=False)
    back_period = pd.read_parquet(period_path)
    assert back_period["assigned_wui_vintage"].isna().sum() == 0
    assert set(int(v) for v in back_period["assigned_wui_vintage"]) == {
        1990, 2000, 2010, 2020,
    }
    assert set(back_period["assigned_wui_vintage_label"]) == {
        "1990", "2000", "2010", "2020",
    }


def test_check_wui_burn_hierarchies():
    cy = _county_year_wui_frame()
    assert v2.check_wui_burn_le_burned(cy).status == PASS
    assert v2.check_wui_burn_le_wui_area(cy).status == PASS
    assert v2.check_wui_burn_composition(cy).status == PASS
    assert v2.check_county_year_uniqueness(cy).status == PASS
    bad = cy.copy()
    bad.loc[bad["year"] == 2021, "wui_burned_area_km2"] = 500.0
    assert v2.check_wui_burn_le_burned(bad).status == FAIL
    assert v2.check_wui_burn_le_wui_area(bad).status == FAIL
    # Total burn may exceed WUI area (non-WUI land burns). That is not a failure
    # as long as the WUI portion stays inside both the burn and the WUI area.
    non_wui = cy.copy()
    non_wui.loc[non_wui["year"] == 2021, "burned_area_km2"] = 500.0
    non_wui.loc[non_wui["year"] == 2021, "wui_burned_area_km2"] = 25.0
    assert non_wui.loc[non_wui["year"] == 2021, "burned_area_km2"].iloc[0] > \
        non_wui.loc[non_wui["year"] == 2021, "wui_area_km2"].iloc[0]
    assert v2.check_wui_burn_le_burned(non_wui).status == PASS
    assert v2.check_wui_burn_le_wui_area(non_wui).status == PASS


def test_check_period_table():
    s2 = _s2()
    cp = bwf.build_county_period_wui_fire(
        _pieces(), _county_year_stage1(), _county_wui(), s2,
        fire_county_intersections=_fire_county_intersections(),
    )
    assert v2.check_period_table(cp, s2).status == PASS
    bad = cp.copy()
    bad.loc[0, "unique_wui_burned_footprint_km2"] = 1e6
    assert v2.check_period_table(bad, s2).status == FAIL


def test_check_change_table():
    change = wch.build_change_table(_county_wui_table())
    assert v2.check_change_table(change, _s2()).status == PASS
    bad = change[change["transition"] != "1990->2020"]
    assert v2.check_change_table(bad, _s2()).status == FAIL


def test_check_nature():
    counties = _counties()
    info = {"pixel_area_km2": 0.0001}
    nat_df = nat.build_county_nature_table(
        {"01001": {1: 2, 2: 2, 3: 2, 4: 2, 5: 5}}, info, counties
    )
    assert v2.check_nature_areas(nat_df).status == PASS
    bad = nat_df.copy()
    bad.loc[0, "nature_wui_area_km2"] = bad.loc[0, "county_area_km2"] * 5
    assert v2.check_nature_areas(bad).status == FAIL


def test_check_comparison_separation():
    comp = cmp_wui.build_comparison(_silvis_2020(), _nature_2020())
    summary = cmp_wui.summarize_comparison(comp)
    assert v2.check_comparison_separation(comp, summary).status == PASS
    bad_summary = {k: v for k, v in summary.items() if k != "methodological_note"}
    assert v2.check_comparison_separation(comp, bad_summary).status == FAIL


def test_check_place_rules():
    pw_df = pd.DataFrame({
        "GEOID": ["0112345", "0112345", "01001"],
        "wui_year": [1990, 2020, 1990],
        "place_area_km2": [100.0, 100.0, 1000.0],
        "wui_area_km2": [25.0, 30.0, 50.0],
    })
    assert v2.check_place_keys(pw_df).status == FAIL  # 5-digit county GEOID leaked
    clean = pw_df[pw_df["GEOID"].str.len() == 7].reset_index(drop=True)
    assert v2.check_place_keys(clean).status == PASS
    assert v2.check_place_wui_bounds(clean).status == PASS
    assert v2.check_place_county_separation(clean).status == PASS
    over = clean.copy()
    over.loc[0, "wui_area_km2"] = 500.0
    assert v2.check_place_wui_bounds(over).status == FAIL


def test_check_spatial_crs(tmp_path):
    g = gpd.GeoDataFrame({"a": [1]}, geometry=[box(0, 0, 1, 1)], crs=CRS5070)
    p = tmp_path / "g.geoparquet"
    g.to_parquet(p)
    ok = v2.check_spatial_crs({"g": (p, 5070)})
    assert ok.status == PASS
    bad = v2.check_spatial_crs({"g": (p, 4269)})
    assert bad.status == FAIL
    missing = v2.check_spatial_crs({"g": (tmp_path / "nope.geoparquet", 5070)})
    assert missing.status == FAIL


def test_check_silvis_class_domain():
    s2 = _s2()
    s2["silvis"]["class_domain"] = ["Water", "Low_Dens_Intermix", "Low_Dens_Interface"]
    good = {
        "flag_class_conflict_rows": 0,
        "fips_blk20_mismatch_rows": 0,
        "observed_domains": {
            "1990": {"flag": {"0": 5, "1": 2, "2": 1},
                     "class": {"Water": 5, "Low_Dens_Intermix": 2,
                               "Low_Dens_Interface": 1}},
        },
    }
    assert v2.check_silvis_class_domain(good, s2).status == PASS
    bad = json.loads(json.dumps(good))
    bad["observed_domains"]["1990"]["class"]["Mystery_Class"] = 3
    assert v2.check_silvis_class_domain(bad, s2).status == FAIL


# ---------------------------------------------------------------------------
# downloader filename derivation (regression: Zenodo /content URLs)
# ---------------------------------------------------------------------------
def test_file_specs_use_documented_filename():
    meta = {
        "url": "https://zenodo.org/api/records/7941460/files/NA.zip/content",
        "documented_file_name": "NA.zip",
        "expected_archive_member_suffix": ".tif",
        "auto_extract": False,
        "additional_files": [{
            "url": "https://zenodo.org/api/records/7941460/files/tabular.zip/content",
            "documented_file_name": "tabular.zip",
            "expected_archive_member_suffix": ".csv",
            "auto_extract": False,
        }],
    }
    specs = dl.file_specs_from_meta(meta)
    assert specs[0]["filename"] == "NA.zip"
    assert specs[1]["filename"] == "tabular.zip"


def test_equi7_scale_is_below_one_in_virginia_and_closer_to_one_farther_north():
    """EQUI7 NA (EPSG:27705) is azimuthal equidistant, not equal-area.
    A nominal 100 m² pixel overstates ground area in Virginia."""
    import area_denominators as ad

    va = gpd.GeoDataFrame(
        geometry=[box(-78.6, 38.0, -78.4, 38.2)], crs=4326
    ).to_crs(5070)
    nd = gpd.GeoDataFrame(
        geometry=[box(-100.2, 47.0, -100.0, 47.2)], crs=4326
    ).to_crs(5070)
    va_scale = float(ad.equi7_ground_over_nominal(va.geometry).iloc[0])
    nd_scale = float(ad.equi7_ground_over_nominal(nd.geometry).iloc[0])
    assert va_scale < 0.99
    assert nd_scale < 1.0
    assert nd_scale > va_scale


def test_silvis_land_and_total_denominators_are_both_kept():
    import area_denominators as ad

    county_wui = pd.DataFrame({
        "GEOID": ["01001"],
        "wui_area_km2": [50.0],
        "pct_county_wui": [5.0],  # 50 / 1000 legal polygon
    })
    denom = pd.DataFrame({
        "GEOID": ["01001"],
        "county_land_area_km2": [800.0],
        "county_water_area_km2": [200.0],
        "equi7_ground_over_nominal": [0.98],
    })
    out = ad.attach_silvis_denominators(county_wui, denom)
    assert out["pct_total_county_area_wui"].iloc[0] == pytest.approx(5.0)
    assert out["pct_county_land_area_wui"].iloc[0] == pytest.approx(50.0 / 800.0 * 100.0)
    assert out["pct_county_wui"].iloc[0] == pytest.approx(5.0)


def test_file_specs_url_pattern_filenames():
    meta = {
        "url_pattern": "https://www2.census.gov/geo/tiger/TIGER2025/PLACE/tl_2025_{state_fips}_place.zip",
        "state_fips_source": "config/analysis.yaml scope.conus_state_fips",
        "expected_archive_member_suffix": ".shp",
        "auto_extract": False,
    }
    specs = dl.file_specs_from_meta(meta)
    assert len(specs) == 49  # 48 CONUS states + DC
    assert specs[0]["url"].endswith("tl_2025_01_place.zip")
    assert specs[0]["filename"] is None  # derived from URL by acquire_one_file


def test_check_nature_class_domain_treats_vrt_nodata_255_as_ok():
    s2 = _s2()
    good = {"observed_class_values": [1, 2, 8], "nodata_value": 255}
    assert v2.check_nature_class_domain(good, s2).status == PASS
    with_nodata_listed = {"observed_class_values": [1, 255], "nodata_value": 255}
    assert v2.check_nature_class_domain(with_nodata_listed, s2).status == PASS
    bad = {"observed_class_values": [1, 99], "nodata_value": 255}
    assert v2.check_nature_class_domain(bad, s2).status == FAIL


def test_stored_path_is_repo_or_home_relative():
    """Provenance/schema records must not embed /Users/<username>/ paths."""
    inside = common.PROJECT_ROOT / "config" / "analysis.yaml"
    stored = common.stored_path(inside)
    assert not stored.startswith("/Users/")
    assert stored == "config/analysis.yaml"
    assert common.resolve_stored_path(stored) == inside.resolve()

    home_file = Path.home() / ".cache" / "wildfire-wui-capstone-test-marker"
    stored_home = common.stored_path(home_file)
    assert stored_home.startswith("~/")
    assert "/Users/" not in stored_home
    assert common.resolve_stored_path(stored_home) == home_file.resolve()

    abs_legacy = str(inside.resolve())
    assert common.resolve_stored_path(abs_legacy) == inside.resolve()


def test_cache_root_uses_home_not_hardcoded_username():
    root = common.cache_root()
    assert root == (Path.home() / ".cache" / "wildfire-wui-capstone").resolve()
    previous = os.environ.get(common.CACHE_ENV)
    os.environ[common.CACHE_ENV] = "~/alt-wui-cache"
    try:
        assert common.cache_root() == (Path.home() / "alt-wui-cache").resolve()
    finally:
        if previous is None:
            os.environ.pop(common.CACHE_ENV, None)
        else:
            os.environ[common.CACHE_ENV] = previous


def test_source_contains_no_machine_specific_absolute_paths():
    root = Path(__file__).resolve().parents[1]
    forbidden = re.compile(r"/Users/[A-Za-z0-9._-]+/")
    hits = []
    for folder in ("src", "tests", "config"):
        for p in (root / folder).rglob("*"):
            if p.suffix not in {".py", ".yaml", ".yml"}:
                continue
            text = p.read_text(encoding="utf-8")
            if forbidden.search(text):
                hits.append(str(p.relative_to(root)))
    assert hits == [], f"machine-specific /Users/ paths in {hits}"