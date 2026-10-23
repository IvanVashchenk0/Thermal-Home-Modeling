"""Exercise the real orchestration with isolated synthetic EnergyPlus subprocesses."""

from dataclasses import replace
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import pandas as pd
import pytest

from test_mvp_reuse_and_ui import demo  # Reuse the existing synthetic runner/asset fixture.
from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.experiments.mvp_batch import (
    generate_batch_cases, measured_speedup, run_mvp_batch, select_cities, worker_limit,
)
from thermal_home_modeling.presentation.batch_results import batch_comparisons
from thermal_home_modeling.weather.discovery import discover_weather


@pytest.fixture
def batch_demo(demo, monkeypatch):
    arguments, runner = demo
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_batch.prepare_baseline",
                        lambda *args: arguments["settings"].baseline_model)
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_batch.available_cpus", lambda: 4)
    return arguments, runner


def test_cartesian_product_normalization_and_duplicates(batch_demo):
    arguments, _ = batch_demo
    weather = arguments["installation"].weather_file
    other = weather.parent / "Tampa.epw"
    cases = generate_batch_cases([weather, other, weather.parent / "." / weather.name], [0, 90, 360, 450])
    assert len(cases) == 4
    assert len({c.simulation_id for c in cases}) == 4
    assert {(c.city, c.orientation_deg) for c in cases} == {("Chicago", 0), ("Chicago", 90), ("Tampa", 0), ("Tampa", 90)}
    for weather_paths, angles in (([], [0]), ([weather], []), ([weather], [45]), ([weather], [float("nan")])):
        with pytest.raises((ConfigurationError, ValueError)):
            generate_batch_cases(weather_paths, angles)


def test_cli_city_resolution_ambiguity_and_cpu_limit(batch_demo, monkeypatch):
    arguments, _ = batch_demo
    installation = arguments["installation"]
    options = discover_weather(installation.weather_data)
    assert select_cities(options, "chicago,Tam,1") == [installation.weather_file, installation.weather_data / "Tampa.epw", installation.weather_file]
    with pytest.raises(ConfigurationError, match="ambiguous"):
        select_cities([options[0], replace(options[0], station_id="99")], "Chicago")
    with pytest.raises(ConfigurationError):
        select_cities(options, "Minneapolis")
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_batch.available_cpus", lambda: 2)
    assert worker_limit("parallel", 8, 12) == 2
    assert worker_limit("parallel", 4, 1) == 1
    assert worker_limit("sequential", 4, 12) == 1


