"""Opt-in verification of stored real outputs; never launches a simulation."""

import json
import os
from pathlib import Path
import re
import sqlite3

import pytest

from thermal_home_modeling.models.mvp import MVPResult
from thermal_home_modeling.presentation.comparison import compare_orientations, format_percent
from thermal_home_modeling.storage.mvp_cache import find_cached_mvp
from thermal_home_modeling.utilities.files import file_digest
from thermal_home_modeling.weather.discovery import read_epw_location


@pytest.mark.integration
def test_real_orientation_pair_city_location_and_ui():
    variables = ("THM_REAL_MVP_BASELINE", "THM_REAL_MVP_RESULT", "THM_REAL_MVP_OTHER")
    if not all(os.environ.get(name) for name in variables):
        pytest.skip("Set THM_REAL_MVP_BASELINE, THM_REAL_MVP_RESULT, THM_REAL_MVP_OTHER to saved real results")
    results = [MVPResult.from_dict(json.loads(Path(os.environ[name]).read_text())) for name in variables]
    baseline, selected, other_city = results
    assert baseline.city == selected.city == "Chicago"
    assert baseline.orientation_deg == 0 and selected.orientation_deg == 90
    assert other_city.city != selected.city
    for result in results:
        assert result.success and result.data_source == "real_energyplus" and result.return_code == 0
        assert result.scope == "annual"
        weather = read_epw_location(Path(result.weather_file))
        generated = json.loads(Path(result.generated_input).read_text())
        assert generated["Site:Location"] == {weather.station_name: weather.site_location()}
        manifest = json.loads((Path(result.generated_input).parent / "run_manifest.json").read_text())
        assert manifest["city"] == result.city
        assert manifest["weather_sha256"] == file_digest(Path(result.weather_file))
        raw = Path(result.output_directory)
        assert "EnergyPlus Completed Successfully" in (raw / "eplusout.end").read_text()
        assert not re.search(r"\*\*\s*(Severe|Fatal)\s*\*\*", (raw / "eplusout.err").read_text(), re.I)
        with sqlite3.connect(raw / "eplusout.sql") as connection:
            value = connection.execute('''SELECT r.Value FROM ReportData r
                JOIN ReportDataDictionary d USING (ReportDataDictionaryIndex)
                JOIN Time t USING (TimeIndex) JOIN EnvironmentPeriods e USING (EnvironmentPeriodIndex)
                WHERE d.Name='Electricity:Facility' AND d.ReportingFrequency='Run Period'
                AND e.EnvironmentType=3 AND t.SimulationDays IN (365,366)
                AND (t.WarmupFlag=0 OR t.WarmupFlag IS NULL)''').fetchone()[0]
            assert result.metrics["facility_electricity_kwh"] == pytest.approx(value / 3_600_000)
        cached = find_cached_mvp(raw.parent.parent, input_signature=result.input_signature)
        # Forced reruns may create a newer valid record for the same effective inputs.
        assert cached is not None and cached.reused
        assert cached.input_signature == result.input_signature and cached.metrics == result.metrics
    changes = compare_orientations(selected, baseline)
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"))
    app.session_state["mvp_result"] = selected
    app.session_state["mvp_baseline"] = baseline
    app.run(timeout=20)
    assert not app.exception
    for change in changes:
        metric = next(item for item in app.metric if item.label == change.label)
        assert metric.value == format_percent(change)
        assert metric.delta == f"{change.difference:+,.1f} {change.unit}"
    assert all(not expander.proto.expanded for expander in app.expander)
