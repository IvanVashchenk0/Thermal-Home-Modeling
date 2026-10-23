from pathlib import Path
import sys

import pytest

from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.errors import ConfigurationError


def test_subprocess_success_and_logs(settings, fake_runner):
    output = settings.paths.simulation_outputs / "path with spaces"
    result = fake_runner.run(settings.baseline_model, settings.weather_path("phoenix.epw"), output)
    assert result.success and result.return_code == 0
    assert result.runtime_seconds >= 0
    assert "SYNTHETIC" in Path(result.stdout_path).read_text()
    assert Path(result.stderr_path).exists()
    assert (output / "execution.json").exists()
    assert "--annual" in result.command
    with pytest.raises(ConfigurationError, match="empty"):
        fake_runner.run(settings.baseline_model, settings.weather_path("phoenix.epw"), output)


@pytest.mark.parametrize("code, expected, message", [
    ("raise SystemExit(7)", "execution", "code 7"),
    ("pass", "simulation", "completion marker"),
    ("from pathlib import Path; Path('eplusout.err').write_text('** Severe ** bad input')", "simulation", "bad input"),
    ("import time; time.sleep(5)", "execution", "timeout"),
])
def test_failed_process_outcomes(settings, code, expected, message):
    runner = EnergyPlusRunner((sys.executable, "-c", code), timeout_seconds=.3)
    result = runner.run(settings.baseline_model, settings.weather_path("phoenix.epw"), settings.paths.simulation_outputs / "test")
    assert not result.success
    assert result.error_type == expected
    assert message in result.error_message
    assert Path(result.stdout_path).exists()


def test_missing_executable(settings):
    with pytest.raises(ConfigurationError, match="not found"):
        EnergyPlusRunner(("this-energyplus-does-not-exist",)).run(settings.baseline_model, settings.weather_path("phoenix.epw"), settings.paths.simulation_outputs / "test")
