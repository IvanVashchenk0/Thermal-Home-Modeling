"""Small, explicit configuration for the bundled demonstration building."""

from dataclasses import dataclass
from pathlib import Path

MODEL_NAME = "5ZoneAirCooled"
MODEL_NOTE = "EnergyPlus demonstration building; NOT the Marvin residential model."
SIMULATION_SCOPES = {"annual": "Annual Weather Simulation"}
# Bump if editing/parsing semantics change without changing the effective model.
MVP_PIPELINE_REVISION = "2-location-and-annual-comparison"
SIZING_POLICY = "Retain the example's original sizing design days and HVAC configuration."
# field -> (EnergyPlus meter, display label, engineering unit)
MVP_METRICS = {
    "facility_electricity_kwh": ("Electricity:Facility", "Annual Facility Electricity", "kWh"),
    "facility_natural_gas_kwh_equivalent": ("NaturalGas:Facility", "Annual Facility Natural Gas", "kWh-equivalent"),
    "heating_natural_gas_kwh_equivalent": ("Heating:NaturalGas", "Heating Natural Gas", "kWh-equivalent"),
    "heating_electricity_kwh": ("Heating:Electricity", "Heating Electricity", "kWh"),
    "cooling_electricity_kwh": ("Cooling:Electricity", "Cooling Electricity", "kWh"),
}
# Wildcards request only existing heating/cooling meters, including their actual fuels.
MVP_METER_REQUESTS = ("Electricity:Facility", "NaturalGas:Facility", "Heating:*", "Cooling:*")


@dataclass(frozen=True)
class MVPPaths:
    """User-requested sibling input/output roots, independent of the research data."""

    input: Path
    output: Path

    @classmethod
    def default(cls) -> "MVPPaths":
        """Use the current user's home directory without embedding a username."""
        root = Path.home() / "Desktop" / "Academics" / "Fall 26" / "ME 487"
        return cls(root / "input", root / "output")
