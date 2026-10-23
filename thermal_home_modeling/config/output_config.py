"""Exact EnergyPlus output requests and their interpretation, in one place."""

from dataclasses import dataclass

from thermal_home_modeling.utilities.validation import nonempty_text


@dataclass(frozen=True)
class PeakVariable:
    """One explicit rate variable/key; no automatic summation across zones.

    Choose a building/system thermal-load rate if a thermal peak is intended.
    The result is a maximum at this reporting frequency, not a sizing load.
    """

    name: str
    key: str
    frequency: str = "Hourly"

    def __post_init__(self) -> None:
        nonempty_text("peak variable name", self.name)
        nonempty_text("peak variable key", self.key)
        if self.key == "*" or self.frequency not in ("Hourly", "Timestep"):
            raise ValueError("Peak variables require an exact key and Hourly or Timestep frequency")


@dataclass(frozen=True)
class OutputConfig:
    """Defaults measure heating/cooling ELECTRICITY, not fuel or thermal load.

    For fuel-consuming equipment, supply verified meters and change energy_basis.
    Missing requested meters are errors, even for a system that uses no such fuel.
    Optional HVAC meters must explicitly cover the desired scope (fans/pumps etc.).
    """

    heating_meters: tuple[str, ...] = ("Heating:Electricity",)
    cooling_meters: tuple[str, ...] = ("Cooling:Electricity",)
    total_hvac_meters: tuple[str, ...] = ()
    peak_heating: PeakVariable | None = None
    peak_cooling: PeakVariable | None = None
    energy_basis: str = "electricity_only"

    def __post_init__(self) -> None:
        nonempty_text("energy_basis", self.energy_basis)
        for field in ("heating_meters", "cooling_meters", "total_hvac_meters"):
            meters = getattr(self, field)
            if not isinstance(meters, tuple) or (field != "total_hvac_meters" and not meters):
                raise ValueError(f"{field} must be a tuple of meter names (heating/cooling cannot be empty)")
            for meter in meters:
                nonempty_text(field, meter)
            if len({meter.casefold() for meter in meters}) != len(meters):
                raise ValueError(f"Duplicate meter in {field} would double-count energy")

    def all_meters(self) -> tuple[str, ...]:
        """Return distinct output requests, preserving configuration order."""
        return tuple(dict.fromkeys(self.heating_meters + self.cooling_meters + self.total_hvac_meters))
