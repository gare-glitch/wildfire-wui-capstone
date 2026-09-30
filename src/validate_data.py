"""Automated validation suite for Stage 1 outputs.

Every check is a pure function over explicit inputs (so pytest can exercise
each one on synthetic data). The runner assembles pipeline artifacts, runs
all checks, writes:

- outputs/tables/data_validation_report.csv  (one row per check result)
- outputs/tables/validation_summary.md       (human-readable summary)

and exits non-zero if any CRITICAL check fails (fail loudly, AGENTS.md #9).

Severity semantics:
- CRITICAL: result is scientifically invalid; pipeline must not proceed.
- WARNING : result is suspicious and must be investigated, but can have a
            legitimate physical explanation (e.g., overlapping perimeters).
- INFO     : recorded for transparency, cannot fail.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    INTERIM_DIR,
    PROCESSED_DIR,
    PROVENANCE_PATH,
    RAW_DIR,
    TABLES_DIR,
    ensure_dirs,
    get_logger,
    load_analysis_config,
    setup_logging,
)

log = get_logger("validate")

CRITICAL, WARNING, INFO = "CRITICAL", "WARNING", "INFO"
PASS, FAIL, WARN, INFO_STATUS = "PASS", "FAIL", "WARN", "INFO"

KM2_PER_M2 = 1e-6


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    name: str
    dataset: str
    severity: str
    status: str
    details: str
    metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "check_id": self.check_id,
            "name": self.name,
            "dataset": self.dataset,
            "severity": self.severity,
            "status": self.status,
            "details": self.details,
        }
        d.update({f"metric_{k}": v for k, v in self.metrics.items()})
        return d


def _status_for(severity: str, ok: bool, warn: bool = False) -> str:
    if ok:
        return PASS
    if severity == INFO:
        return INFO_STATUS
    if severity == WARNING:
        return WARN
    return FAIL


# ---------------------------------------------------------------------------
# Individual checks (pure functions — unit tested in tests/)
# ---------------------------------------------------------------------------
def check_geoid_uniqueness(counties: gpd.GeoDataFrame) -> CheckResult:
    """County source must have exactly one row per GEOID."""
    n, nu = len(counties), counties["GEOID"].nunique()
    ok = n == nu
    dups = counties[counties["GEOID"].duplicated(keep=False)]["GEOID"].unique().tolist()
    return CheckResult(
        check_id="cnt-01",
        name="GEOID uniqueness in county source",
        dataset="counties",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"{nu} unique GEOIDs across {n} rows."
            + (f" Duplicated GEOIDs: {dups[:10]}" if dups else "")
        ),
        metrics={"n_rows": n, "n_unique_geoid": nu},
    )


def check_county_geoid_format(counties: gpd.GeoDataFrame) -> CheckResult:
    """GEOID must be 5-character STATEFP+COUNTYFP strings (zero-padded)."""
    bad = counties[~counties["GEOID"].astype(str).str.fullmatch(r"\d{5}")]["GEOID"].tolist()
    return CheckResult(
        check_id="cnt-02",
        name="GEOID format (5-digit STATEFP+COUNTYFP)",
        dataset="counties",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=f"{len(bad)} malformed GEOIDs" + (f": {bad[:10]}" if bad else " (all 5-digit numeric)."),
        metrics={"n_malformed": len(bad)},
    )


def check_county_areas(counties: gpd.GeoDataFrame, min_km2: float, max_km2: float) -> CheckResult:
    """County areas must fall within plausible CONUS bounds."""
    bad = counties[(counties["county_area_km2"] < min_km2) | (counties["county_area_km2"] > max_km2)]
    lo = float(counties["county_area_km2"].min())
    hi = float(counties["county_area_km2"].max())
    return CheckResult(
        check_id="cnt-03",
        name="County area plausibility (CONUS bounds)",
        dataset="counties",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=(
            f"Areas range {lo:,.1f}-{hi:,.1f} km2; {len(bad)} outside "
            f"[{min_km2}, {max_km2}]"
            + (f": {bad['GEOID'].tolist()[:10]}" if len(bad) else "")
        ),
        metrics={"min_km2": round(lo, 1), "max_km2": round(hi, 1), "n_out_of_bounds": len(bad)},
    )


def check_conus_state_coverage(counties: gpd.GeoDataFrame, conus_fips: list[str]) -> CheckResult:
    """All CONUS state FIPS must be represented in the county source."""
    present = set(counties["STATEFP"].astype(str))
    missing = sorted(set(conus_fips) - present)
    return CheckResult(
        check_id="cnt-04",
        name="CONUS state coverage in county source",
        dataset="counties",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(missing) == 0),
        details=f"{len(present)} distinct STATEFPs present; missing CONUS FIPS: {missing or 'none'}",
        metrics={"n_states_present": len(present), "n_missing": len(missing)},
    )


def check_va_independent_cities(counties: gpd.GeoDataFrame) -> CheckResult:
    """Virginia independent cities (county equivalents) must be present.

    Virginia independent cities carry COUNTYFP >= 500 in TIGER/Line and have
    NAMELSAD values like 'Alexandria city'. The check requires every VA row
    with COUNTYFP >= 500 to be present and to look like a city name.
    """
    va = counties[counties["STATEFP"].astype(str) == "51"]
    va_cities = va[va["COUNTYFP"].astype(str).str.fullmatch(r"5\d\d|6\d\d|7\d\d|8\d\d")]
    n_va, n_city = len(va), len(va_cities)
    all_cities_named_like = (
        va_cities["NAMELSAD"].astype(str).str.lower().str.endswith("city").all()
        if n_city
        else False
    )
    ok = n_city >= 30 and bool(all_cities_named_like)
    return CheckResult(
        check_id="cnt-05",
        name="Virginia independent cities present",
        dataset="counties",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"{n_city} VA county-equivalents with COUNTYFP>=500 out of {n_va} VA rows; "
            f"all NAMELSAD end with 'city': {all_cities_named_like}"
        ),
        metrics={"n_va_rows": n_va, "n_va_independent_cities": n_city},
    )


def check_missing_crs(named_gdfs: dict[str, gpd.GeoDataFrame]) -> CheckResult:
    """Every spatial output must carry a CRS."""
    missing = [name for name, g in named_gdfs.items() if g.crs is None]
    return CheckResult(
        check_id="geo-01",
        name="CRS defined on all spatial artifacts",
        dataset="all spatial outputs",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(missing) == 0),
        details=f"{len(named_gdfs)} spatial artifacts; missing CRS: {missing or 'none'}",
        metrics={"n_artifacts": len(named_gdfs), "n_missing_crs": len(missing)},
    )


def check_conus_crs_is_5070(conus_artifacts: dict[str, gpd.GeoDataFrame]) -> CheckResult:
    """CONUS artifacts used for area math must be in EPSG:5070."""
    wrong = {
        name: (g.crs.to_epsg() if g.crs is not None else None)
        for name, g in conus_artifacts.items()
        if g.crs is None or g.crs.to_epsg() != 5070
    }
    return CheckResult(
        check_id="geo-02",
        name="CONUS artifacts projected in EPSG:5070",
        dataset="all spatial outputs",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(wrong) == 0),
        details=f"Non-5070 artifacts: {wrong or 'none'}",
        metrics={"n_wrong_crs": len(wrong)},
    )


def check_invalid_geometries(named_gdfs: dict[str, gpd.GeoDataFrame]) -> CheckResult:
    """No invalid or empty geometries may remain in processed artifacts."""
    report: dict[str, int] = {}
    for name, g in named_gdfs.items():
        invalid = int((~g.geometry.is_valid).sum()) if len(g) else 0
        empty = int(g.geometry.is_empty.sum()) if len(g) else 0
        report[name] = invalid + empty
    total_bad = sum(report.values())
    return CheckResult(
        check_id="geo-03",
        name="Valid non-empty geometries in artifacts",
        dataset="all spatial outputs",
        severity=CRITICAL,
        status=_status_for(CRITICAL, total_bad == 0),
        details=f"Invalid+empty per artifact: {report}",
        metrics={"n_bad_total": total_bad},
    )


def check_mtbs_year_range(
    fires: gpd.GeoDataFrame, min_year: int, max_year: int
) -> CheckResult:
    """Processed fires must lie within the configured completed year range."""
    y = fires["year"]
    lo, hi = int(y.min()), int(y.max())
    outside = fires[(y < min_year) | (y > max_year)]
    ok = lo >= min_year and hi <= max_year
    return CheckResult(
        check_id="mtb-01",
        name="MTBS year range within configured completed years",
        dataset="mtbs_fires",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"Processed year range {lo}-{hi}; expected within "
            f"{min_year}-{max_year}; {len(outside)} rows outside"
        ),
        metrics={"min_year": lo, "max_year": hi, "n_outside": len(outside)},
    )


def check_missing_fire_id(fires: gpd.GeoDataFrame) -> CheckResult:
    """FIRE_ID is the canonical event key and must be non-null."""
    n_missing = int(fires["FIRE_ID"].isna().sum())
    return CheckResult(
        check_id="mtb-02",
        name="No missing FIRE_ID",
        dataset="mtbs_fires",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n_missing == 0),
        details=f"{n_missing} rows missing FIRE_ID",
        metrics={"n_missing": n_missing},
    )


def check_duplicate_fire_id(fires: gpd.GeoDataFrame) -> CheckResult:
    """One row per FIRE_ID after preprocessing."""
    n, nu = len(fires), fires["FIRE_ID"].nunique()
    dups = fires[fires["FIRE_ID"].duplicated(keep=False)]["FIRE_ID"].unique().tolist()
    return CheckResult(
        check_id="mtb-03",
        name="FIRE_ID uniqueness after preprocessing",
        dataset="mtbs_fires",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n == nu),
        details=f"{nu} unique FIRE_IDs over {n} rows" + (f"; duplicates: {dups[:10]}" if dups else ""),
        metrics={"n_rows": n, "n_unique": nu},
    )


def check_fire_type_domain(fires: gpd.GeoDataFrame, allowed: list[str]) -> CheckResult:
    """Only configured fire types may remain in the processed set."""
    present = sorted(fires["fire_type"].astype(str).unique().tolist())
    unexpected = sorted(set(present) - set(allowed))
    return CheckResult(
        check_id="mtb-04",
        name="Fire type domain matches configuration",
        dataset="mtbs_fires",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(unexpected) == 0),
        details=f"fire_type values present: {present}; unexpected: {unexpected or 'none'}",
        metrics={"n_unexpected_types": len(unexpected)},
    )


def check_duplicate_county_year(county_year: pd.DataFrame) -> CheckResult:
    """Final table must have exactly one row per GEOID x year."""
    n = len(county_year)
    n_unique = len(county_year.drop_duplicates(subset=["GEOID", "year"]))
    ok = n == n_unique
    return CheckResult(
        check_id="agg-01",
        name="No duplicate GEOID x year rows",
        dataset="county_year",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=f"{n_unique} unique county-years over {n} rows",
        metrics={"n_rows": n, "n_unique_county_years": n_unique},
    )


def check_duplicate_geoid_all_years(all_years: pd.DataFrame) -> CheckResult:
    """All-years summary must have exactly one row per GEOID."""
    n = len(all_years)
    nu = all_years["GEOID"].nunique()
    return CheckResult(
        check_id="agg-02",
        name="No duplicate GEOID rows in all-years summary",
        dataset="county_all_years",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n == nu),
        details=f"{nu} unique GEOIDs over {n} rows",
        metrics={"n_rows": n, "n_unique_geoids": nu},
    )


def check_negative_areas(
    county_year: pd.DataFrame, all_years: pd.DataFrame, intersections: pd.DataFrame
) -> CheckResult:
    """No negative areas anywhere (CRITICAL) — geometry math must be sane."""
    bad_cols: dict[str, int] = {}
    for name, df in (
        ("county_year", county_year),
        ("county_all_years", all_years),
        ("intersections", intersections),
    ):
        area_cols = [c for c in df.columns if c.endswith("_km2")]
        n_bad = int(
            sum((df[c] < 0).sum() for c in area_cols)
        )
        if n_bad:
            bad_cols[name] = n_bad
    return CheckResult(
        check_id="agg-03",
        name="No negative area values",
        dataset="all aggregates",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad_cols) == 0),
        details=f"Negative areas by table: {bad_cols or 'none'}",
        metrics={"n_negative": sum(bad_cols.values())},
    )


def check_annual_pct_bounds(county_year: pd.DataFrame) -> CheckResult:
    """Annual burned pct: <0 impossible (CRITICAL); >100 suspicious (WARN).

    Annual pct >100 is geometrically possible only when distinct fire
    perimeters overlap within the same year (or geometry errors exist), so
    these cases are flagged for investigation, never clipped.
    """
    pct = county_year["pct_county_burned_in_year"]
    n_neg = int((pct < 0).sum())
    n_over = int((pct > 100).sum())
    over_examples = (
        county_year.loc[pct > 100, ["GEOID", "year", "pct_county_burned_in_year"]]
        .sort_values("pct_county_burned_in_year", ascending=False)
        .head(10)
    )
    status = FAIL if n_neg > 0 else (WARN if n_over > 0 else PASS)
    details = f"{n_neg} negative pct (impossible); {n_over} county-years with pct>100 (investigate)"
    if n_over:
        details += "; top cases:\n" + over_examples.to_string(index=False)
    return CheckResult(
        check_id="agg-04",
        name="Annual pct burned bounds (0..100+) with >100 investigation",
        dataset="county_year",
        severity=CRITICAL,
        status=status,
        details=details,
        metrics={"n_negative": n_neg, "n_over_100": n_over},
    )


def check_footprint_pct_bounds(all_years: pd.DataFrame) -> CheckResult:
    """Dissolved unique footprint pct must be in [0, 100] by construction."""
    pct = all_years["pct_unique_historical_burn_footprint"]
    n_bad = int(((pct < 0) | (pct > 100.0 + 1e-6)).sum())
    bad = all_years.loc[(pct < 0) | (pct > 100.0 + 1e-6), "GEOID"].tolist()
    return CheckResult(
        check_id="agg-05",
        name="Unique footprint pct within [0,100]",
        dataset="county_all_years",
        severity=CRITICAL,
        status=_status_for(CRITICAL, n_bad == 0),
        details=f"{n_bad} counties with footprint pct outside [0,100]"
        + (f": {bad[:10]}" if bad else ""),
        metrics={"n_out_of_bounds": n_bad},
    )


def check_footprint_le_cumulative(all_years: pd.DataFrame) -> CheckResult:
    """Unique footprint must never exceed cumulative burned area."""
    bad = all_years[
        all_years["unique_historical_burn_footprint_km2"]
        > all_years["cumulative_burned_area_km2"] + 1e-6
    ]
    return CheckResult(
        check_id="agg-06",
        name="Unique footprint <= cumulative burned area",
        dataset="county_all_years",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=f"{len(bad)} counties where footprint exceeds cumulative area",
        metrics={"n_violations": len(bad)},
    )


def check_cumulative_vs_county_area(all_years: pd.DataFrame) -> CheckResult:
    """Cumulative burned area exceeding county area is expected (reburn) —
    but verify it does not exceed a generous multiple that would suggest
    double-counting bugs (each county-year summed once)."""
    ratio = all_years["cumulative_burned_area_km2"] / all_years["county_area_km2"]
    n_over = int((ratio > 1).sum())
    n_absurd = int((ratio > 20).sum())
    worst = float(ratio.max()) if len(ratio) else 0.0
    status = FAIL if n_absurd > 0 else (INFO_STATUS if n_over > 0 else PASS)
    return CheckResult(
        check_id="agg-07",
        name="Cumulative burned area vs county area (reburn sanity)",
        dataset="county_all_years",
        severity=WARNING,
        status=status,
        details=(
            f"{n_over} counties with cumulative > county area (reburn — legitimate); "
            f"max ratio {worst:.2f}; {n_absurd} counties above 20x (would indicate a bug)"
        ),
        metrics={"n_over_100pct": n_over, "max_ratio": round(worst, 2)},
    )


def check_count_consistency(
    county_year: pd.DataFrame, all_years: pd.DataFrame
) -> CheckResult:
    """Aggregate totals must reconcile between the two output tables."""
    sum_fires_cy = int(county_year["fire_count"].sum())
    sum_unique_all = int(all_years["unique_fire_count"].sum())
    ok = sum_fires_cy >= sum_unique_all
    # county-year fire counts sum >= distinct fires overall because a fire
    # spanning multiple counties counts once per county. Equality holds only
    # for single-county fires. Also check no zero-count rows with area.
    zero_count_with_area = int(
        ((county_year["fire_count"] == 0) & (county_year["total_burned_area_km2"] > 0)).sum()
    )
    nonzero_count_zero_area = int(
        ((county_year["fire_count"] > 0) & (county_year["total_burned_area_km2"] <= 0)).sum()
    )
    ok = ok and zero_count_with_area == 0 and nonzero_count_zero_area == 0
    return CheckResult(
        check_id="agg-08",
        name="Count/area internal consistency",
        dataset="aggregates",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"sum(county_year fire_count)={sum_fires_cy:,} >= "
            f"sum(all_years unique_fire_count)={sum_unique_all:,}; "
            f"zero-count-with-area rows={zero_count_with_area}; "
            f"count>0-with-zero-area rows={nonzero_count_zero_area}"
        ),
        metrics={
            "sum_cy_fire_count": sum_fires_cy,
            "sum_all_unique_fire_count": sum_unique_all,
        },
    )


def check_intersection_vs_county_area(
    intersections: pd.DataFrame, counties: gpd.GeoDataFrame
) -> CheckResult:
    """No single intersection fragment may exceed its county's total area."""
    merged = intersections.merge(
        counties[["GEOID", "county_area_km2"]], on="GEOID", how="left"
    )
    bad = merged[merged["intersection_area_km2"] > merged["county_area_km2"]]
    return CheckResult(
        check_id="ovl-01",
        name="Intersection fragment <= county area",
        dataset="intersections",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(bad) == 0),
        details=f"{len(bad)} fragments larger than their county",
        metrics={"n_violations": len(bad)},
    )


