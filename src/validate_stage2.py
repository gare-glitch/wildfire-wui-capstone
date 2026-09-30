"""Automated validation suite for Stage 2 (WUI) outputs.

Follows the Stage 1 validation philosophy (src/validate_data.py): every check
is a pure function over explicit inputs so pytest can exercise each on
synthetic data. The runner loads Stage 2 artifacts, runs all checks, then
writes a COMBINED report:

- outputs/tables/data_validation_report.csv  (Stage 1 rows, if present, +
  Stage 2 rows with distinct check-id prefixes)
- outputs/tables/validation_summary.md       (Stage 1 + Stage 2 sections)

Exits non-zero on any CRITICAL failure (fail loudly, AGENTS.md #9).

Check groups
------------
sch-* schema/domain : WUI classes within documented domains
wui-* county WUI    : area bounds, pct bounds, composition, uniqueness
tmp-* temporal      : vintage-year alignment, pre-1990 baseline handling
wf-*  WUI x fire    : burn hierarchies (WUI burned <= burned <= county)
chg-* WUI change    : change-table internal consistency
nat-* Nature 2020   : raster-derived area bounds and composition
cmp-* comparison    : SILVIS vs Nature structural separation
plc-* places        : place outputs, separation from counties
geo-* CRS           : no CRS mismatch; no area math in geographic CRS
prv-* provenance    : Stage 2 datasets recorded and validated
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    PROVENANCE_PATH,
    TABLES_DIR,
    WUI_DIR,
    ensure_dirs,
    get_logger,
    load_stage2_config,
    setup_logging,
)
from validate_data import (  # noqa: E402
    CRITICAL,
    FAIL,
    INFO,
    INFO_STATUS,
    PASS,
    WARN,
    WARNING,
    CheckResult,
    _status_for,
)

log = get_logger("validate_stage2")

TOL_KM2 = 0.5          # absolute slack for boundary effects, km2
TOL_REL = 0.02         # relative slack vs county area
NATURE_TOL_REL = 0.05  # raster pixelization slack vs vector county area


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _crs_from_geoparquet(path: Path):
    """Read the CRS recorded in GeoParquet metadata without loading geometry."""
    import pyarrow.parquet as pq

    meta = pq.read_schema(path).metadata or {}
    geo = meta.get(b"geo")
    if geo is None:
        return None
    try:
        parsed = json.loads(geo.decode("utf-8"))
    except Exception:
        return None
    columns = parsed.get("columns") or {}
    for col in columns.values():
        if col.get("crs"):
            return col["crs"]
    return None


def _epsg_of(crs_obj):
    """Best-effort EPSG code from a GeoParquet CRS metadata object."""
    if crs_obj is None:
        return None
    if isinstance(crs_obj, dict):
        # pyproj CRS JSON: {"type": "ProjectedCRS", ..., "id": {"authority": "EPSG", "code": 5070}}
        ident = crs_obj.get("id") or {}
        if "code" in ident:
            return int(ident["code"])
        # WKT2 dict? pyproj json has "id"
        return None
    from pyproj import CRS

    try:
        return CRS.from_user_input(crs_obj).to_epsg()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# sch-* schema / domain checks
# ---------------------------------------------------------------------------
def check_silvis_class_domain(schema_report: dict, s2: dict) -> CheckResult:
    """Observed SILVIS WUICLASS values must lie in the documented 14-value
    domain; observed WUIFLAG values must be {0, 1, 2}."""
    domain = set(s2["silvis"]["class_domain"])
    problems = []
    if schema_report.get("flag_class_conflict_rows", 0) != 0:
        problems.append("flag/class conflicts present")
    if schema_report.get("fips_blk20_mismatch_rows", 0) != 0:
        problems.append("FIPS != BLK20 county prefix")
    observed = schema_report.get("observed_domains", {})
    n_class_values = 0
    for v, dom in observed.items():
        for cls in dom.get("class", {}):
            n_class_values += 1
            if cls not in domain:
                problems.append(f"vintage {v}: undocumented class {cls!r}")
        for flag in dom.get("flag", {}):
            if flag not in {"0", "1", "2", "<NA>"}:
                problems.append(f"vintage {v}: unexpected flag {flag!r}")
    return CheckResult(
        check_id="sch-01",
        name="SILVIS WUI class/flag domains match documentation",
        dataset="silvis_schema",
        severity=CRITICAL,
        status=_status_for(CRITICAL, not problems),
        details=(
            f"{n_class_values} observed class values across vintages; "
            + ("; ".join(problems[:8]) if problems else "all within the documented domain")
        ),
        metrics={"n_problems": len(problems)},
    )


def check_nature_class_domain(schema_report: dict, s2: dict) -> CheckResult:
    """Observed Nature raster class values must lie in the documented 1..8 domain.

    VRT NoDataValue for the Schug 2023 NA mosaic is 255 (not 0). Nodata and
    unused 0 are excluded from the domain check when present in the schema.
    """
    valid = set(int(v) for v in s2["nature"]["valid_values"])
    observed = set(schema_report.get("observed_class_values", []))
    extra_ok = {0}
    nodata = schema_report.get("nodata_value")
    if nodata is not None:
        extra_ok.add(int(nodata))
    bad = sorted(observed - valid - extra_ok)
    return CheckResult(
        check_id="sch-02",
        name="Nature WUI raster classes within documented 1-8 domain",
        dataset="nature_schema",
        severity=CRITICAL,
        status=_status_for(CRITICAL, not bad),
        details=f"observed {sorted(observed)}; outside domain: {bad or 'none'}",
        metrics={"n_outside": len(bad)},
    )


# ---------------------------------------------------------------------------
# wui-* county WUI metric checks
# ---------------------------------------------------------------------------
def check_wui_area_nonnegative(county_wui: pd.DataFrame) -> CheckResult:
    area_cols = [c for c in county_wui.columns if c.endswith("_km2")]
    n_bad = int(sum((county_wui[c] < 0).sum() for c in area_cols))
    return CheckResult(
        check_id="wui-01",
        name="County WUI areas non-negative",
        dataset="county_wui_by_vintage",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n_bad == 0),
        details=f"{n_bad} negative area values across {area_cols}",
        metrics={"n_negative": n_bad},
    )


def check_wui_area_le_county(county_wui: pd.DataFrame) -> CheckResult:
    """WUI area <= county area within documented tolerance
    (absolute 0.5 km2 OR 2% of county area, whichever is larger)."""
    slack = np.maximum(TOL_KM2, TOL_REL * county_wui["county_area_km2"])
    bad = county_wui[
        county_wui["wui_area_km2"] > county_wui["county_area_km2"] + slack
    ]
    return CheckResult(
        check_id="wui-02",
        name="County WUI area <= county area (tolerance)",
        dataset="county_wui_by_vintage",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=(
            f"{len(bad)} county-vintages exceed county area beyond tolerance; "
            f"max pct_county_wui={county_wui['pct_county_wui'].max():.2f}%"
            + (f"; worst: {bad['GEOID'].tolist()[:5]}" if len(bad) else "")
        ),
        metrics={"n_violations": len(bad)},
    )


def check_wui_pct_bounds(county_wui: pd.DataFrame, max_pct: float) -> CheckResult:
    pct_cols = ["pct_county_wui", "pct_wui_interface", "pct_wui_intermix",
                "pct_of_wui_that_is_interface", "pct_of_wui_that_is_intermix",
                "pct_county_hu_in_wui"]
    pct_cols = [c for c in pct_cols if c in county_wui.columns]
    neg = int(sum((county_wui[c] < -1e-9).sum() for c in pct_cols))
    over = int(sum((county_wui[c] > max_pct + 1e-6).sum() for c in pct_cols))
    return CheckResult(
        check_id="wui-03",
        name="County WUI percentages within [0, 100]",
        dataset="county_wui_by_vintage",
        severity=CRITICAL,
        status=_status_for(CRITICAL, neg == 0 and over == 0),
        details=f"{neg} negative; {over} above {max_pct}% across {pct_cols}",
        metrics={"n_negative": neg, "n_over": over},
    )


def check_wui_composition_consistency(county_wui: pd.DataFrame) -> CheckResult:
    """interface + intermix must equal total WUI area within tolerance."""
    resid = (
        county_wui["interface_wui_area_km2"] + county_wui["intermix_wui_area_km2"]
        - county_wui["wui_area_km2"]
    ).abs()
    tol = np.maximum(1e-6, TOL_REL * county_wui["wui_area_km2"])
    bad = county_wui[resid > tol]
    return CheckResult(
        check_id="wui-04",
        name="interface + intermix == WUI area (SILVIS)",
        dataset="county_wui_by_vintage",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=(
            f"{len(bad)} county-vintages where the split does not sum to the "
            f"total; max residual {float(resid.max()):.4f} km2"
        ),
        metrics={"n_violations": len(bad)},
    )


def check_county_vintage_uniqueness(county_wui: pd.DataFrame, s2: dict) -> CheckResult:
    """One row per GEOID x wui_year; every county covered in every vintage."""
    n = len(county_wui)
    uniq = len(county_wui.drop_duplicates(subset=["GEOID", "wui_year"]))
    n_counties = county_wui["GEOID"].nunique()
    n_v = len(s2["wui_vintages"])
    complete = n == n_counties * n_v
    return CheckResult(
        check_id="wui-05",
        name="County WUI table: unique GEOID x vintage, complete grid",
        dataset="county_wui_by_vintage",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n == uniq and complete),
        details=(
            f"{n} rows, {uniq} unique (GEOID, vintage); "
            f"{n_counties} counties x {n_v} vintages expected"
        ),
        metrics={"n_rows": n, "n_unique": uniq, "n_counties": n_counties},
    )


# ---------------------------------------------------------------------------
# tmp-* temporal alignment checks
# ---------------------------------------------------------------------------
def _vintage_token(value) -> str:
    """Comparable text for a vintage cell: null -> '', int -> '1990', str kept."""
    if isinstance(value, str):
        return value
    if value is None or pd.isna(value):
        return ""
    return str(int(value))


def _parsed_vintage_int(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Nullable int plus a mask of non-numeric text leaked into the int column.

    Digit-strings are accepted (Parquet may surface an int as text). The
    pre-baseline label stored in this column is a leak: pre-baseline rows
    are valid only when the integer vintage is null.
    """
    values = []
    leaked = []
    for value in series.tolist():
        if isinstance(value, str):
            if value.isdigit():
                values.append(int(value))
                leaked.append(False)
            else:
                values.append(pd.NA)
                leaked.append(True)
        elif value is None or pd.isna(value):
            values.append(pd.NA)
            leaked.append(False)
        else:
            values.append(int(value))
            leaked.append(False)
    return (
        pd.Series(values, index=series.index, dtype="Int64"),
        pd.Series(leaked, index=series.index),
    )


