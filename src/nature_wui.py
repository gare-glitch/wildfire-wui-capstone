"""Stage 2B — Schug et al. (2023) Nature global WUI raster, CONUS counties.

Processes the "global wildland-urban interface" product (Schug et al. 2023,
Nature, DOI 10.1038/s41586-023-06320-0; data Zenodo DOI 10.5281/zenodo.7941460,
version 1.1) as a MODERN ~2020 COMPARISON / SENSITIVITY layer only.

This product is NEVER applied to historic fire years: it represents
approximately 2020 conditions and exists in this pipeline solely to be
compared against the SILVIS 2020 snapshot (see src/compare_wui.py).

Method (windowed/chunked, never a full-RAM load)
-------------------------------------------------
The 4.07 GB NA.zip holds 3,157 EQUI7 tiles (100 km x 100 km, 10 m, uint8,
8 discrete classes) plus a GDAL VRT mosaic. Rather than opening 3,157
rasters, tile extents are parsed directly from the VRT XML. Only tiles
intersecting the CONUS county layer (reprojected once to the raster's
EQUI7 equal-area CRS) are processed, each exactly ONCE:

  for each CONUS-intersecting tile:
      read the tile (one ~100 MB windowed read through /vsizip)
      rasterize the overlapping county polygons to local ids
      np.bincount county-id x class -> accumulate

Class areas per county = pixel counts × the nominal 10 m × 10 m cell
(100 m² on the EQUI7 map). EQUI7 North America is an azimuthal equidistant
projection (EPSG:27705), not equal-area, so that nominal cell is slightly
larger than true ground area away from the projection center. Stored
`nature_*_area_km2` columns keep the nominal pixel areas. Ground-corrected
companions are `nature_*_ground_km2` (nominal × county polygon area in
EPSG:5070 ÷ the same polygon's area in EPSG:27705).

WUI classes (verified against the dataset's class scheme):
    1 forest/shrub/wetland intermix | 2 forest/shrub/wetland interface
    3 grassland intermix            | 4 grassland interface
    5-8 non-WUI classes.

Outputs
-------
data/processed/county_nature_wui_2020.parquet
outputs/tables/county_nature_wui_2020.csv
outputs/tables/schema_nature_raw.{json,txt}   (observed class domain etc.)
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# PROJ environment guard: rasterio honors PROJ_LIB/PROJ_DATA, and foreign
# PROJ installations on the machine (e.g. an Anaconda tree) ship an
# incompatible proj.db ("DATABASE.LAYOUT.VERSION.MINOR" too old). The wheels
# in this venv bundle their own PROJ data; drop any env override BEFORE
# importing rasterio so the bundled database is used. pyproj is unaffected
# (it prefers its bundled datadir) but dropping is harmless there too.
for _var in ("PROJ_LIB", "PROJ_DATA", "PROJ_NETWORK"):
    os.environ.pop(_var, None)

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    TABLES_DIR,
    ensure_dirs,
    get_logger,
    load_stage2_config,
)

log = get_logger("nature_wui")

NA_ZIP = RAW_DIR / "nature_wui" / "NA.zip"
COUNTY_OUT = PROCESSED_DIR / "county_nature_wui_2020.parquet"
COUNTY_CSV = TABLES_DIR / "county_nature_wui_2020.csv"
SCHEMA_JSON = TABLES_DIR / "schema_nature_raw.json"
SCHEMA_TXT = TABLES_DIR / "schema_nature_raw.txt"
CHECKPOINT = INTERIM_DIR / "nature_county_class_pixels.json"

KM2_PER_M2 = 1e-6

# Class identities (config/analysis.yaml stage2.nature.wui_class_map).
INTERMIX_CLASSES = (1, 3)
INTERFACE_CLASSES = (2, 4)
WUI_CLASSES = (1, 2, 3, 4)
VALID_CLASSES = (1, 2, 3, 4, 5, 6, 7, 8)


# ---------------------------------------------------------------------------
# VRT parsing (avoids opening 3,157 rasters)
# ---------------------------------------------------------------------------
# Inspected attributes on the Schug 2023 NA mosaic (NA/mosaic/WUI.vrt):
#   VRTDataset:            rasterXSize, rasterYSize   (NOT sizeX/sizeY)
#   GeoTransform:          minx, pixw, 0, maxy, 0, -pixh
#   VRTRasterBand:         dataType, band; NoDataValue; ColorInterp
#   ComplexSource:
#     SourceFilename       relativeToVRT="1"  text="../X####_Y####/WUI.tif"
#     SourceBand           1
#     SourceProperties     RasterXSize, RasterYSize, DataType, BlockXSize, BlockYSize
#     SrcRect              xOff, yOff, xSize, ySize
#     DstRect              xOff, yOff, xSize, ySize
#     NODATA               255
# Missing required attributes must raise — never int(None).
_RECT_ATTRS = ("xOff", "yOff", "xSize", "ySize")
_SOURCE_PROP_SIZE_ATTRS = ("RasterXSize", "RasterYSize")


def _require_attr(elem: ET.Element, name: str) -> str:
    """Return a required XML attribute; fail loudly if absent or empty."""
    if elem is None:
        raise ValueError(f"VRT is missing the element that should carry {name!r}")
    val = elem.get(name)
    if val is None or str(val).strip() == "":
        raise ValueError(
            f"<{elem.tag}> is missing required GDAL attribute {name!r}. "
            f"Present attributes: {sorted(elem.attrib)}. "
            "The Nature NA mosaic uses rasterXSize/rasterYSize on VRTDataset "
            "and xOff/yOff/xSize/ySize on SrcRect/DstRect (not sizeX/sizeY)."
        )
    return val


def _require_int_attr(elem: ET.Element, name: str) -> int:
    return int(_require_attr(elem, name))


def _require_child(parent: ET.Element, tag: str) -> ET.Element:
    child = parent.find(tag)
    if child is None:
        raise ValueError(f"<{parent.tag}> is missing required child <{tag}>")
    return child


def parse_vrt_tiles(zip_path: Path, vrt_member: str) -> tuple[dict, list[dict]]:
    """Extract the mosaic geotransform and per-tile bounds from the VRT XML.

    Returns (info_dict, tiles) where each tile dict has
    {member, xoff, yoff, nx, ny} in mosaic pixel coordinates. Tile member
    paths inside the VRT are relative to the VRT directory.
    """
    with zipfile.ZipFile(zip_path) as zf:
        vrt_bytes = zf.read(vrt_member)
    root = ET.fromstring(vrt_bytes)

    # This product's VRTDataset does not carry sizeX/sizeY. Refuse that
    # wrong-name fallback so a missing rasterXSize cannot silently become 0.
    if root.get("rasterXSize") is None or root.get("rasterYSize") is None:
        raise ValueError(
            "VRTDataset is missing rasterXSize/rasterYSize "
            f"(present attributes: {sorted(root.attrib)}). "
            "sizeX/sizeY are not valid GDAL VRT mosaic dimension attributes."
        )
    raster_xsize = _require_int_attr(root, "rasterXSize")
    raster_ysize = _require_int_attr(root, "rasterYSize")

    gt_el = root.find("GeoTransform")
    if gt_el is None or not (gt_el.text or "").strip():
        raise ValueError("VRTDataset is missing <GeoTransform>")
    gt = [float(v) for v in gt_el.text.split(",")]
    if len(gt) != 6:
        raise ValueError(f"GeoTransform must have 6 values; got {gt}")
    vrt_dir = vrt_member.rsplit("/", 1)[0]

    bands = root.findall("VRTRasterBand")
    if not bands:
        raise ValueError("VRTDataset has no VRTRasterBand")
    band = bands[0]
    nodata_el = band.find("NoDataValue")
    nodata_value = (
        int(float(nodata_el.text))
        if nodata_el is not None and (nodata_el.text or "").strip()
        else None
    )

    tiles: list[dict] = []
    for src in band.findall("ComplexSource") + band.findall("SimpleSource"):
        fn_el = _require_child(src, "SourceFilename")
        fname = (fn_el.text or "").strip()
        if not fname:
            raise ValueError("ComplexSource SourceFilename is empty")
        rel = fn_el.get("relativeToVRT") == "1"
        dst = _require_child(src, "DstRect")
        sr = _require_child(src, "SrcRect")
        props = _require_child(src, "SourceProperties")
        for attr in _RECT_ATTRS:
            _require_attr(dst, attr)
            _require_attr(sr, attr)
        for attr in _SOURCE_PROP_SIZE_ATTRS:
            _require_attr(props, attr)

        src_xoff = _require_int_attr(sr, "xOff")
        src_yoff = _require_int_attr(sr, "yOff")
        src_nx = _require_int_attr(sr, "xSize")
        src_ny = _require_int_attr(sr, "ySize")
        dst_nx = _require_int_attr(dst, "xSize")
        dst_ny = _require_int_attr(dst, "ySize")
        prop_nx = _require_int_attr(props, "RasterXSize")
        prop_ny = _require_int_attr(props, "RasterYSize")
        if src_xoff != 0 or src_yoff != 0:
            raise ValueError(
                f"Tile {fname} SrcRect origin is ({src_xoff},{src_yoff}); "
                "this pipeline reads each GeoTIFF in full and cannot apply "
                "a partial source window."
            )
        if (src_nx, src_ny) != (dst_nx, dst_ny) or (src_nx, src_ny) != (prop_nx, prop_ny):
            raise ValueError(
                f"Tile {fname} size mismatch: SrcRect {src_nx}x{src_ny}, "
                f"DstRect {dst_nx}x{dst_ny}, SourceProperties {prop_nx}x{prop_ny}"
            )
        source_nodata = src.findtext("NODATA")
        if (
            nodata_value is not None
            and source_nodata is not None
            and int(float(source_nodata)) != nodata_value
        ):
            raise ValueError(
                f"Tile {fname} NODATA {source_nodata} does not match band "
                f"NoDataValue {nodata_value}"
            )
        # VRT paths are relative to the mosaic directory (../X####_Y####/WUI.tif).
        # Zip members do not contain "..", so normalize before /vsizip open.
        member = f"{vrt_dir}/{fname}" if rel else fname
        tiles.append({
            "member": posixpath.normpath(member),
            "xoff": _require_int_attr(dst, "xOff"),
            "yoff": _require_int_attr(dst, "yOff"),
            "nx": dst_nx,
            "ny": dst_ny,
        })
    info = {
        "geotransform": gt,
        "raster_xsize": raster_xsize,
        "raster_ysize": raster_ysize,
        "n_tiles": len(tiles),
        "vrt_member": vrt_member,
        "nodata_value": nodata_value,
        "band_datatype": band.get("dataType"),
    }
    return info, tiles


def tile_bounds(gt: list[float], tile: dict) -> tuple[float, float, float, float]:
    """Geographic-projected bounds of a tile from the mosaic geotransform."""
    minx = gt[0] + tile["xoff"] * gt[1]
    maxy = gt[3] + tile["yoff"] * gt[5]
    maxx = minx + tile["nx"] * gt[1]
    miny = maxy + tile["ny"] * gt[5]
    return minx, miny, maxx, maxy


# ---------------------------------------------------------------------------
# Core per-tile zonal bincount
# ---------------------------------------------------------------------------
def process_tiles(
    zip_path: Path,
    tiles: list[dict],
    gt: list[float],
    counties_eq: gpd.GeoDataFrame,
    s2: dict,
    nodata_value: int | None = None,
) -> tuple[pd.DataFrame, dict]:
    """One pass over CONUS-intersecting tiles accumulating county x class pixels.

    counties_eq must be the CONUS counties projected to the raster's EQUI7
    CRS with columns GEOID and geometry, plus county_name etc.
    """
    pixel_w, pixel_h = abs(gt[1]), abs(gt[5])
    if abs(pixel_w - 10.0) > 1e-6 or abs(pixel_h - 10.0) > 1e-6:
        raise ValueError(
            f"Expected 10 m pixels per the product documentation; got "
            f"{pixel_w} x {pixel_h} m"
        )
    pixel_area_km2 = pixel_w * pixel_h * KM2_PER_M2  # 10x10 m = 0.0001 km2

    # Load checkpoint if present (resumable long run).
    accum: dict[str, dict[int, int]] = {}
    done_tiles: set[str] = set()
    if CHECKPOINT.exists():
        with open(CHECKPOINT, encoding="utf-8") as fh:
            state = json.load(fh)
        accum = {g: {int(c): n for c, n in d.items()}
                 for g, d in state["accum"].items()}
        done_tiles = set(state["done_tiles"])
        log.info("Resuming from checkpoint: %d tiles already processed.",
                 len(done_tiles))

    valid_values = set(s2["nature"]["valid_values"])
    observed_values: set[int] = set()
    crs_wkts: set[str] = set()
    vsizip = f"/vsizip/{zip_path}"

    t0 = time.time()
    n_processed = 0
    n_skipped = 0
    for tile in tiles:
        if tile["member"] in done_tiles:
            n_skipped += 1
            continue
        bminx, bminy, bmaxx, bmaxy = tile_bounds(gt, tile)

        # Counties overlapping this tile (bbox prefilter, cheap).
        sub = counties_eq[
            (counties_eq.bounds["maxx"] >= bminx)
            & (counties_eq.bounds["minx"] <= bmaxx)
            & (counties_eq.bounds["miny"] <= bmaxy)
            & (counties_eq.bounds["maxy"] >= bminy)
        ]
        if len(sub) == 0:
            done_tiles.add(tile["member"])
            continue

        with rasterio.open(f"{vsizip}/{tile['member']}") as src:
            crs_wkts.add(str(src.crs))
            if abs(src.transform.a - pixel_w) > 1e-9 or abs(src.transform.e + pixel_h) > 1e-9:
                raise ValueError(
                    f"Tile {tile['member']} transform does not match the VRT "
                    f"mosaic geotransform"
                )
            tile_transform = src.transform
            cls = src.read(1)

        ny, nx = cls.shape
        # Local ids 1..n over this tile's grid (0 = no county).
        shapes = [
            (geom, i + 1)
            for i, geom in enumerate(sub.geometry.values)
        ]
        ids = rasterize(
            shapes,
            out_shape=(ny, nx),
            transform=tile_transform,
            fill=0,
            dtype="uint16",
            all_touched=False,
        )
        del shapes

        # Observed domain check on covered pixels only (0 pixels are outside
        # the county mask and may legitimately be nodata/ocean).
        covered = ids > 0
        cls_covered = cls[covered]
        obs = [int(v) for v in np.unique(cls_covered)]
        # 0 is unused in this product; band NoDataValue is 255 on the NA VRT.
        skip_values = {0} if nodata_value is None else {0, int(nodata_value)}
        observed_values.update(v for v in obs if v not in skip_values)
        bad = [v for v in obs if v not in valid_values and v not in skip_values]
        if bad:
            raise ValueError(
                f"Tile {tile['member']} contains class values {bad} outside "
                "the documented 1-8 domain (excluding nodata 0)."
            )

        # bincount over (county_local_id, class)
        flat = (ids[covered].astype("int64") - 1) * 16 + cls_covered.astype("int64")
        counts = np.bincount(flat, minlength=16 * len(sub))
        geoids = sub["GEOID"].to_numpy()
        for i in range(len(sub)):
            gid = geoids[i]
            d = accum.setdefault(gid, {})
            row = counts[i * 16:(i + 1) * 16]
            for c in range(16):
                if row[c]:
                    d[c] = d.get(c, 0) + int(row[c])

        done_tiles.add(tile["member"])
        n_processed += 1
        if n_processed % 25 == 0:
            with open(CHECKPOINT, "w", encoding="utf-8") as fh:
                json.dump({"accum": accum, "done_tiles": sorted(done_tiles)}, fh)
            log.info(
                "  tiles processed: %d done, %d skipped (%.0f s elapsed)",
                n_processed, n_skipped, time.time() - t0,
            )
        del cls, ids, covered, cls_covered, flat, counts

    if len(crs_wkts) > 1:
        raise ValueError(f"All tiles must share one CRS; saw {crs_wkts}")
    info = {
        "pixel_area_km2": pixel_area_km2,
        "n_tiles_processed": n_processed,
        "n_tiles_total": len(tiles),
        "observed_class_values": sorted(observed_values),
        "tile_crs_wkt": (list(crs_wkts) or [None])[0],
    }
    return accum, info


# ---------------------------------------------------------------------------
# County table assembly
# ---------------------------------------------------------------------------
def build_county_nature_table(
    accum: dict[str, dict[int, int]],
    info: dict,
    counties: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """County-level Nature WUI metrics (one row per CONUS county)."""
    pixel_area_km2 = info["pixel_area_km2"]
    rows = []
    for _, c in counties.iterrows():
        geoid = c["GEOID"]
        d = accum.get(geoid, {})
        def area(cls_: int) -> float:
            return d.get(cls_, 0) * pixel_area_km2
        cls_areas = {v: area(v) for v in range(16)}
        wui = sum(cls_areas[v] for v in WUI_CLASSES)
        intermix = sum(cls_areas[v] for v in INTERMIX_CLASSES)
        interface = sum(cls_areas[v] for v in INTERFACE_CLASSES)
        classified = sum(cls_areas[v] for v in VALID_CLASSES)
        rows.append({
            "GEOID": geoid,
            "county_name": c["county_name"],
            "state_fips": c["state_fips"],
            "state_name": c["state_name"],
            "county_area_km2": c["county_area_km2"],
            "nature_wui_area_km2": wui,
            "nature_intermix_wui_area_km2": intermix,
            "nature_interface_wui_area_km2": interface,
            "nature_class1_forest_intermix_km2": cls_areas[1],
            "nature_class2_forest_interface_km2": cls_areas[2],
            "nature_class3_grass_intermix_km2": cls_areas[3],
            "nature_class4_grass_interface_km2": cls_areas[4],
            "nature_class5_nonwui_forest_km2": cls_areas[5],
            "nature_class6_nonwui_grass_km2": cls_areas[6],
            "nature_class7_nonwui_urban_km2": cls_areas[7],
            "nature_class8_nonwui_other_km2": cls_areas[8],
            "nature_classified_area_km2": classified,
        })
    df = pd.DataFrame(rows)
    df["pct_county_nature_wui"] = (
        df["nature_wui_area_km2"] / df["county_area_km2"] * 100.0
    )
    df["pct_of_nature_wui_that_is_interface"] = np.where(
        df["nature_wui_area_km2"] > 0,
        df["nature_interface_wui_area_km2"] / df["nature_wui_area_km2"] * 100.0,
        np.nan,
    )
    df["pct_of_nature_wui_that_is_intermix"] = np.where(
        df["nature_wui_area_km2"] > 0,
        df["nature_intermix_wui_area_km2"] / df["nature_wui_area_km2"] * 100.0,
        np.nan,
    )
    # Coverage diagnostic: classified raster area vs county polygon area.
    df["pct_county_covered_by_nature_raster"] = (
        df["nature_classified_area_km2"] / df["county_area_km2"] * 100.0
    )
    if "geometry" in getattr(counties, "columns", []) and "ALAND" in counties.columns:
        from area_denominators import (  # local import: CRS scale, not the raster pass
            attach_nature_denominators,
            county_denominator_frame,
        )
        df = attach_nature_denominators(df, county_denominator_frame(counties))
    return df.sort_values("GEOID").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    s2 = load_stage2_config()
    if not NA_ZIP.exists():
        raise FileNotFoundError(
            f"{NA_ZIP} missing. Run `python src/download_data.py --only nature_wui`."
        )

    info, tiles = parse_vrt_tiles(NA_ZIP, "NA/mosaic/WUI.vrt")
    log.info(
        "Parsed VRT: mosaic %sx%s px, %d tiles, pixel %.0f m.",
        info["raster_xsize"], info["raster_ysize"], info["n_tiles"],
        info["geotransform"][1],
    )

    # Raster CRS from the datacube definition; counties must be reprojected
    # to it once for the rasterize/bincount math (equal-area LAEA grid).
    with zipfile.ZipFile(NA_ZIP) as zf:
        prj = zf.read("NA/datacube-definition.prj").decode("utf-8")
    import rasterio.crs
    raster_crs = rasterio.crs.CRS.from_wkt(prj)
    log.info("Raster CRS (EQUI7 datacube definition): %s", raster_crs.to_string())

    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    counties_eq = counties.to_crs(raster_crs)

    # Keep only tiles intersecting the CONUS counties bbox in raster CRS.
    cb = counties_eq.total_bounds
    conus_tiles = []
    for t in tiles:
        b = tile_bounds(info["geotransform"], t)
        if b[0] <= cb[2] and b[2] >= cb[0] and b[1] <= cb[3] and b[3] >= cb[1]:
            conus_tiles.append(t)
    log.info(
        "%d of %d tiles intersect the CONUS county extent; processing those.",
        len(conus_tiles), len(tiles),
    )

    t0 = time.time()
    accum, proc_info = process_tiles(
        NA_ZIP, conus_tiles, info["geotransform"], counties_eq, s2,
        nodata_value=info.get("nodata_value"),
    )
    log.info(
        "Processed %d tiles in %.0f s; observed class values: %s",
        proc_info["n_tiles_processed"], time.time() - t0,
        proc_info["observed_class_values"],
    )

    with open(SCHEMA_JSON, "w", encoding="utf-8") as fh:
        json.dump({**info, **proc_info, "raster_crs_wkt": prj}, fh, indent=2)
    with open(SCHEMA_TXT, "w", encoding="utf-8") as fh:
        fh.write("Schug et al. 2023 Nature global WUI (NA tiles) — observed schema\n")
        fh.write("=" * 60 + "\n")
        fh.write(f"mosaic:            {info['raster_xsize']}x{info['raster_ysize']} px @ 10 m\n")
        fh.write(f"tiles (total):     {info['n_tiles']}\n")
        fh.write(f"tiles (processed): {proc_info['n_tiles_processed']}\n")
        fh.write(f"pixel area:        {proc_info['pixel_area_km2']} km2\n")
        fh.write(f"observed classes:  {proc_info['observed_class_values']}\n")
        fh.write(f"CRS (WKT):         {prj[:200]}\n")

    table = build_county_nature_table(accum, proc_info, counties)
    table.to_parquet(COUNTY_OUT, index=False)
    table.to_csv(COUNTY_CSV, index=False)
    log.info(
        "Wrote %d county rows. CONUS Nature WUI area: %.0f km2 "
        "(mean county pct %.2f%%, max %.1f%%).",
        len(table), table["nature_wui_area_km2"].sum(),
        table["pct_county_nature_wui"].mean(),
        table["pct_county_nature_wui"].max(),
    )
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("nature_wui.log")
    sys.exit(main())