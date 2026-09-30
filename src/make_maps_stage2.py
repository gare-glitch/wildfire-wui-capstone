"""Generate QA/research maps and plots for Stage 2 (WUI integration).

Seven preliminary graphics for spatial verification — not publication
graphics (AGENTS.md "Engineering Conventions"):

1. CONUS county WUI percentage, SILVIS 1990 snapshot
2. CONUS county WUI percentage, SILVIS 2020 snapshot
3. County WUI area change 1990->2020 (diverging)
4. Historical (1990-2024) WUI burned area, vintage-aligned
5. Percent of WUI burned within each vintage's period (area-weighted)
6. SILVIS vs Nature 2020 county WUI scatter (+ correlation annotations)
7. SILVIS vs Nature 2020 disagreement map (percentage points)

Scientific guardrail: none of these depict "wildfire risk". They show
hazard/history (MTBS burned area), exposure (fire overlap with WUI), and
WUI development (human development adjacent to wildland vegetation).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")  # headless-safe
import matplotlib.colors  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    FIGURES_DIR,
    PROCESSED_DIR,
    TABLES_DIR,
    WUI_DIR,
    ensure_dirs,
    get_logger,
    load_stage2_config,
)

log = get_logger("maps_stage2")

ATTRIB = (
    "Sources: SILVIS WUI Change v4 (1990-2020), Schug et al. 2023 global WUI "
    "(2020), MTBS burned-area boundaries, Census TIGER/Line 2025 counties. "
    "QA graphic — not publication quality."
)


def _choropleth(
    gdf: gpd.GeoDataFrame,
    column: str,
    title: str,
    fname: str,
    cmap: str,
    log_scale: bool,
    cbar_label: str,
    dpi: int,
    diverging: bool = False,
) -> Path:
    """Render one county choropleth; counties with no value render gray.

    Diverging maps center the colormap at 0 so gains and losses read
    symmetrically.
    """
    fig, ax = plt.subplots(figsize=(11, 7))
    gdf.plot(ax=ax, color="#eeeeee", edgecolor="#999999", linewidth=0.15)
    has_value = gdf[column].notna() & (gdf[column].fillna(0) != 0)
    if has_value.any():
        vals = gdf.loc[has_value, column]
        if diverging:
            vmax = float(np.nanmax(np.abs(vals))) or 1.0
            norm = matplotlib.colors.TwoSlopeNorm(vmin=-vmax, vcenter=0.0, vmax=vmax)
        elif log_scale:
            vmin, vmax = max(float(vals.min()), 1e-3), float(vals.max())
            norm = matplotlib.colors.LogNorm(vmin=vmin, vmax=vmax)
        else:
            norm = matplotlib.colors.Normalize(vmin=0, vmax=float(vals.max()))
        gdf.loc[has_value].plot(
            ax=ax, column=column, cmap=cmap, norm=norm,
            edgecolor="#999999", linewidth=0.15, legend=True,
            legend_kwds={"label": cbar_label, "shrink": 0.65},
        )
    ax.set_title(title, fontsize=13)
    ax.set_axis_off()
    fig.text(0.01, 0.01, ATTRIB, fontsize=7, color="#555555")
    out = FIGURES_DIR / fname
    fig.tight_layout()
    fig.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    log.info("Wrote %s", out)
    return out


def _wui_burn_share(county_period: pd.DataFrame) -> pd.Series:
    """Area-weighted share of WUI burned across the four vintage periods.

    For each period the unique WUI burn footprint is compared with that
    period's own WUI area (temporal alignment preserved); the county
    summary weights each period by its WUI area:
        sum_p(footprint_p) / sum_p(wui_area_p).
    """
    numerator = (
        county_period.groupby("GEOID")["unique_wui_burned_footprint_km2"].sum()
    )
    denominator = county_period.groupby("GEOID")["wui_area_km2"].sum()
    share = 100.0 * numerator / denominator.replace(0, np.nan)
    return share.rename("pct_wui_burned_weighted")


def main(argv: list[str] | None = None) -> int:
    s2 = load_stage2_config()
    mapping = s2.get("mapping", {})
    dpi = int(mapping.get("dpi", 200))
    cmap_pct = mapping.get("colormap_wui_pct", "Greens")
    cmap_change = mapping.get("colormap_wui_change", "BrBG")
    cmap_burned = mapping.get("colormap_wui_burned", "OrRd")
    cmap_disagree = mapping.get("colormap_disagreement", "RdBu")
    ensure_dirs()

    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    county_wui = pd.read_parquet(PROCESSED_DIR / "county_wui_by_vintage.parquet")
    county_year = pd.read_parquet(PROCESSED_DIR / "county_year_wui_fire.parquet")
    county_period = pd.read_parquet(PROCESSED_DIR / "county_period_wui_fire.parquet")
    comparison = pd.read_parquet(PROCESSED_DIR / "county_wui_silvis_vs_nature_2020.parquet")

    n_figs = 0

    # --- 1 & 2: WUI percentage by vintage ---------------------------------
    for year, fname in ((1990, "wui_pct_1990.png"), (2020, "wui_pct_2020.png")):
        sub = county_wui[county_wui["wui_year"] == year][
            ["GEOID", "pct_county_land_area_wui"]
        ]
        gdf = counties.merge(sub, on="GEOID", how="left")
        _choropleth(
            gdf, "pct_county_land_area_wui",
            f"County WUI share of Census land area — SILVIS {year} (%)",
            fname, cmap_pct, log_scale=False,
            cbar_label=f"% of Census land area (ALAND) classified as WUI ({year})",
            dpi=dpi,
        )
        n_figs += 1

    # --- 3: WUI area change 1990 -> 2020 -----------------------------------
    change_p = PROCESSED_DIR / "county_wui_change_1990_2020.parquet"
    if change_p.exists():
        change = pd.read_parquet(change_p)
        full = change[change["transition"] == "1990->2020"][
            ["GEOID", "wui_area_change_km2"]
        ]
        gdf = counties.merge(full, on="GEOID", how="left")
        _choropleth(
            gdf, "wui_area_change_km2",
            "County WUI area change, SILVIS 1990 -> 2020 (km2)",
            "wui_change_1990_2020.png", cmap_change, log_scale=False,
            cbar_label="WUI area change 1990-2020 (km2)", dpi=dpi, diverging=True,
        )
        n_figs += 1
    else:
        log.warning("Change table missing; skipping figure 3.")

    # --- 4: historical WUI burned area (vintage-aligned, 1990-2024) ---------
    burn = (
        county_year[county_year["wui_burned_area_km2"].notna()]
        .groupby("GEOID", as_index=False)["wui_burned_area_km2"].sum()
    )
    gdf = counties.merge(burn, on="GEOID", how="left")
    _choropleth(
        gdf, "wui_burned_area_km2",
        "County WUI burned area 1990-2024, vintage-aligned (km2, cumulative perimeters)",
        "wui_burned_area_historical.png", cmap_burned, log_scale=True,
        cbar_label="WUI burned area, km2 (log scale)", dpi=dpi,
    )
    n_figs += 1

    # --- 5: percent of WUI burned (period-weighted) ------------------------
    share = _wui_burn_share(county_period).reset_index()
    gdf = counties.merge(share, on="GEOID", how="left")
    _choropleth(
        gdf, "pct_wui_burned_weighted",
        "Percent of WUI burned within each vintage's period "
        "(area-weighted 1990-2024, unique footprints)",
        "pct_wui_burned.png", cmap_burned, log_scale=False,
        cbar_label="% of county WUI burned (period-weighted)", dpi=dpi,
    )
    n_figs += 1

    # --- 6: SILVIS vs Nature scatter ----------------------------------------
    summary_p = TABLES_DIR / "wui_silvis_vs_nature_summary.json"
    with open(summary_p, encoding="utf-8") as fh:
        summary = json.load(fh)
    r = summary["area_correlation_all_counties"]["pearson"]
    rho = summary["area_correlation_all_counties"]["spearman"]
    fig, ax = plt.subplots(figsize=(8, 8))
    x = comparison["pct_county_silvis_wui"]
    y = comparison["pct_county_nature_wui"]
    ax.scatter(x, y, s=6, alpha=0.35, edgecolors="none", color="#1f77b4")
    lim = float(np.nanmax([x.max(), y.max()])) * 1.02
    ax.plot([0, lim], [0, lim], color="#888888", linewidth=1, linestyle="--",
            label="1:1 line")
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.set_xlabel("SILVIS 2020: WUI share of county area (%)")
    ax.set_ylabel("Schug et al. 2023 (Nature) 2020: WUI share of county area (%)")
    ax.set_title(
        f"County WUI share: SILVIS vs Nature 2020\n"
        f"r={r:.3f}, Spearman rho={rho:.3f} (n={len(comparison):,} counties)",
        fontsize=12,
    )
    ax.legend(loc="upper left", fontsize=9)
    fig.text(0.01, 0.01, ATTRIB, fontsize=7, color="#555555")
    out = FIGURES_DIR / "silvis_vs_nature_scatter.png"
    fig.tight_layout()
    fig.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    log.info("Wrote %s", out)
    n_figs += 1

    # --- 7: disagreement map -------------------------------------------------
    gdf = counties.merge(
        comparison[["GEOID", "wui_pct_point_diff"]], on="GEOID", how="left"
    )
    _choropleth(
        gdf, "wui_pct_point_diff",
        "WUI share disagreement: Nature 2020 minus SILVIS 2020 (percentage points)",
        "silvis_vs_nature_disagreement.png", cmap_disagree, log_scale=False,
        cbar_label="Nature WUI% - SILVIS WUI% (pct points)", dpi=dpi,
        diverging=True,
    )
    n_figs += 1

    log.info("Stage 2 QA figures complete: %d written to %s", n_figs, FIGURES_DIR)
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("make_maps_stage2.log")
    sys.exit(main())