def check_vintage_matches_year(county_year_wui: pd.DataFrame, s2: dict) -> CheckResult:
    """assigned_wui_vintage must equal the period mapping for every row.

    1984-1989 rows are valid when the integer vintage is null and
    assigned_wui_vintage_label is the pre-baseline marker. A label string
    sitting in the integer column is not valid.
    """
    from build_wui_fire import assign_vintage, assigned_vintage_storage

    expected = pd.Series(
        [assign_vintage(int(y), s2) for y in county_year_wui["year"]],
        index=county_year_wui.index,
    )
    exp_int, exp_label = assigned_vintage_storage(expected)
    got_int, leaked = _parsed_vintage_int(county_year_wui["assigned_wui_vintage"])
    both_null = got_int.isna() & exp_int.isna()
    int_match = (got_int.eq(exp_int).fillna(False) | both_null) & ~leaked
    if "assigned_wui_vintage_label" not in county_year_wui.columns:
        label_match = pd.Series(False, index=county_year_wui.index)
    else:
        label_match = (
            county_year_wui["assigned_wui_vintage_label"].map(_vintage_token)
            == exp_label.map(_vintage_token)
        )
    n_bad = int((~(int_match & label_match)).sum())
    return CheckResult(
        check_id="tmp-01",
        name="Assigned WUI vintage matches fire year (period mapping)",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n_bad == 0),
        details=(
            f"{n_bad} rows whose assigned_wui_vintage disagrees with the "
            "configured period mapping"
        ),
        metrics={"n_mismatch": n_bad},
    )


