"""Flat result rows with enough input/provenance information for analysis."""

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any

from thermal_home_modeling.models.simulation_input import SimulationInput


@dataclass(kw_only=True)
class SimulationResult:
    """One checkpoint row; absent metrics serialize as null, never as zero."""

    simulation_id: str
    success: bool
    runtime_seconds: float
    city: str
    climate_zone: int
    weather_file: str
    floor_area_ft2: float
    stories: int
    window_to_wall_ratio: float
    orientation_deg: float
    orientation_mode: str
    window_type: str
    low_e: str
    wall_insulation: str
    attic_r_value: float
    foundation_insulation: str
    output_directory: str
    annual_heating_kwh: float | None = None
    annual_cooling_kwh: float | None = None
    annual_total_hvac_kwh: float | None = None
    peak_heating_kw: float | None = None
    peak_cooling_kw: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    energyplus_return_code: int | None = None
    energyplus_runtime_seconds: float | None = None
    energy_basis: str = "electricity_only"
    metric_sources_json: str = "{}"
    data_source: str = "energyplus"
    run_signature: str = ""
    attempt_id: str = ""
    completed_at: str = ""
    schema_version: int = 1

    @classmethod
    def from_input(cls, simulation: SimulationInput, **values: Any) -> "SimulationResult":
        """Copy every input into the result so no experiment axis is lost."""
        return cls(**simulation.to_dict(), **values, completed_at=datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Return a flat dictionary accepted by pandas.DataFrame."""
        return asdict(self)

    @classmethod
    def column_names(cls) -> list[str]:
        """Provide a stable schema even when exporting an empty dataset."""
        return [field.name for field in fields(cls)]
