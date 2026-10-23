"""Exercise model generation, subprocess, parsing, storage, and resume together."""

from pathlib import Path
import math
import sys
from uuid import uuid4

import pandas as pd

from thermal_home_modeling.config.experiment_config import phoenix_example, small_parameter_space
from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.experiments.batch_runner import BatchRunner
from thermal_home_modeling.experiments.experiment_generator import generate_experiments
from thermal_home_modeling.smoke.fixtures import create_settings


def run_smoke(root: Path) -> Path:
    """Run two synthetic cases and verify datasets/resume; return their directory."""
    smoke_root = root / f"smoke_{uuid4().hex}"
    settings = create_settings(smoke_root)
    runner = EnergyPlusRunner((sys.executable, "-m", "thermal_home_modeling.smoke.fake_energyplus"), timeout_seconds=30)
    batch = BatchRunner(settings, runner=runner, data_source="synthetic_smoke")
    cases = list(generate_experiments([phoenix_example()], small_parameter_space()))
    results = list(batch.run(cases))
    if not all(result.success for result in results):
        raise RuntimeError(f"Smoke pipeline failed: {[result.error_message for result in results]}")
    resumed = list(batch.run(cases))
    if [result.attempt_id for result in resumed] != [result.attempt_id for result in results]:
        raise RuntimeError("Smoke resume repeated completed work")
    parquet = pd.read_parquet(settings.paths.results / "results.parquet")
    csv = pd.read_csv(settings.paths.results / "results.csv")
    if len(parquet) != 2 or len(csv) != 2 or not all(math.isclose(value, 1000) for value in parquet["annual_heating_kwh"]) or not all(math.isclose(value, 2000) for value in parquet["annual_cooling_kwh"]):
        raise RuntimeError("Smoke dataset round-trip failed")
    return settings.paths.results
