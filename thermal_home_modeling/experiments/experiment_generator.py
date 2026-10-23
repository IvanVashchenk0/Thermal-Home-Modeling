"""Lazy Cartesian-product experiments with stable parameter-derived IDs."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import fields, replace
from itertools import product
from typing import Any

from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.models.simulation_input import SimulationInput


def generate_experiments(bases: Iterable[SimulationInput], parameter_space: Mapping[str, Sequence[Any]]) -> Iterator[SimulationInput]:
    """Yield unique combinations per linked city/zone/weather base.

    Axes must be finite sequences. IDs include all normalized inputs and do not
    depend on iteration order. Repeated equivalent combinations are deduplicated.
    """
    allowed = {field.name for field in fields(SimulationInput)} - {"simulation_id", "city", "climate_zone", "weather_file"}
    unknown = set(parameter_space) - allowed
    if unknown:
        raise ConfigurationError(f"Invalid independent axes: {sorted(unknown)}; keep city/zone/weather in base inputs")
    names = sorted(parameter_space)
    axes = []
    for name in names:
        values = parameter_space[name]
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
            raise ConfigurationError(f"Parameter axis {name!r} must be a nonempty finite sequence")
        axes.append(values)
    seen: set[str] = set()
    for base in bases:
        for values in product(*axes):
            try:
                simulation = replace(base, simulation_id="", **dict(zip(names, values)))
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(f"Invalid experiment combination: {dict(zip(names, values))}: {exc}") from exc
            if simulation.simulation_id not in seen:
                seen.add(simulation.simulation_id)
                yield simulation
