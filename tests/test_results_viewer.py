"""The published Streamlit interface reads saved results without an engine."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from thermal_home_modeling.config.mvp_config import MVPPaths


def test_empty_viewer_needs_no_energyplus(tmp_path, monkeypatch):
    paths = MVPPaths(tmp_path / "input", tmp_path / "output")
    monkeypatch.setattr(MVPPaths, "default", lambda: paths)

    def forbidden(*args, **kwargs):
        raise AssertionError("Viewer attempted to detect EnergyPlus")

    monkeypatch.setattr("thermal_home_modeling.energyplus.installation.detect_energyplus", forbidden)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py")).run(timeout=20)
    assert not app.exception
    assert not app.button
    assert any("No residential batch results found" in item.value for item in app.info)