def check_no_2020_wui_on_earlier_years(county_year_wui: pd.DataFrame) -> CheckResult:
    """No row before 2020 may use the 2020 vintage — the exact failure mode
    the temporal design forbids."""
    vintage = county_year_wui["assigned_wui_vintage"].map(_vintage_token)
    bad = county_year_wui[(county_year_wui["year"] < 2020) & (vintage == "2020")]
    return CheckResult(
        check_id="tmp-02",
        name="No 2020 WUI silently applied to pre-2020 fire years",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=(
            f"{len(bad)} rows with year<2020 assigned the 2020 vintage"
            + (f": {bad[['GEOID', 'year']].head(5).to_dict('records')}" if len(bad) else "")
        ),
        metrics={"n_violations": len(bad)},
    )


def check_pre_baseline_nulls(county_year_wui: pd.DataFrame, s2: dict) -> CheckResult:
    """1984-1989: null integer vintage, baseline label, null WUI exposure."""
    label = s2["pre_wui_baseline"]["label"]
    pre = county_year_wui[county_year_wui["year"] < s2["pre_wui_baseline"]["max_year"] + 1]
    wrong_vintage = int(pre["assigned_wui_vintage"].notna().sum())
    if "assigned_wui_vintage_label" in pre.columns:
        wrong_label = int(
            (pre["assigned_wui_vintage_label"].map(_vintage_token) != label).sum()
        )
    else:
        wrong_label = int(len(pre))
    not_null = int(
        pre[["wui_area_km2", "wui_burned_area_km2"]].notna().any(axis=1).sum()
    )
    # Aligned rows (non-null vintage) must carry a vintage-matched WUI area.
    aligned = county_year_wui[county_year_wui["assigned_wui_vintage"].notna()]
    missing_area = int(aligned["wui_area_km2"].isna().sum()) if len(aligned) else 0
    wrong_label = wrong_label + wrong_vintage
    return CheckResult(
        check_id="tmp-03",
        name="1984-1989 rows are pre-WUI baseline with null exposure",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, wrong_label == 0 and not_null == 0 and missing_area == 0),
        details=(
            f"{len(pre)} pre-1990 rows: {wrong_label} mislabeled, {not_null} "
            f"with non-null WUI values; {missing_area} aligned rows missing "
            "vintage-matched WUI area"
        ),
        metrics={"n_pre_rows": len(pre), "n_mislabeled": wrong_label,
                 "n_not_null": not_null},
    )


