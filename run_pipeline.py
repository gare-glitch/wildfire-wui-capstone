"""One-command reproduction of the pipeline.

Stage 1 (default):
    download -> preprocess MTBS -> preprocess boundaries -> spatial overlay
    -> county-year aggregation -> validation -> maps

Stage 2 (WUI integration):
    python run_pipeline.py --stage-set 2
    download WUI/place archives -> SILVIS preprocess (schema-first) ->
    vintage-aligned fire x WUI intersection -> WUI change -> Nature 2020 ->
    SILVIS vs Nature comparison -> Census Places -> Stage 2 validation ->
    Stage 2 QA maps

Stage 2 assumes the Stage 1 outputs already exist (run the default first).

Any stage returning non-zero (including validation critical failures) halts
the run with a non-zero exit code — the pipeline fails loudly (AGENTS.md #9).

Usage:
    python run_pipeline.py                     # Stage 1 full reproduction
    python run_pipeline.py --skip-download     # Stage 1, reuse raw data
    python run_pipeline.py --stage-set 2       # Stage 2 (needs Stage 1 done)
    python run_pipeline.py --stage-set 2 --skip-download   # reuse raw data
    python run_pipeline.py --from-stage overlay            # resume mid-set
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# PROJ environment guard (see src/nature_wui.py): drop foreign PROJ data
# overrides before any geospatial library import so bundled proj.db is used.
for _var in ("PROJ_LIB", "PROJ_DATA", "PROJ_NETWORK"):
    os.environ.pop(_var, None)

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_county_year  # noqa: E402
import build_wui_fire  # noqa: E402
import compare_wui  # noqa: E402
import download_data  # noqa: E402
import make_maps  # noqa: E402
import make_maps_stage2  # noqa: E402
import nature_wui  # noqa: E402
import places_wui  # noqa: E402
import preprocess_boundaries  # noqa: E402
import preprocess_mtbs  # noqa: E402
import preprocess_wui  # noqa: E402
import spatial_overlay  # noqa: E402
import validate_data  # noqa: E402
import validate_stage2  # noqa: E402
import wui_change  # noqa: E402
from common import setup_logging  # noqa: E402

STAGES_STAGE1 = [
    ("download", "Acquire raw MTBS + Census archives with provenance", download_data.main),
    ("preprocess_mtbs", "Inspect + clean MTBS perimeters -> GeoParquet", preprocess_mtbs.main),
    ("preprocess_boundaries", "Inspect + clean TIGER counties -> GeoParquet", preprocess_boundaries.main),
    ("overlay", "Geometric fire x county intersection (EPSG:5070)", spatial_overlay.main),
    ("aggregate", "County-year table + all-years county summary", build_county_year.main),
    ("validate", "Automated validation suite + report", validate_data.main),
    ("maps", "QA choropleth maps", make_maps.main),
]

STAGE2_DATASETS = "silvis_wui,nature_wui,census_places"


def _download_wui(argv: list[str]) -> int:
    return download_data.main(["--only", STAGE2_DATASETS] + list(argv))


STAGES_STAGE2 = [
    ("download_wui", f"Acquire SILVIS + Nature + Census Places archives ({STAGE2_DATASETS})", _download_wui),
    ("preprocess_wui", "SILVIS GDB: schema inspection -> WUI fragments -> county WUI by vintage", preprocess_wui.main),
    ("build_wui_fire", "Vintage-aligned fire x WUI x county intersection + county-year/period tables", build_wui_fire.main),
    ("wui_change", "County WUI change metrics (1990->2000->2010->2020)", wui_change.main),
    ("nature_wui", "Schug/Nature 2020 raster: chunked CONUS zonal statistics", nature_wui.main),
    ("compare_wui", "SILVIS vs Nature 2020 county comparison", compare_wui.main),
    ("places_wui", "Census Places: WUI, fire history, WUI fire exposure", places_wui.main),
    ("validate_stage2", "Stage 2 validation suite + combined report", validate_stage2.main),
    ("maps_stage2", "Stage 2 QA maps and plots", make_maps_stage2.main),
]

_STAGE_SETS = {
    "1": STAGES_STAGE1,
    "2": STAGES_STAGE2,
    "all": STAGES_STAGE1 + STAGES_STAGE2,
}


def _require_stage1_outputs() -> None:
    """Stage 2 stages consume validated Stage 1 artifacts; fail loudly if absent."""
    from common import PROCESSED_DIR

    required = [
        "mtbs_wildfires_conus.geoparquet",
        "counties_conus.geoparquet",
        "county_year_wildfire.parquet",
    ]
    missing = [f for f in required if not (PROCESSED_DIR / f).exists()]
    if missing:
        raise SystemExit(
            "Stage 1 outputs missing: "
            + ", ".join(missing)
            + ". Run `python run_pipeline.py` (Stage 1) first."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument(
        "--stage-set",
        choices=["1", "2", "all"],
        default="1",
        help="Which pipeline stage-set to run (default: 1 = Stage 1 only).",
    )
    parser.add_argument(
        "--from-stage",
        help="Resume the stage-set starting at this stage name.",
    )
    args = parser.parse_args(argv)

    stages = _STAGE_SETS[args.stage_set]
    valid_names = {name for name, _, _ in stages}
    if args.from_stage and args.from_stage not in valid_names:
        parser.error(
            f"--from-stage {args.from_stage!r} is not a stage of stage-set "
            f"{args.stage_set!r}; valid: {sorted(valid_names)}"
        )

    if args.stage_set in ("2", "all"):
        _require_stage1_outputs()

    logger = setup_logging("stage1.log" if args.stage_set == "1" else "stage2.log")
    logger.info("=" * 78)
    logger.info("Pipeline run started (stage-set %s)", args.stage_set)
    logger.info("=" * 78)

    started = False
    t_total = time.perf_counter()
    for name, desc, stage_main in stages:
        if args.from_stage and name != args.from_stage and not started:
            logger.info("[skip] %-24s %s", name, desc)
            continue
        started = True
        if name in ("download", "download_wui") and args.skip_download:
            logger.info("[skip] %-24s %s (--skip-download)", name, desc)
            continue

        logger.info("-" * 78)
        logger.info("[start] %-24s %s", name, desc)
        t0 = time.perf_counter()
        rc = stage_main([])
        elapsed = time.perf_counter() - t0
        logger.info("[done ] %-24s rc=%d (%.1fs)", name, rc, elapsed)
        if rc != 0:
            logger.error(
                "Stage '%s' FAILED (exit code %d). Halting pipeline — see "
                "outputs/logs/ for details.",
                name,
                rc,
            )
            return rc

    logger.info("=" * 78)
    logger.info(
        "Pipeline (stage-set %s) completed successfully in %.1fs.",
        args.stage_set, time.perf_counter() - t_total,
    )
    logger.info(
        "Outputs: data/processed/, outputs/tables/, outputs/figures/, outputs/logs/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())