from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.models.mvp import MVPResult, MVPSimulationInput
from thermal_home_modeling.experiments.mvp_real import build_mvp_model
from thermal_home_modeling.presentation.comparison import compare_orientations, energy_change, format_percent
from thermal_home_modeling.weather.discovery import discover_weather, read_epw_location


def epw(path, station="Chicago Ohare Intl Ap", source="TMY3", station_id="725300"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"LOCATION,{station},IL,USA,{source},{station_id},41.98,-87.92,-6,201\n")
    return path


def test_location_metadata_and_friendly_label(tmp_path):
    location = read_epw_location(epw(tmp_path / "not-the-city.epw"))
    assert location.city == "Chicago" and location.label == "Chicago, IL"
    assert location.station_name == "Chicago Ohare Intl Ap"
    assert location.site_location() == {"latitude": 41.98, "longitude": -87.92, "time_zone": -6., "elevation": 201.}
    assert location.to_dict()["source"] == "TMY3"
    assert location.to_dict()["station_id"] == "725300"


def test_quoted_city_and_bom(tmp_path):
    path = tmp_path / "weather.epw"
    path.write_text('\ufeffLOCATION,"Example, District",Region,CAN,CWEC,123,45,-75,-5,100\n')
    assert read_epw_location(path).station_name == "Example, District"


@pytest.mark.parametrize("header", [
    "NOT LOCATION,a,b,c,d,e,1,2,3,4", "LOCATION,short", "LOCATION,,IL,USA,TMY,1,40,-80,-5,0",
    "LOCATION,Test,IL,USA,TMY,1,nan,0,0,0", "LOCATION,Test,IL,USA,TMY,1,91,0,0,0",
    "LOCATION,Test,IL,USA,TMY,1,40,181,0,0", "LOCATION,Test,IL,USA,TMY,1,40,-80,30,0",
])
def test_bad_header_fails_cleanly(tmp_path, header):
    path = tmp_path / "weather.epw"
    path.write_text(header)
    with pytest.raises(ConfigurationError, match="EPW location"):
        read_epw_location(path)


def test_discovery_disambiguates_and_skips_bad_files(tmp_path, caplog):
    epw(tmp_path / "one.epw")
    epw(tmp_path / "nested/two.EPW", source="OtherSource", station_id="123")
    (tmp_path / "bad.epw").write_text("invalid")
    locations = discover_weather(tmp_path)
    assert len(locations) == 2
    assert len({location.label for location in locations}) == 2
    assert all(location.label.startswith("Chicago, IL —") for location in locations)
    assert any("OtherSource" in location.label for location in locations)
    assert "Skipping invalid" in caplog.text


def test_empty_weather_folder_fails(tmp_path):
    with pytest.raises(ConfigurationError, match="No valid EPW"):
        discover_weather(tmp_path)


def test_site_location_is_populated_from_selected_epw(settings, tmp_path):
    path = epw(tmp_path / "different-city.epw", station="Different City")
    simulation = MVPSimulationInput(weather_file=path, orientation_deg=270)
    original = settings.baseline_model.read_bytes()
    model_path = build_mvp_model(settings.baseline_model, simulation, tmp_path / "generated.epJSON")
    model = json.loads(model_path.read_text())
    assert model["Site:Location"] == {"Different City": read_epw_location(path).site_location()}
    assert simulation.city == "Different City"
    assert simulation.to_dict()["weather_metadata"]["path"] == str(path.resolve())
    assert simulation.to_dict()["scope"] == "annual"
    assert next(iter(model["Building"].values()))["north_axis"] == 270
    assert settings.baseline_model.read_bytes() == original
    with pytest.raises(ValueError, match="scope"):
        MVPSimulationInput(weather_file=path, scope="monthly")


def sample_result(orientation=0, **kwargs):
    return MVPResult(simulation_id=f"test_{orientation}", energyplus_version="test", executable="test",
                     weather_file="same.epw", orientation_deg=orientation, generated_input="test.epJSON",
                     output_directory="test-output", success=True, comparison_signature="compatible", **kwargs)


def test_comparison_math_and_fuel_accounting():
    baseline = sample_result(metrics={"heating_natural_gas_kwh_equivalent": 1000, "heating_electricity_kwh": 10,
                                      "cooling_electricity_kwh": 100, "facility_electricity_kwh": 500})
    selected = replace(baseline, orientation_deg=90, metrics={"heating_natural_gas_kwh_equivalent": 900,
                        "heating_electricity_kwh": 20, "cooling_electricity_kwh": 110, "facility_electricity_kwh": 550})
    heating, cooling, electricity = compare_orientations(selected, baseline)
    assert heating.current == 920 and heating.baseline == 1010
    assert heating.difference == -90 and heating.percent == pytest.approx(-90 / 1010 * 100)
    assert cooling.percent == pytest.approx(10) and electricity.difference == 50
    assert format_percent(heating) == "↓ 8.9%"
    assert format_percent(cooling) == "↑ 10.0%"


@pytest.mark.parametrize("value", [0, 10])
def test_zero_baseline_is_undefined_not_infinite(value):
    change = energy_change("Heating", value, 0, "kWh-eq/year")
    assert change.percent is None and change.difference == value
    assert format_percent(change) == "— (baseline is zero)"


@pytest.mark.parametrize("difference", [
    {"comparison_signature": "different"}, {"weather_file": "other.epw"},
    {"energyplus_version": "different"}, {"scope": "different"}, {"model_name": "different"},
    {"success": False},
])
def test_incompatible_comparisons_are_rejected(difference):
    baseline = sample_result()
    with pytest.raises(ConfigurationError, match="matching"):
        compare_orientations(replace(baseline, orientation_deg=90, **difference), baseline)


def test_missing_fuel_is_not_treated_as_zero():
    baseline = sample_result(metrics={"heating_natural_gas_kwh_equivalent": 100, "heating_electricity_kwh": 0})
    selected = replace(baseline, orientation_deg=90, metrics={"heating_natural_gas_kwh_equivalent": 90})
    assert compare_orientations(selected, baseline) == []
