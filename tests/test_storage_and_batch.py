from dataclasses import replace
import json

import pandas as pd
import pytest

from thermal_home_modeling.errors import ConfigurationError, StorageError
from thermal_home_modeling.experiments.batch_runner import BatchRunner
from thermal_home_modeling.models.simulation_result import SimulationResult
from thermal_home_modeling.storage.result_store import ResultStore
from thermal_home_modeling.utilities.files import write_json


def test_flat_result_and_parquet_roundtrip(tmp_path, simulation):
    store = ResultStore(tmp_path)
    result = SimulationResult.from_input(simulation, success=False, runtime_seconds=.1, output_directory="outputs", error_type="configuration", error_message="missing input")
    store.save(result)
    assert store.load(simulation.simulation_id) == result
    csv, parquet = store.export()
    frame = pd.read_parquet(parquet)
    assert len(frame) == len(pd.read_csv(csv)) == 1
    assert frame.iloc[0]["low_e"] == "272"
    assert pd.isna(frame.iloc[0]["annual_heating_kwh"])
    assert frame.iloc[0]["orientation_mode"] == simulation.orientation_mode


def test_atomic_write_preserves_prior_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    write_json(path, {"success": True})
    def fail_replace(*args):
        raise OSError("disk error")
    monkeypatch.setattr("thermal_home_modeling.utilities.files.os.replace", fail_replace)
    with pytest.raises(OSError):
        write_json(path, {"success": False})
    assert json.loads(path.read_text()) == {"success": True}
    assert len(list(tmp_path.iterdir())) == 1


def test_empty_dataset_and_corrupt_record(tmp_path, simulation):
    store = ResultStore(tmp_path)
    _, parquet = store.export()
    assert pd.read_parquet(parquet).empty
    (store.records / f"{simulation.simulation_id}.json").write_text("broken")
    with pytest.raises(StorageError, match="Invalid checkpoint"):
        store.export()


def test_batch_continues_after_failure_and_resumes(settings, simulation, fake_runner, monkeypatch):
    batch = BatchRunner(settings, runner=fake_runner, data_source="synthetic_smoke")
    invalid = replace(simulation, simulation_id="", floor_area_ft2=3000)
    results = list(batch.run([invalid, simulation]))
    assert [result.success for result in results] == [False, True]
    assert results[0].error_type == "model_generation"
    assert results[1].annual_heating_kwh == pytest.approx(1000)
    assert len(batch.store.to_dataframe()) == 2
    def must_not_run(*args, **kwargs):
        raise AssertionError("Completed run should be reused")
    monkeypatch.setattr(fake_runner, "run", must_not_run)
    resumed = list(batch.run([simulation]))
    assert resumed[0].attempt_id == results[1].attempt_id


def test_partial_iteration_is_checkpointed(settings, simulation, fake_runner):
    batch = BatchRunner(settings, runner=fake_runner, data_source="synthetic_smoke")
    iterator = batch.run([simulation, replace(simulation, simulation_id="", orientation_deg=90)])
    first = next(iterator)
    assert batch.store.load(first.simulation_id).success
    iterator.close()
    assert len(pd.read_parquet(settings.paths.results / "results.parquet")) == 1


def test_changed_assets_cannot_reuse_success(settings, simulation, fake_runner):
    batch = BatchRunner(settings, runner=fake_runner, data_source="synthetic_smoke")
    list(batch.run([simulation]))
    settings.weather_path("phoenix.epw").write_text("changed weather")
    with pytest.raises(ConfigurationError, match="different inputs/assets/settings"):
        list(batch.run([simulation]))
    assert batch.store.load(simulation.simulation_id).success


def test_failed_run_can_retry_after_missing_weather_is_fixed(settings, simulation, fake_runner):
    batch = BatchRunner(settings, runner=fake_runner, data_source="synthetic_smoke")
    simulation = replace(simulation, simulation_id="", weather_file="missing.epw")
    failed = list(batch.run([simulation]))[0]
    assert failed.error_type == "configuration"
    settings.weather_path("missing.epw").write_text("synthetic weather")
    succeeded = list(batch.run([simulation]))[0]
    assert succeeded.success
    assert succeeded.attempt_id != failed.attempt_id
    assert len(batch.store.to_dataframe()) == 1