def check_vintage_wui_area_matches(county_year_wui: pd.DataFrame,
                                   county_wui: pd.DataFrame) -> CheckResult:
    """The wui_area_km2 joined into each row must equal the county WUI area
    of that row's assigned vintage (no cross-vintage leakage)."""
    lookup = county_wui.set_index(["GEOID", "wui_year"])["wui_area_km2"]
    aligned = county_year_wui[county_year_wui["wui_area_km2"].notna()].copy()
    aligned["v"] = aligned["assigned_wui_vintage"].astype(int)
    expected = pd.to_numeric(
        pd.Series(
            [lookup.get((g, v)) for g, v in zip(aligned["GEOID"], aligned["v"])],
            index=aligned.index,
        ),
        errors="coerce",
    )
    diff = (aligned["wui_area_km2"] - expected).abs()
    n_bad = int(((diff > 1e-6) & expected.notna()).sum())
    n_missing = int(expected.isna().sum())
    return CheckResult(
        check_id="tmp-04",
        name="Row WUI area equals its assigned vintage's county WUI area",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n_bad == 0),
        details=(
            f"{n_bad} rows whose wui_area_km2 is not the vintage-matched value"
            + (f"; {n_missing} rows with no county-vintage lookup" if n_missing else "")
        ),
        metrics={"n_mismatch": n_bad, "n_missing_lookup": n_missing},
    )


# ---------------------------------------------------------------------------
# wf-* WUI x fire checks
# ---------------------------------------------------------------------------
def check_wui_burn_le_burned(county_year_wui: pd.DataFrame) -> CheckResult:
    bad = county_year_wui[
        county_year_wui["wui_burned_area_km2"]
        > county_year_wui["burned_area_km2"] * (1 + 1e-6) + TOL_KM2
    ]
    return CheckResult(
        check_id="wf-01",
        name="WUI burned area <= total burned area (county-year)",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=f"{len(bad)} county-years where WUI burn exceeds total burn",
        metrics={"n_violations": len(bad)},
    )


def check_wui_burn_le_wui_area(county_year_wui: pd.DataFrame) -> CheckResult:
    """Per single year, WUI burned cannot exceed the vintage's WUI area
    (multi-year reburn may exceed it; per-year rows may not)."""
    bad = county_year_wui[
        (county_year_wui["wui_area_km2"].notna())
        & (county_year_wui["wui_burned_area_km2"]
           > county_year_wui["wui_area_km2"] * (1 + TOL_REL) + TOL_KM2)
    ]
    return CheckResult(
        check_id="wf-02",
        name="Annual WUI burned area <= county WUI area",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=(
            f"{len(bad)} county-years where annual WUI burn exceeds the "
            "vintage-matched WUI area"
        ),
        metrics={"n_violations": len(bad)},
    )


def check_wui_burn_composition(county_year_wui: pd.DataFrame) -> CheckResult:
    """interface burned + intermix burned == WUI burned within tolerance."""
    both = county_year_wui[county_year_wui["wui_burned_area_km2"].notna()]
    resid = (
        both["interface_wui_burned_km2"].fillna(0)
        + both["intermix_wui_burned_km2"].fillna(0)
        - both["wui_burned_area_km2"]
    ).abs()
    bad = int((resid > np.maximum(1e-6, TOL_REL * both["wui_burned_area_km2"])).sum())
    return CheckResult(
        check_id="wf-03",
        name="interface + intermix burn == WUI burn (county-year)",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, bad == 0),
        details=f"{bad} rows where the interface/intermix split does not sum",
        metrics={"n_violations": bad},
    )


def check_county_year_uniqueness(county_year_wui: pd.DataFrame) -> CheckResult:
    n, uniq = len(county_year_wui), len(
        county_year_wui.drop_duplicates(subset=["GEOID", "year"])
    )
    return CheckResult(
        check_id="wf-04",
        name="No duplicate GEOID x year rows",
        dataset="county_year_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n == uniq),
        details=f"{uniq} unique county-years over {n} rows",
        metrics={"n_rows": n, "n_unique": uniq},
    )


def check_period_table(county_period: pd.DataFrame, s2: dict) -> CheckResult:
    """Four periods, unique GEOID x period, unique footprint <= WUI area."""
    expected_periods = {
        f"{span['min_year']}-{span['max_year']}" for span in s2["vintage_periods"].values()
    }
    got = set(county_period["period"].unique())
    n, uniq = len(county_period), len(
        county_period.drop_duplicates(subset=["GEOID", "period"])
    )
    bad_fp = county_period[
        county_period["unique_wui_burned_footprint_km2"]
        > county_period["wui_area_km2"] * (1 + TOL_REL) + TOL_KM2
    ]
    ok = got == expected_periods and n == uniq and len(bad_fp) == 0
    return CheckResult(
        check_id="wf-05",
        name="County-period table: 4 periods, unique keys, footprint <= WUI",
        dataset="county_period_wui_fire",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"periods {sorted(got)} (expected {sorted(expected_periods)}); "
            f"{n} rows / {uniq} unique; {len(bad_fp)} footprint violations"
        ),
        metrics={"n_rows": n, "n_footprint_violations": len(bad_fp)},
    )


