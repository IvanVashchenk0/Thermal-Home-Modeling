"""Implemented residential dimensions only; unsupported fields are errors."""
from dataclasses import asdict, dataclass
from pathlib import Path

@dataclass(frozen=True)
class ResidentialInput:
    city_id: str
    weather_file: Path
    house_id: str = "1S"
    wall_id: str = "W3"
    attic_r_ip: float = 49
    orientation_deg: float = 0

    def __post_init__(self):
        object.__setattr__(self, 'weather_file', Path(self.weather_file).resolve())
        object.__setattr__(self, 'attic_r_ip', float(self.attic_r_ip))
        object.__setattr__(self, 'orientation_deg', float(self.orientation_deg))

    def to_dict(self):
        return {**asdict(self), "weather_file": str(self.weather_file.resolve())}
