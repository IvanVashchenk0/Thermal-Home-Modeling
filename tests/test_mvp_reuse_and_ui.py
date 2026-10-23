"""Exercise reuse with synthetic subprocess output; no real matrix is launched."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from thermal_home_modeling.config.mvp_config import MVPPaths
from thermal_home_modeling.energyplus.installation import EnergyPlusInstallation
from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.experiments.baseline_comparison import run_orientation_study
from thermal_home_modeling.experiments.mvp_real import run_real_mvp
from thermal_home_modeling.models.mvp import MVPResult
from thermal_home_modeling.presentation.comparison import compare_orientations, format_percent
from thermal_home_modeling.weather.discovery import read_epw_location


@pytest.fixture
def demo(settings, tmp_path, fake_runner, monkeypatch):
    installation_root = tmp_path / "installed"
    installation_root.mkdir()
    executable = installation_root / "energyplus"
    executable.write_text("TEST binary identity")
    weather = installation_root / "Chicago.epw"
    weather.write_text("LOCATION,Chicago Ohare Intl Ap,IL,USA,TEST,1,41.98,-87.92,-6,201\n")
    second_weather = installation_root / "Tampa.epw"
    second_weather.write_text("LOCATION,Tampa International Ap,FL,USA,TEST,2,27.97,-82.53,-5,6\n")
    installation = EnergyPlusInstallation(executable, "test", installation_root, settings.baseline_model.parent,
                                         installation_root, settings.baseline_model, weather)
    paths = MVPPaths(tmp_path / "input", tmp_path / "output")
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_real.prepare_baseline", lambda *args: settings.baseline_model)
    original_run = fake_runner.run
    def emit_facility(*args, **kwargs):
        execution = original_run(*args, **kwargs)
        with sqlite3.connect(Path(args[2]) / "eplusout.sql") as connection:
            connection.execute("INSERT INTO ReportDataDictionary VALUES (3,1,'Sum','','Electricity:Facility','Run Period','J')")
            connection.execute("INSERT INTO ReportData VALUES (5,1,3,10800000000)")
        return execution
    fake_runner.run = Mock(side_effect=emit_facility)
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_real.EnergyPlusRunner",
                        Mock(return_value=fake_runner, _check_completion=EnergyPlusRunner._check_completion))
    return dict(settings=settings, paths=paths, installation=installation), fake_runner


def test_exact_run_reused_and_orientation_changes_run_again(demo):
    arguments, runner = demo
    first = run_real_mvp(0, **arguments)
    reused = run_real_mvp(0, **arguments)
    rotated = run_real_mvp(90, **arguments)
    assert first.success and rotated.success
    assert reused.reused and reused.simulation_id == first.simulation_id
    assert runner.run.call_count == 2
    assert first.comparison_signature == rotated.comparison_signature
    assert first.input_signature != rotated.input_signature
    assert len(list(arguments["paths"].output.glob("mvp_*"))) == 2


@pytest.mark.parametrize("changed", ["weather", "version", "timeout", "model", "executable"])
def test_changed_inputs_cannot_reuse(demo, changed):
    arguments, runner = demo
    initial = run_real_mvp(**arguments)
    if changed == "weather":
        arguments["installation"].weather_file.write_text(arguments["installation"].weather_file.read_text() + "changed weather content")
    elif changed == "version":
        arguments["installation"] = replace(arguments["installation"], version="changed")
    elif changed == "timeout":
        arguments["settings"] = replace(arguments["settings"], timeout_seconds=60)
    elif changed == "model":
        baseline = arguments["settings"].baseline_model
        model = json.loads(baseline.read_text())
        next(iter(model["Building"].values()))["terrain"] = "City"
        baseline.write_text(json.dumps(model))
    else:
        arguments["installation"].executable.write_text("changed binary")
    changed_result = run_real_mvp(**arguments)
    assert not changed_result.reused and changed_result.input_signature != initial.input_signature
    assert runner.run.call_count == 2


@pytest.mark.parametrize("damaged", ["result", "sql", "model", "end"])
def test_damaged_cached_artifacts_are_not_reused(demo, damaged):
    arguments, runner = demo
    first = run_real_mvp(**arguments)
    raw = Path(first.output_directory)
    if damaged == "result":
        path = raw.parent / "result.json"
        row = json.loads(path.read_text())
        row["facility_electricity_kwh"] = 999
        path.write_text(json.dumps(row))
    elif damaged == "sql":
        with sqlite3.connect(raw / "eplusout.sql") as connection:
            connection.execute("UPDATE ReportData SET Value=5 WHERE ReportDataIndex=5")
    elif damaged == "model":
        Path(first.generated_input).write_text("modified input")
    else:
        (raw / "eplusout.end").write_text("Failed")
    again = run_real_mvp(**arguments)
    assert not again.reused and again.simulation_id != first.simulation_id
    assert runner.run.call_count == 2


def test_automatic_baseline_is_bounded_and_optional(demo):
    arguments, runner = demo
    arguments["weather_file"] = arguments["installation"].weather_file
    selected_only = run_orientation_study(90, auto_baseline=False, **arguments)
    assert selected_only.baseline is None and runner.run.call_count == 1
    compared = run_orientation_study(90, auto_baseline=True, **arguments)
    assert compared.selected.reused and compared.baseline.success
    assert compared.baseline.orientation_deg == 0 and runner.run.call_count == 2
    cached_pair = run_orientation_study(90, **arguments)
    assert cached_pair.selected.reused and cached_pair.baseline.reused
    assert runner.run.call_count == 2


def test_city_switch_updates_model_metadata_and_command(demo):
    arguments, runner = demo
    weather = arguments["installation"].weather_data / "Tampa.epw"
    result = run_real_mvp(90, weather_file=weather, **arguments)
    model = json.loads(Path(result.generated_input).read_text())
    location = read_epw_location(weather)
    assert model["Site:Location"] == {location.station_name: location.site_location()}
    assert result.city == "Tampa" and result.weather_file == str(weather)
    assert runner.run.call_args.args[1] == weather
    payload = json.loads((Path(result.generated_input).parent / "simulation_input.json").read_text())
    assert payload["city"] == "Tampa" and payload["scope"] == "annual"
    manifest = json.loads((Path(result.generated_input).parent / "run_manifest.json").read_text())
    assert manifest["weather_metadata"]["latitude"] == 27.97