# ---------------------------------------------------------------------------
# chg-* WUI change checks
# ---------------------------------------------------------------------------
def check_change_table(county_change: pd.DataFrame, s2: dict) -> CheckResult:
    """All four transitions present per county; keys unique."""
    expected_t = {(1990, 2000), (2000, 2010), (2010, 2020), (1990, 2020)}
    got_t = set(zip(county_change["base_year"], county_change["end_year"]))
    n_counties = county_change["GEOID"].nunique()
    dup = len(county_change) - len(
        county_change.drop_duplicates(subset=["GEOID", "base_year", "end_year"])
    )
    # Absolute change must reconcile with the endpoint vintages carried in-row.
    resid = (
        county_change["wui_area_km2_new"] - county_change["wui_area_km2_old"]
        - county_change["wui_area_change_km2"]
    ).abs()
    n_resid = int((resid > 1e-6).sum())
    # interface + intermix change must sum to total change (within tolerance)
    comp = (
        county_change["interface_wui_area_change_km2"]
        + county_change["intermix_wui_area_change_km2"]
        - county_change["wui_area_change_km2"]
    ).abs()
    n_comp = int((comp > np.maximum(1e-6, TOL_REL * county_change["wui_area_km2_new"])).sum())
    ok = got_t == expected_t and dup == 0 and n_resid == 0 and n_comp == 0
    return CheckResult(
        check_id="chg-01",
        name="WUI change table: transitions complete, unique, internally consistent",
        dataset="county_wui_change",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"transitions {sorted(got_t)}; {n_counties} counties; {dup} "
            f"duplicate keys; {n_resid} endpoint mismatches; {n_comp} "
            "composition mismatches"
        ),
        metrics={"n_transitions": len(got_t), "n_duplicates": dup,
                 "n_endpoint_mismatch": n_resid, "n_composition_mismatch": n_comp},
    )


# ---------------------------------------------------------------------------
# nat-* Nature 2020 checks
# ---------------------------------------------------------------------------
def check_nature_areas(county_nature: pd.DataFrame) -> CheckResult:
    n_neg = int((county_nature["nature_wui_area_km2"] < 0).sum())
    slack = np.maximum(
        TOL_KM2, NATURE_TOL_REL * county_nature["county_area_km2"]
    )
    n_over = int(
        (county_nature["nature_wui_area_km2"]
         > county_nature["county_area_km2"] + slack).sum()
    )
    resid = (
        county_nature["nature_interface_wui_area_km2"]
        + county_nature["nature_intermix_wui_area_km2"]
        - county_nature["nature_wui_area_km2"]
    ).abs()
    n_comp = int((resid > np.maximum(1e-6, NATURE_TOL_REL * county_nature["nature_wui_area_km2"])).sum())
    ok = n_neg == 0 and n_over == 0 and n_comp == 0
    return CheckResult(
        check_id="nat-01",
        name="Nature WUI areas: non-negative, <= county, composition sums",
        dataset="county_nature_wui_2020",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"{n_neg} negative, {n_over} beyond county area (+{NATURE_TOL_REL:.0%} "
            f"raster slack), {n_comp} composition violations"
        ),
        metrics={"n_negative": n_neg, "n_over_county": n_over,
                 "n_composition": n_comp},
    )


def check_nature_coverage(county_nature: pd.DataFrame) -> CheckResult:
    """Raster coverage diagnostic: most counties should be well covered by
    classified raster pixels; systematic under-coverage would distort
    comparisons."""
    cov = county_nature["pct_county_covered_by_nature_raster"]
    n_low = int((cov < 90).sum())
    med = float(cov.median()) if len(cov) else 0.0
    status = INFO_STATUS if n_low == 0 else WARN
    return CheckResult(
        check_id="nat-02",
        name="Nature raster coverage per county (diagnostic)",
        dataset="county_nature_wui_2020",
        severity=WARNING,
        status=status,
        details=(
            f"median coverage {med:.1f}% of legal county area; {n_low} counties "
            "below 90%. Coverage tracks Census water; counties still below 90% "
            "of ALAND are listed in outputs/tables/nature_coverage_land_below_90.csv "
            "(raster value 0 on land, not a passed check)."
        ),
        metrics={"median_coverage_pct": round(med, 1), "n_under_90": n_low},
    )


# ---------------------------------------------------------------------------
# cmp-* comparison checks
# ---------------------------------------------------------------------------
def check_comparison_separation(comparison: pd.DataFrame, summary: dict) -> CheckResult:
    """SILVIS and Nature values must live in separate, clearly named columns;
    correlations must be finite and recorded."""
    problems = []
    for col in ("silvis_wui_area_km2", "nature_wui_area_km2",
                "pct_county_silvis_wui", "pct_county_nature_wui"):
        if col not in comparison.columns:
            problems.append(f"missing column {col}")
    corr = summary.get("area_correlation_all_counties", {})
    if not (isinstance(corr.get("pearson"), (int, float))
            and isinstance(corr.get("spearman"), (int, float))):
        problems.append("correlations missing from summary")
    note = summary.get("methodological_note", "")
    if not note:
        problems.append("no methodological note distinguishing definitions")
    return CheckResult(
        check_id="cmp-01",
        name="SILVIS vs Nature: separate columns, finite correlations, distinct definitions",
        dataset="county_wui_silvis_vs_nature_2020",
        severity=CRITICAL,
        status=_status_for(CRITICAL, not problems),
        details=(
            "separate silvis_/nature_ columns present; "
            f"Pearson r={corr.get('pearson')}, Spearman rho={corr.get('spearman')}"
            + ("; " + "; ".join(problems) if problems else "")
        ),
        metrics={"n_problems": len(problems)},
    )


