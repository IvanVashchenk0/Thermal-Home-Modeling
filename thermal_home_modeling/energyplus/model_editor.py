"""epJSON adapter with explicit boundaries for future building edits.

The metadata file is an engineering attestation about the baseline, not proof
derived from geometry. Only orientation and output requests are edited today.
"""

import json
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Iterable

from thermal_home_modeling.config.output_config import OutputConfig
from thermal_home_modeling.errors import ConfigurationError, ModelGenerationError
from thermal_home_modeling.models.simulation_input import SimulationInput
from thermal_home_modeling.utilities.files import write_json


def request_sql_meters(model: dict[str, Any], names: Iterable[str]) -> None:
    """Enable SQLite and request RunPeriod meters without duplicate exact requests."""
    model["Output:SQLite"] = {"THM SQLite": {"option_type": "SimpleAndTabular"}}
    meters = model.setdefault("Output:Meter", {})
    for index, name in enumerate(names):
        # Promote an existing meter-file-only request so it also appears in SQL/ESO.
        for entries in (meters, model.get("Output:Meter:MeterFileOnly", {})):
            for key, request in list(entries.items()):
                if (request.get("key_name", "").casefold() == name.casefold()
                        and request.get("reporting_frequency", "").casefold() in ("runperiod", "environment")):
                    del entries[key]
        key = f"THM Meter {index}"
        while key in meters:
            key += "_"
        meters[key] = {"key_name": name, "reporting_frequency": "RunPeriod"}


class ModelEditor(Protocol):
    """The batch runner does not depend on an underlying model file format."""

    suffix: str

    def build_model(self, baseline_model: Path, simulation_input: SimulationInput, output_model_path: Path) -> Path:
        """Build an isolated model or explain why a requested change is unsupported."""
        ...


class EpJSONModelEditor:
    """Preserve baseline physics and reject unsupported parameter changes."""

    suffix = ".epJSON"

    def __init__(self, baseline_parameters: Path, outputs: OutputConfig):
        self.baseline_parameters = baseline_parameters
        self.outputs = outputs

    def build_model(self, baseline_model: Path, simulation_input: SimulationInput, output_model_path: Path) -> Path:
        """Copy baseline, verify unchanged parameters, rotate, and request SQL data."""
        if baseline_model.resolve() == output_model_path.resolve():
            raise ModelGenerationError("Generated model must not overwrite the baseline")
        if baseline_model.suffix.lower() != ".epjson":
            raise ModelGenerationError("Initial editor supports epJSON only; convert IDF using the matching EnergyPlus version")
        try:
            model = json.loads(baseline_model.read_text(encoding="utf-8"))
            baseline = json.loads(self.baseline_parameters.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Missing baseline asset: {exc.filename}") from exc
        except (OSError, ValueError) as exc:
            raise ModelGenerationError(f"Cannot read baseline epJSON/parameters: {exc}") from exc
        if not isinstance(model, dict) or not isinstance(baseline, dict):
            raise ModelGenerationError("Baseline and parameter metadata must be JSON objects")
        for method in (self.change_floor_area, self.change_stories, self.change_orientation_mode,
                       self.change_wwr, self.change_window_construction, self.change_low_e,
                       self.change_wall_insulation, self.change_attic_insulation, self.change_foundation_insulation):
            method(model, simulation_input, baseline)
        try:
            self.change_orientation(model, simulation_input.orientation_deg)
            self.configure_outputs(model)
            write_json(output_model_path, model)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ModelGenerationError(f"Invalid epJSON structure or unwritable generated model: {exc}") from exc
        return output_model_path

    @staticmethod
    def _require_baseline_value(simulation: SimulationInput, baseline: dict[str, Any], field: str) -> None:
        if field not in baseline:
            raise ModelGenerationError(f"Baseline metadata lacks {field!r}; record its verified value before running")
        requested = getattr(simulation, field)
        if isinstance(baseline[field], bool) or baseline[field] != requested:
            raise ModelGenerationError(f"TODO: changing {field} is not implemented (baseline={baseline[field]!r}, requested={requested!r})")

    def change_floor_area(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: resize surfaces, zones, internal gains, and dependent geometry."""
        self._require_baseline_value(simulation, baseline, "floor_area_ft2")

    def change_stories(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: create floors/zones and preserve interzone adjacency."""
        self._require_baseline_value(simulation, baseline, "stories")

    def change_orientation_mode(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: derive the long/short facade direction from the actual footprint."""
        self._require_baseline_value(simulation, baseline, "orientation_mode")

    def change_wwr(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: resize fenestration within parent walls; define facade allocation."""
        self._require_baseline_value(simulation, baseline, "window_to_wall_ratio")

    def change_window_construction(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: map double/triple pane choices to verified constructions."""
        self._require_baseline_value(simulation, baseline, "window_type")

    def change_low_e(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: use validated optical/thermal data for the specified glass stack."""
        self._require_baseline_value(simulation, baseline, "low_e")

    def change_wall_insulation(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: map assemblies to construction/material layers."""
        self._require_baseline_value(simulation, baseline, "wall_insulation")

    def change_attic_insulation(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: convert US R-values and update the correct roof/ceiling layers."""
        self._require_baseline_value(simulation, baseline, "attic_r_value")

    def change_foundation_insulation(self, model: dict, simulation: SimulationInput, baseline: dict) -> None:
        """TODO: identify slab/basement/foundation objects before modifying them."""
        self._require_baseline_value(simulation, baseline, "foundation_insulation")

    @staticmethod
    def change_orientation(model: dict[str, Any], orientation_deg: float) -> None:
        """Set the absolute north axis only when every coordinate system is Relative."""
        buildings = model.get("Building", {})
        rules = model.get("GlobalGeometryRules", {})
        if len(buildings) != 1 or len(rules) != 1:
            raise ModelGenerationError("Orientation editing requires exactly one Building and GlobalGeometryRules object")
        rule = next(iter(rules.values()))
        for field in ("coordinate_system", "daylighting_reference_point_coordinate_system", "rectangular_surface_coordinate_system"):
            default = "" if field == "coordinate_system" else "Relative"
            if rule.get(field, default).casefold() != "relative":
                raise ModelGenerationError(f"Orientation editing requires Relative {field}; World coordinates need a geometry adapter")
        if model.get("Site:VariableLocation"):
            raise ModelGenerationError("Scheduled building location/orientation requires a specialized editor")
        next(iter(buildings.values()))["north_axis"] = orientation_deg

    def configure_outputs(self, model: dict[str, Any]) -> None:
        """Request SQL, annual meters, and explicitly configured peak variables."""
        request_sql_meters(model, self.outputs.all_meters())
        for label, variable in (("Heating", self.outputs.peak_heating), ("Cooling", self.outputs.peak_cooling)):
            if variable:
                model.setdefault("Output:Variable", {})[f"THM Peak {label}"] = {
                    "key_value": variable.key, "variable_name": variable.name,
                    "reporting_frequency": variable.frequency,
                }
