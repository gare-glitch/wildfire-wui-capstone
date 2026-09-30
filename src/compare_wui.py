"""SILVIS 2020 vs Nature/Schug 2020 county WUI comparison (sensitivity layer).

Compares two INDEPENDENTLY derived 2020 WUI products at the county level:

- SILVIS WUI Change v4 (2020 snapshot): census-block polygons; WUI defined
  by the Federal Register 66:751 housing-density/vegetation rule
  (intermix: >=50% vegetation AND >=6.177635 housing units/km2; interface:
  <=50% vegetation within 2.414 km of a >=500-ha patch that is >=75%
  vegetation). Housing from decennial censuses; vegetation circa
  1992/2001/2011/2019 land cover.
- Schug et al. (2023) "The global wildland-urban interface" (Nature): 10 m
  global grid; WUI defined from built-up area (building footprints /
  settlement layers) within a fixed distance of wildland vegetation
  (>=20% vegetation cover), circa 2020 inputs.

Neither product is treated as ground truth. Differences are expected for
documented methodological reasons (see docs/wui_methodology.md):
different settlement proxies (housing units vs building footprints),
different vegetation inputs and vintages, different minimum mapping
units, and vector-block vs raster-pixel geometry.

Outputs
-------
- data/processed/county_wui_silvis_vs_nature_2020.parquet
- outputs/tables/county_wui_silvis_vs_nature_2020.csv
- outputs/tables/wui_silvis_vs_nature_summary.json  (correlations, diffs)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

from common import (  # noqa: E402
    PROCESSED_DIR,
    TABLES_DIR,
    get_logger,
    load_stage2_config,
)

log = get_logger("compare_wui")

COMPARISON_YEAR = 2020
COVERAGE_THRESHOLD_PCT = 95.0  # well-covered subset for sensitivity correlations

OUT_PARQUET = PROCESSED_DIR / "county_wui_silvis_vs_nature_2020.parquet"
OUT_CSV = TABLES_DIR / "county_wui_silvis_vs_nature_2020.csv"
OUT_SUMMARY = TABLES_DIR / "wui_silvis_vs_nature_summary.json"


def build_comparison(silvis_2020: pd.DataFrame, nature: pd.DataFrame) -> pd.DataFrame:
    """County-level side-by-side comparison of the two 2020 WUI products.

    silvis_2020: rows of county_wui_by_vintage with wui_year == 2020.
    nature:     county_nature_wui_2020 table.
    """
    keep_s = [
        "GEOID", "county_name", "state_fips", "state_name", "county_area_km2",
        "wui_area_km2", "pct_county_wui",
        "interface_wui_area_km2", "intermix_wui_area_km2",
        "pct_of_wui_that_is_interface", "hu_in_wui",
    ]
    keep_n = [
        "GEOID", "nature_wui_area_km2", "pct_county_nature_wui",
        "nature_interface_wui_area_km2", "nature_intermix_wui_area_km2",
        "pct_of_nature_wui_that_is_interface",
        "pct_county_covered_by_nature_raster",
    ]
    df = silvis_2020[keep_s].merge(nature[keep_n], on="GEOID", how="inner",
                                  validate="one_to_one")
    if len(df) != len(silvis_2020) or len(df) != len(nature):
        raise ValueError(
            f"County join mismatch: silvis {len(silvis_2020)}, nature "
            f"{len(nature)}, joined {len(df)} — GEOID keys must align exactly."
        )

    df = df.rename(columns={"wui_area_km2": "silvis_wui_area_km2",
                            "pct_county_wui": "pct_county_silvis_wui",
                            "interface_wui_area_km2": "silvis_interface_wui_area_km2",
                            "intermix_wui_area_km2": "silvis_intermix_wui_area_km2",
                            "pct_of_wui_that_is_interface": "pct_of_silvis_wui_that_is_interface",
                            "hu_in_wui": "silvis_hu_in_wui"})

    # Absolute and relative differences (Nature minus SILVIS; sign convention
    # documented in the CSV header via column names).
    df["wui_area_diff_km2"] = df["nature_wui_area_km2"] - df["silvis_wui_area_km2"]
    df["wui_pct_point_diff"] = (
        df["pct_county_nature_wui"] - df["pct_county_silvis_wui"]
    )
    df["wui_area_pct_diff"] = (
        (df["nature_wui_area_km2"] - df["silvis_wui_area_km2"])
        / df["silvis_wui_area_km2"].replace(0, pd.NA) * 100.0
    )
    # Interface share difference in percentage points (composition only —
    # both shares are within-product).
    df["interface_share_pct_point_diff"] = (
        df["pct_of_nature_wui_that_is_interface"]
        - df["pct_of_silvis_wui_that_is_interface"]
    )

    df["silvis_wui"] = df["silvis_wui_area_km2"] > 0
    df["nature_wui"] = df["nature_wui_area_km2"] > 0
    # Disagreement flag: one product finds WUI, the other does not.
    df["wui_presence_disagreement"] = df["silvis_wui"] != df["nature_wui"]

    order = [
        "GEOID", "county_name", "state_fips", "state_name", "county_area_km2",
        "silvis_wui_area_km2", "nature_wui_area_km2", "wui_area_diff_km2",
        "wui_area_pct_diff",
        "pct_county_silvis_wui", "pct_county_nature_wui", "wui_pct_point_diff",
        "silvis_interface_wui_area_km2", "nature_interface_wui_area_km2",
        "silvis_intermix_wui_area_km2", "nature_intermix_wui_area_km2",
        "pct_of_silvis_wui_that_is_interface",
        "pct_of_nature_wui_that_is_interface",
        "interface_share_pct_point_diff",
        "silvis_hu_in_wui",
        "pct_county_covered_by_nature_raster",
        "silvis_wui", "nature_wui", "wui_presence_disagreement",
    ]
    return df[order].sort_values("GEOID").reset_index(drop=True)


def summarize_comparison(df: pd.DataFrame) -> dict:
    """Correlations and distributional summaries of the comparison."""
    well = df[df["pct_county_covered_by_nature_raster"] >= COVERAGE_THRESHOLD_PCT]

    def corrs(sub: pd.DataFrame, col_a: str, col_b: str) -> dict:
        pearson = sub[col_a].corr(sub[col_b], method="pearson")
        spearman = sub[col_a].corr(sub[col_b], method="spearman")
        return {"pearson": round(float(pearson), 4),
                "spearman": round(float(spearman), 4), "n": int(len(sub))}

    return {
        "n_counties": int(len(df)),
        "n_well_covered_counties": int(len(well)),
        "coverage_threshold_pct": COVERAGE_THRESHOLD_PCT,
        "area_correlation_all_counties": corrs(df, "silvis_wui_area_km2",
                                               "nature_wui_area_km2"),
        "area_correlation_well_covered": corrs(well, "silvis_wui_area_km2",
                                               "nature_wui_area_km2"),
        "pct_correlation_all_counties": corrs(df, "pct_county_silvis_wui",
                                              "pct_county_nature_wui"),
        "pct_correlation_well_covered": corrs(well, "pct_county_silvis_wui",
                                              "pct_county_nature_wui"),
        "conus_silvis_wui_km2": round(float(df["silvis_wui_area_km2"].sum()), 1),
        "conus_nature_wui_km2": round(float(df["nature_wui_area_km2"].sum()), 1),
        "mean_abs_area_diff_km2": round(float(df["wui_area_diff_km2"].abs().mean()), 3),
        "median_abs_area_diff_km2": round(float(df["wui_area_diff_km2"].abs().median()), 3),
        "mean_abs_pct_point_diff": round(float(df["wui_pct_point_diff"].abs().mean()), 3),
        "n_counties_nature_gt_silvis": int((df["wui_area_diff_km2"] > 0).sum()),
        "n_counties_silvis_gt_nature": int((df["wui_area_diff_km2"] < 0).sum()),
        "n_presence_disagreement": int(df["wui_presence_disagreement"].sum()),
        "mean_interface_share_silvis_pct": round(
            float(df["pct_of_silvis_wui_that_is_interface"].mean()), 2),
        "mean_interface_share_nature_pct": round(
            float(df["pct_of_nature_wui_that_is_interface"].mean()), 2),
        "methodological_note": (
            "Products are independent definitions: SILVIS applies the Federal "
            "Register 66:751 census-block housing-density/vegetation rule; "
            "Schug et al. 2023 classify a 10 m grid from building/settlement "
            "proximity to wildland vegetation. Neither is treated as correct."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    silvis_path = PROCESSED_DIR / "county_wui_by_vintage.parquet"
    nature_path = PROCESSED_DIR / "county_nature_wui_2020.parquet"
    for p in (silvis_path, nature_path):
        if not p.exists():
            log.error("Missing input %s — run the producing stage first.", p)
            return 1

    silvis = pd.read_parquet(silvis_path)
    s2020 = silvis[silvis["wui_year"] == COMPARISON_YEAR].copy()
    nature = pd.read_parquet(nature_path)

    t0 = time.time()
    comp = build_comparison(s2020, nature)
    summary = summarize_comparison(comp)

    comp.to_parquet(OUT_PARQUET, index=False)
    comp.to_csv(OUT_CSV, index=False)
    with open(OUT_SUMMARY, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    log.info("Wrote %d county comparison rows to %s (%.1f s).",
             len(comp), OUT_CSV, time.time() - t0)
    log.info("SILVIS CONUS WUI: %.0f km2 | Nature CONUS WUI: %.0f km2",
             summary["conus_silvis_wui_km2"], summary["conus_nature_wui_km2"])
    log.info("County WUI-area correlation (all): r=%s rho=%s",
             summary["area_correlation_all_counties"]["pearson"],
             summary["area_correlation_all_counties"]["spearman"])
    log.info("Presence disagreements (WUI vs no-WUI): %d counties",
             summary["n_presence_disagreement"])
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("compare_wui.log")
    sys.exit(main())