# ---------------------------------------------------------------------------
# plc-* place checks
# ---------------------------------------------------------------------------
def check_place_keys(place_wui: pd.DataFrame) -> CheckResult:
    bad = int((~place_wui["GEOID"].astype(str).str.fullmatch(r"\d{7}")).sum())
    dup = len(place_wui) - len(place_wui.drop_duplicates(subset=["GEOID", "wui_year"]))
    return CheckResult(
        check_id="plc-01",
        name="Place GEOID format (7-digit) and key uniqueness",
        dataset="place_wui_by_vintage",
        severity=CRITICAL,
        status=_status_for(CRITICAL, bad == 0 and dup == 0),
        details=f"{bad} malformed place GEOIDs; {dup} duplicate (GEOID, vintage) rows",
        metrics={"n_malformed": bad, "n_duplicates": dup},
    )


def check_place_wui_bounds(place_wui: pd.DataFrame) -> CheckResult:
    slack = np.maximum(TOL_KM2, TOL_REL * place_wui["place_area_km2"])
    bad = int((place_wui["wui_area_km2"] > place_wui["place_area_km2"] + slack).sum())
    n_neg = int((place_wui["wui_area_km2"] < 0).sum())
    return CheckResult(
        check_id="plc-02",
        name="Place WUI area within [0, place area]",
        dataset="place_wui_by_vintage",
        severity=CRITICAL,
        status=_status_for(CRITICAL, bad == 0 and n_neg == 0),
        details=f"{bad} places with WUI > place area; {n_neg} negative",
        metrics={"n_over": bad, "n_negative": n_neg},
    )


def check_place_exposure(place_exposure: pd.DataFrame, s2: dict) -> CheckResult:
    label = s2["pre_wui_baseline"]["label"]
    if "assigned_wui_vintage_label" in place_exposure.columns:
        pre_mask = place_exposure["assigned_wui_vintage_label"].map(_vintage_token) == label
    else:
        pre_mask = place_exposure["assigned_wui_vintage"].map(_vintage_token) == label
    pre_mask = pre_mask.fillna(False)
    pre = place_exposure[pre_mask]
    aligned = place_exposure[~pre_mask]
    exposure_null = not pre[["wui_area_km2", "wui_burned_area_km2"]].notna().any().any()
    vintage_null = bool(pre["assigned_wui_vintage"].isna().all()) if len(pre) else True
    pre_null_ok = exposure_null and vintage_null
    burn_ok = int((
        aligned["wui_burned_area_km2"].fillna(0)
        > aligned["burned_area_km2"] * (1 + 1e-6) + TOL_KM2
    ).sum()) if len(aligned) else 0
    if len(aligned):
        got = aligned["assigned_wui_vintage"].map(_vintage_token)
        expected = [
            _vintage_token(s2_lookup(int(y))) for y in aligned["year"]
        ]
        vintage_ok = int((got.to_numpy() != np.asarray(expected, dtype=object)).sum())
    else:
        vintage_ok = 0
    ok = pre_null_ok and burn_ok == 0 and vintage_ok == 0
    return CheckResult(
        check_id="plc-03",
        name="Place exposure: baseline nulls, burn hierarchy, vintage mapping",
        dataset="place_wui_fire_exposure",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"pre-baseline null exposure: {pre_null_ok}; {burn_ok} burn-hierarchy "
            f"violations; {vintage_ok} vintage mismatches"
        ),
        metrics={"n_burn_violations": burn_ok, "n_vintage_mismatch": vintage_ok},
    )


def s2_lookup(year: int):
    from build_wui_fire import assign_vintage
    from common import load_stage2_config as _l

    return assign_vintage(int(year), _l())


def check_place_county_separation(place_wui: pd.DataFrame) -> CheckResult:
    """Place and county outputs must never be mixed: place keys are 7-digit,
    county keys are 5-digit. A 5-digit GEOID inside a place table would mean
    county rows leaked into place outputs."""
    leaked = int((place_wui["GEOID"].astype(str).str.len() == 5).sum())
    return CheckResult(
        check_id="plc-04",
        name="Place tables contain no county GEOIDs (separation)",
        dataset="place_outputs",
        severity=CRITICAL,
        status=_status_for(CRITICAL, leaked == 0),
        details=f"{leaked} 5-digit (county-style) GEOIDs found in place outputs",
        metrics={"n_leaked": leaked},
    )


# ---------------------------------------------------------------------------
# geo-* CRS checks (metadata-only; no geometry loads)
# ---------------------------------------------------------------------------
def check_spatial_crs(spatial_specs: dict[str, tuple[Path, int]]) -> CheckResult:
    """Every spatial artifact must record the expected EPSG in GeoParquet
    metadata — geographic CRS area math is impossible to slip through."""
    problems = []
    for name, (path, expected) in spatial_specs.items():
        if not path.exists():
            problems.append(f"{name}: missing {path.name}")
            continue
        epsg = _epsg_of(_crs_from_geoparquet(path))
        if epsg != expected:
            problems.append(f"{name}: EPSG {epsg} != expected {expected}")
    return CheckResult(
        check_id="geo-05",
        name="Stage 2 spatial artifacts carry expected CRS (no geographic area math)",
        dataset="stage2 spatial outputs",
        severity=CRITICAL,
        status=_status_for(CRITICAL, not problems),
        details="; ".join(problems) if problems else "all artifacts match expected EPSG",
        metrics={"n_problems": len(problems)},
    )


