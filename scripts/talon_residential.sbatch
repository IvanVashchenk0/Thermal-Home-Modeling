#!/usr/bin/env bash
#SBATCH --job-name=thermal-residential
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --output=slurm-%x-%j.out
#SBATCH --error=slurm-%x-%j.err
#SBATCH --signal=B:USR1@600

set -euo pipefail

# Supply --cpus-per-task, --mem, --time and (if needed) --partition at submission.
cd "${SLURM_SUBMIT_DIR:?Submit from the project checkout}"
: "${THM_INPUT_ROOT:?Set an absolute writable input root}"
: "${THM_OUTPUT_ROOT:?Set an absolute writable output root}"
: "${ENERGYPLUS_EXE:?Set the verified Linux EnergyPlus executable}"
: "${THM_PYTHON:?Set the verified Python environment executable}"
[[ "$THM_INPUT_ROOT" = /* && "$THM_OUTPUT_ROOT" = /* && "$ENERGYPLUS_EXE" = /* && "$THM_PYTHON" = /* ]] || {
  echo "THM_INPUT_ROOT, THM_OUTPUT_ROOT, ENERGYPLUS_EXE and THM_PYTHON must be absolute paths" >&2
  exit 2
}
mkdir -p "$THM_INPUT_ROOT" "$THM_OUTPUT_ROOT"

if (( $# == 0 )); then
  set -- --config examples/residential_all_cities.json --sampling full_factorial --fresh
fi
exec "$THM_PYTHON" main.py residential-batch "$@" --workers auto
