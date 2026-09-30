"""Stage 2C — U.S. Census Places: WUI, fire history, and WUI fire exposure.

Extends the Stage 2 county framework to 2025 TIGER/Line Places (incorporated
places + census designated places). Places are conceptually different from
counties: they cover only a fraction of CONUS land and their boundaries
change over time. Place outputs are NEVER combined with county tables.

Boundary-vintage assumption (documented in docs/wui_methodology.md): only the
2025 TIGER place boundaries exist in this pipeline; historical fire overlap
is measured against CURRENT boundaries. This is a limitation, not a claim
that today's boundaries matched the 1990s landscape.

Outputs (all keyed on the 7-char place GEOID = STATEFP + PLACEFP)
----------------------------------------------------------------
- data/processed/wui/places_conus.geoparquet     merged places, EPSG:5070
- data/processed/wui/place_wui_by_vintage.parquet + outputs/tables/place_wui_by_vintage.csv
- data/processed/wui/place_wui_2020.parquet + outputs/tables/place_wui_2020.csv
- outputs/tables/place_historical_fire_summary.csv (place x year, all MTBS years)
- outputs/tables/place_wui_fire_exposure.csv (place x year, vintage-aligned)

Temporal alignment mirrors the county workflow: 1990/2000/2010/2020 snapshots
represent their decadal fire periods; 1984-1989 fire years have a null
assigned_wui_vintage, assigned_wui_vintage_label "pre_1990_no_wui_baseline",
and null WUI exposure.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyogrio  # noqa: E402
import shapely  # noqa: E402
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    TABLES_DIR,
    WUI_DIR,
    ensure_dirs,
    get_logger,
    load_stage2_config,
)

log = get_logger("places_wui")

CONUS_EQUAL_AREA_EPSG = 5070
KM2_PER_M2 = 1e-6
FRAGMENT_BATCH_ROWS = 250_000
MIN_INTERSECTION_KM2 = 1e-6  # drop pure boundary touches at place level too

PLACES_PATH = WUI_DIR / "places_conus.geoparquet"
PLACE_WUI_PATH = WUI_DIR / "place_wui_by_vintage.parquet"
PLACE_WUI_2020_PATH = WUI_DIR / "place_wui_2020.parquet"
PLACE_FIRE_SUMMARY_PATH = WUI_DIR / "place_historical_fire_summary.parquet"
PLACE_EXPOSURE_PATH = WUI_DIR / "place_wui_fire_exposure.parquet"

FRAGMENTS_PATH = WUI_DIR / "silvis_wui_fragments.geoparquet"
FIRE_WUI_PIECES_PATH = INTERIM_DIR / "fires_wui_intersections.geoparquet"

REQUIRED_PLACE_FIELDS = [
    "STATEFP", "PLACEFP", "GEOID", "NAME", "NAMELSAD", "LSAD", "FUNCSTAT",
]


# ---------------------------------------------------------------------------
# Stage A: merge state place files
# ---------------------------------------------------------------------------
def _read_place_zip(zpath: Path, stem: str) -> gpd.GeoDataFrame:
    """Read one TIGER place shapefile from its zip (GDAL virtual FS).

    TIGER place zips are ~1-4 MB, so direct archive reads are cheap. Two
    well-documented OGR forms are tried; the first that works is used and
    remembered for the remaining states.
    """
    candidates = [
        (f"/vsizip/{zpath}/{stem}.shp", None),
        (f"zip://{zpath}!{stem}.shp", None),
        (f"zip://{zpath}", stem),
    ]
    last_err: Exception | None = None
    for path, layer in candidates:
        try:
            return pyogrio.read_dataframe(path, layer=layer) if layer else \
                pyogrio.read_dataframe(path)
        except Exception as exc:  # try the next documented form
            last_err = exc
    raise RuntimeError(f"Could not read {zpath} via GDAL zip handlers: {last_err}")


def merge_place_files(conus_state_fips: list[str]) -> gpd.GeoDataFrame:
    """Concatenate per-state TIGER place shapefiles into one CONUS layer.

    Reads each zip via GDAL's zip:// handler (files are ~1-4 MB each, so
    direct reads are cheap). Verifies required Census fields in every state
    and writes a schema report before any geometry work.
    """
    raw_dir = RAW_DIR / "census_places"
    frames = []
    schema_report: dict = {
        "states_expected": len(conus_state_fips),
        "states_read": 0,
        "columns_first_file": None,
        "n_places_total": 0,
        "crs_first_file": None,
        "funcstat_values": None,
        "lsad_values": None,
    }
    for i, fips in enumerate(conus_state_fips):
        zpath = raw_dir / f"tl_2025_{fips}_place.zip"
        if not zpath.exists():
            raise FileNotFoundError(
                f"Missing {zpath}. Run `python src/download_data.py --only "
                "census_places` first."
            )
        stem = f"tl_2025_{fips}_place"
        gdf = _read_place_zip(zpath, stem)
        missing = [c for c in REQUIRED_PLACE_FIELDS if c not in gdf.columns]
        if missing:
            raise ValueError(f"{zpath.name}: missing expected fields {missing}")
        if i == 0:
            schema_report["columns_first_file"] = list(gdf.columns)
            schema_report["crs_first_file"] = (
                gdf.crs.to_string() if gdf.crs is not None else None
            )
        frames.append(gdf)
        schema_report["states_read"] += 1
        schema_report["n_places_total"] += len(gdf)
        if (i + 1) % 10 == 0:
            log.info("  merged %d/%d state place files (%d places so far)",
                     i + 1, len(conus_state_fips), schema_report["n_places_total"])

    crs = frames[0].crs
    places = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs=crs
    )
    del frames
    if places.crs is None or places.crs.to_epsg() != 4269:
        raise ValueError(
            f"TIGER places must arrive in EPSG:4269 (NAD83 geographic); saw "
            f"{places.crs}."
        )
    dup = places["GEOID"].duplicated().sum()
    if dup:
        raise ValueError(f"{dup} duplicate place GEOIDs across state files.")

    schema_report["funcstat_values"] = sorted(places["FUNCSTAT"].astype(str).unique())
    schema_report["lsad_values"] = sorted(places["LSAD"].astype(str).unique())

    places = places[REQUIRED_PLACE_FIELDS + ["ALAND", "AWATER", "geometry"]].copy()
    for c in REQUIRED_PLACE_FIELDS:
        places[c] = places[c].astype(str)

    log.info("Reprojecting %d places to EPSG:5070.", len(places))
    places = places.to_crs(CONUS_EQUAL_AREA_EPSG)
    places["place_area_km2"] = shapely.area(
        np.asarray(places.geometry.values, dtype=object)
    ) * KM2_PER_M2
    places["place_land_area_km2"] = places["ALAND"].astype("float64") * KM2_PER_M2
    places["place_water_area_km2"] = places["AWATER"].astype("float64") * KM2_PER_M2
    places = places.sort_values("GEOID").reset_index(drop=True)

    with open(TABLES_DIR / "schema_places_raw.json", "w", encoding="utf-8") as fh:
        json.dump(schema_report, fh, indent=2)
    with open(TABLES_DIR / "schema_places_raw.txt", "w", encoding="utf-8") as fh:
        fh.write("Census TIGER/Line 2025 Places (49 CONUS state files, merged)\n")
        fh.write("=" * 70 + "\n\n")
        fh.write(f"states merged:  {schema_report['states_read']}\n")
        fh.write(f"places total:   {schema_report['n_places_total']}\n")
        fh.write(f"source CRS:     {schema_report['crs_first_file']}\n")
        fh.write(f"FUNCSTAT values: {schema_report['funcstat_values']}\n")
        fh.write(f"LSAD values:    {schema_report['lsad_values']}\n\n")
        fh.write("fields (first file):\n")
        for c in schema_report["columns_first_file"]:
            fh.write(f"  - {c}\n")
        fh.write("\nrequired fields verified in every state file:\n")
        for c in REQUIRED_PLACE_FIELDS:
            fh.write(f"  - {c}\n")

    places.to_parquet(PLACES_PATH, index=False)
    log.info("Wrote %d merged places to %s", len(places), PLACES_PATH)
    return places


# ---------------------------------------------------------------------------
# Stage B: place x vintage WUI (chunked over SILVIS fragments)
# ---------------------------------------------------------------------------
def _accumulate_batch(
    pairs: gpd.GeoDataFrame,
    place_geoms: np.ndarray,
    place_geoids: np.ndarray,
    vintages: list[str],
    intermix: set,
    interface: set,
    accum: dict,
) -> None:
    """Intersect candidate (fragment, place) pairs and accumulate areas.

    accum[place_geoid][vintage] = [wui_km2, interface_km2, intermix_km2]
    """
    p_idx = pairs["index_right"].to_numpy()
    inter = shapely.intersection(
        np.asarray(pairs.geometry.values, dtype=object),
        place_geoms[p_idx],
    )
    area = shapely.area(np.asarray(inter, dtype=object)) * KM2_PER_M2
    ok = area >= MIN_INTERSECTION_KM2
    if not ok.any():
        return
    ok_idx = np.nonzero(ok)[0]
    ok_geoid = place_geoids[p_idx][ok_idx]
    ok_area = area[ok_idx]
    for v in vintages:
        cls = pairs[f"wui_class_{v}"].to_numpy()[ok_idx]
        # object array holds None where the block was not WUI in vintage v
        is_wui = np.array(
            [c is not None and not (isinstance(c, float) and np.isnan(c)) for c in cls],
            dtype=bool,
        )
        if not is_wui.any():
            continue
        sel_geoid = ok_geoid[is_wui]
        sel_area = ok_area[is_wui]
        sel_cls = cls[is_wui]
        iface_mask = np.array([c in interface for c in sel_cls], dtype=bool)
        imix_mask = np.array([c in intermix for c in sel_cls], dtype=bool)
        for slot, mask in ((0, np.ones(len(sel_geoid), dtype=bool)),
                           (1, iface_mask),
                           (2, imix_mask)):
            if not mask.any():
                continue
            part = pd.DataFrame({"g": sel_geoid[mask], "a": sel_area[mask]})
            for g, s in part.groupby("g", sort=False)["a"].sum().items():
                accum[g][v][slot] += float(s)


def build_place_wui_by_vintage(
    places: gpd.GeoDataFrame,
    fragments_path: Path,
    s2: dict,
) -> pd.DataFrame:
    """Per (place, vintage) WUI area from true geometric intersection.

    Fragments are processed in parquet batches to bound memory; places stay
    resident (they are small). Each (fragment, place) pair is intersected
    once; the piece is attributed to every vintage in which the block was
    WUI in that vintage.
    """
    vintages = [str(y) for y in s2["wui_vintages"]]
    intermix = set(s2["silvis"]["intermix_classes"])
    interface = set(s2["silvis"]["interface_classes"])

    place_geoms = np.asarray(places.geometry.values, dtype=object)
    geoids = places["GEOID"].to_numpy()
    accum: dict[str, dict[str, list[float]]] = {
        g: {v: [0.0, 0.0, 0.0] for v in vintages} for g in geoids
    }

    import pyarrow.parquet as pq

    pf = pq.ParquetFile(fragments_path)
    t0 = time.time()
    n_batches = 0
    for batch in pf.iter_batches(batch_size=FRAGMENT_BATCH_ROWS,
                                 columns=["geometry"] + [f"wui_class_{v}" for v in vintages]):
        d = batch.to_pydict()
        geoms = shapely.from_wkb(np.asarray(d["geometry"], dtype=object))
        bdf = gpd.GeoDataFrame(
            {f"wui_class_{v}": np.asarray(d[f"wui_class_{v}"], dtype=object)
             for v in vintages},
            geometry=gpd.GeoSeries(geoms, crs=CONUS_EQUAL_AREA_EPSG),
        )
        pairs = gpd.sjoin(bdf, places[["GEOID", "geometry"]], how="inner",
                          predicate="intersects")
        if len(pairs):
            _accumulate_batch(pairs, place_geoms, geoids, vintages,
                              intermix, interface, accum)
        n_batches += 1
        if n_batches % 10 == 0:
            log.info("  place WUI: fragment batch %d done (%.0f s)",
                     n_batches, time.time() - t0)
        del bdf, pairs, geoms, d

    rows = []
    for g in geoids:
        for v in vintages:
            wui, iface, imix = accum[g][v]
            rows.append({
                "GEOID": g,
                "wui_year": int(v),
                "wui_area_km2": wui,
                "interface_wui_area_km2": iface,
                "intermix_wui_area_km2": imix,
            })
    df = pd.DataFrame(rows)
    attrs = ["GEOID", "NAME", "NAMELSAD", "STATEFP", "PLACEFP",
             "LSAD", "FUNCSTAT", "place_area_km2"]
    for extra in ("place_land_area_km2", "place_water_area_km2"):
        if extra in places.columns:
            attrs.append(extra)
    df = places[attrs].merge(df, on="GEOID", how="left", validate="one_to_many")
    for c in ("wui_area_km2", "interface_wui_area_km2", "intermix_wui_area_km2"):
        df[c] = df[c].fillna(0.0)
    df["pct_place_wui"] = df["wui_area_km2"] / df["place_area_km2"] * 100.0
    df["pct_total_place_area_wui"] = df["pct_place_wui"]
    if "place_land_area_km2" in df.columns:
        df["pct_place_land_area_wui"] = np.where(
            df["place_land_area_km2"] > 0,
            df["wui_area_km2"] / df["place_land_area_km2"] * 100.0,
            np.nan,
        )
    df["pct_place_wui_interface"] = (
        df["interface_wui_area_km2"] / df["place_area_km2"] * 100.0
    )
    df["pct_place_wui_intermix"] = (
        df["intermix_wui_area_km2"] / df["place_area_km2"] * 100.0
    )
    df["pct_of_place_wui_that_is_interface"] = np.where(
        df["wui_area_km2"] > 0,
        df["interface_wui_area_km2"] / df["wui_area_km2"] * 100.0,
        np.nan,
    )
    df["pct_of_place_wui_that_is_intermix"] = np.where(
        df["wui_area_km2"] > 0,
        df["intermix_wui_area_km2"] / df["wui_area_km2"] * 100.0,
        np.nan,
    )
    return df.sort_values(["GEOID", "wui_year"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Stage C: place x year historical fire summary
# ---------------------------------------------------------------------------
def build_place_fire_summary(
    fires: gpd.GeoDataFrame,
    places: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Place x year MTBS summary: distinct FIRE_ID counts + burned area.

    Fires are reprojected to EPSG:5070 first; burned area within each place
    is a true geometric intersection, matching the county workflow.
    """
    if fires.crs is None or fires.crs.to_epsg() != 4269:
        raise ValueError(f"Expected MTBS fires in EPSG:4269, saw {fires.crs}")
    fires = fires.to_crs(CONUS_EQUAL_AREA_EPSG)
    pairs = gpd.sjoin(
        fires[["fire_id", "year", "geometry"]].reset_index(drop=True),
        places[["GEOID", "geometry"]],
        how="inner",
        predicate="intersects",
    )
    log.info("place fire summary: %d candidate (fire, place) pairs.", len(pairs))
    place_geoms = np.asarray(places.geometry.values, dtype=object)
    inter = shapely.intersection(
        np.asarray(pairs.geometry.values, dtype=object),
        place_geoms[pairs["index_right"].to_numpy()],
    )
    area = shapely.area(np.asarray(inter, dtype=object)) * KM2_PER_M2
    ok = area >= MIN_INTERSECTION_KM2
    kept = pairs.loc[ok].copy()
    kept["burned_area_km2"] = area[ok]
    kept["place_geoid"] = places["GEOID"].to_numpy()[kept["index_right"].to_numpy()]

    summary = (
        kept.groupby(["place_geoid", "year"], as_index=False)
        .agg(fire_count=("fire_id", "nunique"),
             burned_area_km2=("burned_area_km2", "sum"))
        .rename(columns={"place_geoid": "GEOID"})
    )
    attrs = places[["GEOID", "NAME", "NAMELSAD", "STATEFP", "LSAD",
                    "FUNCSTAT", "place_area_km2"]]
    summary = attrs.merge(summary, on="GEOID", how="left")
    summary["fire_count"] = summary["fire_count"].fillna(0).astype("int64")
    summary["burned_area_km2"] = summary["burned_area_km2"].fillna(0.0)
    # Keep only rows where at least one fire overlapped the place
    summary = summary[summary["fire_count"] > 0]
    return summary.sort_values(["GEOID", "year"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Stage D: place x year vintage-aligned WUI fire exposure
# ---------------------------------------------------------------------------
def build_place_wui_fire_exposure(
    pieces_path: Path,
    places: gpd.GeoDataFrame,
    fire_summary: pd.DataFrame,
    place_wui: pd.DataFrame,
    s2: dict,
) -> pd.DataFrame:
    """Place x year WUI burn exposure from the fire x WUI intersection pieces.

    Reuses the county workflow's pieces (fire ∩ WUI-fragment geometries):
    each piece is intersected with places, and attributed to the place-year
    of its fire with the vintage-appropriate interface/intermix class.
    """
    from build_wui_fire import (  # local import: shared contract
        assign_vintage,
        assigned_vintage_storage,
    )

    intermix = set(s2["silvis"]["intermix_classes"])
    interface = set(s2["silvis"]["interface_classes"])

    pieces = gpd.read_parquet(pieces_path)
    if pieces.crs is None or pieces.crs.to_epsg() != CONUS_EQUAL_AREA_EPSG:
        raise ValueError(f"Pieces must be EPSG:5070, saw {pieces.crs}")
    pieces = pieces.rename(columns={"GEOID": "county_geoid"})

    pairs = gpd.sjoin(
        pieces[["fire_id", "year", "geometry"] + [f"wui_class_{v}" for v in s2["wui_vintages"]]],
        places[["GEOID", "geometry"]],
        how="inner",
        predicate="intersects",
    )
    log.info("place exposure: %d candidate (piece, place) pairs from %d pieces.",
             len(pairs), len(pieces))
    place_geoms = np.asarray(places.geometry.values, dtype=object)
    inter = shapely.intersection(
        np.asarray(pairs.geometry.values, dtype=object),
        place_geoms[pairs["index_right"].to_numpy()],
    )
    area = shapely.area(np.asarray(inter, dtype=object)) * KM2_PER_M2
    ok = area >= MIN_INTERSECTION_KM2
    kept = pairs.loc[ok].copy()
    kept["wui_burned_km2"] = area[ok]
    kept["place_geoid"] = places["GEOID"].to_numpy()[kept["index_right"].to_numpy()]
    kept["vintage"] = [assign_vintage(int(y), s2) for y in kept["year"]]

    # interface/intermix per piece from its assigned vintage class
    # (vectorized per vintage, not per row)
    v_arr = kept["vintage"].to_numpy()
    cls = np.empty(len(kept), dtype=object)
    for v in s2["wui_vintages"]:
        m = v_arr == v
        if m.any():
            cls[m] = kept.loc[m, f"wui_class_{v}"].to_numpy()
    kept["is_interface"] = [c in interface for c in cls]
    kept["is_intermix"] = [c in intermix for c in cls]

    # Aggregate per (place, year) with piece-level interface/intermix splits.
    kept["_iface_a"] = np.where(kept["is_interface"], kept["wui_burned_km2"], 0.0)
    kept["_imix_a"] = np.where(kept["is_intermix"], kept["wui_burned_km2"], 0.0)
    grp = (
        kept.groupby(["place_geoid", "year"], as_index=False)
        .agg(wui_burned_area_km2=("wui_burned_km2", "sum"),
             n_wui_burning_fires=("fire_id", "nunique"),
             interface_wui_burned_km2=("_iface_a", "sum"),
             intermix_wui_burned_km2=("_imix_a", "sum"))
        .rename(columns={"place_geoid": "GEOID"})
    )

    # Base: every place-year with any fire history (from the Stage C summary)
    out = fire_summary.copy()
    out["assigned_wui_vintage"] = [assign_vintage(int(y), s2) for y in out["year"]]
    pre_label = s2["pre_wui_baseline"]["label"]
    pre_mask = out["assigned_wui_vintage"] == pre_label

    out = out.merge(grp, on=["GEOID", "year"], how="left")
    burn_cols = ["wui_burned_area_km2", "interface_wui_burned_km2",
                 "intermix_wui_burned_km2"]
    for c in burn_cols:
        out[c] = out[c].astype("float64")
    # No WUI burn in an aligned year means zero, not missing; pre-baseline
    # years keep NaN (exposure is undefined, never backfilled).
    out["n_wui_burning_fires"] = out["n_wui_burning_fires"].astype("float64")
    out.loc[~pre_mask, burn_cols + ["n_wui_burning_fires"]] = (
        out.loc[~pre_mask, burn_cols + ["n_wui_burning_fires"]].fillna(0.0)
    )

    # Vintage-matched place WUI area (null for pre-baseline rows)
    wui_area = place_wui[["GEOID", "wui_year", "wui_area_km2"]].rename(
        columns={"wui_year": "assigned_wui_vintage"}
    )
    has_vintage = out["assigned_wui_vintage"] != pre_label
    sub = out.loc[has_vintage, ["GEOID", "assigned_wui_vintage"]].merge(
        wui_area, on=["GEOID", "assigned_wui_vintage"], how="left"
    )
    out["wui_area_km2"] = np.nan
    out.loc[has_vintage, "wui_area_km2"] = sub["wui_area_km2"].to_numpy()

    out["pct_wui_burned_in_year"] = np.where(
        (out["wui_area_km2"] > 0) & out["wui_burned_area_km2"].notna(),
        out["wui_burned_area_km2"] / out["wui_area_km2"] * 100.0,
        np.nan,
    )
    out["pct_burned_area_that_was_wui"] = np.where(
        (out["burned_area_km2"] > 0) & out["wui_burned_area_km2"].notna(),
        out["wui_burned_area_km2"] / out["burned_area_km2"] * 100.0,
        np.nan,
    )
    vintage, labels = assigned_vintage_storage(out["assigned_wui_vintage"])
    out["assigned_wui_vintage"] = vintage
    out["assigned_wui_vintage_label"] = labels
    order = [
        "GEOID", "NAME", "NAMELSAD", "STATEFP", "LSAD", "FUNCSTAT",
        "place_area_km2", "year", "assigned_wui_vintage",
        "assigned_wui_vintage_label",
        "fire_count", "burned_area_km2", "wui_area_km2",
        "wui_burned_area_km2", "pct_wui_burned_in_year",
        "pct_burned_area_that_was_wui",
        "interface_wui_burned_km2", "intermix_wui_burned_km2",
    ]
    return out[order].sort_values(["GEOID", "year"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from common import load_analysis_config

    ensure_dirs()
    s2 = load_stage2_config()
    conus_fips = [str(f) for f in load_analysis_config()["scope"]["conus_state_fips"]]
    runtimes: dict[str, float] = {}

    # --- places layer -----------------------------------------------------
    if PLACES_PATH.exists():
        log.info("Places layer exists; reusing %s", PLACES_PATH)
        places = gpd.read_parquet(PLACES_PATH)
    else:
        t0 = time.time()
        places = merge_place_files(conus_fips)
        runtimes["merge_place_files_s"] = time.time() - t0
    if places.crs is None or places.crs.to_epsg() != CONUS_EQUAL_AREA_EPSG:
        raise ValueError(f"Places must be EPSG:5070, saw {places.crs}")

    # --- place x vintage WUI ----------------------------------------------
    if not FRAGMENTS_PATH.exists():
        log.error("Missing %s — run src/preprocess_wui.py first.", FRAGMENTS_PATH)
        return 1
    t0 = time.time()
    place_wui = build_place_wui_by_vintage(places, FRAGMENTS_PATH, s2)
    runtimes["place_wui_by_vintage_s"] = time.time() - t0
    place_wui.to_parquet(PLACE_WUI_PATH, index=False)
    place_wui.to_csv(TABLES_DIR / "place_wui_by_vintage.csv", index=False)
    p2020 = place_wui[place_wui["wui_year"] == 2020].copy()
    p2020.to_parquet(PLACE_WUI_2020_PATH, index=False)
    p2020.to_csv(TABLES_DIR / "place_wui_2020.csv", index=False)
    log.info("Wrote %d place-vintage WUI rows (%d places x %d vintages); "
             "%d places have >0 WUI in 2020.",
             len(place_wui), len(places), len(s2["wui_vintages"]),
             int((p2020["wui_area_km2"] > 0).sum()))

    # --- place fire summary -------------------------------------------------
    fires = gpd.read_parquet(PROCESSED_DIR / "mtbs_wildfires_conus.geoparquet")
    t0 = time.time()
    fire_summary = build_place_fire_summary(fires, places)
    runtimes["place_fire_summary_s"] = time.time() - t0
    fire_summary.to_parquet(PLACE_FIRE_SUMMARY_PATH, index=False)
    fire_summary.to_csv(TABLES_DIR / "place_historical_fire_summary.csv", index=False)
    log.info("Wrote %d place-year fire summary rows (%d places with fire history).",
             len(fire_summary), fire_summary["GEOID"].nunique())

    # --- place exposure ------------------------------------------------------
    if not FIRE_WUI_PIECES_PATH.exists():
        log.error("Missing %s — run src/build_wui_fire.py first.",
                  FIRE_WUI_PIECES_PATH)
        return 1
    t0 = time.time()
    exposure = build_place_wui_fire_exposure(
        FIRE_WUI_PIECES_PATH, places, fire_summary, place_wui, s2
    )
    runtimes["place_exposure_s"] = time.time() - t0
    exposure.to_parquet(PLACE_EXPOSURE_PATH, index=False)
    exposure.to_csv(TABLES_DIR / "place_wui_fire_exposure.csv", index=False)
    log.info("Wrote %d place-year exposure rows.", len(exposure))

    with open(TABLES_DIR / "places_runtimes.json", "w", encoding="utf-8") as fh:
        json.dump(runtimes, fh, indent=2)
    log.info("Stage 2C runtimes (s): %s", json.dumps(runtimes))
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("places_wui.log")
    sys.exit(main())