"""Discover and inspect an existing installation; never download EnergyPlus."""

from dataclasses import asdict, dataclass
import logging
from pathlib import Path
import re
import shutil
import subprocess

from thermal_home_modeling.errors import ConfigurationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnergyPlusInstallation:
    """Verified local executable, version, and bundled assets."""

    executable: Path
    version: str
    root: Path
    example_files: Path
    weather_data: Path
    example_model: Path
    weather_file: Path

    def to_dict(self) -> dict[str, str]:
        """Serialize installation provenance for manifests."""
        return {key: str(value) for key, value in asdict(self).items()}


def detect_energyplus(executable: str = "energyplus") -> EnergyPlusInstallation:
    """Inspect configured/PATH executable, then bounded common macOS locations.

    An explicit invalid override fails instead of silently choosing another version.
    """
    candidates: list[Path] = []
    searched = [f"PATH/configured executable: {executable}"]
    located = shutil.which(executable)
    if located:
        candidates.append(Path(located))
    if executable == "energyplus" and not candidates:
        for parent in (Path("/Applications"), Path.home() / "Applications", Path.home(), Path.home() / "Downloads", Path("/usr/local")):
            searched.append(str(parent / "EnergyPlus*/energyplus"))
            candidates.extend(sorted(parent.glob("EnergyPlus*/energyplus"), reverse=True))
    errors = []
    for candidate in candidates:
        try:
            result = subprocess.run([str(candidate), "--version"], capture_output=True, text=True, timeout=15, check=True)
            match = re.search(r"EnergyPlus, Version\s+([^\s,]+)", result.stdout)
            if not match:
                raise ValueError(f"Unrecognized version output: {result.stdout.strip()}")
            actual = candidate.resolve()
            roots = (actual.parent, actual.parent.parent)
            root = next((path for path in roots if (path / "ExampleFiles").is_dir() and (path / "WeatherData").is_dir()), None)
            if root is None:
                raise ValueError(f"ExampleFiles/WeatherData not found beside {actual}")
            example = root / "ExampleFiles" / "5ZoneAirCooled.idf"
            if not example.is_file():
                raise ValueError(f"Bundled example missing: {example}")
            weather = sorted(path for path in (root / "WeatherData").rglob("*")
                             if path.is_file() and path.suffix.lower() == ".epw")
            if not weather:
                raise ValueError(f"No bundled EPW found under {root / 'WeatherData'}")
            # Preserve the original CLI default when Chicago is installed; the UI
            # discovers all LOCATION headers independently and does not require it.
            default_weather = next((path for path in weather if "chicago" in path.name.casefold()), weather[0])
            installation = EnergyPlusInstallation(actual, match.group(1), root, root / "ExampleFiles", root / "WeatherData", example, default_weather)
            for label, value in (("EnergyPlus version", installation.version), ("Executable", actual), ("Install root", root), ("ExampleFiles", installation.example_files), ("WeatherData", installation.weather_data)):
                logger.info("%s: %s", label, value)
            return installation
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            errors.append(f"{candidate}: {exc}")
    raise ConfigurationError(
        "Could not detect a usable local EnergyPlus installation. Searched: "
        + "; ".join(searched + errors)
        + ". Set ENERGYPLUS_EXE to the installed executable and ensure its bundled ExampleFiles/WeatherData are present."
    )
