"""Pure comparison math; units and missing/zero baselines stay explicit."""

from dataclasses import dataclass
import math

from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.models.mvp import MVPResult

HEATING_FIELDS = ("heating_natural_gas_kwh_equivalent", "heating_electricity_kwh")


@dataclass(frozen=True)
class EnergyChange:
    """Full precision difference; percent is undefined when the baseline is zero."""

    label: str
    current: float
    baseline: float
    difference: float
    percent: float | None
    unit: str


def energy_change(label: str, current: float, baseline: float, unit: str) -> EnergyChange:
    """Compute a neutral signed change, never divide by zero or round raw values."""
    if not all(math.isfinite(value) and value >= 0 for value in (current, baseline)):
        raise ValueError("Energy values must be finite and nonnegative")
    difference = current - baseline
    return EnergyChange(label, current, baseline, difference,
                        100 * difference / baseline if baseline else None, unit)


def compare_orientations(selected: MVPResult, baseline: MVPResult) -> list[EnergyChange]:
    """Compare only identical effective inputs with a 0° baseline, per measured fuel."""
    if (not selected.success or not baseline.success or baseline.orientation_deg != 0
            or not selected.comparison_signature
            or selected.comparison_signature != baseline.comparison_signature
            or selected.weather_file != baseline.weather_file
            or selected.energyplus_version != baseline.energyplus_version
            or selected.scope != baseline.scope or selected.model_name != baseline.model_name):
        raise ConfigurationError("The selected result and 0° baseline do not have matching simulation inputs")
    changes = []
    current_heating = {key for key in HEATING_FIELDS if key in selected.metrics}
    baseline_heating = {key for key in HEATING_FIELDS if key in baseline.metrics}
    if current_heating and current_heating == baseline_heating:
        changes.append(energy_change("Heating", sum(selected.metrics[key] for key in current_heating),
                                     sum(baseline.metrics[key] for key in baseline_heating), "kWh-eq/year"))
    for label, field in (("Cooling", "cooling_electricity_kwh"), ("Electricity", "facility_electricity_kwh")):
        if field in selected.metrics and field in baseline.metrics:
            changes.append(energy_change(label, selected.metrics[field], baseline.metrics[field], "kWh/year"))
    return changes


def format_percent(change: EnergyChange) -> str:
    """Arrows express direction, not a judgment of good or bad."""
    if change.percent is None:
        return "— (baseline is zero)"
    if abs(change.percent) < 0.05:
        return "0.0%"
    return f"{'↑' if change.percent > 0 else '↓'} {abs(change.percent):.1f}%"


def heating_cooling_components(result: MVPResult) -> dict[str, float]:
    """Return disjoint end uses on a purchased-energy-equivalent chart basis."""
    labels = {"heating_natural_gas_kwh_equivalent": "Heating · natural gas",
              "heating_electricity_kwh": "Heating · electricity",
              "cooling_electricity_kwh": "Cooling · electricity"}
    return {label: result.metrics[key] for key, label in labels.items() if key in result.metrics}
