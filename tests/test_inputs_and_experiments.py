from dataclasses import replace
from itertools import islice
import json

import pytest

from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.experiments.experiment_generator import generate_experiments
from thermal_home_modeling.models.simulation_input import SimulationInput


@pytest.mark.parametrize("changes", [
    {"floor_area_ft2": 0}, {"floor_area_ft2": float("nan")}, {"floor_area_ft2": True},
    {"stories": 3}, {"stories": True}, {"stories": 1.0}, {"climate_zone": 1},
    {"window_to_wall_ratio": -0.1}, {"window_to_wall_ratio": 1.1},
    {"orientation_deg": float("inf")}, {"attic_r_value": -1}, {"low_e": ""},
    {"window_type": "single"}, {"orientation_mode": "unknown"},
    {"simulation_id": "../unsafe"}, {"simulation_id": "CON"}, {"weather_file": "weather.csv"},
])
def test_invalid_input(simulation, changes):
    with pytest.raises(ValueError):
        replace(simulation, **changes)


def test_normalized_stable_ids(simulation):
    first = replace(simulation, simulation_id="", orientation_deg=-90, floor_area_ft2=1500)
    second = replace(simulation, simulation_id="", orientation_deg=630, floor_area_ft2=1500.0)
    assert first.orientation_deg == 270
    assert first.simulation_id == second.simulation_id
    assert replace(first, simulation_id="", attic_r_value=60).simulation_id != first.simulation_id
    assert SimulationInput(**json.loads(json.dumps(first.to_dict()))) == first


def test_combinations_are_unique_and_order_independent(simulation):
    axes = {"stories": [1, 2], "orientation_deg": [0, 90, 360]}
    first = list(generate_experiments([simulation], axes))
    second = list(generate_experiments([simulation], dict(reversed(list(axes.items())))))
    assert len(first) == 4
    assert [item.simulation_id for item in first] == [item.simulation_id for item in second]


def test_large_space_is_lazy(simulation):
    cases = generate_experiments([simulation], {"floor_area_ft2": range(1, 10001), "orientation_deg": range(360)})
    assert len(list(islice(cases, 3))) == 3


@pytest.mark.parametrize("axes", [{"unknown": [1]}, {"city": ["Boston"]}, {"low_e": "272"}, {"stories": []}])
def test_invalid_axes(simulation, axes):
    with pytest.raises(ConfigurationError):
        list(generate_experiments([simulation], axes))
