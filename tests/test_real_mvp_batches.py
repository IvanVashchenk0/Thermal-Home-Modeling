"""Opt-in read-only checks of the three requested real batch validation artifacts."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3

import pandas as pd
import pytest

from thermal_home_modeling.config.mvp_config import MVP_METRICS
from thermal_home_modeling.experiments.mvp_batch import BatchOutcome, measured_speedup
from thermal_home_modeling.presentation.batch_results import batch_comparisons
from thermal_home_modeling.utilities.files import file_digest
from thermal_home_modeling.weather.discovery import read_epw_location

VARIABLES = ("THM_REAL_BATCH_SEQUENTIAL", "THM_REAL_BATCH_PARALLEL", "THM_REAL_BATCH_MULTICITY")


def load_batches():
    if not all(os.environ.get(key) for key in VARIABLES):
        pytest.skip("Set THM_REAL_BATCH_SEQUENTIAL, THM_REAL_BATCH_PARALLEL, THM_REAL_BATCH_MULTICITY to existing batch directories")
    outcomes = []
    for variable in VARIABLES:
        directory = Path(os.environ[variable]).resolve()
        summary = json.loads((directory / "batch_summary.json").read_text())
        outcomes.append(BatchOutcome(summary["batch_id"], directory, pd.read_parquet(directory / "batch_results.parquet"), summary))
    return outcomes


@pytest.mark.integration
def test_real_batch_artifacts_process_overlap_locations_and_sql():
    outcomes = load_batches()
    simulation_ids, raw_directories = set(), set()
    for outcome in outcomes:
        summary, frame = outcome.summary, outcome.frame
        assert summary["total_cases"] == summary["newly_executed_cases"] == summary["succeeded"] == 4
        assert summary["failed"] == summary["cached_cases"] == 0
        assert summary["benchmark_eligible"] and frame.new_execution.all() and not frame.cached.any()
        csv = pd.read_csv(outcome.directory / "batch_results.csv")
        assert csv.simulation_id.tolist() == frame.simulation_id.tolist()
        manifest = json.loads((outcome.directory / "batch_manifest.json").read_text())
        assert manifest["status"] == "complete" and manifest["reuse"] is False
        assert len(manifest["results"]) == 4
        process_events = []
        for row in frame.itertuples():
            assert row.simulation_id not in simulation_ids and row.output_directory not in raw_directories
            simulation_ids.add(row.simulation_id)
            raw_directories.add(row.output_directory)
            result = json.loads(Path(row.result_path).read_text())
            assert result["success"] and result["return_code"] == 0 and result["data_source"] == "real_energyplus"
            raw = Path(row.output_directory)
            assert "EnergyPlus Completed Successfully" in (raw / "eplusout.end").read_text()
            errors = (raw / "eplusout.err").read_text()
            assert not re.search(r"\*\*\s*(Severe|Fatal)\s*\*\*", errors, re.I)
            assert row.warning_count == len(result["warnings"])
            location = read_epw_location(Path(row.weather_file))
            model = json.loads(Path(row.generated_input).read_text())
            assert model["Site:Location"] == {location.station_name: location.site_location()}
            assert next(iter(model["Building"].values()))["north_axis"] == row.orientation_deg
            run_manifest = json.loads((Path(row.generated_input).parent / "run_manifest.json").read_text())
            assert run_manifest["weather_sha256"] == file_digest(Path(row.weather_file))
            baseline_manifest = json.loads(Path(run_manifest["baseline_manifest"]).read_text())
            assert file_digest(Path(baseline_manifest["original_example_path"])) == baseline_manifest["original_sha256"]
            execution = json.loads((raw / "execution.json").read_text())
            assert execution["command"][execution["command"].index("--weather") + 1] == row.weather_file
            assert execution["command"][-1] == row.generated_input
            process_events.extend([(datetime.fromisoformat(execution["started_at"]), 1),
                                   (datetime.fromisoformat(execution["finished_at"]), -1)])
            with sqlite3.connect(raw / "eplusout.sql") as connection:
                for field, (meter, _, _) in MVP_METRICS.items():
                    values = connection.execute('''SELECT r.Value FROM ReportData r
                        JOIN ReportDataDictionary d USING (ReportDataDictionaryIndex)
                        JOIN Time t USING (TimeIndex) JOIN EnvironmentPeriods e USING (EnvironmentPeriodIndex)
                        WHERE d.Name=? AND d.ReportingFrequency='Run Period' AND e.EnvironmentType=3
                        AND t.SimulationDays IN (365,366) AND (t.WarmupFlag=0 OR t.WarmupFlag IS NULL)''', (meter,)).fetchall()
                    assert len(values) == 1
                    expected = values[0][0] / 3_600_000
                    assert getattr(row, field) == pytest.approx(expected)
                    assert result[field] == pytest.approx(expected)
                    assert csv.loc[csv.simulation_id == row.simulation_id, field].iloc[0] == pytest.approx(expected)
        active = peak = 0
        for _, delta in sorted(process_events):
            active += delta
            peak = max(peak, active)
        assert active == 0 and peak == (1 if summary["mode"] == "sequential" else 4)
        assert summary["sum_energyplus_runtime_seconds"] == pytest.approx(frame.energyplus_runtime_seconds.sum())
        assert summary["new_successful_simulations_per_minute"] == pytest.approx(240 / summary["wall_clock_seconds"])
    sequential, parallel, multicity = outcomes
    assert set(sequential.frame.orientation_deg) == set(parallel.frame.orientation_deg) == {0, 90, 180, 270}
    assert set(sequential.frame.city) == set(parallel.frame.city) == {"Chicago"}
    assert multicity.frame.weather_file.nunique() == 2 and set(multicity.frame.orientation_deg) == {0, 90}
    for field in MVP_METRICS:
        assert sequential.frame.sort_values("orientation_deg")[field].tolist() == parallel.frame.sort_values("orientation_deg")[field].tolist()
    assert measured_speedup(sequential.summary, parallel.summary) == pytest.approx(sequential.summary["wall_clock_seconds"] / parallel.summary["wall_clock_seconds"])
    assert measured_speedup(sequential.summary, multicity.summary) is None


@pytest.mark.integration
def test_saved_real_batch_ui_and_each_city_baseline():
    from streamlit.testing.v1 import AppTest
    sequential, parallel, multicity = load_batches()
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"))
    app.session_state["batch_result"] = parallel
    app.session_state["batch_history"] = [sequential.summary, parallel.summary]
    app.run(timeout=20)
    assert not app.exception
    speedup = measured_speedup(sequential.summary, parallel.summary)
    assert any(f"Observed speedup: {speedup:.2f}×" in info.value for info in app.info)
    app.session_state["batch_result"] = multicity
    app.session_state["batch_history"] = [multicity.summary]
    app.run(timeout=20)
    assert not app.exception
    assert any("Batch complete: 4 configurations; 0 reused" in message.value for message in app.success)
    frame = multicity.frame.set_index("simulation_id")
    changes = batch_comparisons(multicity.frame)
    assert changes["City / weather"].nunique() == 2
    fields = {"Cooling electricity": "cooling_electricity_kwh", "Heating natural gas": "heating_natural_gas_kwh_equivalent",
              "Whole-building electricity": "facility_electricity_kwh"}
    for _, change in changes.iterrows():
        selected, baseline = frame.loc[change.Simulation], frame.loc[change["Baseline simulation"]]
        assert selected.weather_file == baseline.weather_file and baseline.orientation_deg == 0
        field = fields[change.Metric]
        assert change["Change (%)"] == pytest.approx(100 * (selected[field] - baseline[field]) / baseline[field])