def check_event_counts(filter_report: dict) -> CheckResult:
    """Record wildfire event counts before/after each filtering step."""
    total = filter_report.get("n_raw_total", 0)
    conus = filter_report.get("n_conus", 0)
    retained = filter_report.get("n_retained", 0)
    by_type = filter_report.get("by_fire_type", {})
    ok = retained <= conus <= total
    return CheckResult(
        check_id="ovl-02",
        name="Event count accounting (raw -> filters -> retained)",
        dataset="mtbs_fires",
        severity=CRITICAL,
        status=_status_for(CRITICAL, ok),
        details=(
            f"raw={total:,} | by FIRE_TYPE={by_type} | retained "
            f"({filter_report.get('fire_types_included')}, CONUS, "
            f"{filter_report.get('min_year')}-{filter_report.get('max_year')})={retained:,}"
        ),
        metrics={"n_raw": total, "n_retained": retained},
    )


def check_provenance(provenance_path: Path) -> CheckResult:
    """Provenance manifest must exist, record both datasets, and mark them valid."""
    if not provenance_path.exists():
        return CheckResult(
            check_id="prv-01",
            name="Provenance manifest present and complete",
            dataset="provenance",
            severity=CRITICAL,
            status=FAIL,
            details=f"Provenance file missing: {provenance_path}",
        )
    with open(provenance_path, "r", encoding="utf-8") as fh:
        prov = json.load(fh)
    datasets = prov.get("datasets", {})
    problems = []
    for key in ("mtbs_perimeter", "census_counties"):
        rec = datasets.get(key)
        if rec is None:
            problems.append(f"{key}: missing")
            continue
        for field in ("url", "retrieval_utc", "sha256", "size_bytes", "archive_valid"):
            if rec.get(field) is None and rec.get("status") != "failed":
                problems.append(f"{key}: missing {field}")
        if rec.get("archive_valid") is not True:
            problems.append(f"{key}: archive not validated")
    return CheckResult(
        check_id="prv-01",
        name="Provenance manifest present and complete",
        dataset="provenance",
        severity=CRITICAL,
        status=_status_for(CRITICAL, len(problems) == 0),
        details=(
            "All datasets recorded with url/retrieval/sha256/size/archive_valid."
            if not problems
            else "; ".join(problems)
        ),
        metrics={"n_datasets": len(datasets)},
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _write_report(results: list[CheckResult], out_csv: Path, out_md: Path) -> None:
    df = pd.DataFrame([r.as_dict() for r in results])
    df.to_csv(out_csv, index=False)

    n_fail = sum(1 for r in results if r.status == FAIL)
    n_warn = sum(1 for r in results if r.status == WARN)
    lines = [
        "# Stage 1 Validation Summary",
        "",
        f"- Checks run: {len(results)}",
        f"- PASS: {sum(1 for r in results if r.status == PASS)}",
        f"- WARN: {n_warn} (investigated; see details)",
        f"- FAIL: {n_fail}",
        "",
        "| Check | Dataset | Severity | Status | Details |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        details = r.details.replace("\n", "<br>").replace("|", "\\|")
        lines.append(
            f"| {r.check_id} {r.name} | {r.dataset} | {r.severity} | {r.status} | {details} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    cfg = load_analysis_config()
    conus_fips = [str(f) for f in cfg["scope"]["conus_state_fips"]]
    min_year, max_year = int(cfg["years"]["min_year"]), int(cfg["years"]["max_year"])
    allowed_types = list(cfg["fire_types"]["include"])

    ensure_dirs()
    log.info("Loading Stage 1 artifacts for validation...")

    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    fires = gpd.read_parquet(PROCESSED_DIR / "mtbs_wildfires_conus.geoparquet")
    intersections = gpd.read_parquet(INTERIM_DIR / "fires_county_intersections.geoparquet")
    county_year = pd.read_parquet(PROCESSED_DIR / "county_year_wildfire.parquet")
    all_years = pd.read_parquet(PROCESSED_DIR / "county_all_years_wildfire_summary.parquet")
    with open(PROCESSED_DIR / "mtbs_filter_report.json", "r", encoding="utf-8") as fh:
        filter_report = json.load(fh)

    spatial_artifacts = {
        "counties_conus": counties,
        "mtbs_wildfires_conus": fires,
        "fires_county_intersections": intersections,
    }

    results: list[CheckResult] = [
        check_provenance(PROVENANCE_PATH),
        check_geoid_uniqueness(counties),
        check_county_geoid_format(counties),
        check_county_areas(
            counties,
            float(cfg["validation"]["min_county_area_km2"]),
            float(cfg["validation"]["max_county_area_km2"]),
        ),
        check_conus_state_coverage(counties, conus_fips),
        check_va_independent_cities(counties),
        check_missing_crs(spatial_artifacts),
        check_conus_crs_is_5070(spatial_artifacts),
        check_invalid_geometries(spatial_artifacts),
        check_mtbs_year_range(fires, min_year, max_year),
        check_missing_fire_id(fires),
        check_duplicate_fire_id(fires),
        check_fire_type_domain(fires, allowed_types),
        check_duplicate_county_year(county_year),
        check_duplicate_geoid_all_years(all_years),
        check_negative_areas(county_year, all_years, intersections),
        check_annual_pct_bounds(county_year),
        check_footprint_pct_bounds(all_years),
        check_footprint_le_cumulative(all_years),
        check_cumulative_vs_county_area(all_years),
        check_count_consistency(county_year, all_years),
        check_intersection_vs_county_area(intersections, counties),
        check_event_counts(filter_report),
    ]

    out_csv = TABLES_DIR / "data_validation_report.csv"
    out_md = TABLES_DIR / "validation_summary.md"
    _write_report(results, out_csv, out_md)

    n_crit_fail = sum(1 for r in results if r.status == FAIL and r.severity == CRITICAL)
    for r in results:
        if r.status in (FAIL, WARN):
            log.log(
                logging.ERROR if r.status == FAIL else logging.WARNING,
                "[%s] %s -> %s: %s",
                r.status,
                r.check_id,
                r.name,
                r.details,
            )
    log.info(
        "Validation complete: %d checks, %d critical failures, %d warnings. "
        "Report: %s | Summary: %s",
        len(results),
        n_crit_fail,
        sum(1 for r in results if r.status == WARN),
        out_csv,
        out_md,
    )
    return 1 if n_crit_fail else 0


if __name__ == "__main__":
    setup_logging("validate.log")
    sys.exit(main())