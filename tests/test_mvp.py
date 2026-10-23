"""MVP unit/UI tests use fixtures; the opt-in integration test reads a real run."""

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import Mock

import pandas as pd
import pytest

from thermal_home_modeling.config.mvp_config import MVPPaths, MVP_METRICS
from thermal_home_modeling.config.output_config import OutputConfig
from thermal_home_modeling.energyplus.installation import EnergyPlusInstallation, detect_energyplus
from thermal_home_modeling.energyplus.result_parser import SQLiteResultParser
from thermal_home_modeling.errors import ConfigurationError, ResultParsingError
from thermal_home_modeling.experiments.mvp_real import build_mvp_model, prepare_baseline, run_real_mvp
from thermal_home_modeling.models.mvp import MVPSimulationInput, MVPResult
from thermal_home_modeling.utilities.files import file_digest


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "EnergyPlus"
    (root / "ExampleFiles").mkdir(parents=True)
    (root / "WeatherData").mkdir()
    example = root / "ExampleFiles/5ZoneAirCooled.idf"
    example.write_text("TEST FIXTURE, not a valid IDF;")
    weather = root / "WeatherData/TestChicago.epw"
    weather.write_text("LOCATION,Chicago Ohare Intl Ap,IL,USA,TEST,725300,41.98,-87.92,-6,201\nTEST FIXTURE, not full weather\n")
    (root / "energyplus").write_text("TEST EXECUTABLE IDENTITY ONLY")
    return EnergyPlusInstallation(root / "energyplus", "test-version", root, example.parent, weather.parent, example, weather)


def test_user_paths_use_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    paths = MVPPaths.default()
    assert paths.input == tmp_path / "Desktop/Academics/Fall 26/ME 487/input"
    assert paths.output == paths.input.parent / "output"


def test_demo_input_has_no_unverified_residential_fields(installation):
    first = MVPSimulationInput(weather_file=installation.weather_file, orientation_deg=450)
    second = MVPSimulationInput(weather_file=installation.weather_file)
    assert first.orientation_deg == 90
    assert first.simulation_id != second.simulation_id
    assert "floor_area_ft2" not in first.to_dict()
    assert "NOT the Marvin" in first.to_dict()["model_note"]


