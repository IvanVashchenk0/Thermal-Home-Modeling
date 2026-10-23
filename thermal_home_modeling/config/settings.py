"""Environment-backed settings; importing this module has no side effects."""

from dataclasses import dataclass, field, replace
import os
from pathlib import Path

from dotenv import dotenv_values

from thermal_home_modeling.config.output_config import OutputConfig
from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.utilities.paths import ProjectPaths, repository_root, resolve_path
from thermal_home_modeling.utilities.validation import finite_number


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one campaign, injectable in tests."""

    paths: ProjectPaths
    baseline_model: Path
    baseline_parameters: Path
    energyplus_exe: str = "energyplus"
    timeout_seconds: float = 3600
    outputs: OutputConfig = field(default_factory=OutputConfig)

    def __post_init__(self) -> None:
        if not self.energyplus_exe.strip():
            raise ConfigurationError("ENERGYPLUS_EXE cannot be empty")
        try:
            if finite_number("timeout_seconds", self.timeout_seconds) <= 0:
                raise ValueError("timeout_seconds must be positive")
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        """Load root/.env, then environment overrides; never mutate os.environ."""
        root = (root or repository_root()).resolve()
        values = {**dotenv_values(root / ".env"), **os.environ}

        def setting(name: str, default: str) -> str:
            return values.get(name) or default

        paths = ProjectPaths.from_root(root, setting("THM_DATA_DIR", "data"))
        overrides = {
            field_name: resolve_path(values[environment], root)
            for field_name, environment in {
                "weather": "THM_WEATHER_DIR", "generated_models": "THM_GENERATED_MODELS_DIR",
                "simulation_outputs": "THM_OUTPUT_DIR", "results": "THM_RESULTS_DIR",
            }.items() if values.get(environment)
        }
        paths = replace(paths, **overrides)
        executable = setting("ENERGYPLUS_EXE", "energyplus")
        if "/" in executable or "\\" in executable or executable.startswith("~"):
            executable = str(resolve_path(executable, root))
        try:
            timeout = float(setting("THM_TIMEOUT_SECONDS", "3600"))
        except ValueError as exc:
            raise ConfigurationError("THM_TIMEOUT_SECONDS must be a number") from exc
        return cls(
            paths=paths,
            baseline_model=resolve_path(setting("THM_BASELINE_MODEL", str(paths.baseline_models / "baseline.epJSON")), root),
            baseline_parameters=resolve_path(setting("THM_BASELINE_PARAMETERS", str(paths.baseline_models / "baseline_parameters.json")), root),
            energyplus_exe=executable, timeout_seconds=timeout,
        )

    def weather_path(self, weather_file: str | Path) -> Path:
        """Resolve an experiment's EPW against the configured weather directory."""
        return resolve_path(weather_file, self.paths.weather)
