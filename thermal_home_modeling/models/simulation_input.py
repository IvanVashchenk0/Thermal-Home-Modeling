"""One immutable, validated experiment definition."""

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any, Literal

from thermal_home_modeling.utilities.files import fingerprint
from thermal_home_modeling.utilities.validation import finite_number, nonempty_text, validate_simulation_id


@dataclass(frozen=True, kw_only=True)
class SimulationInput:
    """Engineering inputs; attic R-value is in h·ft²·°F/Btu.

    Orientation is the absolute Building north axis, normalized to [0, 360).
    Climate zone numbers are study labels; verify city/EPW assignments separately.
    """

    city: str
    climate_zone: int
    weather_file: Path | str
    floor_area_ft2: float = 1500.0
    stories: int = 1
    window_to_wall_ratio: float = 0.15
    orientation_deg: float = 0.0
    orientation_mode: Literal["long_side_south", "short_side_south"] = "long_side_south"
    window_type: Literal["double", "triple"] = "double"
    low_e: str = "272"
    wall_insulation: str = "baseline"
    attic_r_value: float = 49.0
    foundation_insulation: str = "baseline"
    simulation_id: str = ""

    def __post_init__(self) -> None:
        for name in ("city", "low_e", "wall_insulation", "foundation_insulation"):
            object.__setattr__(self, name, nonempty_text(name, getattr(self, name)))
        if type(self.climate_zone) is not int or self.climate_zone not in range(2, 8):
            raise ValueError("climate_zone must be an integer from 2 through 7")
        if type(self.stories) is not int or self.stories not in (1, 2):
            raise ValueError("stories must be 1 or 2")
        for name in ("floor_area_ft2", "window_to_wall_ratio", "orientation_deg", "attic_r_value"):
            object.__setattr__(self, name, finite_number(name, getattr(self, name)))
        if self.floor_area_ft2 <= 0:
            raise ValueError("floor_area_ft2 must be positive")
        if not 0 <= self.window_to_wall_ratio <= 1:
            raise ValueError("window_to_wall_ratio must be between 0 and 1 inclusive")
        if self.attic_r_value < 0:
            raise ValueError("attic_r_value must be nonnegative")
        if self.orientation_mode not in ("long_side_south", "short_side_south"):
            raise ValueError("unsupported orientation_mode")
        if self.window_type not in ("double", "triple"):
            raise ValueError("window_type must be double or triple")
        if not isinstance(self.weather_file, (str, Path)) or not str(self.weather_file).strip():
            raise ValueError("weather_file must be a path to an EPW file")
        weather = Path(self.weather_file).expanduser()
        if weather.suffix.lower() != ".epw":
            raise ValueError("weather_file must have the .epw extension")
        object.__setattr__(self, "weather_file", weather)
        object.__setattr__(self, "orientation_deg", self.orientation_deg % 360)
        if not self.simulation_id:
            prefix = re.sub(r"[^A-Za-z0-9]+", "_", self.city).strip("_").upper()[:30] or "SIM"
            object.__setattr__(self, "simulation_id", f"{prefix}_{fingerprint(self.parameters())[:20]}")
        validate_simulation_id(self.simulation_id)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record."""
        row = asdict(self)
        row["weather_file"] = self.weather_file.as_posix()
        return row

    def parameters(self) -> dict[str, Any]:
        """Return normalized parameters used by stable ID generation."""
        row = self.to_dict()
        row.pop("simulation_id")
        return row
