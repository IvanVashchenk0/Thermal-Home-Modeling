# Talon residential campaigns

This is a deployment template, not a record of a successful Talon run. Confirm
the current partition, job limits, storage policy, Python module, and EnergyPlus
installation with Talon before submitting. EnergyPlus must have its `ExampleFiles`
and `WeatherData` directories. The five research EPWs under
`data/weather/research/` are ignored by Git and must be copied separately. The
matching Washington Dulles EPW must be available under EnergyPlus `WeatherData`.

## Configure

From the repository checkout, install Python 3.11+ and dependencies in a Linux
environment, then export absolute paths. Choose input/output roots on an approved
filesystem with enough quota. Check that the selected executable reports the
expected EnergyPlus version and that `residential-batch --dry-run` finds all six
stations and exactly 1,728 unique cases.

```sh
export THM_PYTHON=/verified/path/to/venv/bin/python
export ENERGYPLUS_EXE=/verified/path/to/energyplus
export THM_INPUT_ROOT=/verified/writable/path/input
export THM_OUTPUT_ROOT=/verified/writable/path/output
"$THM_PYTHON" main.py residential-batch --config examples/residential_all_cities.json --sampling full_factorial --fresh --dry-run
```

The Slurm script is `scripts/talon_residential.sbatch`. It requests one node and
one task, runs Python in the foreground, and writes separate `slurm-*.out` and
`slurm-*.err` logs in the submission directory. It appends `--workers auto` and
does not use the detached `--background` mode. Slurm sends a warning signal ten
minutes before the time limit, when supported; the Python CLI stops scheduling,
waits for active cases, preserves their records, and exits nonzero. Slurm may
terminate work before active simulations finish if the remaining time is too
short. Resume from the saved batch afterward.

The script intentionally does not specify a partition, module, CPU count,
memory, or time. Supply verified values at submission. These are **examples**:

```sh
sbatch --export=ALL --cpus-per-task=4 --mem=16G --time=02:00:00 \
  scripts/talon_residential.sbatch --count 1 --sampling balanced --seed 42 --fresh

sbatch --export=ALL --cpus-per-task=4 --mem=16G --time=04:00:00 \
  scripts/talon_residential.sbatch --config examples/residential_validation_12.json --fresh

sbatch --export=ALL --cpus-per-task=8 --mem=32G --time=08:00:00 \
  scripts/talon_residential.sbatch --count 50 --sampling balanced --seed 42 --fresh

sbatch --export=ALL --cpus-per-task=16 --mem=64G --time=24:00:00 \
  scripts/talon_residential.sbatch --config examples/residential_all_cities.json --sampling full_factorial --fresh
```

The CPU, memory, and time values above are placeholders, not measured Talon
requirements. Add `--partition=...` only after verifying the correct partition.
Run stages in order; inspect Slurm `MaxRSS`, process count, wall time, filesystem
use, EnergyPlus warnings, and data integrity before increasing scale. Avoid
overlapping campaigns that share output roots unless their combined allocated
resources and filesystem load have been reviewed.

## Verify, recover, resume

```sh
"$THM_PYTHON" main.py residential-verify --batch "$THM_OUTPUT_ROOT/batches/residential_ID"
"$THM_PYTHON" main.py residential-recover --batch "$THM_OUTPUT_ROOT/batches/residential_ID"
sbatch --export=ALL --cpus-per-task=4 --mem=16G --time=04:00:00 \
  scripts/talon_residential.sbatch --resume-batch "$THM_OUTPUT_ROOT/batches/residential_ID"
```

Verification exits zero only when every saved case has a successful unique
record, CSV and Parquet match the records, and the generated model and raw files
pass traceability and completion checks. Recovery should be invoked only after
the original job has stopped; it rewrites derived CSV/Parquet and reconciles the
manifest from indexed records. A partial or failed batch remains incomplete.
Resume creates a new attempt from saved cases and catalog snapshots and reuses
validated successful results. Do not pass `--fresh` on resume.
