"""Stage 2A — SILVIS U.S. WUI change data (1990/2000/2010/2020) preprocessing.

Builds the temporal WUI exposure layer from the authoritative SILVIS/UW
"U.S. WUI Change" dataset (CONUS_WUI_block_1990_2020_change_v4, FileGDB).

What this module does
---------------------
1. Schema-first inspection (AGENTS.md rule 2): reads the actual GDB layer
   metadata, field names/dtypes, CRS, feature count, and the *observed* value
   domains of WUIFLAG{year} / WUICLASS{year}, then verifies them against the
   domains documented in config/analysis.yaml (which mirror the FGDC
   metadata shipped with the archive). Writes a schema report to
   outputs/tables/schema_silvis_raw.{json,txt}. Fails loudly on
   disagreement — processing never proceeds on an unverified schema.

2. Streams the ~8M census-block polygons in RAM-bounded chunks:
   - keeps blocks that are WUI in ANY vintage (WUIFLAG{year} != 0),
   - projects to EPSG:5070 (area math never happens in geographic coords),
   - computes per-polygon fragment area,
   - assigns each block to its county via the dataset's own FIPS field
     (Census blocks nest within counties by construction; verified against
     BLK20's leading state+county digits),
   - writes ONE GeoParquet fragment table
     data/processed/wui/silvis_wui_fragments.geoparquet with per-vintage
     WUI class columns (null when the block is not WUI that year).

3. Aggregates fragments to county-level WUI metrics per vintage
   (data/processed/county_wui_by_vintage.parquet +
    outputs/tables/county_wui_by_vintage.csv), one row per GEOID x vintage,
   including area-allocation of housing units when a block is represented
   by multiple polygon fragments.

Disk / performance notes
------------------------
The raw 4.3 GB zip is NOT extracted permanently. GDAL /vsizip/ random access
to this FileGDB proved unusably slow (measured), so the pipeline extracts
the GDB to data/interim/silvis_gdb/ (needs ~7.3 GB free), processes it, and
the interim extraction is deleted afterwards (it is regenerable from the
immutable raw zip). All heavy reads are chunked; runtimes are logged.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
import shapely
from pyproj import CRS

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    TABLES_DIR,
    WUI_DIR,
    ensure_dirs,
    get_logger,
    load_analysis_config,
    load_stage2_config,
    resolve_stored_path,
    stored_path,
)

log = get_logger("preprocess_wui")

KM2_PER_M2 = 1e-6
CONUS_EQUAL_AREA_EPSG = 5070

RAW_ZIP = RAW_DIR / "silvis_wui" / "CONUS_WUI_block_1990_2020_change_v4_gcs_na83_GDB.zip"
GDB_DIR_NAME = "CONUS_WUI_block_1990_2020_change_v4_gcs_na83.gdb"
EXTRACT_DIR = INTERIM_DIR / "silvis_gdb"
FRAGMENTS_PATH = WUI_DIR / "silvis_wui_fragments.geoparquet"
COUNTY_WUI_PATH = PROCESSED_DIR / "county_wui_by_vintage.parquet"
COUNTY_WUI_CSV = TABLES_DIR / "county_wui_by_vintage.csv"
SCHEMA_JSON = TABLES_DIR / "schema_silvis_raw.json"
SCHEMA_TXT = TABLES_DIR / "schema_silvis_raw.txt"

ATTRIBUTE_CHUNK = 1_000_000   # attribute-only pass (no geometry)
GEOMETRY_CHUNK = 250_000      # geometric pass (RAM-bounded)


# ---------------------------------------------------------------------------
# Raw archive -> interim extraction (regenerable, deleted after use)
# ---------------------------------------------------------------------------
def ensure_gdb_extracted() -> Path:
    """Extract the FileGDB from the raw zip if needed; return its path.

    /vsizip/ was measured as unusably slow for this FileGDB (the driver
    re-seeks/decompresses constantly against a 4.3 GB zip), so processing
    works from a temporary local extraction. Requires ~7.3 GB free disk;
    the caller is expected to delete EXTRACT_DIR when done (see
    cleanup_extraction()).

    An existing extraction is only reused after verifying EVERY archive
    member is present with the exact expected size — a partial extraction
    (e.g. from a killed run) is deleted and redone rather than trusted.
    """
    gdb_path = EXTRACT_DIR / GDB_DIR_NAME
    if gdb_path.exists():
        if _extraction_complete(gdb_path):
            log.info("Reusing existing GDB extraction (verified complete): %s",
                     gdb_path)
            return gdb_path
        log.warning(
            "Existing extraction is incomplete/corrupt; deleting and "
            "re-extracting: %s", gdb_path
        )
        shutil.rmtree(gdb_path)
    if not RAW_ZIP.exists():
        raise FileNotFoundError(
            f"Raw SILVIS archive not found at {RAW_ZIP}. "
            "Run `python src/download_data.py --only silvis_wui` first."
        )
    free_gb = shutil.disk_usage(EXTRACT_DIR.anchor).free / 1e9
    if free_gb < 9.0:
        raise RuntimeError(
            f"Need ~7.3 GB free to extract the SILVIS GDB; only {free_gb:.1f} GB free. "
            "Free disk space and re-run."
        )
    log.info("Extracting %s -> %s (this takes a few minutes)...", RAW_ZIP.name, EXTRACT_DIR)
    t0 = time.time()
    with zipfile.ZipFile(RAW_ZIP) as zf:
        zf.extractall(EXTRACT_DIR)
    log.info("Extraction done in %.0f s.", time.time() - t0)
    return gdb_path


def _extraction_complete(gdb_path: Path) -> bool:
    """True if every zip member exists on disk with the exact expected size."""
    try:
        with zipfile.ZipFile(RAW_ZIP) as zf:
            members = zf.infolist()
            for info in members:
                p = EXTRACT_DIR / info.filename
                if not p.is_file() or p.stat().st_size != info.file_size:
                    return False
    except (OSError, zipfile.BadZipFile):
        return False
    return len(members) > 0


def cleanup_extraction() -> None:
    """Delete the regenerable interim extraction (raw zip is retained)."""
    if EXTRACT_DIR.exists():
        shutil.rmtree(EXTRACT_DIR)
        log.info("Deleted regenerable interim extraction %s", EXTRACT_DIR)


# ---------------------------------------------------------------------------
# Stage A: schema-first inspection and domain verification
# ---------------------------------------------------------------------------
def _silvis_columns(s2: dict) -> list[str]:
    """Exact attribute columns this stage consumes (from the registry config)."""
    sil = s2["silvis"]
    cols = [sil["block_id_field"], sil["county_fips_field"]]
    for y in s2["wui_vintages"]:
        cols.append(sil["flag_fields"][str(y)])
        cols.append(sil["class_fields"][str(y)])
        cols.append(sil["housing_unit_fields"][str(y)])
    return cols


def inspect_schema(gdb_path: Path, s2: dict, write_report: bool = True) -> dict:
    """Inspect the actual GDB schema + observed domains; verify vs config.

    Returns a report dict. Raises on CRITICAL disagreements:
    - layer missing, required fields missing,
    - CRS not NAD83 geographic (EPSG:4269) as documented,
    - observed WUICLASS domain values outside the documented 14-value domain,
    - observed WUIFLAG values outside {0, 1, 2},
    - flag/class inconsistency (flag says WUI but class is not a WUI class),
    - FIPS not equal to the county digits of BLK20.

    Housing-unit domain is NOT assumed: counts are reported, not validated
    against a threshold (SILVIS's allocation methodology can produce
    fractional units).
    """
    layer = s2["silvis"]["layer"]
    sil = s2["silvis"]
    vintages = [str(y) for y in s2["wui_vintages"]]

    try:
        info = pyogrio.read_info(gdb_path, layer=layer)
    except Exception as exc:
        available = []
        try:
            available = [name for name, _ in pyogrio.list_layers(gdb_path)]
        except Exception:
            pass
        raise ValueError(
            f"Configured SILVIS layer {layer!r} could not be opened from "
            f"{gdb_path}: {exc}. Layers actually present: {available}. "
            "Fix stage2.silvis.layer in config/analysis.yaml (and the "
            "layer_name in config/datasets.yaml) to the inspected value."
        ) from exc
    fields = list(info["fields"])
    dtypes = [str(d) for d in info["dtypes"]]
    crs_wkt = info.get("crs")
    crs_epsg = CRS.from_user_input(crs_wkt).to_epsg() if crs_wkt else None
    n_features = int(info["features"])

    report: dict = {
        "gdb_path": stored_path(gdb_path),
        "layer": layer,
        "n_features": n_features,
        "crs_wkt": str(crs_wkt),
        "crs_epsg": crs_epsg,
        "fields": {f: d for f, d in zip(fields, dtypes)},
    }

    required = _silvis_columns(s2)
    missing = [c for c in required if c not in fields]
    report["required_fields_missing"] = missing
    if missing:
        raise ValueError(f"SILVIS GDB is missing required fields: {missing}")
    if crs_epsg != 4269:
        raise ValueError(
            f"SILVIS GDB CRS must be NAD83 geographic (EPSG:4269) per the FGDC "
            f"metadata; got EPSG:{crs_epsg}"
        )

    # Attribute-only streaming pass: observed domains + integrity relations.
    observed = {v: {"flag": {}, "class": {}} for v in vintages}
    n_rows = 0
    hu_stats = {v: {"min": np.inf, "max": -np.inf} for v in vintages}
    flag_class_conflicts = 0
    fips_mismatch = 0
    blk20_state_mismatch = 0
    duplicate_blk20 = 0
    seen_blk = set()
    wui_any = 0

    use_cols = _silvis_columns(s2)
    for start in range(0, n_features, ATTRIBUTE_CHUNK):
        df = pyogrio.read_dataframe(
            gdb_path,
            layer=layer,
            use_columns=use_cols,
            read_geometry=False,
            skip_features=start,
            max_features=ATTRIBUTE_CHUNK,
        )
        if df.empty:
            break
        n_rows += len(df)
        blk = df[sil["block_id_field"]].astype(str)
        fips = df[sil["county_fips_field"]].astype(str)
        mismatch = (fips != blk.str[:5])
        fips_mismatch += int(mismatch.sum())
        dup = blk.duplicated(keep=False)
        duplicate_blk20 += int(dup.sum())
        blk20_state_mismatch += int((blk.str[:2] != fips.str[:2]).sum())
        # memory-safe dedup count
        wui_any += int(df[[sil["flag_fields"][v] for v in vintages]]
                       .ne(0).any(axis=1).sum())
        for v in vintages:
            fcol, ccol, hcol = (
                sil["flag_fields"][v], sil["class_fields"][v],
                sil["housing_unit_fields"][v],
            )
            fv = df[fcol].value_counts(dropna=False)
            for k, n in fv.items():
                key = "<NA>" if pd.isna(k) else str(int(k)) if isinstance(k, (int, np.integer, float, np.floating)) and not pd.isna(k) else str(k)
                observed[v]["flag"][key] = observed[v]["flag"].get(key, 0) + int(n)
            cv = df[ccol].value_counts(dropna=False)
            for k, n in cv.items():
                key = "<NA>" if pd.isna(k) else str(k)
                observed[v]["class"][key] = observed[v]["class"].get(key, 0) + int(n)
# flag/class consistency: flag != 0 <=> class is a WUI class, AND
            # the flag value must match the class category
            # (1 = intermix, 2 = interface)
            is_intermix = df[ccol].isin(sil["intermix_classes"])
            is_interface = df[ccol].isin(sil["interface_classes"])
            flag_class_conflicts += int(
                (df[fcol].ne(0) != (is_intermix | is_interface)).sum()
            )
            flag_class_conflicts += int((df[fcol].eq(1) != is_intermix).sum())
            flag_class_conflicts += int((df[fcol].eq(2) != is_interface).sum())
            hu = pd.to_numeric(df[hcol], errors="coerce")
            hu_stats[v]["min"] = min(hu_stats[v]["min"], float(hu.min()))
            hu_stats[v]["max"] = max(hu_stats[v]["max"], float(hu.max()))
        del df
        log.info("  schema pass: %d / %d rows", n_rows, n_features)

    report["n_rows_scanned"] = n_rows
    report["observed_domains"] = observed
    report["hu_range"] = hu_stats
    report["fips_blk20_mismatch_rows"] = fips_mismatch
    report["duplicate_blk20_rows"] = duplicate_blk20
    report["n_blocks_wui_any_vintage"] = wui_any
    report["flag_class_conflict_rows"] = flag_class_conflicts

    # --- Verification against the documented domains ----------------------
    problems: list[str] = []
    valid_flags = {"0", "1", "2"}
    documented_classes = set(sil["class_domain"])
    wui_class_set = set(sil["intermix_classes"] + sil["interface_classes"])
    for v in vintages:
        bad_flags = [k for k in observed[v]["flag"] if k not in valid_flags]
        if bad_flags:
            problems.append(f"WUIFLAG{v}: undocumented values {bad_flags}")
        bad_classes = [k for k in observed[v]["class"] if k not in documented_classes]
        if bad_classes:
            problems.append(f"WUICLASS{v}: undocumented values {bad_classes}")
        # Group-sum consistency: the intermix class counts together must equal
        # the flag==1 count, and interface class counts together the flag==2
        # count (individual class counts are subsets, not equal to the group).
        n_intermix = sum(
            cnt for cname, cnt in observed[v]["class"].items()
            if cname in sil["intermix_classes"]
        )
        n_interface = sum(
            cnt for cname, cnt in observed[v]["class"].items()
            if cname in sil["interface_classes"]
        )
        n_flag1 = observed[v]["flag"].get("1", 0)
        n_flag2 = observed[v]["flag"].get("2", 0)
        if n_intermix != n_flag1:
            problems.append(
                f"WUICLASS{v}: {n_intermix} intermix blocks in total vs "
                f"{n_flag1} WUIFLAG{v}==1 blocks"
            )
        if n_interface != n_flag2:
            problems.append(
                f"WUICLASS{v}: {n_interface} interface blocks in total vs "
                f"{n_flag2} WUIFLAG{v}==2 blocks"
            )
    if fips_mismatch:
        problems.append(f"{fips_mismatch} rows where FIPS != BLK20[:5]")
    if flag_class_conflicts:
        problems.append(f"{flag_class_conflicts} flag/class consistency conflicts")
    report["schema_problems"] = problems

    if write_report:
        _write_schema_report(report)

    if problems:
        raise ValueError(
            "SILVIS schema verification FAILED:\n  - " + "\n  - ".join(problems)
        )
    log.info(
        "Schema verified: %d rows, all WUIFLAG in {0,1,2}, all WUICLASS values "
        "inside the documented 14-value domain, FIPS consistent with BLK20.",
        n_rows,
    )
    return report


def _write_schema_report(report: dict) -> None:
    SCHEMA_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(SCHEMA_JSON, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(SCHEMA_TXT, "w", encoding="utf-8") as fh:
        fh.write("SILVIS WUI change v4 — observed schema report\n")
        fh.write("=" * 60 + "\n")
        fh.write(f"layer:        {report['layer']}\n")
        fh.write(f"features:     {report['n_features']}\n")
        fh.write(f"crs:          EPSG:{report['crs_epsg']} ({report['crs_wkt'][:80]}...)\n")
        fh.write("\nfields (name: dtype):\n")
        for f, d in report["fields"].items():
            fh.write(f"  {f}: {d}\n")
        fh.write("\nobserved domains:\n")
        for v, dom in report["observed_domains"].items():
            fh.write(f"  vintage {v}:\n")
            fh.write("    WUIFLAG counts:  " + json.dumps(dom["flag"]) + "\n")
            fh.write("    WUICLASS counts: " + json.dumps(dom["class"]) + "\n")
        fh.write(f"\nhousing-unit ranges: {json.dumps(report['hu_range'])}\n")
        fh.write(f"\nrows with FIPS != BLK20[:5]: {report['fips_blk20_mismatch_rows']}\n")
        fh.write(f"rows with duplicated BLK20:  {report['duplicate_blk20_rows']}\n")
        fh.write(f"blocks WUI in >=1 vintage:   {report['n_blocks_wui_any_vintage']}\n")
        fh.write(f"flag/class conflicts:         {report['flag_class_conflict_rows']}\n")


# ---------------------------------------------------------------------------
# Stage B: chunked geometric pass -> WUI fragments GeoParquet
# ---------------------------------------------------------------------------
def _geoparquet_schema(crs: CRS, extra_fields: pa.schema) -> pa.Schema:
    """GeoParquet 1.0 schema: geometry as WKB + geo metadata in key/value."""
    geo_meta = {
        "version": "1.0.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "crs": crs.to_json(),
            }
        },
    }
    fields = list(extra_fields)
    fields.append(pa.field("geometry", pa.binary()))
    return pa.schema(fields, metadata={b"geo": json.dumps(geo_meta).encode("utf-8")})


def build_wui_fragments(gdb_path: Path, s2: dict) -> pd.DataFrame:
    """Stream WUI blocks to one GeoParquet fragment table.

    One row per (block polygon, county). Blocks nest within counties
    (verified in inspect_schema via FIPS == BLK20[:5]), so county
    assignment uses the dataset's own FIPS field — no spatial overlay
    against county polygons is needed and no CRS disagreement is possible
    at this step. All areas are computed in EPSG:5070.
    """
    sil = s2["silvis"]
    layer = sil["layer"]
    vintages = [str(y) for y in s2["wui_vintages"]]
    use_cols = _silvis_columns(s2)

    info = pyogrio.read_info(gdb_path, layer=layer)
    n_features = int(info["features"])
    flag_cols = [sil["flag_fields"][v] for v in vintages]

    arrow_fields = [
        pa.field("BLK20", pa.string()),
        pa.field("GEOID", pa.string()),
        pa.field("fragment_area_km2", pa.float64()),
    ] + [pa.field(f"wui_class_{v}", pa.string()) for v in vintages] \
      + [pa.field(f"hu_{v}", pa.float64()) for v in vintages]
    schema = _geoparquet_schema(CRS.from_epsg(CONUS_EQUAL_AREA_EPSG), pa.schema(arrow_fields))

    t0 = time.time()
    n_written = 0
    n_skipped_nonwui = 0
    with pq.ParquetWriter(FRAGMENTS_PATH, schema) as writer:
        for start in range(0, n_features, GEOMETRY_CHUNK):
            gdf = pyogrio.read_dataframe(
                gdb_path,
                layer=layer,
                use_columns=use_cols,
                skip_features=start,
                max_features=GEOMETRY_CHUNK,
            )
            if gdf.empty:
                break
            wui_mask = gdf[flag_cols].ne(0).any(axis=1).to_numpy()
            n_skipped_nonwui += int((~wui_mask).sum())
            gdf = gdf.loc[wui_mask].copy()
            if gdf.empty:
                continue

            # NAD83 geographic -> CONUS Albers (meters) for area math.
            if CRS.from_user_input(gdf.crs).to_epsg() != CONUS_EQUAL_AREA_EPSG:
                gdf = gdf.to_crs(CONUS_EQUAL_AREA_EPSG)
            geoms = np.asarray(gdf.geometry.values, dtype=object)
            invalid = ~shapely.is_valid(geoms)
            if invalid.any():
                geoms[invalid] = shapely.make_valid(geoms[invalid])
            empty = shapely.is_empty(geoms) | shapely.is_missing(geoms)
            if empty.any():
                keep = ~empty
                gdf = gdf.loc[keep]
                geoms = geoms[keep]
            area_km2 = shapely.area(geoms) * KM2_PER_M2

            data = {
                "BLK20": gdf[sil["block_id_field"]].astype(str).to_numpy(),
                "GEOID": gdf[sil["county_fips_field"]].astype(str).to_numpy(),
                "fragment_area_km2": area_km2,
            }
            for v in vintages:
                flag = gdf[sil["flag_fields"][v]].to_numpy()
                cls = gdf[sil["class_fields"][v]].astype("object")
                # class only where the block is WUI that vintage
                cls = cls.where(flag != 0, other=None)
                data[f"wui_class_{v}"] = cls.to_numpy()
                hu = pd.to_numeric(gdf[sil["housing_unit_fields"][v]], errors="coerce")
                data[f"hu_{v}"] = hu.to_numpy(dtype="float64")
            data["geometry"] = shapely.to_wkb(geoms, include_srid=False)

            writer.write_table(pa.table(data, schema=schema))
            n_written += len(gdf)
            done = min(start + GEOMETRY_CHUNK, n_features)
            log.info(
                "  fragments: rows %d-%d done (%.0f s elapsed, %d WUI fragments kept)",
                start, done, time.time() - t0, n_written,
            )
            del gdf, geoms, data

    log.info(
        "Wrote %d WUI block fragments (%d non-WUI blocks skipped) to %s "
        "in %.0f s.",
        n_written, n_skipped_nonwui, FRAGMENTS_PATH, time.time() - t0,
    )
    meta = {
        "n_fragments": n_written,
        "n_nonwui_blocks_skipped": n_skipped_nonwui,
    }
    with open(WUI_DIR / "silvis_fragments_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return pd.DataFrame({"n_fragments": [n_written]})


# ---------------------------------------------------------------------------
# Stage C: county-level WUI metrics per vintage
# ---------------------------------------------------------------------------
def allocate_housing(df: pd.DataFrame) -> pd.DataFrame:
    """Allocate block housing units to fragments by area share.

    HU{year} in SILVIS is a block-level attribute. When a block is
    represented by multiple polygon fragments, each fragment carries the
    block's raw HU; allocating by fragment-area share of the block's total
    area preserves the county housing total exactly (sum of shares = 1).
    For single-fragment blocks this is a no-op.
    """
    out = df.copy()
    blk_total = out.groupby("BLK20")["fragment_area_km2"].transform("sum")
    for col in [c for c in out.columns if c.startswith("hu_")]:
        out[col] = out[col] * (out["fragment_area_km2"] / blk_total)
    return out


def county_wui_metrics(
    fragments: pd.DataFrame,
    counties: gpd.GeoDataFrame,
    county_hu_totals: dict[str, dict[str, float]],
    s2: dict,
) -> pd.DataFrame:
    """One row per GEOID x vintage with county WUI area/composition/housing.

    pct_wui_interface / pct_wui_intermix are shares of the COUNTY area
    (they sum to pct_county_wui); the share-of-WUI versions are reported in
    pct_of_wui_that_is_interface / pct_of_wui_that_is_intermix.
    """
    vintages = [str(y) for y in s2["wui_vintages"]]
    sil = s2["silvis"]
    intermix = set(sil["intermix_classes"])
    interface = set(sil["interface_classes"])

    df = allocate_housing(fragments)
    rows: list[dict] = []
    for v in vintages:
        cls = df[f"wui_class_{v}"]
        wui = df[cls.notna()]
        base = (
            wui.assign(
                intermix_km2=np.where(wui[f"wui_class_{v}"].isin(intermix),
                                      wui["fragment_area_km2"], 0.0),
                interface_km2=np.where(wui[f"wui_class_{v}"].isin(interface),
                                       wui["fragment_area_km2"], 0.0),
            )
            .groupby("GEOID", as_index=False)
            .agg(
                wui_area_km2=("fragment_area_km2", "sum"),
                interface_wui_area_km2=("interface_km2", "sum"),
                intermix_wui_area_km2=("intermix_km2", "sum"),
                hu_in_wui=(f"hu_{v}", "sum"),
                n_wui_blocks=("BLK20", "nunique"),
            )
        )
        for _, r in base.iterrows():
            rows.append({
                "GEOID": r["GEOID"],
                "wui_year": int(v),
                "wui_area_km2": r["wui_area_km2"],
                "interface_wui_area_km2": r["interface_wui_area_km2"],
                "intermix_wui_area_km2": r["intermix_wui_area_km2"],
                "hu_in_wui": r["hu_in_wui"],
                "n_wui_blocks": int(r["n_wui_blocks"]),
            })
    metrics = pd.DataFrame(rows)

    # Full county x vintage grid: every CONUS county must appear for every
    # vintage (zero-filled where it has no WUI), so absence is distinguishable
    # from missingness and GEOID x wui_year is a unique, complete key.
    attrs = ["GEOID", "county_name", "state_fips", "state_name", "county_area_km2"]
    for extra in ("county_land_area_km2", "county_water_area_km2"):
        if extra in counties.columns:
            attrs.append(extra)
    years = pd.DataFrame({"wui_year": [int(v) for v in vintages]})
    grid = counties[attrs].merge(years, how="cross")
    metrics = grid.merge(metrics, on=["GEOID", "wui_year"], how="left")
    for c in ("wui_area_km2", "interface_wui_area_km2", "intermix_wui_area_km2",
              "hu_in_wui", "n_wui_blocks"):
        metrics[c] = metrics[c].fillna(0.0 if c != "n_wui_blocks" else 0)
    metrics["n_wui_blocks"] = metrics["n_wui_blocks"].astype("int64")

    # County total housing units per vintage (all blocks, WUI + non-WUI),
    # accumulated during the attribute-only inspection pass. One row per
    # (GEOID, wui_year); merging (rather than mapping) keeps the totals
    # vintage-specific.
    hu_rows = [
        {"GEOID": g, "wui_year": int(v), "hu_total_county": float(n)}
        for v in vintages
        for g, n in county_hu_totals.get(v, {}).items()
    ]
    if hu_rows:
        metrics = metrics.merge(
            pd.DataFrame(hu_rows), on=["GEOID", "wui_year"], how="left"
        )
    else:
        metrics["hu_total_county"] = np.nan
    metrics["hu_total_county"] = metrics["hu_total_county"].astype("float64")

    metrics["pct_county_wui"] = metrics["wui_area_km2"] / metrics["county_area_km2"] * 100.0
    # pct_county_wui uses the full legal polygon (land + water). The land-area
    # percentage is the preferred exposure denominator when ALAND is present.
    metrics["pct_total_county_area_wui"] = metrics["pct_county_wui"]
    if "county_land_area_km2" in metrics.columns:
        metrics["pct_county_land_area_wui"] = np.where(
            metrics["county_land_area_km2"] > 0,
            metrics["wui_area_km2"] / metrics["county_land_area_km2"] * 100.0,
            np.nan,
        )
    metrics["pct_wui_interface"] = (
        metrics["interface_wui_area_km2"] / metrics["county_area_km2"] * 100.0
    )
    metrics["pct_wui_intermix"] = (
        metrics["intermix_wui_area_km2"] / metrics["county_area_km2"] * 100.0
    )
    metrics["pct_of_wui_that_is_interface"] = np.where(
        metrics["wui_area_km2"] > 0,
        metrics["interface_wui_area_km2"] / metrics["wui_area_km2"] * 100.0,
        np.nan,
    )
    metrics["pct_of_wui_that_is_intermix"] = np.where(
        metrics["wui_area_km2"] > 0,
        metrics["intermix_wui_area_km2"] / metrics["wui_area_km2"] * 100.0,
        np.nan,
    )
    metrics["pct_county_hu_in_wui"] = np.where(
        metrics["hu_total_county"] > 0,
        metrics["hu_in_wui"] / metrics["hu_total_county"] * 100.0,
        np.nan,
    )

    cols = [
        "GEOID", "county_name", "state_fips", "state_name", "wui_year",
        "county_area_km2", "county_land_area_km2", "county_water_area_km2",
        "wui_area_km2", "pct_county_wui", "pct_total_county_area_wui",
        "pct_county_land_area_wui",
        "interface_wui_area_km2", "intermix_wui_area_km2",
        "pct_wui_interface", "pct_wui_intermix",
        "pct_of_wui_that_is_interface", "pct_of_wui_that_is_intermix",
        "hu_total_county", "hu_in_wui", "pct_county_hu_in_wui", "n_wui_blocks",
    ]
    cols = [c for c in cols if c in metrics.columns]
    metrics = metrics[cols].sort_values(["GEOID", "wui_year"]).reset_index(drop=True)
    return metrics


def accumulate_county_hu(gdb_path: Path, s2: dict) -> dict[str, dict[str, float]]:
    """Per-vintage county housing totals over ALL blocks (attribute-only)."""
    sil = s2["silvis"]
    layer = sil["layer"]
    vintages = [str(y) for y in s2["wui_vintages"]]
    n_features = int(pyogrio.read_info(gdb_path, layer=layer)["features"])
    use_cols = [sil["county_fips_field"]] + [sil["housing_unit_fields"][v] for v in vintages]

    totals: dict[str, dict[str, float]] = {v: {} for v in vintages}
    for start in range(0, n_features, ATTRIBUTE_CHUNK):
        df = pyogrio.read_dataframe(
            gdb_path,
            layer=layer,
            use_columns=use_cols,
            read_geometry=False,
            skip_features=start,
            max_features=ATTRIBUTE_CHUNK,
        )
        if df.empty:
            break
        geoid = df[sil["county_fips_field"]].astype(str)
        for v in vintages:
            hu = pd.to_numeric(df[sil["housing_unit_fields"][v]], errors="coerce").fillna(0.0)
            part = pd.Series(hu.values, index=geoid.values).groupby(level=0).sum()
            for g, s in part.items():
                totals[v][g] = totals[v].get(g, 0.0) + float(s)
        del df
    return totals


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    s2 = load_stage2_config()
    cfg = load_analysis_config()

    gdb_path = ensure_gdb_extracted()

    # --- Stage A: schema inspection (resume from a verified report if the
    # GDB has not changed; the full pass re-scans 9.1M rows) ---------------
    schema_report_path = SCHEMA_JSON
    report = None
    if schema_report_path.exists():
        try:
            with open(schema_report_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            gdb_mtime = max(
                p.stat().st_mtime for p in gdb_path.rglob("*") if p.is_file()
            )
            cached_gdb = cached.get("gdb_path")
            same_gdb = (
                cached_gdb is not None
                and resolve_stored_path(cached_gdb) == gdb_path.resolve()
            )
            if (same_gdb
                    and schema_report_path.stat().st_mtime >= gdb_mtime
                    and not cached.get("schema_problems")):
                report = cached
                log.info(
                    "Reusing verified schema report from %s "
                    "(GDB unchanged; %s rows scanned).",
                    schema_report_path.name, cached.get("n_rows_scanned"),
                )
        except (OSError, ValueError):
            report = None
    if report is None:
        report = inspect_schema(gdb_path, s2)

    # --- Stage B: fragments ------------------------------------------------
    if FRAGMENTS_PATH.exists():
        log.info("Fragments table already exists; reusing %s", FRAGMENTS_PATH)
    else:
        build_wui_fragments(gdb_path, s2)

    # --- County housing totals need the GDB (all blocks incl. non-WUI) -----
    hu_cache = INTERIM_DIR / "silvis_county_hu_totals.json"
    if hu_cache.exists():
        with open(hu_cache, encoding="utf-8") as fh:
            county_hu = json.load(fh)
    else:
        county_hu = accumulate_county_hu(gdb_path, s2)
        with open(hu_cache, "w", encoding="utf-8") as fh:
            json.dump(county_hu, fh)

    # --- Stage C: county metrics per vintage ------------------------------
    t0 = time.time()
    fragments = gpd.read_parquet(FRAGMENTS_PATH)
    log.info("Loaded %d WUI fragments (%.0f s).", len(fragments), time.time() - t0)
    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    metrics = county_wui_metrics(fragments, counties, county_hu, s2)

    metrics.to_parquet(COUNTY_WUI_PATH, index=False)
    metrics.to_csv(COUNTY_WUI_CSV, index=False)
    log.info(
        "Wrote %d county-vintage rows (%d counties x %d vintages) to %s",
        len(metrics), metrics["GEOID"].nunique(), len(s2["wui_vintages"]),
        COUNTY_WUI_PATH,
    )
    for v in s2["wui_vintages"]:
        sub = metrics[metrics["wui_year"] == v]
        log.info(
            "  vintage %s: CONUS WUI area %.0f km2 | mean county pct_wui %.2f%% "
            "| max %.1f%%",
            v, sub["wui_area_km2"].sum(), sub["pct_county_wui"].mean(),
            sub["pct_county_wui"].max(),
        )
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("preprocess_wui.log")
    sys.exit(main())