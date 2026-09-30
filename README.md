# Wildfire Exposure in the Wildland-Urban Interface (WUI) — UVA Systems Engineering Capstone

Reproducible geospatial research pipeline relating historical U.S. wildfire
activity to communities in the wildland-urban interface.

**Current status: Stage 1 complete; Stage 2 (WUI integration) complete** —
Stage 1 is a validated county-year wildfire spatial database built from the
authoritative MTBS record and Census TIGER/Line county boundaries. Stage 2
adds decadal SILVIS WUI exposure, a Nature 2020 sensitivity comparison, and
Census Places. See [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Repository layout

```
wildfire-wui-capstone/
├── AGENTS.md               # binding rules for humans and AI agents
├── PROJECT_STATUS.md       # stage tracker, validation results, decisions
├── run_pipeline.py         # ONE-COMMAND reproduction (--stage-set {1,2,all})
├── requirements.txt        # pinned environment (Python 3.12)
├── config/
│   ├── datasets.yaml       # authoritative source registry (URLs, metadata)
│   └── analysis.yaml       # scope, CRS, fire types, years, WUI vintages, thresholds
├── data/
│   ├── raw/                # immutable downloads + provenance.json (git-tracked)
│   ├── interim/            # reusable intermediate GeoParquet artifacts
│   └── processed/          # final products (GeoParquet/Parquet/CSV)
├── src/
│   ├── download_data.py    # provenance-aware downloader (resume, CRC, SHA-256)
│   ├── preprocess_mtbs.py  # schema inspection + cleaning + filtering
│   ├── preprocess_boundaries.py
│   ├── spatial_overlay.py  # true geometric fire × county intersection
│   ├── build_county_year.py# county-year + all-years aggregation + footprints
│   ├── validate_data.py    # 25-check Stage 1 validation suite, fails loudly
│   ├── make_maps.py        # Stage 1 QA choropleths
│   ├── preprocess_wui.py   # SILVIS GDB → fragments → county WUI metrics
│   ├── build_wui_fire.py   # temporal alignment + fire × WUI × county intersection
│   ├── wui_change.py       # county WUI change 1990→2020
│   ├── nature_wui.py       # Schug 2023 raster → county metrics (chunked, resumable)
│   ├── compare_wui.py      # SILVIS 2020 vs Nature 2020 sensitivity comparison
│   ├── places_wui.py       # Census Places workflow (separate outputs)
│   ├── validate_stage2.py  # Stage 2 validation suite, fails loudly
│   └── make_maps_stage2.py # 7 Stage 2 QA figures
├── tests/                  # pytest suite (pure deterministic functions)
├── outputs/                # figures/, tables/, logs/ (validation report lives here)
└── docs/                   # methodology, data dictionary, data sources
```

## Reproducing the pipeline from a fresh environment

Prerequisites: Python 3.12+, ~10 GB disk for raw data (SILVIS GDB 4.3 GB +
Nature raster 4.1 GB + MTBS 0.4 GB), ~6 GB RAM.

```bash
# 1. Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 2. Install pinned dependencies
pip install -r requirements.txt

# 3. Stage 1 (downloads ~450 MB of raw data on first run)
python run_pipeline.py                     # or: make stage1

# 4. Stage 2 (downloads ~8.5 GB of WUI raw data on first run;
#    requires Stage 1 outputs — fails loudly if they are missing)
python run_pipeline.py --stage-set 2       # or: make stage2
```

`python run_pipeline.py --stage-set all` chains Stage 1 then Stage 2 in one
command. Because the Nature raster mosaic is very large, the Stage 2 raster
step is **checkpointed** (`data/interim/nature_county_class_pixels.json`) and
resumable.

Useful variants:

```bash
python run_pipeline.py --skip-download            # reuse raw data already in data/raw
python run_pipeline.py --from-stage overlay       # resume Stage 1 from a stage
python run_pipeline.py --stage-set 2 --skip-download  # Stage 2 without re-validating archives
pytest tests/                                    # run the full test suite
```

> **macOS/iCloud warning:** do not keep this repository in an
> iCloud-synced folder with "Optimize Mac Storage" enabled. During Stage 2
> development, iCloud evicted repository files under disk pressure and several
> (including the virtual environment) could not be re-materialized, which
> corrupted Stage 1 artifacts and stalled runs. Keep the repo (and its
> virtualenv) on local-only storage; a venv outside the repo works fine.
>
> **External data cache (portable):** large artifacts can live outside the
> repo without hardcoding a username path. A fresh clone can:
>
> 1. **Redirect all data tiers** (recommended on iCloud or small disks):
>    ```bash
>    export WILDFIRE_WUI_DATA_ROOT="$HOME/.cache/wildfire-wui-capstone"
>    # equivalently: echo '$HOME/.cache/wildfire-wui-capstone' > .data_root
>    python run_pipeline.py --stage-set 2
>    ```
>    The pipeline creates that directory tree automatically (`raw/`,
>    `interim/`, `processed/`).
> 2. **Keep in-repo `data/` paths and offload large files** to
>    `$WILDFIRE_WUI_CACHE` (default `~/.cache/wildfire-wui-capstone`),
>    replacing each with a symlink:
>    ```bash
>    python src/offload_data.py            # or --dry-run first
>    ```
>
> `ensure_dirs()` always creates the cache layout. Override the cache
> location with `WILDFIRE_WUI_CACHE` if needed. None of these options
> embed `/Users/<username>/...` in source.

### What the Stage 1 command does

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

### What the Stage 2 command does

1. **Download** the SILVIS WUI change v4 FileGDB, the Schug et al. (2023)
   North America raster product (Zenodo), and 49 Census TIGER/Line Place
   state files — same CRC/SHA-256 provenance discipline.
2. **Preprocess WUI**: extract + inspect the SILVIS GDB, build
   block-fragment GeoParquet in EPSG:5070, compute county WUI metrics per
   vintage (1990/2000/2010/2020).
3. **Fire × WUI × county**: true geometric intersection with **period-matched
   WUI** (1990→fires 1990–99, 2000→2000–09, 2010→2010–19, 2020→2020–24;
   1984–89 marked `pre_1990_no_wui_baseline` with null exposure) →
   county-year and county-period WUI/fire tables.
4. **WUI change** metrics 1990→2000→2010→2020 per county.
5. **Nature 2020** raster processing (chunked, resumable) as a modern
   sensitivity layer only, then the **SILVIS vs Nature 2020 comparison**
   (correlations, scatter, disagreement map).
6. **Places**: merged TIGER Place files × WUI vintages × fire history as
   separate place-level outputs.
7. **Validate** with the Stage 2 suite (appended to the same validation
   report) and **map** 7 QA figures.

Full Stage 2 methodology: [`docs/wui_methodology.md`](docs/wui_methodology.md).

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
| `data/processed/county_wui_by_vintage.parquet` (+ CSV) | county WUI area/composition/housing per 1990/2000/2010/2020 snapshot |
| `data/processed/county_year_wui_fire.parquet` (+ CSV) | county × year with vintage-matched WUI burned area and concepts A–D |
| `data/processed/county_period_wui_fire.parquet` (+ CSV) | county × period (1990-99 … 2020-24) summaries |
| `data/processed/county_wui_change_1990_2020.csv` (+ Parquet) | county WUI change per transition |
| `data/processed/county_nature_wui_2020.parquet` (+ CSV) | Schug 2023 raster WUI per county (2020-only sensitivity) |
| `data/processed/county_wui_silvis_vs_nature_2020.parquet` (+ CSV) | 2020 product comparison |
| `data/processed/wui/place_*.parquet` (+ CSV) | Census Places WUI / fire / exposure outputs (separate from counties) |
| `outputs/tables/data_validation_report.csv` | Stage 1 + Stage 2 check results |
| `outputs/figures/*.png` | QA maps (3 Stage 1 + 7 Stage 2) |

## Project roadmap

- **Stage 1 (done)**: MTBS wildfire × county spatial database.
- **Stage 2 (done)**: SILVIS U.S. WUI 1990/2000/2010/2020, temporal
  alignment, fire × WUI × county intersection, WUI change, Schug et al.
  (2023) Nature 2020 sensitivity comparison, Census Places.
- **Stage 3 (planned)**: environmental predictors (LST, precipitation, soil
  moisture, vegetation, drought, topography).
- **Stage 4 (planned)**: predictive modeling with **spatial/temporal
  holdouts** (never random row splits) — gated on validation passing
  (AGENTS.md rule #11).

## House rules

Read [`AGENTS.md`](AGENTS.md) before contributing: never invent metadata,
inspect schemas before processing, never overwrite raw data, record sources
and dates, validate every stage, and no modeling until validation passes.