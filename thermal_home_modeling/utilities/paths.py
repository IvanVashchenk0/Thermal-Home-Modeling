"""Paths are absolute and anchored to a single project root."""

from dataclasses import dataclass
from pathlib import Path


def repository_root() -> Path:
    """Locate the root in the supported source/editable installation layout."""
    return Path(__file__).resolve().parents[2]


def resolve_path(value: str | Path, root: Path) -> Path:
    """Resolve a user path relative to root, including home expansion."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


@dataclass(frozen=True)
class ProjectPaths:
    """All shared input and output locations for a campaign."""

    root: Path
    data: Path
    weather: Path
    baseline_models: Path
    generated_models: Path
    simulation_outputs: Path
    results: Path

    @classmethod
    def from_root(cls, root: Path | None = None, data_dir: str | Path = "data") -> "ProjectPaths":
        """Create paths without performing filesystem writes."""
        root = (root or repository_root()).resolve()
        data = resolve_path(data_dir, root)
        return cls(root, data, data / "weather", data / "baseline_models", data / "generated_models", data / "simulation_outputs", data / "results")

    def ensure_directories(self) -> None:
        """Create the campaign directories if necessary."""
        for path in (self.weather, self.baseline_models, self.generated_models, self.simulation_outputs, self.results):
            path.mkdir(parents=True, exist_ok=True)
