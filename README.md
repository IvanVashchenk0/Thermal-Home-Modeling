# Thermal Home Modeling

A local Python research application that generates provisional one-zone residential
building models, runs annual EnergyPlus experiments from the command line, and
displays saved results in a read-only Streamlit viewer.

Residential V0 varies six research cities, three one-story house sizes, six reviewed
wall assemblies, city-eligible attic R-values, and four physically distinct
orientations. Its rectangular geometry, ground-contact slab, glazing, schedules,
internal gains, and ideal-load HVAC remain provisional. Reported heating and cooling
figures are annual **ideal zone loads in joules**, not purchased fuel or electricity.
The generated models and outputs are suitable for software verification and
comparative exploration, not calibrated home-performance claims.

## Stack and installation

Python 3.11+, EnergyPlus installed separately, EPW weather files, pandas/PyArrow,
openpyxl, pytest, and Streamlit. On macOS or Linux:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Set `ENERGYPLUS_EXE` in a local `.env` if the executable is not on PATH.
Copy `.env.example` for available settings. EnergyPlus must include its
`ExampleFiles` and `WeatherData` directories; the current installation
detector checks those assets even for residential CLI runs.

## Catalog and weather setup

The active normalized catalog, its revision pointer, reviewed mappings, and V0
assumptions are included in `catalogs/`. The original source workbook is excluded
from Git; it is not needed to run the published revision. Machine-specific validation
records are also excluded; their historical findings are summarized in `docs/`.
To import a revised, authorized workbook locally:

```sh
.venv/bin/python main.py catalog-import --workbook /path/to/workbook.xlsx
```

Review physical mapping changes before running: importing does not silently update
`residential_mappings.json`. Five pinned research weather archives are restored
and checked with:

```sh
.venv/bin/python main.py weather-fetch
```

Washington DC additionally needs the matching Dulles TMY3 EPW in the installed
EnergyPlus `WeatherData` directory. All six city bindings are validated against
EPW station metadata; no substitute station is selected automatically. The weather
archives and source workbook are excluded from the intended Git inventory.

## Run residential campaigns

```sh
# Inspect exact valid/new/cached counts without launching EnergyPlus
.venv/bin/python main.py residential-batch --count 100 --seed 42 --dry-run

# Persistent local worker; four concurrent EnergyPlus processes at most
.venv/bin/python main.py residential-batch --count 100 --sampling balanced --seed 42 --workers 4 --background

# Use an editable saved selection, or run the complete supported factorial
.venv/bin/python main.py residential-batch --config examples/residential_validation_12.json --workers 1 --fresh
.venv/bin/python main.py residential-batch --config examples/residential_all_cities.json --sampling full_factorial --workers 8 --background
```

Use `--mode sequential` for serial execution, `--fresh` to bypass verified cache
reuse, and `--save-config` to preserve the resolved experiment specification.
Fixed-count selection supports seeded `random` and `balanced` sampling. Do not
start a full factorial campaign casually: it contains 1,728 unique cases.

By default, the CLI prints job and batch directories under the sibling `../output/` root.
Set `THM_INPUT_ROOT` and `THM_OUTPUT_ROOT` to absolute paths to place residential
artifacts elsewhere. Generated epJSON inputs are under the selected input root's
`generated/` directory. Each run keeps its own
`result.json`, EnergyPlus SQLite database, diagnostics, and logs. Each batch keeps
snapshots, a manifest, `batch_results.csv`, `batch_results.parquet`, and a timing
summary. These local outputs are ignored by Git.

```sh
.venv/bin/python main.py residential-batch --job-status /path/to/output/jobs/job_ID
.venv/bin/python main.py residential-batch --resume-job /path/to/output/jobs/job_ID --workers 4
.venv/bin/python main.py residential-batch --resume-batch /path/to/output/batches/residential_ID --workers 4
```

Resume creates a new attempt and revalidates completed cases before reuse.
Keep the same configured roots when resuming; omit `--fresh` to reuse validated
completed cases. `--workers auto` uses the smallest known Slurm task allocation,
CPU affinity, and cgroup quota. Any positive manual count is accepted but is
still capped by those limits. Batch manifests and summaries save the request,
detected limits, and effective worker count.

After a campaign has stopped, verify or recover its derived exports using:

```sh
.venv/bin/python main.py residential-verify --batch /path/to/output/batches/residential_ID
.venv/bin/python main.py residential-recover --batch /path/to/output/batches/residential_ID
```

Verification requires one successful, unique result per saved case, matching
indexed JSON, CSV, and Parquet data, plus traceable model and raw files. Recovery
rebuilds both exports from indexed records; it cannot manufacture missing or
failed simulations. Run recovery only after the original writer has stopped.
`--compare-batch` compares timing only for equivalent, successful, entirely fresh
case sets. Run `.venv/bin/python main.py residential-batch --help` for all options.

## View saved results

```sh
.venv/bin/streamlit run streamlit_app.py
```

The viewer opens existing residential batch indexes without detecting or launching
EnergyPlus. It filters configurations, shows annual ideal heating/cooling loads,
status and warnings, batch summaries, provenance, and selected file downloads.
Running new simulations, stopping workers, and resuming jobs are CLI operations.

## Verification and boundaries

```sh
.venv/bin/python -m pytest -q
.venv/bin/python main.py smoke
```

The smoke command uses a synthetic subprocess and produces no engineering result.
Real EnergyPlus integration checks are optional. The commercial demonstration and
earlier generic CLI workflows remain in the source for compatibility, but the
Streamlit product surface is residential results only. There is no finance, carbon,
optimization, regression, or real equipment efficiency. For Talon setup and staged
Slurm commands, see [docs/talon.md](docs/talon.md). Talon execution remains unverified.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the execution path and artifact
map, and [docs/flexible_residential_campaigns.md](docs/flexible_residential_campaigns.md)
for detailed experiment behavior. The dated validation reports describe historical
verification runs, not current setup instructions.
# Thermal-Home-Modeling
