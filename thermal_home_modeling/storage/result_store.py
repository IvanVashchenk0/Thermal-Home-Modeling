"""Atomic checkpoints are authoritative; CSV/Parquet are rebuildable views."""

import json
from pathlib import Path

import pandas as pd

from thermal_home_modeling.errors import StorageError
from thermal_home_modeling.models.simulation_result import SimulationResult
from thermal_home_modeling.utilities.files import atomic_destination, write_json
from thermal_home_modeling.utilities.validation import validate_simulation_id


def export_dataframe(frame: pd.DataFrame, directory: Path, *, stem: str = "results") -> tuple[Path, Path]:
    """Write a research or demo dataframe using the same atomic CSV/Parquet policy."""
    csv_path, parquet_path = directory / f"{stem}.csv", directory / f"{stem}.parquet"
    try:
        with atomic_destination(parquet_path) as temporary:
            frame.to_parquet(temporary, engine="pyarrow", index=False)
        with atomic_destination(csv_path) as temporary:
            frame.to_csv(temporary, index=False)
    except (OSError, ValueError, ImportError) as exc:
        raise StorageError(f"Dataset export failed; JSON checkpoints remain available: {exc}") from exc
    return csv_path, parquet_path


class ResultStore:
    """One JSON checkpoint per ID, one row per ID in each exported dataset.

    Sequential single-writer use only. Retries replace the latest row; their full
    inputs/results remain under separate attempt directories for inspection.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.records = directory / "records"
        self.records.mkdir(parents=True, exist_ok=True)

    def load(self, simulation_id: str) -> SimulationResult | None:
        """Read one checkpoint; never silently discard a corrupt record."""
        validate_simulation_id(simulation_id)
        path = self.records / f"{simulation_id}.json"
        if not path.exists():
            return None
        try:
            result = SimulationResult(**json.loads(path.read_text(encoding="utf-8")))
            if result.schema_version != 1 or result.simulation_id != simulation_id:
                raise ValueError("Checkpoint schema or simulation ID mismatch")
            return result
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"Invalid checkpoint {path}: {exc}") from exc

    def save(self, result: SimulationResult) -> None:
        """Commit one row before starting another simulation."""
        validate_simulation_id(result.simulation_id)
        try:
            write_json(self.records / f"{result.simulation_id}.json", result.to_dict())
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"Could not checkpoint {result.simulation_id}: {exc}") from exc

    def to_dataframe(self) -> pd.DataFrame:
        """Collect current checkpoints in deterministic order for analysis."""
        records = [self.load(path.stem).to_dict() for path in sorted(self.records.glob("*.json"))]
        frame = pd.DataFrame(records, columns=SimulationResult.column_names())
        # Stable Arrow schemas across all-failure/all-null and successful batches.
        integer_columns = {"climate_zone", "stories", "energyplus_return_code", "schema_version"}
        numeric_columns = {"runtime_seconds", "floor_area_ft2", "window_to_wall_ratio", "orientation_deg", "attic_r_value",
                           "annual_heating_kwh", "annual_cooling_kwh", "annual_total_hvac_kwh", "peak_heating_kw", "peak_cooling_kw", "energyplus_runtime_seconds"}
        types = {column: "boolean" if column == "success" else "Int64" if column in integer_columns else "Float64" if column in numeric_columns else "string" for column in frame.columns}
        return frame.astype(types)

    def export(self) -> tuple[Path, Path]:
        """Rebuild atomic CSV/Parquet snapshots from checkpoints.

        Each file is replaced atomically, but the pair is not a transaction. After
        interruption, rerun export to align both with the authoritative records.
        """
        return export_dataframe(self.to_dataframe(), self.directory)
