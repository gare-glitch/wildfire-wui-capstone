# AGENTS.md — Persistent Instructions for AI Agents and Contributors

This repository is a reproducible geospatial research pipeline for a University of
Virginia Systems Engineering capstone studying wildfire exposure in the U.S.
wildland-urban interface (WUI). All agents (human or AI) working in this repo must
follow these rules.

## Non-Negotiable Rules

1. **Never invent fields, URLs, metadata, CRS information, or results.** Every
   field name, coordinate reference system, source URL, and statistic must come
   from an inspected file, an authoritative published document, or a recorded
   pipeline step. If a fact cannot be verified, mark it explicitly as
   UNVERIFIED or omit it.
2. **Inspect schemas before processing.** Before writing any processing logic
   against a dataset, load and print its actual column names, dtypes, CRS,
   geometry types, and category values. Do not rely on memory or documentation
   alone.
3. **Never overwrite raw data.** Everything under `data/raw/` is immutable.
   Processed outputs are written to `data/interim/` or `data/processed/` only.
   If a raw file is corrupt, quarantine it (rename with `.corrupt` suffix) and
   re-download — never modify it in place.
4. **Record source URLs and download dates.** Every dataset consumed by the
   pipeline must have an entry in `docs/data_sources.md` and a machine-readable
   provenance record in `data/raw/provenance.json` (written by
   `src/download_data.py`).
5. **Use appropriate projected coordinate systems for area.** Never compute
   areas in latitude/longitude. CONUS area measurements use
   `EPSG:5070 (NAD83 / Conus Albers, meters)`. Non-CONUS jurisdictions must not
   be measured in CONUS projections; they are excluded from Stage 1 and
   documented (see `docs/methodology.md`).
6. **GEOID is the canonical county identifier.** All county keys, joins, and
   outputs key on the Census GEOID (STATEFP + COUNTYFP string, zero-padded).
7. **FIRE_ID is the canonical MTBS event identifier** where present in the
   source. Event counts must count DISTINCT FIRE_ID values, never geometry
   fragments.
8. **Save reusable intermediate spatial files as GeoParquet.** Any spatial
   artifact that a later stage re-reads must be written as GeoParquet
   (`.gpkg` only where a tool requires otherwise, which must be justified).
9. **Validate after every major stage.** Each pipeline stage ends by invoking
   the validation framework. `run_pipeline.py` fails loudly (non-zero exit) on
   any CRITICAL validation failure.
10. **Document every major assumption** in `docs/methodology.md` and, for
    stage-level decisions, in `PROJECT_STATUS.md` under "Key Decisions".
11. **Modeling is prohibited until spatial data validation passes.** No
    regression, Random Forest, XGBoost, or other modeling code may be added
    while `PROJECT_STATUS.md` lists validation as incomplete.
12. **Future model evaluation must use spatial and/or temporal holdouts**
    (e.g., leave-one-state-out, hold out entire fire years), never random
    row-level splits alone. Random splits on spatially autocorrelated data
    leak information between train and test sets.
13. **Update `PROJECT_STATUS.md` after every completed stage.**

## Scope Discipline

- Stage 1 (current): MTBS wildfire × U.S. county spatial database ONLY.
- Do NOT implement WUI datasets (SILVIS, Schug et al. 2023), environmental
  satellite predictors (LST, precipitation, soil moisture, drought indices,
  vegetation indices), topographic variables, or any predictive modeling.
  These are planned later stages (see "Future Work" in `docs/methodology.md`).

## Engineering Conventions

- Python 3.12+. Virtual environment lives in `.venv/` (not committed).
- Dependencies are pinned in `requirements.txt`.
- Entry point: `python run_pipeline.py` (equivalently `make stage1`).
  It chains: download → preprocess → overlay → aggregate → validate → maps.
- All heavy steps log to `outputs/logs/` with timestamps.
- Maps produced by the pipeline are QA/research graphics, not publication
  graphics.
- Tests: `pytest tests/` must pass before declaring a stage complete. Add a
  test for every deterministic transformation you introduce.
- Commits should follow the pipeline milestones (structure, download,
  preprocess, overlay, aggregate, validate, document).

## Data Handling Notes

- MTBS maps only **large fires** (roughly ≥ 405 ha / 1,000 ac in the western
  U.S., ≥ 202 ha / 500 ac in the eastern U.S. in practice). Never describe
  MTBS counts as "all fires."
- MTBS coverage: CONUS + Alaska + Hawaii + Puerto Rico, 1984–present. The
  most recent year(s) may be incomplete; the pipeline pins an explicit
  completed end year in `config/analysis.yaml`.
- Cumulative burned area can legitimately exceed county area across many
  years (reburning). Annual burned area exceeding county area requires
  investigation (usually overlapping perimeters of distinct incidents).
- Virginia independent cities are county equivalents (GEOID-level rows in
  TIGER/Line) and must never be dropped.