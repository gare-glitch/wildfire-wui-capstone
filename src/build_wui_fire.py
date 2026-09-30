"""Stage 2 — temporal alignment of MTBS wildfires with SILVIS WUI by county.

Core scientific rule (docs/methodology.md): the WUI is time-varying, so each
fire year is matched to the SILVIS decadal snapshot representing that period:

    1990 snapshot -> fire years 1990-1999
    2000 snapshot -> fire years 2000-2009
    2010 snapshot -> fire years 2010-2019
    2020 snapshot -> fire years 2020-2024

MTBS fire years 1984-1989 precede the earliest SILVIS snapshot: fire history
is retained but temporally aligned WUI exposure is NULL.
`assigned_wui_vintage` is a nullable integer (null for 1984-1989) and
`assigned_wui_vintage_label` carries "pre_1990_no_wui_baseline" for those
years. The 2020 map is never applied to earlier years and no interpolation
between snapshots is performed.

The fire x WUI overlap is a TRUE geometric intersection of fire perimeters
with WUI block fragments — never "burned area x WUI percentage".

Outputs
-------
data/interim/fires_wui_intersections.geoparquet
    One row per (fire, WUI block fragment) intersection piece, with the
    block's per-vintage WUI class attached. A piece contributes to a
    vintage's WUI burned area only where the block was WUI that vintage.

data/processed/county_year_wui_fire.parquet (+ outputs/tables CSV)
    One row per GEOID x year (Stage 1 county-year fire rows + WUI columns):
      assigned_wui_vintage (nullable int), assigned_wui_vintage_label,
      wui_area_km2 (vintage-matched),
      wui_burned_area_km2, interface_wui_burned_km2, intermix_wui_burned_km2,
      pct_wui_burned_in_year        (B/C separation: share of the county's
                                     vintage WUI area that burned that year)
      pct_burned_area_that_was_wui   (share of the year's burned area that
                                     fell on WUI)
    Concept separation (A vs B vs C vs D) follows the Stage 2 plan:
      A = burned_area_km2            total wildfire burned area (Stage 1)
      B = wui_burned_area_km2        WUI burned area (intersection)
      C = pct_wui_burned_in_year     percent of WUI burned
      D = pct_burned_area_that_was_wui  percent of burn occurring in WUI

data/processed/county_period_wui_fire.parquet (+ outputs/tables CSV)
    One row per GEOID x period (1990-99, 2000-09, 2010-19, 2020-24) with
    cumulative and UNIQUE-dissolved WUI burn footprints (reburning counted
    once in the footprint metrics; cumulative sums may legitimately exceed
    the WUI area across many years).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    TABLES_DIR,
    ensure_dirs,
    get_logger,
    load_analysis_config,
    load_stage2_config,
)
from preprocess_wui import FRAGMENTS_PATH  # noqa: E402

from spatial_overlay import _polygonal  # noqa: E402

log = get_logger("wui_fire")

KM2_PER_M2 = 1e-6
CONUS_EQUAL_AREA_EPSG = 5070

FIRE_WUI_PIECES_PATH = INTERIM_DIR / "fires_wui_intersections.geoparquet"
COUNTY_YEAR_OUT = PROCESSED_DIR / "county_year_wui_fire.parquet"
COUNTY_PERIOD_OUT = PROCESSED_DIR / "county_period_wui_fire.parquet"


# ---------------------------------------------------------------------------
# Temporal alignment (pure functions — unit-tested)
# ---------------------------------------------------------------------------
def assigned_vintage_storage(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Split assign_vintage results into a nullable int and a string label.

    `assign_vintage` returns 1990/2000/2010/2020 or the pre-baseline label.
    A single object column cannot be written to Parquet (ArrowInvalid: the
    label cannot convert to int64). Storage is therefore two columns:

    - assigned_wui_vintage: pandas Int64, pd.NA for pre-baseline rows
    - assigned_wui_vintage_label: "1990"/"2000"/"2010"/"2020" or the
      pre-baseline label verbatim
    """
    vintages: list = []
    labels: list[str] = []
    for value in series.tolist():
        if isinstance(value, str):
            vintages.append(pd.NA)
            labels.append(value)
        elif value is None or pd.isna(value):
            raise ValueError(
                "assigned_wui_vintage is missing before storage split; "
                "assign_vintage must return an int or the pre-baseline label"
            )
        else:
            year = int(value)
            vintages.append(year)
            labels.append(str(year))
    vintage = pd.Series(vintages, index=series.index, dtype="Int64")
    label = pd.Series(labels, index=series.index, dtype="string")
    return vintage, label


