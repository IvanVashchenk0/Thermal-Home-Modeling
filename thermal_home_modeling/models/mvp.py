"""Minimal demo records, with no fabricated residential envelope attributes."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from thermal_home_modeling.config.mvp_config import MODEL_NAME, MODEL_NOTE, MVP_METRICS, SIMULATION_SCOPES
from thermal_home_modeling.utilities.validation import finite_number, validate_simulation_id
from thermal_home_modeling.weather.discovery import read_epw_location


@dataclass(frozen=True, kw_only=True)
class MVPSimulationInput:
    """One annual demonstration run with a selected EPW and north axis."""

    weather_file: Path
    orientation_deg: float = 0
    simulation_id: str = field(default_factory=lambda: f"mvp_{uuid4().hex}")
    city: str = field(default="", init=False)
    weather_metadata: dict[str, Any] = field(default_factory=dict, init=False)
    scope: str = "annual"
    model_name: str = field(default=MODEL_NAME, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "orientation_deg", finite_number("orientation_deg", self.orientation_deg) % 360)
        object.__setattr__(self, "weather_file", Path(self.weather_file).resolve())
        validate_simulation_id(self.simulation_id)
        if self.weather_file.suffix.lower() != ".epw":
            raise ValueError("weather_file must have an .epw extension")
        if self.scope not in SIMULATION_SCOPES:
            raise ValueError(f"Unsupported simulation scope {self.scope!r}; only annual is implemented")
        location = read_epw_location(self.weather_file)
        object.__setattr__(self, "city", location.city)
        object.__setattr__(self, "weather_metadata", location.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Describe exactly what Python will populate in the demonstration model."""
        return {**asdict(self), "weather_file": str(self.weather_file), "model_note": MODEL_NOTE,
                "modified_physics_fields": ["Building.north_axis", "Site:Location"]}


@dataclass(kw_only=True)
class MVPResult:
    """A compact run record; only verified meter values enter its metrics mapping."""

    simulation_id: str
    energyplus_version: str
    executable: str
    weather_file: str
    orientation_deg: float
    generated_input: str
    output_directory: str
    success: bool = False
    data_source: str = "real_energyplus"
    model_name: str = MODEL_NAME
    model_note: str = MODEL_NOTE
    city: str = "Chicago"
    weather_label: str = ""
    weather_metadata: dict[str, Any] = field(default_factory=dict)
    scope: str = "annual"
    input_signature: str = ""
    comparison_signature: str = ""
    sql_sha256: str = ""
    reused: bool = False
    runtime_seconds: float = 0
    energyplus_runtime_seconds: float | None = None
    return_code: int | None = None
    started_at: str = ""
    finished_at: str = ""
    error_type: str | None = None
    error_message: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    unavailable_meters: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MVPResult":
        """Load current or earlier flat result JSON without changing old artifacts."""
        row = dict(payload)
        metrics = {field: row.pop(field) for field in MVP_METRICS if field in row}
        row.pop("metric_sources", None)
        return cls(**row, metrics=metrics)

    def to_dict(self) -> dict[str, Any]:
        """Flatten verified metrics into JSON fields and retain their named sources."""
        row = asdict(self)
        row.update(row.pop("metrics"))
        row["metric_sources"] = {key: MVP_METRICS[key][0] for key in self.metrics}
        return row

    def to_dataframe(self) -> pd.DataFrame:
        """Produce one analysis row using the existing Pandas/export conventions."""
        row = self.to_dict()
        for key in ("warnings", "unavailable_meters", "metric_sources", "weather_metadata"):
            row[f"{key}_json"] = json.dumps(row.pop(key), sort_keys=True)
        return pd.DataFrame([row])