def test_detection_inspects_the_selected_installation(installation, monkeypatch):
    monkeypatch.setattr("thermal_home_modeling.energyplus.installation.shutil.which", lambda _: str(installation.executable))
    monkeypatch.setattr("thermal_home_modeling.energyplus.installation.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "EnergyPlus, Version test-version\n", ""))
    assert detect_energyplus() == installation


def test_bad_override_has_actionable_search_message(monkeypatch):
    monkeypatch.setattr("thermal_home_modeling.energyplus.installation.shutil.which", lambda _: None)
    with pytest.raises(ConfigurationError, match="Searched:.*ENERGYPLUS_EXE"):
        detect_energyplus("/missing/explicit/energyplus")


def test_conversion_is_copy_only_and_cached(installation, tmp_path, monkeypatch):
    original = file_digest(installation.example_model)
    commands = []
    def convert(command, **kwargs):
        commands.append(command)
        destination = Path(command[-1]).with_suffix(".epJSON")
        destination.write_text('{"Building": {}}')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_real.subprocess.run", convert)
    baseline = prepare_baseline(installation, tmp_path / "input")
    assert prepare_baseline(installation, tmp_path / "input") == baseline
    assert len(commands) == 1 and "--convert-only" in commands[0]
    assert Path(commands[0][-1]) != installation.example_model
    assert file_digest(installation.example_model) == original
    manifest = json.loads((baseline.parent / "baseline_manifest.json").read_text())
    assert manifest["energyplus_version"] == installation.version
    assert manifest["converted_sha256"] == file_digest(baseline)
    baseline.write_text("changed")
    with pytest.raises(ConfigurationError, match="provenance"):
        prepare_baseline(installation, tmp_path / "input")


def test_generated_orientation_and_meter_requests(settings, tmp_path, installation):
    source = settings.baseline_model.read_bytes()
    simulation = MVPSimulationInput(weather_file=installation.weather_file, orientation_deg=180)
    path = build_mvp_model(settings.baseline_model, simulation, tmp_path / "generated/model.epJSON")
    model = json.loads(path.read_text())
    assert settings.baseline_model.read_bytes() == source
    assert next(iter(model["Building"].values()))["north_axis"] == 180
    requests = {entry["key_name"] for entry in model["Output:Meter"].values()}
    assert {"Electricity:Facility", "NaturalGas:Facility", "Heating:*", "Cooling:*"} == requests
    assert "Output:SQLite" in model


def test_optional_absence_differs_from_corrupt_output(sql_directory):
    parser = SQLiteResultParser(OutputConfig())
    values = parser.parse_annual_meters(sql_directory, required=("Cooling:Electricity",), optional=("NaturalGas:Facility",))
    assert values == {"Cooling:Electricity": pytest.approx(2000)}
    with sqlite3.connect(sql_directory / "eplusout.sql") as connection:
        connection.execute("UPDATE ReportData SET Value = -5 WHERE ReportDataDictionaryIndex = 1")
    with pytest.raises(ResultParsingError):
        parser.parse_annual_meters(sql_directory, required=("Cooling:Electricity",), optional=("Heating:Electricity",))


def test_real_energyplus_null_summary_warmup_flag(sql_directory):
    with sqlite3.connect(sql_directory / "eplusout.sql") as connection:
        connection.execute('UPDATE "Time" SET WarmupFlag = NULL WHERE TimeIndex = 1')
    result = SQLiteResultParser(OutputConfig()).parse(sql_directory)
    assert result.annual_heating_kwh == pytest.approx(1000)
    assert result.annual_cooling_kwh == pytest.approx(2000)


def test_real_workflow_reuses_runner_exactly_once_and_keeps_failures(settings, installation, tmp_path, monkeypatch, fake_runner):
    # Actual fixture subprocess emits no facility meter; require a clean parsing failure.
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_real.prepare_baseline", lambda *a: settings.baseline_model)
    runner_factory = Mock(return_value=fake_runner, _check_completion=fake_runner._check_completion)
    monkeypatch.setattr("thermal_home_modeling.experiments.mvp_real.EnergyPlusRunner", runner_factory)
    original_run = fake_runner.run
    fake_runner.run = Mock(wraps=original_run)
    result = run_real_mvp(settings=settings, installation=installation, paths=MVPPaths(tmp_path / "input", tmp_path / "output"))
    assert fake_runner.run.call_count == 1
    assert not result.success and result.error_type == "result_parsing"
    directory = Path(result.output_directory)
    assert (directory / "eplusout.sql").is_file()
    row = json.loads((directory.parent / "result.json").read_text())
    assert row["data_source"] == "real_energyplus"
    assert "facility_electricity_kwh" not in row
    execution = json.loads((directory.parent / "execution.json").read_text())
    assert execution["started_at"] and execution["finished_at"]
    assert len(pd.read_parquet(directory.parent / "results.parquet")) == 1


@pytest.mark.integration
def test_saved_real_mvp_matches_raw_sqlite_and_streamlit():
    """Read a recorded real run; this test NEVER launches another simulation."""
    filename = os.environ.get("THM_REAL_MVP_RESULT")
    if not filename:
        pytest.skip("Set THM_REAL_MVP_RESULT to an existing real output/<id>/result.json")
    row = json.loads(Path(filename).read_text())
    assert row["data_source"] == "real_energyplus" and row["success"]
    raw = Path(row["output_directory"])
    assert "EnergyPlus Completed Successfully" in (raw / "eplusout.end").read_text()
    assert row["return_code"] == 0
    generated = json.loads(Path(row["generated_input"]).read_text())
    assert next(iter(generated["Building"].values()))["north_axis"] == row["orientation_deg"]
    run_manifest = json.loads((Path(row["generated_input"]).parent / "run_manifest.json").read_text())
    provenance = json.loads(Path(run_manifest["baseline_manifest"]).read_text())
    assert file_digest(Path(provenance["original_example_path"])) == provenance["original_sha256"]
    assert file_digest(Path(row["generated_input"])) == run_manifest["generated_sha256"]
    baseline = json.loads(Path(run_manifest["baseline"]).read_text())
    changed_objects = {key for key in baseline.keys() | generated.keys() if baseline.get(key) != generated.get(key)}
    assert changed_objects <= {"Building", "Site:Location", "Output:Meter", "Output:Meter:MeterFileOnly", "Output:SQLite", "Output:VariableDictionary"}
    with sqlite3.connect(raw / "eplusout.sql") as connection:
        for field, (meter, _, _) in MVP_METRICS.items():
            if field not in row:
                continue
            values = connection.execute('''SELECT r.Value, d.Units, t.SimulationDays
                FROM ReportData r JOIN ReportDataDictionary d USING (ReportDataDictionaryIndex)
                JOIN "Time" t USING (TimeIndex) JOIN EnvironmentPeriods e USING (EnvironmentPeriodIndex)
                WHERE d.Name = ? COLLATE NOCASE AND d.ReportingFrequency = 'Run Period'
                  AND e.EnvironmentType = 3 AND (t.WarmupFlag = 0 OR t.WarmupFlag IS NULL)''', (meter,)).fetchall()
            assert len(values) == 1 and values[0][1] == "J" and values[0][2] in (365, 366)
            assert row[field] == pytest.approx(values[0][0] / 3_600_000)
    parquet = pd.read_parquet(raw.parent / "results.parquet")
    assert parquet.iloc[0]["facility_electricity_kwh"] == row["facility_electricity_kwh"]
    # Render that exact real record, without launching a second EnergyPlus run.
    from streamlit.testing.v1 import AppTest
    fields = {field: row.pop(field) for field in MVP_METRICS if field in row}
    row.pop("metric_sources")
    result = MVPResult(**row, metrics=fields)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"))
    app.session_state["mvp_result"] = result
    app.run(timeout=20)
    assert not app.exception
    assert any(metric.label == "HEATING" for metric in app.metric)
    assert any("WHOLE-BUILDING ENERGY" in caption.value for caption in app.caption)