def assign_vintage(year: int, s2: dict) -> int | str:
    """Return the SILVIS vintage representing `year`, or the pre-baseline label.

    The mapping is period-specific by design (config/analysis.yaml
    stage2.vintage_periods). Years before the first snapshot get the
    pre-1990 baseline label; they are never assigned 1990 WUI.
    """
    pre = s2["pre_wui_baseline"]
    if pre["min_year"] <= year <= pre["max_year"]:
        return pre["label"]
    for v, span in s2["vintage_periods"].items():
        if span["min_year"] <= year <= span["max_year"]:
            return int(v)
    raise ValueError(
        f"Fire year {year} is outside every configured WUI period and the "
        "pre-1990 baseline window; extend stage2 config or investigate."
    )


def vintage_of_period(period_label: str, s2: dict) -> int:
    """The single SILVIS vintage assigned to a named period (e.g. '1990-1999')."""
    vintages = {v: (span["min_year"], span["max_year"]) for v, span in s2["vintage_periods"].items()}
    lo, hi = (int(x) for x in period_label.split("-"))
    for v, (vlo, vhi) in vintages.items():
        if (lo, hi) == (vlo, vhi):
            return int(v)
    raise ValueError(f"Period label {period_label!r} does not match any configured vintage period")


# ---------------------------------------------------------------------------
# Stage B: true fire x WUI intersection
# ---------------------------------------------------------------------------
def intersect_fires_wui(
    fires: gpd.GeoDataFrame,
    fragments: gpd.GeoDataFrame,
    min_intersection_area_km2: float,
    s2: dict,
) -> gpd.GeoDataFrame:
    """Geometric intersection of fire perimeters with WUI block fragments.

    Block fragments keep one geometry per (block, county) with per-vintage
    class columns, so each (fire, fragment) pair is intersected ONCE and the
    piece is attributed to each vintage where the block was WUI.

    Fires are restricted to 1990+ here: 1984-1989 fire years belong to the
    pre-WUI baseline and receive no WUI exposure (see assign_vintage).
    """
    if fires.crs is None or fragments.crs is None:
        raise ValueError("Both layers must have a defined CRS.")
    if fragments.crs.to_epsg() != CONUS_EQUAL_AREA_EPSG:
        raise ValueError(
            f"WUI fragments must be in EPSG:5070; got {fragments.crs}"
        )
    if fires.crs.to_epsg() != CONUS_EQUAL_AREA_EPSG:
        log.info("Reprojecting fires %s -> EPSG:5070", fires.crs)
        fires = fires.to_crs(CONUS_EQUAL_AREA_EPSG)

    pre_label = s2["pre_wui_baseline"]["label"]
    n_pre = int((fires["year"] <= s2["pre_wui_baseline"]["max_year"]).sum())
    if n_pre:
        log.info(
            "Excluding %d fires from the WUI intersection (years 1984-1989, "
            "pre-1990 baseline '%s'). Fire history for those years is retained "
            "in the county-year table with null WUI exposure.",
            n_pre, pre_label,
        )
        fires = fires[fires["year"] > s2["pre_wui_baseline"]["max_year"]].copy()

    vintages = [str(y) for y in s2["wui_vintages"]]
    frag_cols = ["BLK20", "GEOID", "fragment_area_km2"] + [f"wui_class_{v}" for v in vintages]
    frags = fragments[frag_cols + ["geometry"]].reset_index(drop=True)

    # Candidate pairs: fire bbox intersects fragment bbox.
    t0 = time.time()
    pairs = gpd.sjoin(
        fires[["fire_id", "year", "geometry"]].reset_index(drop=True),
        frags,
        how="inner",
        predicate="intersects",
    )
    log.info(
        "%d candidate (fire, WUI fragment) pairs from %d fires x %d fragments (%.0f s)",
        len(pairs), len(fires), len(frags), time.time() - t0,
    )

    fire_geom_arr = np.asarray(fires.geometry.values, dtype=object)
    frag_geom_arr = np.asarray(frags.geometry.values, dtype=object)
    fire_idx = pairs.index.to_numpy()
    frag_pos = pairs["index_right"].to_numpy()

    inter = shapely.intersection(fire_geom_arr[fire_idx], frag_geom_arr[frag_pos])
    inter = np.asarray([_polygonal(g) for g in inter], dtype=object)
    nonnone = np.array([g is not None for g in inter], dtype=bool)
    area_m2 = np.zeros(len(inter), dtype=float)
    if nonnone.any():
        area_m2[nonnone] = shapely.area(inter[nonnone])
    keep = (area_m2 * KM2_PER_M2) >= min_intersection_area_km2
    log.info(
        "Kept %d fire-WUI pieces >= %.6f km2 (dropped %d slivers/touches).",
        int(keep.sum()), min_intersection_area_km2, int((~keep).sum()),
    )

    kept = pairs.loc[keep].reset_index(drop=True)
    kept_geoms = [g for g, k in zip(inter, keep) if k]
    out = gpd.GeoDataFrame(
        {
            "fire_id": kept["fire_id"].to_numpy(),
            "year": kept["year"].to_numpy(),
            "GEOID": kept["GEOID"].to_numpy(),
            "BLK20": kept["BLK20"].to_numpy(),
            "fragment_area_km2": kept["fragment_area_km2"].to_numpy(),
            **{f"wui_class_{v}": kept[f"wui_class_{v}"].to_numpy() for v in vintages},
        },
        geometry=kept_geoms,
        crs=fragments.crs,
    )
    out["intersection_area_km2"] = shapely.area(
        np.asarray(out.geometry.values, dtype=object)
    ) * KM2_PER_M2
    return out


