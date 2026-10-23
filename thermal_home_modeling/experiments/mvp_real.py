"""One real example simulation, shared by the CLI and the small Streamlit page."""

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import time

from thermal_home_modeling.config.mvp_config import (
    MODEL_NOTE, MVP_METRICS, MVP_METER_REQUESTS, MVPPaths,
    MVP_PIPELINE_REVISION, SIMULATION_SCOPES, SIZING_POLICY,
)
from thermal_home_modeling.config.output_config import OutputConfig
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.energyplus.installation import EnergyPlusInstallation, detect_energyplus
from thermal_home_modeling.energyplus.model_editor import EpJSONModelEditor, request_sql_meters
from thermal_home_modeling.energyplus.result_parser import SQLiteResultParser
from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.errors import ConfigurationError, ModelGenerationError, ThermalModelingError
from thermal_home_modeling.models.mvp import MVPResult, MVPSimulationInput
from thermal_home_modeling.storage.result_store import export_dataframe
from thermal_home_modeling.storage.mvp_cache import find_cached_mvp
from thermal_home_modeling.utilities.files import file_digest, fingerprint, write_json
from thermal_home_modeling.weather.discovery import read_epw_location

logger = logging.getLogger(__name__)


def prepare_baseline(installation: EnergyPlusInstallation, input_directory: Path) -> Path:
    """Copy and convert the installed example without executing a simulation.

    A content/version-specific directory keeps provenance immutable across upgrades
    and avoids replacing the baseline used by an earlier run.
    """
    source_hash = file_digest(installation.example_model)
    version = re.sub(r"[^A-Za-z0-9_.-]", "_", installation.version)
    directory = input_directory / "mvp_baseline" / f"{version}_{source_hash[:12]}"
    baseline = directory / "5ZoneAirCooled.epJSON"
    manifest_path = directory / "baseline_manifest.json"
    if baseline.exists() and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("original_sha256") != source_hash
                or manifest.get("converted_sha256") != file_digest(baseline)
                or manifest.get("energyplus_version") != installation.version):
            raise ConfigurationError(f"Baseline provenance mismatch at {directory}; inspect it before reuse")
        return baseline
    directory.mkdir(parents=True, exist_ok=True)
    owned_idf = directory / installation.example_model.name
    shutil.copy2(installation.example_model, owned_idf)
    command = [str(installation.executable), "--convert-only", "--output-directory", str(directory), str(owned_idf)]
    logger.info("Converting project-owned IDF using %s", installation.version)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with (directory / "conversion_stdout.log").open("wb") as stdout, (directory / "conversion_stderr.log").open("wb") as stderr:
            process = subprocess.run(command, cwd=directory, stdout=stdout, stderr=stderr, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelGenerationError(f"IDF conversion failed; inspect {directory}: {exc}") from exc
    if process.returncode != 0 or not baseline.is_file():
        raise ModelGenerationError(f"IDF conversion returned {process.returncode} or produced no epJSON; inspect {directory}")
    write_json(manifest_path, {
        **installation.to_dict(), "original_example_path": str(installation.example_model),
        "original_file_name": installation.example_model.name, "original_sha256": source_hash,
        "energyplus_version": installation.version, "conversion_timestamp": started_at,
        "conversion_finished_at": datetime.now(timezone.utc).isoformat(),
        "conversion_command": command, "conversion_return_code": process.returncode,
        "converted_model": str(baseline), "converted_sha256": file_digest(baseline),
        "weather_file_used": str(installation.weather_file),
        "weather_sha256": file_digest(installation.weather_file), "note": MODEL_NOTE,
    })
    return baseline


def populate_mvp_model(baseline: Path, simulation: MVPSimulationInput) -> dict:
    """Populate orientation, EPW location, and outputs without changing the baseline."""
    try:
        model = json.loads(baseline.read_text(encoding="utf-8"))
        EpJSONModelEditor.change_orientation(model, simulation.orientation_deg)
        weather = read_epw_location(simulation.weather_file)
        model["Site:Location"] = {weather.station_name: weather.site_location()}
        request_sql_meters(model, MVP_METER_REQUESTS)
        model["Output:VariableDictionary"] = {"THM Dictionary": {"key_field": "regular", "sort_option": "Name"}}
        return model
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ModelGenerationError(f"Cannot generate MVP epJSON: {exc}") from exc


def build_mvp_model(baseline: Path, simulation: MVPSimulationInput, destination: Path) -> Path:
    """Reuse the checked orientation editor and SQL output helper on the real copy."""
    if baseline.resolve() == destination.resolve():
        raise ModelGenerationError("Generated model must not overwrite the baseline")
    write_json(destination, populate_mvp_model(baseline, simulation))
    return destination


def comparison_inputs(model: dict, baseline: Path, simulation: MVPSimulationInput,
                      installation: EnergyPlusInstallation, settings: Settings) -> dict:
    """Describe every effective input except the orientation being compared."""
    normalized = json.loads(json.dumps(model))
    next(iter(normalized["Building"].values()))["north_axis"] = 0.0
    return {
        "pipeline_revision": MVP_PIPELINE_REVISION,
        "energyplus_version": installation.version,
        "executable": str(installation.executable),
        "executable_sha256": file_digest(installation.executable),
        "source_model_sha256": file_digest(installation.example_model),
        "baseline_sha256": file_digest(baseline), "model_name": simulation.model_name,
        "model_at_zero_degrees": normalized,
        "weather_file": str(simulation.weather_file),
        "weather_sha256": file_digest(simulation.weather_file),
        "weather_metadata": simulation.weather_metadata, "scope": simulation.scope,
        "meter_requests": list(MVP_METER_REQUESTS), "metric_definitions": MVP_METRICS,
        "timeout_seconds": settings.timeout_seconds, "sizing_policy": SIZING_POLICY,
    }


def read_warnings(output_directory: Path) -> list[str]:
    """Retain EnergyPlus warning headers and continuation details, including summaries."""
    path = output_directory / "eplusout.err"
    if not path.exists():
        return []
    warnings: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.search(r"\*\*\s*Warning\s*\*\*", line, re.I):
            warnings.append(line.strip())
        elif warnings and "**   ~~~   **" in line:
            warnings[-1] += "\n" + line.strip()
    return warnings


def collect_mvp_metrics(result: MVPResult) -> None:
    """Populate verified metrics from preserved output, without executing EnergyPlus.

    Also usable to reparse an already executed run after a parser fix.
    """
    result.success = False
    result.metrics.clear()
    result.unavailable_meters = []
    raw_output = Path(result.output_directory)
    error_type, error_message = EnergyPlusRunner._check_completion(raw_output, result.return_code)
    if error_type:
        result.success = False
        result.error_type, result.error_message = error_type, error_message
        return
    names = tuple(spec[0] for spec in MVP_METRICS.values())
    values = SQLiteResultParser(OutputConfig()).parse_annual_meters(
        raw_output, required=("Electricity:Facility",),
        optional=tuple(name for name in names if name != "Electricity:Facility"),
    )
    result.metrics = {field: values[spec[0]] for field, spec in MVP_METRICS.items() if spec[0] in values}
    result.unavailable_meters = [name for name in names if name not in values]
    result.error_type = result.error_message = None
    result.success = True


def run_real_mvp(orientation_deg: float = 0, *, settings: Settings | None = None,
                 paths: MVPPaths | None = None,
                 installation: EnergyPlusInstallation | None = None,
                 weather_file: Path | None = None, scope: str = "annual",
                 reuse: bool = True, simulation_id: str | None = None) -> MVPResult:
    """Reuse an exact successful run or execute one real annual simulation.

    Conversion is a separate --convert-only process; it never runs a simulation.
    Failure results and all raw files are retained. No retries or sweep occur here.
    Use reuse=False for an explicit rerun. Old runs without signatures are preserved
    but cannot qualify for automatic reuse or comparisons.
    """
    settings = settings or Settings.from_env()
    paths = paths or MVPPaths.default()
    installation = installation or detect_energyplus(settings.energyplus_exe)
    simulation = MVPSimulationInput(weather_file=weather_file or installation.weather_file,
                                    orientation_deg=orientation_deg, scope=scope,
                                    **({"simulation_id": simulation_id} if simulation_id else {}))
    baseline = prepare_baseline(installation, paths.input)
    model = populate_mvp_model(baseline, simulation)
    compatible_inputs = comparison_inputs(model, baseline, simulation, installation, settings)
    compatible_signature = fingerprint(compatible_inputs)
    signature = fingerprint({"comparison_signature": compatible_signature, "orientation_deg": simulation.orientation_deg})
    if reuse:
        cached = find_cached_mvp(paths.output, input_signature=signature)
        if cached:
            logger.info("Loaded existing simulation %s", cached.simulation_id)
            return cached
    input_directory = paths.input / "generated" / simulation.simulation_id
    run_directory = paths.output / simulation.simulation_id
    generated = input_directory / "model.epJSON"
    raw_output = run_directory / "energyplus"
    input_directory.mkdir(parents=True, exist_ok=False)
    run_directory.mkdir(parents=True, exist_ok=False)
    result = MVPResult(
        simulation_id=simulation.simulation_id, energyplus_version=installation.version,
        executable=str(installation.executable), weather_file=str(simulation.weather_file),
        orientation_deg=simulation.orientation_deg, generated_input=str(generated),
        output_directory=str(raw_output), started_at=datetime.now(timezone.utc).isoformat(),
        city=simulation.city, weather_metadata=simulation.weather_metadata,
        weather_label=simulation.weather_metadata["label"], scope=scope,
        input_signature=signature, comparison_signature=compatible_signature,
    )
    start = time.perf_counter()
    log = logging.LoggerAdapter(logger, {"simulation_id": simulation.simulation_id})
    try:
        write_json(input_directory / "simulation_input.json", {
            **simulation.to_dict(), "energyplus_version": installation.version,
            "meter_requests": list(MVP_METER_REQUESTS), "annual_run": True,
        })
        write_json(generated, model)
        write_json(input_directory / "run_manifest.json", {
            "simulation_id": simulation.simulation_id, "installation": installation.to_dict(),
            "baseline": str(baseline), "baseline_manifest": str(baseline.parent / "baseline_manifest.json"),
            "generated_model": str(generated), "generated_sha256": file_digest(generated),
            "city": simulation.city, "weather_file": str(simulation.weather_file),
            "weather_metadata": simulation.weather_metadata, "scope": scope,
            "orientation_deg": simulation.orientation_deg, "energyplus_version": installation.version,
            "weather_sha256": compatible_inputs["weather_sha256"],
            "input_signature": signature, "comparison_signature": compatible_signature,
            "comparison_inputs": compatible_inputs,
            "output_directory": str(raw_output), "model_note": MODEL_NOTE,
        })
        log.info("Generated actual simulation input: %s", generated)
        execution = EnergyPlusRunner((str(installation.executable),), settings.timeout_seconds).run(
            generated, simulation.weather_file, raw_output, simulation_id=simulation.simulation_id,
        )
        write_json(run_directory / "execution.json", asdict(execution))
        result.return_code = execution.return_code
        result.energyplus_runtime_seconds = execution.runtime_seconds
        if not execution.success:
            result.error_type, result.error_message = execution.error_type, execution.error_message
        else:
            collect_mvp_metrics(result)
            result.sql_sha256 = file_digest(raw_output / "eplusout.sql")
    except ThermalModelingError as exc:
        result.error_type, result.error_message = exc.category, str(exc)
    except (OSError, ValueError) as exc:
        result.error_type, result.error_message = "configuration", str(exc)
    finally:
        result.runtime_seconds = time.perf_counter() - start
        result.finished_at = datetime.now(timezone.utc).isoformat()
        result.warnings = read_warnings(raw_output)
        write_json(run_directory / "result.json", result.to_dict())
    export_dataframe(result.to_dataframe(), run_directory)
    if result.warnings:
        log.warning("EnergyPlus reported %s warning entries; inspect %s", len(result.warnings), raw_output / "eplusout.err")
    if not result.success:
        log.error("Real MVP failed (%s): %s", result.error_type, result.error_message)
    return result


def format_summary(result: MVPResult) -> str:
    """Concise terminal summary, with units and paths from the actual run."""
    lines = ["THERMAL HOME MODELING — REAL ENERGYPLUS MVP", "", f"EnergyPlus: {result.energyplus_version}",
             f"Model: {result.model_name} (demonstration building)", f"Weather: {result.weather_label or result.city}",
             f"Scope: {SIMULATION_SCOPES[result.scope]}", f"Orientation: {result.orientation_deg:g} deg", "",
             "Loaded existing simulation" if result.reused else "New simulation completed",
             f"Simulation: {'SUCCESS' if result.success else 'FAILED'}"]
    if result.energyplus_runtime_seconds is not None:
        lines.append(f"EnergyPlus runtime: {result.energyplus_runtime_seconds:.2f} s")
    if result.error_message:
        lines.append(f"Error ({result.error_type}): {result.error_message}")
    if result.metrics:
        lines.extend(["", "RESULTS (annual consumption, not thermal loads)"])
        for field, value in result.metrics.items():
            _, label, unit = MVP_METRICS[field]
            lines.append(f"{label}: {value:,.2f} {unit}")
    if result.unavailable_meters:
        lines.append("Unavailable (omitted): " + ", ".join(result.unavailable_meters))
    lines.extend([f"Warning entries: {len(result.warnings)} (see eplusout.err)", "",
                  f"Generated input: {result.generated_input}", f"Raw output: {result.output_directory}",
                  f"Result: {Path(result.output_directory).parent / 'result.json'}"])
    return "\n".join(lines)