# ---------------------------------------------------------------------------
# prv-* provenance
# ---------------------------------------------------------------------------
def check_stage2_provenance() -> CheckResult:
    if not PROVENANCE_PATH.exists():
        return CheckResult(
            check_id="prv-02",
            name="Stage 2 datasets recorded in provenance manifest",
            dataset="provenance",
            severity=CRITICAL,
            status=FAIL,
            details=f"Provenance file missing: {PROVENANCE_PATH}",
        )
    with open(PROVENANCE_PATH, encoding="utf-8") as fh:
        prov = json.load(fh)
    datasets = prov.get("datasets", {})
    problems = []
    for key in ("silvis_wui", "nature_wui", "census_places"):
        rec = datasets.get(key)
        if rec is None:
            problems.append(f"{key}: not recorded")
            continue
        if rec.get("archive_valid") is not True:
            problems.append(f"{key}: archive not validated")
    return CheckResult(
        check_id="prv-02",
        name="Stage 2 datasets recorded in provenance manifest",
        dataset="provenance",
        severity=CRITICAL,
        status=_status_for(CRITICAL, not problems),
        details="; ".join(problems) if problems else
        "silvis_wui, nature_wui, census_places recorded and validated",
        metrics={"n_datasets_recorded": len(datasets)},
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _write_combined_report(results: list[CheckResult], out_csv: Path, out_md: Path) -> None:
    """Stage 1 rows (from its CSV, if present) + Stage 2 rows in one report."""
    stage1_df = None
    if out_csv.exists():
        try:
            prior = pd.read_csv(out_csv)
            # A rerun must replace Stage 2 rows already in the combined report.
            # Keeping every prior row would duplicate them and mislabel the
            # previous Stage 2 block as Stage 1.
            stage2_ids = {r.check_id for r in results}
            stage1_df = prior[~prior["check_id"].isin(stage2_ids)]
            if stage1_df.empty:
                stage1_df = None
        except Exception:
            stage1_df = None
    stage2_df = pd.DataFrame([r.as_dict() for r in results])
    combined = stage2_df if stage1_df is None else pd.concat(
        [stage1_df, stage2_df], ignore_index=True
    )
    combined.to_csv(out_csv, index=False)

    def counts(rows, label):
        n_fail = sum(1 for r in rows if r.status == FAIL)
        n_warn = sum(1 for r in rows if r.status == WARN)
        return (
            f"## {label}\n\n"
            f"- Checks run: {len(rows)}\n"
            f"- PASS: {sum(1 for r in rows if r.status == PASS)}\n"
            f"- WARN: {n_warn} (investigated; see details)\n"
            f"- FAIL: {n_fail}\n\n"
            "| Check | Dataset | Severity | Status | Details |\n|---|---|---|---|---|\n"
            + "".join(
                f"| {r.check_id} {r.name} | {r.dataset} | {r.severity} | {r.status} "
                f"| {str(r.details).replace(chr(10), '<br>').replace('|', '\\|')} |\n"
                for r in rows
            )
        )

    lines = ["# Data Validation Summary (Stage 1 + Stage 2)", ""]
    if stage1_df is not None:
        lines.append(
            f"(Stage 1: {len(stage1_df)} checks carried over from the previous "
            "validation run; see its log for details.)\n"
        )
    lines.append(counts(results, "Stage 2 — WUI integration"))
    lines.append(
        "\n### Burn-area inequalities (wf-01, wf-02, wf-05)\n\n"
        "These checks do **not** require total burned area ≤ WUI area. "
        "Wildfire can burn non-WUI land, so `burned_area_km2` may exceed "
        "`wui_area_km2`.\n\n"
        "- wf-01: annual `wui_burned_area_km2` ≤ `burned_area_km2` "
        "(the WUI portion of that year's burn).\n"
        "- wf-02: annual `wui_burned_area_km2` ≤ vintage-matched `wui_area_km2`. "
        "This is a single-year quantity. Summing it across years is cumulative "
        "exposure and can exceed WUI area where land reburns; that sum is not "
        "tested here and is not a percent of unique coverage.\n"
        "- wf-05: within each vintage period, "
        "`unique_wui_burned_footprint_km2` ≤ that period's `wui_area_km2`. "
        "The period's cumulative `wui_burned_area_km2` (per-event sum) is a "
        "different column and is allowed to exceed the unique footprint.\n"
    )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    s2 = load_stage2_config()
    log.info("Loading Stage 2 artifacts for validation...")

    # --- inputs -----------------------------------------------------------
    paths = {
        "county_wui": PROCESSED_DIR / "county_wui_by_vintage.parquet",
        "county_year_wui": PROCESSED_DIR / "county_year_wui_fire.parquet",
        "county_period": PROCESSED_DIR / "county_period_wui_fire.parquet",
        "county_change": PROCESSED_DIR / "county_wui_change_1990_2020.parquet",
        "county_nature": PROCESSED_DIR / "county_nature_wui_2020.parquet",
        "comparison": PROCESSED_DIR / "county_wui_silvis_vs_nature_2020.parquet",
        "place_wui": WUI_DIR / "place_wui_by_vintage.parquet",
        "place_exposure": WUI_DIR / "place_wui_fire_exposure.parquet",
    }
    optional = {"place_wui", "place_exposure", "county_change"}
    missing_required = [k for k, p in paths.items()
                        if not p.exists() and k not in optional]
    if missing_required:
        log.error("Missing required artifacts: %s", missing_required)
        return 1

    results: list[CheckResult] = [check_stage2_provenance()]

    # schema reports
    sil_schema_p = TABLES_DIR / "schema_silvis_raw.json"
    nat_schema_p = TABLES_DIR / "schema_nature_raw.json"
    if sil_schema_p.exists():
        with open(sil_schema_p, encoding="utf-8") as fh:
            results.append(check_silvis_class_domain(json.load(fh), s2))
    else:
        results.append(CheckResult(
            check_id="sch-01", name="SILVIS schema report present",
            dataset="silvis_schema", severity=CRITICAL, status=FAIL,
            details=f"{sil_schema_p} missing — run preprocess_wui first.",
        ))
    if nat_schema_p.exists():
        with open(nat_schema_p, encoding="utf-8") as fh:
            results.append(check_nature_class_domain(json.load(fh), s2))
    else:
        results.append(CheckResult(
            check_id="sch-02", name="Nature schema report present",
            dataset="nature_schema", severity=CRITICAL, status=FAIL,
            details=f"{nat_schema_p} missing — run nature_wui first.",
        ))

    # county WUI
    cw = pd.read_parquet(paths["county_wui"])
    results += [
        check_wui_area_nonnegative(cw),
        check_wui_area_le_county(cw),
        check_wui_pct_bounds(cw, float(s2["validation"]["max_pct_county_wui"])),
        check_wui_composition_consistency(cw),
        check_county_vintage_uniqueness(cw, s2),
    ]

    # temporal alignment + fire exposure
    cy = pd.read_parquet(paths["county_year_wui"])
    results += [
        check_vintage_matches_year(cy, s2),
        check_no_2020_wui_on_earlier_years(cy),
        check_pre_baseline_nulls(cy, s2),
        check_vintage_wui_area_matches(cy, cw),
        check_wui_burn_le_burned(cy),
        check_wui_burn_le_wui_area(cy),
        check_wui_burn_composition(cy),
        check_county_year_uniqueness(cy),
    ]
    cp = pd.read_parquet(paths["county_period"])
    results.append(check_period_table(cp, s2))

    # change table (optional only if the producing stage was skipped)
    if paths["county_change"].exists():
        results.append(check_change_table(pd.read_parquet(paths["county_change"]), s2))

    # nature + comparison
    nat = pd.read_parquet(paths["county_nature"])
    results += [check_nature_areas(nat), check_nature_coverage(nat)]
    comp = pd.read_parquet(paths["comparison"])
    summary_p = TABLES_DIR / "wui_silvis_vs_nature_summary.json"
    with open(summary_p, encoding="utf-8") as fh:
        results.append(check_comparison_separation(comp, json.load(fh)))

    # places (documented blocker if missing)
    if paths["place_wui"].exists():
        pw = pd.read_parquet(paths["place_wui"])
        results += [check_place_keys(pw), check_place_wui_bounds(pw),
                    check_place_county_separation(pw)]
        if paths["place_exposure"].exists():
            pe = pd.read_parquet(paths["place_exposure"])
            results.append(check_place_exposure(pe, s2))
    else:
        results.append(CheckResult(
            check_id="plc-01", name="Place outputs present",
            dataset="place_outputs", severity=WARNING, status=WARN,
            details="Place outputs missing — Stage 2C not completed or blocked; "
            "documented blocker required.",
        ))

    # CRS via GeoParquet metadata (no geometry loads); places only if Stage 2C ran
    spatial_specs = {
        "silvis_wui_fragments": (WUI_DIR / "silvis_wui_fragments.geoparquet", 5070),
        "fires_wui_intersections": (INTERIM_DIR / "fires_wui_intersections.geoparquet", 5070),
    }
    if (WUI_DIR / "places_conus.geoparquet").exists():
        spatial_specs["places_conus"] = (WUI_DIR / "places_conus.geoparquet", 5070)
    results.append(check_spatial_crs(spatial_specs))

    out_csv = TABLES_DIR / "data_validation_report.csv"
    out_md = TABLES_DIR / "validation_summary.md"
    _write_combined_report(results, out_csv, out_md)

    n_crit_fail = sum(1 for r in results if r.status == FAIL and r.severity == CRITICAL)
    for r in results:
        if r.status in (FAIL, WARN):
            log.log(
                logging.ERROR if r.status == FAIL else logging.WARNING,
                "[%s] %s -> %s: %s", r.status, r.check_id, r.name, r.details,
            )
    log.info(
        "Stage 2 validation complete: %d checks, %d critical failures, "
        "%d warnings. Report: %s | Summary: %s",
        len(results), n_crit_fail,
        sum(1 for r in results if r.status == WARN), out_csv, out_md,
    )
    return 1 if n_crit_fail else 0


if __name__ == "__main__":
    setup_logging("validate_stage2.log")
    sys.exit(main())