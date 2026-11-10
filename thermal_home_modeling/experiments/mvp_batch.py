"""Bounded local workers with a single coordinator for batch state and exports."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
from typing import Callable, Iterable
from uuid import uuid4

import pandas as pd

from thermal_home_modeling.config.mvp_config import MVP_METRICS, MVPPaths, SIZING_POLICY
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.energyplus.installation import EnergyPlusInstallation, detect_energyplus
from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.experiments.mvp_real import prepare_baseline, run_real_mvp
from thermal_home_modeling.experiments.coordinator import coordinate_cases
from thermal_home_modeling.experiments.cpu_resources import cpu_limits
from thermal_home_modeling.models.mvp import MVPResult, MVPSimulationInput
from thermal_home_modeling.storage.result_store import export_dataframe
from thermal_home_modeling.utilities.files import fingerprint, write_json
from thermal_home_modeling.weather.discovery import EPWLocation, discover_weather

LARGE_BATCH_THRESHOLD = 20
TERMINAL = {"Complete", "Cached", "Failed"}


def generate_batch_cases(weather_files: Iterable[Path], orientations: Iterable[float]) -> list[MVPSimulationInput]:
    """Resolve paths and normalize angles before deduplicating effective inputs."""
    angles = tuple(orientations)
    cases = {}
    for weather in weather_files:
        for angle in angles:
            case = MVPSimulationInput(weather_file=weather, orientation_deg=angle)
            if case.orientation_deg not in (0, 90, 180, 270):
                raise ConfigurationError("Batch orientations must be 0, 90, 180, or 270 degrees")
            cases.setdefault((str(case.weather_file), case.orientation_deg, case.scope), case)
    if not cases:
        raise ConfigurationError("Select at least one city and one orientation")
    return list(cases.values())


def select_cities(options: list[EPWLocation], names: str) -> list[Path]:
    """Resolve CLI labels, station IDs, or unique city prefixes; never guess ambiguity."""
    selected = []
    for name in names.split(","):
        query = name.strip().casefold()
        if not query:
            raise ConfigurationError("City names must not be empty")
        exact = [w for w in options if query in (w.city.casefold(), w.label.casefold(), w.station_id.casefold(), w.path.name.casefold())]
        matches = exact or [w for w in options if w.city.casefold().startswith(query)]
        if len(matches) != 1:
            labels = "; ".join(f"{w.label} (station {w.station_id})" for w in (matches or options))
            raise ConfigurationError(f"City {name!r} is missing or ambiguous. Choose a unique city prefix or station ID: {labels}")
        selected.append(matches[0].path)
    return selected


def available_cpus() -> int:
    """Use the most restrictive known allocation, affinity, or quota."""
    return cpu_limits()["available_cpus"]


def worker_limit(mode: str, workers: int | None, case_count: int) -> int:
    if mode not in ("sequential", "parallel"):
        raise ConfigurationError("Execution mode must be sequential or parallel")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise ConfigurationError("Requested workers must be a positive integer or auto")
    return 1 if mode == "sequential" else min(workers or available_cpus(), available_cpus(), case_count)


@dataclass(frozen=True)
class BatchProgress:
    batch_id: str
    total: int
    completed: int
    running: int
    succeeded: int
    failed: int
    cached: int
    elapsed_seconds: float
    rows: list[dict]


@dataclass
class BatchOutcome:
    batch_id: str
    directory: Path
    frame: pd.DataFrame
    summary: dict


def batch_frame(rows: list[dict]) -> pd.DataFrame:
    """Stable nullable types even for a batch with only failures or missing meters."""
    frame = pd.DataFrame(rows)
    floats = {*MVP_METRICS, "orientation_deg", "energyplus_runtime_seconds", "original_energyplus_runtime_seconds"}
    integers = {"warning_count", "return_code"}
    booleans = {"success", "cached", "new_execution"}
    return frame.astype({key: "Float64" if key in floats else "Int64" if key in integers
                         else "boolean" if key in booleans else "string" for key in frame.columns})


def _row(batch_id: str, case: MVPSimulationInput, result: MVPResult) -> dict:
    executed = not result.reused and result.energyplus_runtime_seconds is not None
    return {
        "batch_id": batch_id, "simulation_id": result.simulation_id,
        "city": result.city, "weather_label": result.weather_label or case.weather_metadata["label"],
        "weather_file": result.weather_file, "orientation_deg": result.orientation_deg,
        "scope": result.scope, "energyplus_version": result.energyplus_version,
        "input_signature": result.input_signature, "comparison_signature": result.comparison_signature,
        "data_source": result.data_source,
        "success": result.success, "cached": result.reused, "new_execution": executed,
        "status": "Cached" if result.reused else "Complete" if result.success else "Failed",
        "energyplus_runtime_seconds": result.energyplus_runtime_seconds if executed else None,
        "original_energyplus_runtime_seconds": result.energyplus_runtime_seconds,
        **{key: result.metrics.get(key) if result.success else None for key in MVP_METRICS},
        "warning_count": len(result.warnings), "warnings_json": json.dumps(result.warnings),
        "unavailable_meters_json": json.dumps(result.unavailable_meters),
        "error_type": result.error_type, "error_message": result.error_message,
        "return_code": result.return_code,
        "result_path": str(Path(result.output_directory).parent / "result.json"),
        "generated_input": result.generated_input, "output_directory": result.output_directory,
    }


def _failure(case: MVPSimulationInput, paths: MVPPaths, installation: EnergyPlusInstallation, exc: Exception) -> MVPResult:
    """Retain even preparation/export exceptions under the preassigned case ID."""
    directory = paths.output / case.simulation_id
    saved = directory / "result.json"
    result = None
    if saved.is_file():
        try:
            result = MVPResult.from_dict(json.loads(saved.read_text()))
        except (OSError, ValueError, TypeError):
            pass
    if result is None:
        result = MVPResult(simulation_id=case.simulation_id, city=case.city,
                           weather_file=str(case.weather_file), weather_metadata=case.weather_metadata,
                           weather_label=case.weather_metadata["label"], orientation_deg=case.orientation_deg,
                           energyplus_version=installation.version, executable=str(installation.executable),
                           generated_input=str(paths.input / "generated" / case.simulation_id / "model.epJSON"),
                           output_directory=str(directory / "energyplus"))
    result.success = False
    result.error_type = getattr(exc, "category", "batch_worker")
    result.error_message = f"{type(exc).__name__}: {exc}"
    # If storage itself failed, the coordinator still retains this failure in the batch.
    try:
        write_json(saved, result.to_dict())
    except OSError:
        pass
    return result


def run_mvp_batch(weather_files: Iterable[Path], orientations: Iterable[float], *,
                  mode: str = "sequential", workers: int = 4, reuse: bool = True,
                  allow_large: bool = False, settings: Settings | None = None,
                  paths: MVPPaths | None = None, installation: EnergyPlusInstallation | None = None,
                  on_progress: Callable[[BatchProgress], None] | None = None) -> BatchOutcome:
    """Coordinate isolated MVP workers; callbacks and shared writes run on the caller.

    Started/completed events come from actual worker entry/exit, not submission.
    The original single-writer research ResultStore is never used by these workers.
    """
    cases = generate_batch_cases(weather_files, orientations)
    if len(cases) > LARGE_BATCH_THRESHOLD and not allow_large:
        raise ConfigurationError(f"{len(cases)} configurations exceed {LARGE_BATCH_THRESHOLD}; explicitly confirm a large batch")
    limit = worker_limit(mode, workers, len(cases))
    settings = settings or Settings.from_env()
    paths = paths or MVPPaths.default()
    installation = installation or detect_energyplus(settings.energyplus_exe)
    weather_labels = {str(weather.path): weather.label for weather in discover_weather(installation.weather_data)}
    started = time.perf_counter()
    batch_id = f"batch_{uuid4().hex}"
    directory = paths.output / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=False)
    states = [{"city": weather_labels.get(str(c.weather_file), c.weather_metadata["label"]), "orientation_deg": c.orientation_deg,
               "status": "Queued", "runtime_seconds": None} for c in cases]
    manifest = {"batch_id": batch_id, "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "running", "mode": mode, "requested_workers": workers,
                "max_workers": limit, "available_cpus": available_cpus(), "reuse": reuse,
                "installation": installation.to_dict(), "sizing_policy": SIZING_POLICY,
                "metric_units": {key: spec[2] for key, spec in MVP_METRICS.items()},
                "cases": [c.to_dict() for c in cases], "case_states": states}
    records = {}

    def progress() -> None:
        statuses = [state["status"] for state in states]
        if on_progress:
            on_progress(BatchProgress(batch_id, len(cases), sum(s in TERMINAL for s in statuses),
                                      statuses.count("Running"), statuses.count("Complete") + statuses.count("Cached"),
                                      statuses.count("Failed"), statuses.count("Cached"),
                                      time.perf_counter() - started, [dict(s) for s in states]))

    def checkpoint() -> None:
        manifest["results"] = [records[i] for i in sorted(records)]
        write_json(directory / "batch_manifest.json", manifest)
        if records:
            export_dataframe(batch_frame(manifest["results"]), directory, stem="batch_results")

    checkpoint()
    progress()

    def execute(index: int):
        case = cases[index]
        try:
            result = run_real_mvp(case.orientation_deg, weather_file=case.weather_file, scope=case.scope,
                                  simulation_id=case.simulation_id, settings=settings, paths=paths,
                                  installation=installation, reuse=reuse)
        except Exception as exc:
            result = _failure(case, paths, installation, exc)
        return result

    try:
        # Conversion is complete before submission. Workers only read this cached baseline.
        manifest["baseline"] = str(prepare_baseline(installation, paths.input))
        checkpoint()
        def on_event(event, index, result):
            if event == "started":
                states[index]["status"] = "Running"
            else:
                row = _row(batch_id, cases[index], result)
                row["weather_label"] = weather_labels.get(row["weather_file"], row["weather_label"])
                records[index] = row
                states[index].update(status=row["status"], runtime_seconds=row["energyplus_runtime_seconds"])
                checkpoint()
            progress()
        coordinate_cases(cases, execute, limit, on_event, progress)
    except BaseException as exc:
        manifest.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                        error=f"{type(exc).__name__}: {exc}")
        write_json(directory / "batch_manifest.json", manifest)
        raise

    frame = batch_frame([records[i] for i in range(len(cases))])
    duration = time.perf_counter() - started
    runtimes = [r["energyplus_runtime_seconds"] for r in records.values() if r["new_execution"]]
    executed_successes = sum(r["new_execution"] and r["success"] for r in records.values())
    signatures = sorted(r["input_signature"] for r in records.values())
    summary = {
        "batch_id": batch_id, "mode": mode, "total_cases": len(cases), "max_workers": limit,
        "requested_workers": workers, "available_cpus": manifest["available_cpus"],
        "succeeded": int(frame.success.sum()), "failed": int((~frame.success).sum()),
        "cached_cases": int(frame.cached.sum()), "newly_executed_cases": len(runtimes),
        "new_successful_cases": executed_successes, "wall_clock_seconds": duration,
        "sum_energyplus_runtime_seconds": sum(runtimes),
        "mean_energyplus_runtime_seconds": statistics.mean(runtimes) if runtimes else None,
        "median_energyplus_runtime_seconds": statistics.median(runtimes) if runtimes else None,
        "new_successful_simulations_per_minute": 60 * executed_successes / duration if duration else None,
        "configuration_set_signature": fingerprint(signatures) if all(signatures) else None,
        "benchmark_eligible": len(runtimes) == len(cases) == executed_successes and all(signatures),
        "timing_basis": "Whole batch wall time includes preparation, cache checks, parsing, and dataset exports. Runtime/throughput excludes cached cases.",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "csv_path": str(directory / "batch_results.csv"), "parquet_path": str(directory / "batch_results.parquet"),
    }
    manifest.update(status="complete" if not summary["failed"] else "complete_with_failures",
                    finished_at=summary["finished_at"])
    write_json(directory / "batch_manifest.json", manifest)
    write_json(directory / "batch_summary.json", summary)
    return BatchOutcome(batch_id, directory, frame, summary)


def measured_speedup(first: dict, second: dict) -> float | None:
    """Only compare wholly new, successful, equivalent sequential/parallel batches."""
    pair = {first.get("mode"): first, second.get("mode"): second}
    if set(pair) != {"sequential", "parallel"}:
        return None
    sequential, parallel = pair["sequential"], pair["parallel"]
    if not all(s.get("benchmark_eligible") and s.get("configuration_set_signature") for s in pair.values()):
        return None
    if sequential["configuration_set_signature"] != parallel["configuration_set_signature"]:
        return None
    return sequential["wall_clock_seconds"] / parallel["wall_clock_seconds"] if parallel["wall_clock_seconds"] > 0 else None
