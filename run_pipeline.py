"""One-command reproduction of Stage 1.

Runs the full pipeline in order:

    download -> preprocess MTBS -> preprocess boundaries -> spatial overlay
    -> county-year aggregation -> validation -> maps

Any stage returning non-zero (including validation critical failures) halts
the run with a non-zero exit code — Stage 1 fails loudly (AGENTS.md #9).

Usage:
    python run_pipeline.py                 # full reproduction
    python run_pipeline.py --skip-download  # reuse raw data already on disk
    python run_pipeline.py --from-stage overlay
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_county_year  # noqa: E402
import download_data  # noqa: E402
import make_maps  # noqa: E402
import preprocess_boundaries  # noqa: E402
import preprocess_mtbs  # noqa: E402
import spatial_overlay  # noqa: E402
import validate_data  # noqa: E402
from common import setup_logging  # noqa: E402

STAGES = [
    ("download", "Acquire raw MTBS + Census archives with provenance", download_data.main),
    ("preprocess_mtbs", "Inspect + clean MTBS perimeters -> GeoParquet", preprocess_mtbs.main),
    ("preprocess_boundaries", "Inspect + clean TIGER counties -> GeoParquet", preprocess_boundaries.main),
    ("overlay", "Geometric fire x county intersection (EPSG:5070)", spatial_overlay.main),
    ("aggregate", "County-year table + all-years county summary", build_county_year.main),
    ("validate", "Automated validation suite + report", validate_data.main),
    ("maps", "QA choropleth maps", make_maps.main),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument(
        "--from-stage",
        choices=[s[0] for s in STAGES],
        help="Resume the pipeline starting at this stage.",
    )
    args = parser.parse_args(argv)

    logger = setup_logging("stage1.log")
    logger.info("=" * 78)
    logger.info("Stage 1 pipeline run started")
    logger.info("=" * 78)

    started = False
    t_total = time.perf_counter()
    for name, desc, stage_main in STAGES:
        if args.from_stage and name != args.from_stage and not started:
            logger.info("[skip] %-24s %s", name, desc)
            continue
        started = True
        if name == "download" and args.skip_download:
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
        "Stage 1 pipeline completed successfully in %.1fs.", time.perf_counter() - t_total
    )
    logger.info(
        "Outputs: data/processed/, outputs/tables/, outputs/figures/, outputs/logs/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())