@pytest.mark.parametrize("mode, expected_concurrency", [("sequential", 1), ("parallel", 4)])
def test_execution_isolated_aggregation_and_atomic_exports(batch_demo, monkeypatch, mode, expected_concurrency):
    arguments, runner = batch_demo
    caller = threading.get_ident()
    lock, barrier = threading.Lock(), threading.Barrier(expected_concurrency)
    active = maximum = 0
    original = runner.run
    def tracked_run(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait(timeout=10)
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1
    runner.run = tracked_run
    import thermal_home_modeling.experiments.mvp_batch as batch_module
    export = batch_module.export_dataframe
    def tracked_export(*args, **kwargs):
        assert threading.get_ident() == caller
        return export(*args, **kwargs)
    monkeypatch.setattr(batch_module, "export_dataframe", tracked_export)
    progress = []
    def observe(snapshot):
        assert threading.get_ident() == caller
        progress.append(snapshot)
    outcome = run_mvp_batch([arguments["installation"].weather_file], [0, 90, 180, 270],
                            mode=mode, workers=4, reuse=False, on_progress=observe, **arguments)
    assert maximum == expected_concurrency
    assert outcome.summary["max_workers"] == expected_concurrency
    assert outcome.summary["newly_executed_cases"] == outcome.summary["succeeded"] == 4
    assert outcome.summary["cached_cases"] == 0 and outcome.summary["benchmark_eligible"]
    assert progress[0].completed == 0 and progress[0].running == 0
    assert progress[-1].completed == 4 and progress[-1].running == 0
    assert max(p.running for p in progress) <= expected_concurrency
    assert list(outcome.frame.orientation_deg) == [0, 90, 180, 270]
    assert outcome.frame.generated_input.nunique() == outcome.frame.output_directory.nunique() == 4
    for row in outcome.frame.itertuples():
        assert Path(row.result_path).is_file()
        assert (Path(row.output_directory) / "eplusout.sql").is_file()
        assert Path(row.generated_input).is_file()
    assert not list(outcome.directory.glob("*.tmp"))
    manifest = json.loads((outcome.directory / "batch_manifest.json").read_text())
    assert manifest["status"] == "complete" and len(manifest["results"]) == 4
    csv, parquet = pd.read_csv(outcome.directory / "batch_results.csv"), pd.read_parquet(outcome.directory / "batch_results.parquet")
    assert len(csv) == len(parquet) == 4
    assert parquet.heating_natural_gas_kwh_equivalent.isna().all()
    assert parquet.facility_electricity_kwh.tolist() == [3000] * 4
    assert csv.simulation_id.tolist() == parquet.simulation_id.tolist()


def test_cache_excluded_from_benchmark_and_rerun_keeps_artifacts(batch_demo):
    arguments, runner = batch_demo
    weather = [arguments["installation"].weather_file]
    first = run_mvp_batch(weather, [0, 90], mode="parallel", **arguments)
    cached = run_mvp_batch(weather, [0, 90], mode="parallel", **arguments)
    assert runner.run.call_count == 2
    assert cached.summary["cached_cases"] == 2 and cached.summary["newly_executed_cases"] == 0
    assert cached.summary["sum_energyplus_runtime_seconds"] == 0
    assert cached.summary["mean_energyplus_runtime_seconds"] is None
    assert cached.summary["new_successful_simulations_per_minute"] == 0
    assert cached.frame.energyplus_runtime_seconds.isna().all()
    assert cached.frame.original_energyplus_runtime_seconds.notna().all()
    assert not cached.summary["benchmark_eligible"]
    fresh = run_mvp_batch(weather, [0, 90], reuse=False, **arguments)
    assert runner.run.call_count == 4
    assert not set(first.frame.simulation_id) & set(fresh.frame.simulation_id)
    assert all(Path(path).is_file() for path in first.frame.result_path)
    assert measured_speedup(first.summary, fresh.summary) == pytest.approx(fresh.summary["wall_clock_seconds"] / first.summary["wall_clock_seconds"])
    assert measured_speedup(cached.summary, fresh.summary) is None


def test_failed_case_does_not_stop_others(batch_demo, monkeypatch):
    arguments, _ = batch_demo
    import thermal_home_modeling.experiments.mvp_batch as module
    original = module.run_real_mvp
    def fail_one(angle, **kwargs):
        if angle == 90:
            raise RuntimeError("TEST failure before process start")
        return original(angle, **kwargs)
    monkeypatch.setattr(module, "run_real_mvp", fail_one)
    outcome = run_mvp_batch([arguments["installation"].weather_file], [0, 90, 180, 270], mode="parallel", **arguments)
    assert outcome.summary["failed"] == 1 and outcome.summary["succeeded"] == 3
    assert outcome.summary["newly_executed_cases"] == 3
    failed = outcome.frame.loc[~outcome.frame.success].iloc[0]
    assert failed.orientation_deg == 90 and "TEST failure" in failed.error_message
    assert json.loads(Path(failed.result_path).read_text())["success"] is False
    assert pd.isna(failed.cooling_electricity_kwh)


def test_process_failure_preserves_warning_error_and_runtime(batch_demo):
    arguments, runner = batch_demo
    original = runner.run
    def fail_rotated(model, weather, raw, **kwargs):
        execution = original(model, weather, raw, **kwargs)
        angle = next(iter(json.loads(model.read_text())["Building"].values()))["north_axis"]
        if angle == 90:
            (raw / "eplusout.err").write_text("** Warning ** TEST retained warning\n** Severe ** TEST failed case\n")
            return replace(execution, success=False, error_type="simulation", error_message="TEST failed case")
        return execution
    runner.run = fail_rotated
    result = run_mvp_batch([arguments["installation"].weather_file], [0, 90], mode="parallel", **arguments)
    assert result.summary["succeeded"] == result.summary["failed"] == 1
    assert result.summary["newly_executed_cases"] == 2
    failed = result.frame.loc[~result.frame.success].iloc[0]
    assert failed.warning_count == 1 and "TEST retained warning" in failed.warnings_json
    assert failed.error_type == "simulation" and failed.energyplus_runtime_seconds > 0
    assert pd.isna(failed.facility_electricity_kwh)


def test_baseline_preparation_precedes_worker_entry(batch_demo, monkeypatch):
    arguments, _ = batch_demo
    ready = False
    caller = threading.get_ident()
    def prepare(*args):
        nonlocal ready
        assert threading.get_ident() == caller
        ready = True
        return arguments["settings"].baseline_model
    def read_prepared(*args):
        assert ready and threading.get_ident() != caller
        return arguments["settings"].baseline_model
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_batch.prepare_baseline", prepare)
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_real.prepare_baseline", read_prepared)
    assert run_mvp_batch([arguments["installation"].weather_file], [0, 90], mode="parallel", **arguments).summary["succeeded"] == 2


def test_large_batch_requires_explicit_confirmation(batch_demo, monkeypatch):
    arguments, runner = batch_demo
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_batch.LARGE_BATCH_THRESHOLD", 1)
    with pytest.raises(ConfigurationError, match="confirm"):
        run_mvp_batch([arguments["installation"].weather_file], [0, 90], **arguments)
    assert runner.run.call_count == 0
    assert run_mvp_batch([arguments["installation"].weather_file], [0, 90], allow_large=True, **arguments).summary["succeeded"] == 2


def test_baselines_are_per_weather_and_compatible_with_nulls_and_zero(batch_demo):
    arguments, _ = batch_demo
    weather = arguments["installation"].weather_file
    result = run_mvp_batch([weather, weather.parent / "Tampa.epw"], [0, 90], mode="parallel", **arguments)
    frame = result.frame.copy()
    # Presentation-only fixture; these values are never saved as real outputs.
    frame.loc[frame.city == "Chicago", "cooling_electricity_kwh"] = [100, 110]
    frame.loc[frame.city == "Tampa", "cooling_electricity_kwh"] = [200, 300]
    changes = batch_comparisons(frame)
    cooling = changes.loc[(changes.Metric == "Cooling electricity") & (changes["Orientation (°)"] == 90)]
    assert dict(zip(cooling["City / weather"], cooling["Change (%)"])) == {"Chicago, IL": 10, "Tampa, FL": 50}
    frame.loc[(frame.city == "Tampa") & (frame.orientation_deg == 0), "cooling_electricity_kwh"] = 0
    zero = batch_comparisons(frame)
    assert zero.loc[(zero["City / weather"] == "Tampa, FL") & (zero.Metric == "Cooling electricity"), "Change (%)"].isna().all()
    frame.loc[(frame.city == "Chicago") & (frame.orientation_deg == 0), "success"] = False
    assert not batch_comparisons(frame)["City / weather"].str.contains("Chicago").any()
    frame.loc[frame.orientation_deg == 90, "comparison_signature"] = "different"
    assert (batch_comparisons(frame)["Orientation (°)"] == 0).all()


def test_mismatched_cases_and_cached_batches_cannot_claim_speedup():
    sequential = dict(mode="sequential", benchmark_eligible=True, configuration_set_signature="a", wall_clock_seconds=20)
    parallel = dict(mode="parallel", benchmark_eligible=True, configuration_set_signature="a", wall_clock_seconds=8)
    assert measured_speedup(sequential, parallel) == 2.5
    assert measured_speedup(sequential, {**parallel, "configuration_set_signature": "b"}) is None
    assert measured_speedup(sequential, {**parallel, "benchmark_eligible": False}) is None


def test_same_city_stations_remain_distinct_chart_series(batch_demo):
    arguments, _ = batch_demo
    first = arguments["installation"].weather_file
    second = first.parent / "ChicagoOther.epw"
    # Two LOCATIONs that clean to the same city must retain station identity.
    second.write_text("LOCATION,Chicago Intl Ap,IL,USA,OTHER,99,41.5,-87.5,-6,200\n")
    result = run_mvp_batch([first, second], [0, 90], mode="parallel", **arguments)
    assert result.frame.weather_label.nunique() == 2
    assert result.frame.city.nunique() == 1
    assert batch_comparisons(result.frame)["City / weather"].nunique() == 2


def test_batch_cli_options_and_exit_status(batch_demo, monkeypatch, caplog):
    from thermal_home_modeling.cli import main
    arguments, _ = batch_demo
    monkeypatch.setattr("thermal_home_modeling.energyplus.installation.detect_energyplus", lambda *a: arguments["installation"])
    def run(weather, angles, **kwargs):
        kwargs.pop("settings")
        kwargs.pop("installation")
        return run_mvp_batch(weather, angles, **kwargs, **arguments)
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_batch.run_mvp_batch", run)
    assert main(["mvp-batch", "--cities", "Chicago,Tampa", "--orientations", "0,90", "--mode", "parallel", "--workers", "4", "--rerun"]) == 0
    assert main(["mvp-batch", "--cities", "MissingCity"]) == 1
