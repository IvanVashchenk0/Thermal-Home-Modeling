"""One selected orientation and, optionally, one compatible 0° reference."""

from dataclasses import dataclass
from pathlib import Path

from thermal_home_modeling.config.mvp_config import MVPPaths
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.energyplus.installation import EnergyPlusInstallation
from thermal_home_modeling.experiments.mvp_real import run_real_mvp
from thermal_home_modeling.models.mvp import MVPResult
from thermal_home_modeling.storage.mvp_cache import find_cached_mvp


@dataclass(frozen=True)
class OrientationStudy:
    """Selected result and an optional baseline; never an unbounded parameter sweep."""

    selected: MVPResult
    baseline: MVPResult | None


def run_orientation_study(orientation_deg: float, *, weather_file: Path,
                          scope: str = "annual", auto_baseline: bool = True,
                          settings: Settings | None = None, paths: MVPPaths | None = None,
                          installation: EnergyPlusInstallation | None = None) -> OrientationStudy:
    """Reuse matching outputs and execute at most two new simulations on submission."""
    paths = paths or MVPPaths.default()
    arguments = dict(weather_file=weather_file, scope=scope, settings=settings, paths=paths, installation=installation)
    selected = run_real_mvp(orientation_deg, **arguments)
    if not selected.success:
        return OrientationStudy(selected, None)
    if selected.orientation_deg == 0:
        return OrientationStudy(selected, selected)
    baseline = find_cached_mvp(paths.output, comparison_signature=selected.comparison_signature, orientation_deg=0)
    if baseline is None and auto_baseline:
        baseline = run_real_mvp(0, **arguments)
    return OrientationStudy(selected, baseline)