# ---------------------------------------------------------------------------
# County-year WUI fire table
# ---------------------------------------------------------------------------
def build_county_year_wui_fire(
    pieces: pd.DataFrame,
    county_year_stage1: pd.DataFrame,
    county_wui: pd.DataFrame,
    s2: dict,
) -> pd.DataFrame:
    """Join Stage 1 county-year fire history with vintage-matched WUI burns."""
    vintages = [str(y) for y in s2["wui_vintages"]]
    intermix = set(s2["silvis"]["intermix_classes"])
    interface = set(s2["silvis"]["interface_classes"])

    out = county_year_stage1.copy()
    out["assigned_wui_vintage"] = [
        assign_vintage(int(y), s2) for y in out["year"]
    ]

    # WUI burned area per (GEOID, year) for each vintage the pieces carry.
    per_vintage = {}
    for v in vintages:
        cls = pieces[f"wui_class_{v}"]
        wui_pieces = pieces[cls.notna()].copy()
        wui_pieces["is_interface"] = wui_pieces[f"wui_class_{v}"].isin(interface)
        wui_pieces["is_intermix"] = wui_pieces[f"wui_class_{v}"].isin(intermix)
        agg = (
            wui_pieces.groupby(["GEOID", "year"], as_index=False)
            .agg(
                wui_burned_area_km2=("intersection_area_km2", "sum"),
                interface_wui_burned_km2=(
                    "intersection_area_km2",
                    lambda s: float(s[wui_pieces.loc[s.index, "is_interface"]].sum()),
                ),
                intermix_wui_burned_km2=(
                    "intersection_area_km2",
                    lambda s: float(s[wui_pieces.loc[s.index, "is_intermix"]].sum()),
                ),
            )
        )
        per_vintage[int(v)] = agg

    # Attach the vintage-specific burn metrics to each county-year row.
    for metric in ("wui_burned_area_km2", "interface_wui_burned_km2",
                   "intermix_wui_burned_km2"):
        out[metric] = np.nan
    for v, agg in per_vintage.items():
        vintage_rows = out["assigned_wui_vintage"] == v
        merged = out.loc[vintage_rows, ["GEOID", "year"]].merge(
            agg, on=["GEOID", "year"], how="left"
        )
        for metric in ("wui_burned_area_km2", "interface_wui_burned_km2",
                       "intermix_wui_burned_km2"):
            out.loc[vintage_rows, metric] = merged[metric].fillna(0.0).to_numpy()
        # County-years inside a vintage period but with no fires get no row
        # here (they exist only if Stage 1 recorded fires that year), so no
        # zero-fill is needed beyond the merge.

    # Fill genuine zero-burn rows: any county-year with fires but no WUI burn.
    burn_known = out["assigned_wui_vintage"].apply(lambda x: isinstance(x, int))
    for metric in ("wui_burned_area_km2", "interface_wui_burned_km2",
                   "intermix_wui_burned_km2"):
        mask = burn_known & out[metric].isna()
        out.loc[mask, metric] = 0.0

    # Vintage-matched county WUI area.
    wui_area = county_wui[["GEOID", "wui_year", "wui_area_km2",
                            "interface_wui_area_km2", "intermix_wui_area_km2"]]
    out = out.merge(
        wui_area.rename(columns={"wui_year": "assigned_wui_vintage"}),
        on=["GEOID", "assigned_wui_vintage"],
        how="left",
    )
    out = out.rename(columns={"wui_area_km2_y": "wui_area_km2"})
    if "wui_area_km2_x" in out.columns:
        out = out.drop(columns=["wui_area_km2_x"])

    # Percent metrics. C: share of the county's vintage WUI that burned in
    # the year. D: share of that year's burned area that was WUI.
    out["pct_wui_burned_in_year"] = np.where(
        out["wui_area_km2"].notna() & (out["wui_area_km2"] > 0),
        out["wui_burned_area_km2"] / out["wui_area_km2"] * 100.0,
        np.nan,
    )
    out["pct_burned_area_that_was_wui"] = np.where(
        out["total_burned_area_km2"] > 0,
        out["wui_burned_area_km2"] / out["total_burned_area_km2"] * 100.0,
        np.nan,
    )

    # Rename Stage-1 column to the Stage-2 plan's requested name, keeping the
    # values identical (documented in docs/data_dictionary.md).
    out = out.rename(columns={"total_burned_area_km2": "burned_area_km2"})

    # One dtype per column so Parquet can round-trip. Pre-baseline rows keep
    # a null integer vintage; the label column carries the explicit marker.
    vintage, labels = assigned_vintage_storage(out["assigned_wui_vintage"])
    out["assigned_wui_vintage"] = vintage
    out["assigned_wui_vintage_label"] = labels

    cols = [
        "GEOID", "county_name", "state_fips", "state_name", "year",
        "assigned_wui_vintage", "assigned_wui_vintage_label",
        "fire_count", "burned_area_km2",
        "pct_county_burned_in_year", "county_area_km2", "wui_area_km2",
        "wui_burned_area_km2", "pct_wui_burned_in_year",
        "pct_burned_area_that_was_wui", "interface_wui_burned_km2",
        "intermix_wui_burned_km2",
    ]
    out = out[cols].sort_values(["GEOID", "year"]).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# County-period summary (1990-99, 2000-09, 2010-19, 2020-24)
