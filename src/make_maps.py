"""Generate QA/research maps of Stage 1 county wildfire metrics.

Produces three CONUS county choropleths:
1. cumulative historical burned area (km2)
2. historical wildfire count (distinct fires)
3. percent of county in the unique historical burn footprint

These are preliminary QA graphics for spatial verification — not publication
graphics (AGENTS.md "Engineering Conventions").
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")  # headless-safe
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    FIGURES_DIR,
    PROCESSED_DIR,
    ensure_dirs,
    get_logger,
    load_analysis_config,
)

log = get_logger("maps")

ATTRIB = (
    "Sources: MTBS national burned-area boundaries (USGS EROS/USFS), "
    "Census TIGER/Line 2025 counties. QA graphic — not publication quality."
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
) -> Path:
    """Render one choropleth; counties with no recorded large fire are gray."""
    fig, ax = plt.subplots(figsize=(11, 7))

    has_value = gdf[column].fillna(0) > 0
    base = gdf.plot(
        ax=ax,
        color="#eeeeee",
        edgecolor="#999999",
        linewidth=0.15,
    )
    if has_value.any():
        vals = gdf.loc[has_value, column]
        if log_scale:
            vmin, vmax = max(vals.min(), 1e-3), vals.max()
            norm = matplotlib.colors.LogNorm(vmin=vmin, vmax=vmax)
        else:
            norm = matplotlib.colors.Normalize(vmin=0, vmax=vals.max())
        gdf.loc[has_value].plot(
            ax=ax,
            column=column,
            cmap=cmap,
            norm=norm,
            edgecolor="#999999",
            linewidth=0.15,
            legend=True,
            legend_kwds={"label": cbar_label, "shrink": 0.65},
        )
    ax.set_title(title, fontsize=13)
    ax.set_axis_off()
    fig.text(
        0.01,
        0.01,
        ATTRIB,
        fontsize=7,
        color="#555555",
    )
    out = FIGURES_DIR / fname
    fig.tight_layout()
    fig.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    log.info("Wrote %s", out)
    return out


def main(argv: list[str] | None = None) -> int:
    cfg = load_analysis_config()
    dpi = int(cfg["mapping"]["dpi"])
    ensure_dirs()

    counties = gpd.read_parquet(PROCESSED_DIR / "counties_conus.geoparquet")
    summary = pd.read_parquet(
        PROCESSED_DIR / "county_all_years_wildfire_summary.parquet"
    )

    # Left-join keeps every county; fire metrics are NaN where no fire touched.
    gdf = counties.merge(
        summary[
            [
                "GEOID",
                "cumulative_burned_area_km2",
                "unique_fire_count",
                "pct_unique_historical_burn_footprint",
            ]
        ],
        on="GEOID",
        how="left",
    )
    log.info(
        "Mapping %d CONUS counties (%d with >=1 recorded large wildfire).",
        len(gdf),
        int((gdf["unique_fire_count"].fillna(0) > 0).sum()),
    )

    outputs = [
        _choropleth(
            gdf,
            "cumulative_burned_area_km2",
            "Cumulative MTBS large-wildfire burned area by county, 1984-2024",
            "map_conus_cumulative_burned_area.png",
            cfg["mapping"]["colormap_burned_area"],
            log_scale=True,
            cbar_label="cumulative burned area, km$^2$ (log scale)",
            dpi=dpi,
        ),
        _choropleth(
            gdf,
            "unique_fire_count",
            "Count of distinct MTBS large wildfires by county, 1984-2024",
            "map_conus_wildfire_count.png",
            cfg["mapping"]["colormap_fire_count"],
            log_scale=True,
            cbar_label="distinct fires (log scale)",
            dpi=dpi,
        ),
        _choropleth(
            gdf,
            "pct_unique_historical_burn_footprint",
            "Percent of county within the unique historical burn footprint, 1984-2024",
            "map_conus_pct_unique_burn_footprint.png",
            cfg["mapping"]["colormap_footprint"],
            log_scale=True,
            cbar_label="% of county area ever burned (log scale)",
            dpi=dpi,
        ),
    ]
    log.info("Generated %d maps.", len(outputs))
    return 0


if __name__ == "__main__":
    from common import setup_logging

    setup_logging("maps.log")
    sys.exit(main())