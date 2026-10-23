"""Small editable examples; never launch a large design by default."""

from thermal_home_modeling.models.simulation_input import SimulationInput


def phoenix_example() -> SimulationInput:
    """One illustrative study input; supply the corresponding real EPW/model."""
    return SimulationInput(city="Phoenix", climate_zone=2, weather_file="phoenix.epw")


def small_parameter_space() -> dict[str, list[float]]:
    """Two rotations of the same baseline: two simulations, not a large study."""
    return {"orientation_deg": [0, 90]}

# For a future geometry/construction editor, pass axes such as:
# {"floor_area_ft2": [1500, 3000, 5000], "stories": [1, 2],
#  "window_to_wall_ratio": [0.15, 0.25, 0.35], "window_type": ["double", "triple"],
#  "low_e": ["180", "272", "366"], "orientation_deg": [0, 90]}
# Supply city, climate_zone, and weather_file together as separate base inputs;
# they must not be independently cross-multiplied. Zones 3 and 4 are not chosen yet.
