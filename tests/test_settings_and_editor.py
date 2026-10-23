from dataclasses import replace
import json

import pytest

from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.energyplus.model_editor import EpJSONModelEditor
from thermal_home_modeling.errors import ConfigurationError, ModelGenerationError
from thermal_home_modeling.utilities.paths import ProjectPaths


def test_paths_are_independent_of_cwd(tmp_path, monkeypatch):
    root = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ProjectPaths.from_root(root).results == root / "data/results"
    assert not (root / "data").exists()


def test_env_precedence_and_relative_paths(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ENERGYPLUS_EXE=from_dotenv\nTHM_TIMEOUT_SECONDS=5\nTHM_DATA_DIR=campaign\n")
    monkeypatch.setenv("ENERGYPLUS_EXE", "bin/energyplus")
    settings = Settings.from_env(tmp_path)
    assert settings.energyplus_exe == str(tmp_path / "bin/energyplus")
    assert settings.timeout_seconds == 5
    assert settings.weather_path("test.epw") == tmp_path / "campaign/weather/test.epw"


def test_invalid_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("THM_TIMEOUT_SECONDS", "nan")
    with pytest.raises(ConfigurationError):
        Settings.from_env(tmp_path)


def test_editor_preserves_baseline_and_adds_real_requests(settings, simulation):
    original = settings.baseline_model.read_bytes()
    editor = EpJSONModelEditor(settings.baseline_parameters, settings.outputs)
    generated = settings.paths.generated_models / "case.epJSON"
    editor.build_model(settings.baseline_model, replace(simulation, orientation_deg=90), generated)
    model = json.loads(generated.read_text())
    assert settings.baseline_model.read_bytes() == original
    assert next(iter(model["Building"].values()))["north_axis"] == 90
    assert next(iter(model["Output:SQLite"].values()))["option_type"] == "SimpleAndTabular"
    assert {entry["key_name"] for entry in model["Output:Meter"].values()} == {"Heating:Electricity", "Cooling:Electricity"}


@pytest.mark.parametrize("changes", [{"floor_area_ft2": 3000}, {"stories": 2}, {"window_to_wall_ratio": .35}, {"low_e": "366"}, {"attic_r_value": 60}, {"window_type": "triple"}, {"orientation_mode": "short_side_south"}, {"wall_insulation": "upgrade"}, {"foundation_insulation": "upgrade"}])
def test_unsupported_edits_fail_instead_of_mislabeling(settings, simulation, changes):
    editor = EpJSONModelEditor(settings.baseline_parameters, settings.outputs)
    with pytest.raises(ModelGenerationError, match="not implemented"):
        editor.build_model(settings.baseline_model, replace(simulation, **changes), settings.paths.generated_models / "case.epJSON")


def test_world_coordinates_are_rejected(settings, simulation):
    model = json.loads(settings.baseline_model.read_text())
    model["GlobalGeometryRules"]["Rules"]["coordinate_system"] = "World"
    settings.baseline_model.write_text(json.dumps(model))
    with pytest.raises(ModelGenerationError, match="Relative"):
        EpJSONModelEditor(settings.baseline_parameters, settings.outputs).build_model(settings.baseline_model, simulation, settings.paths.generated_models / "case.epJSON")


def test_missing_metadata_is_configuration_failure(settings, simulation):
    with pytest.raises(ConfigurationError):
        EpJSONModelEditor(settings.paths.root / "missing.json", settings.outputs).build_model(settings.baseline_model, simulation, settings.paths.generated_models / "case.epJSON")