# ---------------------------------------------------------------------------
def _unique_footprint(geoms) -> float:
    if len(geoms) == 0:
        return 0.0
    return float(shapely.area(shapely.union_all(list(geoms))) * KM2_PER_M2)


def build_county_period_wui_fire(
    pieces: gpd.GeoDataFrame,
    county_year_stage1: pd.DataFrame,
    county_wui: pd.DataFrame,
    s2: dict,
    fire_county_intersections: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """County x period summary with cumulative and unique WUI burn footprints.

    Cumulative WUI burned area sums perimeters per event (reburned land
    counts every time); the unique footprint dissolves the union of all
    fire-perimeter/WUI intersections in the period (reburned land counts
    once). Both are reported so reburning is explicit, never hidden.

    `fire_county_intersections` (Stage 1 fire x county fragments, carrying
    FIRE_ID + year) supplies exact distinct-fire counts per county; it is
    injectable for testing. When None it is read from the standard interim
    path.
    """
    period_rows = []
    for vintage_key, span in sorted(
        ((k, v) for k, v in s2["vintage_periods"].items()),
        key=lambda kv: kv[1]["min_year"],
    ):
        # Config keys are bare vintage years ("1990"); the human-readable
        # period label is derived from the configured year span.
        v = int(vintage_key)
        lo, hi = span["min_year"], span["max_year"]
        period_label = f"{lo}-{hi}"

        # --- fire history per county over the period ---------------------
        # Full county x period grid: every county in the county WUI table
        # appears in every period, zero-filled where it had no fires, so
        # GEOID x period is a complete unique key (mirrors the county-year
        # table's county coverage; Stage 1 county-years only cover fire years).
        counties_key = county_wui[["GEOID"]].drop_duplicates("GEOID")
        cy = county_year_stage1[
            county_year_stage1["year"].between(lo, hi)
        ]
        hist = (
            cy.groupby("GEOID", as_index=False)
            .agg(
                fire_count=("fire_count", "sum"),      # sum of per-year distinct counts below
                n_fire_years=("year", "nunique"),
                burned_area_km2=("total_burned_area_km2", "sum"),
            )
        )
        hist = counties_key.merge(hist, on="GEOID", how="left")
        hist["n_fire_years"] = hist["n_fire_years"].fillna(0).astype("int64")
        hist["fire_count"] = hist["fire_count"].fillna(0).astype("int64")
        hist["burned_area_km2"] = hist["burned_area_km2"].fillna(0.0)
        # Distinct fires per county over the period requires the fragment
        # table's fire ids (county-year table does not carry them).
        # Distinct-fire counting is exact via the fire-county intersections
        # when available; fall back to summing per-year counts would
        # double-count multi-year... single-year fires, so instead compute
        # from the full intersections below.
        period_rows.append((period_label, v, lo, hi, hist))

    # Distinct fires per (county, period) from the county-fire fragments.
    if fire_county_intersections is None:
        ints = gpd.read_parquet(INTERIM_DIR / "fires_county_intersections.geoparquet")
    else:
        ints = fire_county_intersections
    out_frames = []
    for period_label, v, lo, hi, hist in period_rows:
        sub_int = ints[ints["year"].between(lo, hi)]
        distinct = (
            sub_int.groupby("GEOID", as_index=False)
            .agg(distinct_fires=("FIRE_ID", "nunique"))
        )
        merged = hist.drop(columns=["fire_count"]).merge(
            distinct, on="GEOID", how="left"
        )
        merged["distinct_fires"] = merged["distinct_fires"].fillna(0).astype("int64")

        # --- WUI burn pieces for this period/vintage --------------------
        cls_col = f"wui_class_{v}"
        sub_pieces = pieces[pieces[f"wui_class_{str(v)}"].notna() &
                            pieces["year"].between(lo, hi)]

        cum = (
            sub_pieces.groupby("GEOID", as_index=False)
            .agg(
                wui_burned_area_km2=("intersection_area_km2", "sum"),
            )
        )
        merged = merged.merge(cum, on="GEOID", how="left")

        # Unique dissolved footprints per county (all WUI / interface / intermix).
        intermix = set(s2["silvis"]["intermix_classes"])
        interface = set(s2["silvis"]["interface_classes"])
        foot_all: dict[str, float] = {}
        foot_if: dict[str, float] = {}
        foot_im: dict[str, float] = {}
        for geoid, grp in sub_pieces.groupby("GEOID"):
            foot_all[geoid] = _unique_footprint(grp.geometry.values)
            if_geom = grp[grp[cls_col].isin(interface)].geometry.values
            im_geom = grp[grp[cls_col].isin(intermix)].geometry.values
            foot_if[geoid] = _unique_footprint(if_geom)
            foot_im[geoid] = _unique_footprint(im_geom)
        foot = pd.DataFrame(
            {
                "GEOID": pd.Series(list(foot_all.keys()), dtype=object),
                "unique_wui_burned_footprint_km2": pd.Series(
                    list(foot_all.values()), dtype=float
                ),
                "unique_interface_wui_burned_km2": pd.Series(
                    [foot_if[g] for g in foot_all], dtype=float
                ),
                "unique_intermix_wui_burned_km2": pd.Series(
                    [foot_im[g] for g in foot_all], dtype=float
                ),
            }
        )
        merged = merged.merge(foot, on="GEOID", how="left")

        merged["period"] = period_label
        merged["assigned_wui_vintage"] = int(v)
        merged["assigned_wui_vintage_label"] = str(int(v))
        out_frames.append(merged)

    out = pd.concat(out_frames, ignore_index=True)
    for c in ("wui_burned_area_km2", "unique_wui_burned_footprint_km2",
              "unique_interface_wui_burned_km2", "unique_intermix_wui_burned_km2"):
        out[c] = out[c].fillna(0.0)

    # Vintage-matched county WUI area for percentage metrics.
    wui_area = county_wui[["GEOID", "wui_year", "wui_area_km2"]]
    out = out.merge(
        wui_area.rename(columns={"wui_year": "assigned_wui_vintage"}),
        on=["GEOID", "assigned_wui_vintage"],
        how="left",
    )
    out["pct_wui_burned_footprint_in_period"] = np.where(
        out["wui_area_km2"] > 0,
        out["unique_wui_burned_footprint_km2"] / out["wui_area_km2"] * 100.0,
        np.nan,
    )
    out["pct_burned_area_that_was_wui"] = np.where(
        out["burned_area_km2"] > 0,
        out["wui_burned_area_km2"] / out["burned_area_km2"] * 100.0,
        np.nan,
    )

    # Every period has a SILVIS snapshot, so this column is non-null. Int64
    # matches the county-year vintage dtype (no mixed label strings).
    out["assigned_wui_vintage"] = out["assigned_wui_vintage"].astype("Int64")
    out["assigned_wui_vintage_label"] = out["assigned_wui_vintage_label"].astype("string")

    cols = [
        "GEOID", "period", "assigned_wui_vintage", "assigned_wui_vintage_label",
        "n_fire_years",
        "distinct_fires", "burned_area_km2", "wui_area_km2",
        "wui_burned_area_km2", "unique_wui_burned_footprint_km2",
        "pct_wui_burned_footprint_in_period", "pct_burned_area_that_was_wui",
        "unique_interface_wui_burned_km2", "unique_intermix_wui_burned_km2",
    ]
    out = out[cols].sort_values(["GEOID", "period"]).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    cfg = load_analysis_config()
    s2 = load_stage2_config()
    t_start = time.time()

    county_year_stage1 = pd.read_parquet(PROCESSED_DIR / "county_year_wildfire.parquet")
    county_wui = pd.read_parquet(PROCESSED_DIR / "county_wui_by_vintage.parquet")

    if FIRE_WUI_PIECES_PATH.exists():
        log.info("Reusing existing fire-WUI intersection pieces: %s",
                 FIRE_WUI_PIECES_PATH)
        pieces = gpd.read_parquet(FIRE_WUI_PIECES_PATH)
        log.info(
            "Loaded %d cached fire-WUI pieces and %d Stage-1 county-year rows.",
            len(pieces), len(county_year_stage1),
        )
    else:
        fires = gpd.read_parquet(PROCESSED_DIR / "mtbs_wildfires_conus.geoparquet")
        fragments = gpd.read_parquet(FRAGMENTS_PATH)
        log.info(
            "Loaded %d fires, %d WUI fragments, %d Stage-1 county-year rows.",
            len(fires), len(fragments), len(county_year_stage1),
        )
        pieces = intersect_fires_wui(
            fires, fragments,
            min_intersection_area_km2=float(cfg["intersection"]["min_intersection_area_km2"]),
            s2=s2,
        )
        pieces.to_parquet(FIRE_WUI_PIECES_PATH, index=False)
        log.info("Wrote %d fire-WUI pieces to %s", len(pieces), FIRE_WUI_PIECES_PATH)

    log.info(
        "Fire-WUI pieces: %d fires with WUI overlap, %d counties affected, "
        "%.0f km2 of vintage-specific WUI burned (max piece %.2f km2).",
        pieces["fire_id"].nunique(), pieces["GEOID"].nunique(),
        pieces["intersection_area_km2"].sum(), pieces["intersection_area_km2"].max(),
    )

    t0 = time.time()
    county_year = build_county_year_wui_fire(
        pieces, county_year_stage1, county_wui, s2
    )
    county_year.to_parquet(COUNTY_YEAR_OUT, index=False)
    county_year.to_csv(TABLES_DIR / "county_year_wui_fire.csv", index=False)
    log.info(
        "Wrote %d county-year WUI fire rows (%.0f s).",
        len(county_year), time.time() - t0,
    )

    t0 = time.time()
    county_period = build_county_period_wui_fire(
        pieces, county_year_stage1, county_wui, s2
    )
    county_period.to_parquet(COUNTY_PERIOD_OUT, index=False)
    county_period.to_csv(TABLES_DIR / "county_period_wui_fire.csv", index=False)
    log.info(
        "Wrote %d county-period rows (%.0f s). Total stage runtime %.0f s.",
        len(county_period), time.time() - t0, time.time() - t_start,
    )
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("wui_fire.log")
    sys.exit(main())