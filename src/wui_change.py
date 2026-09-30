"""Stage 2A — county-level WUI change metrics between SILVIS vintages.

Long-form change table (one row per GEOID x transition):
    1990->2000, 2000->2010, 2010->2020, and the full 1990->2020 span.

For each transition:
    wui_area_change_km2              absolute WUI area change
    pct_wui_area_change               percentage change (of the base year WUI)
    interface_wui_area_change_km2     interface component change
    intermix_wui_area_change_km2     intermix component change
    pct_interface_change / pct_intermix_change   component pct changes
    hu_in_wui_change                  change in WUI housing units
    pct_hu_in_wui_change

Guardrail (AGENTS.md / Stage 2 plan): WUI growth is described as *change in
WUI extent*, never as a cause of wildfire exposure — no causal language is
attached to these metrics.

Output:
    outputs/tables/county_wui_change_1990_2020.csv
    data/processed/county_wui_change_1990_2020.parquet
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PROCESSED_DIR,
    TABLES_DIR,
    ensure_dirs,
    get_logger,
    load_stage2_config,
)

log = get_logger("wui_change")

TRANSITIONS = [(1990, 2000), (2000, 2010), (2010, 2020), (1990, 2020)]

CHANGE_CSV = TABLES_DIR / "county_wui_change_1990_2020.csv"
CHANGE_PARQUET = PROCESSED_DIR / "county_wui_change_1990_2020.parquet"


# ---------------------------------------------------------------------------
# Pure functions (unit-tested)
# ---------------------------------------------------------------------------
def pct_change(new: pd.Series, old: pd.Series) -> pd.Series:
    """Percentage change vs the base year; NaN where the base is 0.

    Uses a masked division (np.where would evaluate the divide branch for
    zero bases too, raising ZeroDivisionError on scalars and RuntimeWarnings
    on Series).
    """
    new = np.asarray(new, dtype=float)
    old = np.asarray(old, dtype=float)
    new, old = np.broadcast_arrays(new, old)
    out = np.full(new.shape, np.nan)
    mask = old > 0
    np.divide(new - old, old, out=out, where=mask)
    out[mask] *= 100.0
    if out.ndim == 0:
        return float(out)
    return pd.Series(out, index=getattr(new, "index", None))


def build_change_table(county_wui: pd.DataFrame) -> pd.DataFrame:
    """Compute all transition metrics from the county x vintage table.

    Input must have columns:
      GEOID, county_name, state_fips, state_name, wui_year, wui_area_km2,
      interface_wui_area_km2, intermix_wui_area_km2, hu_in_wui
    All CONUS counties must be present for every vintage (zero-filled by the
    upstream stage), so left joins preserve every county in every transition.
    """
    key = ["GEOID", "county_name", "state_fips", "state_name"]
    wide_by_year = {
        y: g.set_index("GEOID")
        for y, g in county_wui.groupby("wui_year")
    }

    frames = []
    for old_year, new_year in TRANSITIONS:
        if old_year not in wide_by_year or new_year not in wide_by_year:
            raise ValueError(
                f"county WUI table missing vintage {old_year} or {new_year}"
            )
        old = wide_by_year[old_year]
        new = wide_by_year[new_year]
        # Align on the county set (one_to_one: one row per county per vintage).
        cols = ["wui_area_km2", "interface_wui_area_km2",
                "intermix_wui_area_km2", "hu_in_wui"]
        j = old[cols].join(new[cols], how="inner", lsuffix="_old", rsuffix="_new")
        j = j.reset_index()

        j["wui_area_change_km2"] = j["wui_area_km2_new"] - j["wui_area_km2_old"]
        j["pct_wui_area_change"] = pct_change(j["wui_area_km2_new"], j["wui_area_km2_old"])
        j["interface_wui_area_change_km2"] = (
            j["interface_wui_area_km2_new"] - j["interface_wui_area_km2_old"]
        )
        j["intermix_wui_area_change_km2"] = (
            j["intermix_wui_area_km2_new"] - j["intermix_wui_area_km2_old"]
        )
        j["pct_interface_area_change"] = pct_change(
            j["interface_wui_area_km2_new"], j["interface_wui_area_km2_old"]
        )
        j["pct_intermix_area_change"] = pct_change(
            j["intermix_wui_area_km2_new"], j["intermix_wui_area_km2_old"]
        )
        j["hu_in_wui_change"] = j["hu_in_wui_new"] - j["hu_in_wui_old"]
        j["pct_hu_in_wui_change"] = pct_change(j["hu_in_wui_new"], j["hu_in_wui_old"])

        # regained/lost simple descriptors (0-area base years -> NaN pct, but
        # absolute change is still well defined)
        j["transition"] = f"{old_year}->{new_year}"
        j["base_year"] = old_year
        j["end_year"] = new_year
        j = j.merge(
            county_wui[key].drop_duplicates("GEOID"), on="GEOID", how="left"
        )
        frames.append(j)

    out = pd.concat(frames, ignore_index=True)
    out_cols = key + [
        "transition", "base_year", "end_year",
        "wui_area_change_km2", "pct_wui_area_change",
        "interface_wui_area_change_km2", "pct_interface_area_change",
        "intermix_wui_area_change_km2", "pct_intermix_area_change",
        "hu_in_wui_change", "pct_hu_in_wui_change",
        "wui_area_km2_old", "wui_area_km2_new",
    ]
    out = out[out_cols].sort_values(
        ["GEOID", "base_year", "end_year"]
    ).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    load_stage2_config()  # presence check: stage2 config must exist

    county_wui = pd.read_parquet(PROCESSED_DIR / "county_wui_by_vintage.parquet")
    change = build_change_table(county_wui)

    change.to_csv(CHANGE_CSV, index=False)
    change.to_parquet(CHANGE_PARQUET, index=False)

    full = change[change["transition"] == "1990->2020"]
    log.info(
        "Wrote %d county-transition rows (%d counties x %d transitions).",
        len(change), change["GEOID"].nunique(), len(TRANSITIONS),
    )
    log.info(
        "1990->2020 CONUS totals: WUI area %+.0f km2 (%+.1f%%), interface "
        "%+.0f km2, intermix %+.0f km2, WUI housing units %+.0f.",
        full["wui_area_change_km2"].sum(),
        full["pct_wui_area_change"].mean(),
        full["interface_wui_area_change_km2"].sum(),
        full["intermix_wui_area_change_km2"].sum(),
        full["hu_in_wui_change"].sum(),
    )
    log.info(
        "Counties losing WUI 1990->2020: %d of %d (max loss %.1f km2; max gain %.1f km2).",
        int((full["wui_area_change_km2"] < 0).sum()), len(full),
        float(-full.loc[full["wui_area_change_km2"] < 0, "wui_area_change_km2"].max()),
        float(full["wui_area_change_km2"].max()),
    )
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("wui_change.log")
    sys.exit(main())