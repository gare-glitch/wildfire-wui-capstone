# Wildfire Exposure in the Wildland-Urban Interface (WUI) — UVA Systems Engineering Capstone

Reproducible geospatial research pipeline relating historical U.S. wildfire
activity to communities in the wildland-urban interface.

**Current status: Stage 1 complete** — a validated county-year wildfire
spatial database built from the authoritative MTBS record and Census
TIGER/Line county boundaries. See [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Repository layout

```
wildfire-wui-capstone/
├── AGENTS.md               # binding rules for humans and AI agents
├── PROJECT_STATUS.md       # stage tracker, validation results, decisions
├── run_pipeline.py         # ONE-COMMAND Stage 1 reproduction
├── requirements.txt        # pinned environment (Python 3.12)
├── config/
│   ├── datasets.yaml       # authoritative source registry (URLs, metadata)
│   └── analysis.yaml       # scope, CRS, fire types, years, thresholds
├── data/
│   ├── raw/                # immutable downloads + provenance.json (git-tracked)
│   ├── interim/            # reusable intermediate GeoParquet artifacts
│   └── processed/          # final Stage 1 products (GeoParquet/Parquet/CSV)
├── src/
│   ├── download_data.py    # provenance-aware downloader (resume, CRC, SHA-256)
│   ├── preprocess_mtbs.py  # schema inspection + cleaning + filtering
│   ├── preprocess_boundaries.py
│   ├── spatial_overlay.py  # true geometric fire × county intersection
│   ├── build_county_year.py# county-year + all-years aggregation + footprints
│   ├── validate_data.py    # 25-check validation suite, fails loudly
│   └── make_maps.py        # QA choropleths
├── tests/                  # pytest suite (pure deterministic functions)
├── outputs/                # figures/, tables/, logs/ (validation report lives here)
└── docs/                   # methodology, data dictionary, data sources
```

## Reproducing Stage 1 from a fresh environment

Prerequisites: Python 3.12+, ~1 GB disk for raw data, ~4 GB RAM.

```bash
# 1. Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 2. Install pinned dependencies
pip install -r requirements.txt

# 3. Run everything (downloads ~450 MB of raw data on first run)
python run_pipeline.py
```

`make stage1` is an equivalent entry point (see `Makefile`).

Useful variants:

```bash
python run_pipeline.py --skip-download   # reuse raw data already in data/raw
python run_pipeline.py --from-stage overlay  # resume from a stage
pytest tests/                            # run the test suite
```

### What the one command does

1. **Download** the MTBS national burned-area-boundary archive (USGS EROS)
   and the 2025 Census TIGER/Line national county file, verifying each ZIP's
   CRC integrity and recording SHA-256 + retrieval time to
   `data/raw/provenance.json`. Existing valid archives are never re-downloaded.
2. **Preprocess** both datasets with schema inspection reports
   (`outputs/tables/schema_*`), loud assertions on CRS/GEOID uniqueness/
   Virginia independent cities, and geometry repair counts.
3. **Overlay**: exact geometric intersection of wildfire perimeters with
   counties in **EPSG:5070** (CONUS Albers, meters) — multi-county fires
   contribute their true intersected area to every county.
4. **Aggregate** to a county-year table and an all-years county summary,
   including the dissolved **unique historical burn footprint**.
5. **Validate** with 25 automated checks; any CRITICAL failure aborts the run
   with a non-zero exit code. Report:
   `outputs/tables/data_validation_report.csv` + `validation_summary.md`.
6. **Map** three CONUS QA choropleths into `outputs/figures/`.

## Stage 1 analysis scope (summary — full detail in docs/methodology.md)

- **Fires**: MTBS perimeters with `incid_type ∈ {Wildfire, Wildland Fire
  Use}`, ignition years **1984–2024** (2025+ records are incomplete in the
  national archive), CONUS only (AK/HI/PR preserved separately for a later
  stage with jurisdiction-appropriate projections).
- **Counties**: 2025 TIGER/Line county equivalents, 48 states + DC
  (3,109 geographies, Virginia independent cities included).
- **Denominators**: full legal county area (incl. water) in EPSG:5070 —
  never latitude/longitude.

## Key outputs

| File | Contents |
|---|---|
| `data/processed/mtbs_wildfires_conus.geoparquet` | cleaned CONUS wildfire perimeters |
| `data/processed/county_year_wildfire.parquet` (+ CSV) | county × year burned area, fire counts, % burned |
| `data/processed/county_all_years_wildfire_summary.parquet` (+ CSV) | cumulative burned area, unique footprint, counts per county |
| `outputs/tables/data_validation_report.csv` | 25-check validation results |
| `outputs/figures/map_conus_*.png` | QA maps |

## Project roadmap

- **Stage 1 (done)**: MTBS wildfire × county spatial database.
- **Stage 2 (planned)**: SILVIS U.S. WUI 1990/2000/2010/2020 and Schug et al.
  (2023) global WUI integration.
- **Stage 3 (planned)**: environmental predictors (LST, precipitation, soil
  moisture, vegetation, drought, topography).
- **Stage 4 (planned)**: predictive modeling with **spatial/temporal
  holdouts** (never random row splits) — gated on validation passing
  (AGENTS.md rule #11).

## House rules

Read [`AGENTS.md`](AGENTS.md) before contributing: never invent metadata,
inspect schemas before processing, never overwrite raw data, record sources
and dates, validate every stage, and no modeling until validation passes.