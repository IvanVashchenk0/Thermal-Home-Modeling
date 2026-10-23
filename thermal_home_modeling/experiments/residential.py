"""Residential planning/checkpoints on the shared coordinator, runner and SQL parser."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from itertools import product
import json
from pathlib import Path
import time
import statistics
from uuid import uuid4

import pandas as pd

from thermal_home_modeling.catalogs.workbook import load_catalog
from thermal_home_modeling.config.mvp_config import MVPPaths
from thermal_home_modeling.config.output_config import OutputConfig
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.energyplus.installation import detect_energyplus
from thermal_home_modeling.energyplus.residential_model import LOAD_VARIABLES, PARSER_CONFIG, generate_residential_model, load_v0_configuration, symmetric_180
from thermal_home_modeling.energyplus.result_parser import SQLiteResultParser
from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.errors import ConfigurationError, ThermalModelingError
from thermal_home_modeling.experiments.coordinator import coordinate_cases
from thermal_home_modeling.experiments.mvp_batch import BatchOutcome, BatchProgress, worker_limit
from thermal_home_modeling.experiments.mvp_real import read_warnings
from thermal_home_modeling.models.residential import ResidentialInput
from thermal_home_modeling.storage.result_store import export_dataframe
from thermal_home_modeling.utilities.files import file_digest, fingerprint, write_json
from thermal_home_modeling.weather.discovery import discover_weather


def resolve_residential_weather(installation, mappings, city_id="washington_dc"):
    bindings = mappings.get("weather_bindings", {mappings["active_city"]: mappings["weather_binding"]})
    if city_id not in bindings:
        raise ConfigurationError(f"{city_id}: matching weather mapping not implemented")
    binding = bindings[city_id]
    from thermal_home_modeling.catalogs.workbook import CATALOG_ROOT
    local = CATALOG_ROOT.parent / "data/weather/research"
    options = discover_weather(installation.weather_data) + (discover_weather(local) if local.exists() else [])
    matches = [w.path for w in options if w.station_id == binding["station_id"]
               and w.station_name == binding["station_name"] and w.region == binding["region"]]
    if len(matches) != 1:
        raise ConfigurationError(f"A unique matching {city_id} EPW is required; no substitute weather is allowed")
    return matches[0]


def generate_residential_cases(weather_file, *, cities=("washington_dc",), houses=("1S",), walls=("W3", "W9"),
                               attic_levels=(49, 80), orientations=(0, 90), **unsupported):
    if unsupported:
        raise ConfigurationError(f"Catalog-only/unsupported residential parameters: {', '.join(unsupported)}")
    cases = {}
    for city, house, wall, attic, angle in product(cities, houses, walls, attic_levels, orientations):
        case = ResidentialInput(city, Path(weather_file).resolve(), house, wall, float(attic), float(angle))
        cases.setdefault(fingerprint(case.to_dict()), case)
    if not cases:
        raise ConfigurationError("Select at least one supported value for every active dimension")
    return list(cases.values())


def _cached(paths, signature, candidates=None):
    # Only compact records are scanned; at most a matching candidate SQL is opened.
    for path in (candidates if candidates is not None else sorted(paths.output.glob("res_*/result.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)):
        try:
            result = json.loads(path.read_text())
            if not result["success"] or result["input_signature"] != signature:
                continue
            raw, generated = Path(result["output_directory"]), Path(result["generated_input"])
            manifest = json.loads((generated.parent / "run_manifest.json").read_text())
            if json.loads((generated.parent / "simulation_input.json").read_text()) != result["inputs"]:
                continue
            if raw.resolve() != (path.parent / "energyplus").resolve() or fingerprint(manifest["effective_inputs"]) != signature:
                continue
            if file_digest(generated) != result["model_sha256"] or file_digest(raw / "eplusout.sql") != result["sql_sha256"]:
                continue
            if fingerprint(json.loads(generated.read_text())) != fingerprint(manifest["effective_inputs"]["model"]):
                continue
            if EnergyPlusRunner._check_completion(raw, result["return_code"])[0]:
                continue
            outputs = SQLiteResultParser(OutputConfig()).parse_raw_annual_variables(raw, LOAD_VARIABLES, key=PARSER_CONFIG["key"])
            if outputs != result["outputs"]:
                continue
            return {**result, "cached": True}
        except (OSError, ValueError, KeyError, TypeError, ThermalModelingError):
            continue
    return None


def prepare_residential_cases(cases, catalog, mappings, assumptions, installation, paths, *, reuse=True):
    plans = []
    from thermal_home_modeling.experiments.residential_design import physical_identity
    executable_hash = file_digest(installation.executable)
    candidates = {}
    if reuse:
        for path in sorted(paths.output.glob("res_*/result.json"), key=lambda p:p.stat().st_mtime_ns, reverse=True):
            try:
                row = json.loads(path.read_text())
                if row.get("success"):
                    candidates.setdefault(row["input_signature"], []).append(path)
            except (OSError, ValueError, KeyError):
                continue
    weather_hashes, identities = {}, set()
    for case in cases:
        model = generate_residential_model(case, catalog, mappings, assumptions, installation.version)
        if symmetric_180(model) and case.orientation_deg >= 180:
            case = replace(case, orientation_deg=case.orientation_deg % 180)
            model = generate_residential_model(case, catalog, mappings, assumptions, installation.version)
        if case.weather_file not in weather_hashes:
            weather_hashes[case.weather_file] = file_digest(case.weather_file)
        physical = physical_identity(model, weather_hashes[case.weather_file])
        if physical in identities:
            continue
        identities.add(physical)
        # Full workbook hashes are provenance, not physical cache identity. Only
        # relevant catalog/model values participate, so note/price edits do not rerun physics.
        relevant_catalog = {"wall_mapping": catalog["wall_assemblies"][case.wall_id]["value"]["mapping"],
                            "house": {key: catalog["houses"][case.house_id]["value"][key] for key in ("stories", "floor_area_ft2")},
                            "attic_r_ip": case.attic_r_ip}
        effective = {"model": model, "effective_catalog_revision": fingerprint(relevant_catalog),
                     "weather_sha256": weather_hashes[case.weather_file], "weather_path": str(case.weather_file),
                     "energyplus_version": installation.version, "executable_sha256": executable_hash,
                     "parser_configuration": PARSER_CONFIG, "generator_revision": "residential-v0-1"}
        signature = fingerprint(effective)
        plans.append({"case": case, "model": model, "effective": effective, "signature": signature,
                      "cached_result": _cached(paths, signature, candidates.get(signature, [])) if reuse else None,
                      "symmetry_180_verified": symmetric_180(model)})
    return plans


def execute_residential_case(plan, catalog, assumptions, paths, installation, settings, campaign_directory):
    if plan["cached_result"] is not None:
        return plan["cached_result"]
    case = plan["case"]
    simulation_id = "res_" + uuid4().hex
    generated = paths.input / "generated" / simulation_id / "model.epJSON"
    directory = paths.output / simulation_id
    raw = directory / "energyplus"
    result = {"simulation_id": simulation_id, "input_signature": plan["signature"], "success": False, "cached": False,
              "data_source": "real_energyplus_residential_v0", "inputs": case.to_dict(),
              "city": catalog["locations"][case.city_id]["value"]["city"],
              "climate_zone": catalog["locations"][case.city_id]["value"]["climate_zone"],
              "generated_input": str(generated), "output_directory": str(raw), "result_path": str(directory / "result.json"),
              "catalog_revision": catalog["workbook_sha256"], "campaign_snapshot": str(campaign_directory / "catalog_snapshot.json"),
              "energyplus_version": installation.version, "outputs": {}, "warnings": [], "error_type": None, "error_message": None,
              "return_code": None, "energyplus_runtime_seconds": None, "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        generated.parent.mkdir(parents=True, exist_ok=False)
        directory.mkdir(parents=True, exist_ok=False)
        write_json(generated, plan["model"])
        write_json(generated.parent / "simulation_input.json", case.to_dict())
        result["model_sha256"] = file_digest(generated)
        write_json(generated.parent / "run_manifest.json", {"effective_inputs": plan["effective"],
            "input_signature": plan["signature"], "catalog_revision": catalog["workbook_sha256"],
            "catalog_snapshot": str(campaign_directory / "catalog_snapshot.json"), "provisional_assumptions": assumptions,
            "symmetry_180_verified": plan["symmetry_180_verified"], "generated_sha256": result["model_sha256"],
            "weather_location": plan["model"]["Site:Location"], "data_status": "Provisional", "research_validated": False})
        execution = EnergyPlusRunner((str(installation.executable),), settings.timeout_seconds).run(
            generated, case.weather_file, raw, simulation_id=simulation_id)
        write_json(directory / "execution.json", asdict(execution))
        result.update(return_code=execution.return_code, energyplus_runtime_seconds=execution.runtime_seconds,
                      error_type=execution.error_type, error_message=execution.error_message)
        if execution.success:
            result["outputs"] = SQLiteResultParser(OutputConfig()).parse_raw_annual_variables(raw, LOAD_VARIABLES, key=PARSER_CONFIG["key"])
            result["sql_sha256"] = file_digest(raw / "eplusout.sql")
            result["success"] = True
    except Exception as exc:
        result.update(error_type=getattr(exc, "category", "residential"), error_message=f"{type(exc).__name__}: {exc}")
    finally:
        result["warnings"] = read_warnings(raw)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(directory / "result.json", result)
    return result


def residential_index_row(batch_id, result):
    inputs = result["inputs"]
    row = {"batch_id": batch_id, "simulation_id": result["simulation_id"], **inputs,
           "city": result["city"], "climate_zone": result["climate_zone"],
           "success": result["success"], "cached": result["cached"],
           "status": "Cached" if result["cached"] else "Complete" if result["success"] else "Failed",
           "runtime_seconds": None if result["cached"] else result["energyplus_runtime_seconds"],
           "original_runtime_seconds": result["energyplus_runtime_seconds"],
           "warning_count": len(result["warnings"]), "warnings_json": json.dumps(result["warnings"]),
           "error_type": result["error_type"], "error_message": result["error_message"],
           **{key: result[key] for key in ("input_signature", "catalog_revision", "generated_input", "output_directory", "result_path", "energyplus_version")}}
    for field in LOAD_VARIABLES:
        output = result["outputs"].get(field, {})
        row[field] = output.get("value") if result["success"] else None
        row[field+"_units"] = output.get("units")
        row[field+"_frequency"] = output.get("frequency")
    return row


def run_residential_batch(cases, *, mode="parallel", workers=4, reuse=True, allow_large=False, on_progress=None,
                          settings=None, installation=None, paths=None, catalog=None, mappings=None, assumptions=None,
                          parent_batch=None, experiment=None, batch_directory=None):
    settings = settings or Settings.from_env()
    installation = installation or detect_energyplus(settings.energyplus_exe)
    paths = paths or MVPPaths.default()
    catalog = catalog or load_catalog()
    default_mappings, default_assumptions = load_v0_configuration()
    mappings, assumptions = mappings or default_mappings, assumptions or default_assumptions
    if not cases:
        raise ConfigurationError("A batch needs at least one valid case")
    limit = worker_limit(mode, workers, len(cases))
    start = time.perf_counter()
    plans = prepare_residential_cases(cases, catalog, mappings, assumptions, installation, paths, reuse=reuse)
    cases = [p["case"] for p in plans]
    limit = worker_limit(mode, workers, len(cases))
    batch_id = Path(batch_directory).name if batch_directory else "residential_" + uuid4().hex
    directory = Path(batch_directory) if batch_directory else paths.output / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "catalog_snapshot.json", catalog)
    write_json(directory / "mapping_snapshot.json", mappings)
    write_json(directory / "assumptions_snapshot.json", assumptions)
    states = [{"city": catalog["locations"][p["case"].city_id]["value"]["city"], "orientation_deg": p["case"].orientation_deg,
               "wall": p["case"].wall_id, "attic": p["case"].attic_r_ip, "status": "Queued", "runtime_seconds": None} for p in plans]
    manifest = {"batch_id": batch_id, "workflow": "residential_v0", "status": "running", "mode": mode, "max_workers": limit,
                "parent_batch": parent_batch, "cases": [p["case"].to_dict() for p in plans], "case_states": states,
                "input_signatures": [p["signature"] for p in plans], "catalog_revision": catalog["workbook_sha256"],
                "started_at": datetime.now(timezone.utc).isoformat(), "results": []}
    if experiment is not None:
        write_json(directory / "experiment.json", experiment)
    records = {}
    last_checkpoint = 0.0
    last_export_count = 0
    def checkpoint(force=False):
        nonlocal last_checkpoint, last_export_count
        if not force and time.perf_counter()-last_checkpoint < 2 and len(records)-last_export_count < 25:
            return
        last_checkpoint, last_export_count = time.perf_counter(), len(records)
        manifest["results"] = [records[i] for i in sorted(records)]
        write_json(directory / "batch_manifest.json", manifest)
        if records:
            frame = pd.DataFrame(manifest["results"])
            for field in ("heating_load_j", "cooling_load_j", "runtime_seconds", "original_runtime_seconds"):
                frame[field] = frame[field].astype("Float64")
            export_dataframe(frame, directory, stem="batch_results")
    def progress():
        status = [s["status"] for s in states]
        if on_progress:
            on_progress(BatchProgress(batch_id, len(plans), len(records), status.count("Running"),
                        status.count("Complete")+status.count("Cached"), status.count("Failed"), status.count("Cached"),
                        time.perf_counter()-start, [dict(s) for s in states]))
    def execute(index):
        return execute_residential_case(plans[index], catalog, assumptions, paths, installation, settings, directory)
    def event(kind, index, result):
        if kind == "started":
            states[index]["status"] = "Running"
        else:
            row = residential_index_row(batch_id, result)
            records[index] = row
            write_json(directory / "records" / f"{index:08d}.json", row)
            states[index].update(status=row["status"], runtime_seconds=row["runtime_seconds"])
            checkpoint()
        progress()
    checkpoint()
    try:
        progress()
        coordinate_cases(plans, execute, limit, event, progress)
    except BaseException as exc:
        manifest.update(status="interrupted", interruption=f"{type(exc).__name__}: {exc}")
        checkpoint(force=True)
        raise
    checkpoint(force=True)
    frame = pd.read_parquet(directory / "batch_results.parquet")
    summary = {"batch_id": batch_id, "mode": mode, "max_workers": limit, "total_cases": len(cases),
               "succeeded": int(frame.success.sum()), "failed": int((~frame.success).sum()), "cached_cases": int(frame.cached.sum()),
               "newly_executed_cases": int(frame.runtime_seconds.notna().sum()), "wall_clock_seconds": time.perf_counter()-start,
               "catalog_revision": catalog["workbook_sha256"], "research_validated": False}
    manifest["status"] = "complete" if not summary["failed"] else "complete_with_failures"
    checkpoint(force=True)
    elapsed = time.perf_counter()-start
    runtimes = frame.runtime_seconds.dropna().tolist()
    new_successes = int((frame.success & ~frame.cached).sum())
    summary.update(wall_clock_seconds=elapsed, sum_energyplus_runtime_seconds=sum(runtimes),
                   mean_energyplus_runtime_seconds=statistics.mean(runtimes) if runtimes else None,
                   median_energyplus_runtime_seconds=statistics.median(runtimes) if runtimes else None,
                   new_successful_simulations_per_minute=60*new_successes/elapsed if elapsed else 0,
                   completed_configurations_per_minute=60*len(frame)/elapsed if elapsed else 0,
                   case_set_signature=fingerprint(sorted(frame.input_signature.tolist())),
                   benchmark_eligible=not summary["failed"] and not summary["cached_cases"] and len(runtimes)==len(frame),
                   csv_path=str(directory / "batch_results.csv"), parquet_path=str(directory / "batch_results.parquet"))
    write_json(directory / "batch_summary.json", summary)
    return BatchOutcome(batch_id, directory, frame, summary)


def resume_residential_batch(directory, **kwargs):
    """New campaign attempt from immutable prior snapshots; valid finished cases are reused."""
    directory = Path(directory)
    manifest = json.loads((directory / "batch_manifest.json").read_text())
    if manifest.get("workflow") != "residential_v0":
        raise ConfigurationError("Resume requires a residential V0 batch")
    cases = [ResidentialInput(**{**row, "weather_file": Path(row["weather_file"])}) for row in manifest["cases"]]
    return run_residential_batch(cases, catalog=json.loads((directory / "catalog_snapshot.json").read_text()),
        mappings=json.loads((directory / "mapping_snapshot.json").read_text()),
        assumptions=json.loads((directory / "assumptions_snapshot.json").read_text()), parent_batch=manifest["batch_id"],
        experiment=json.loads((directory / "experiment.json").read_text()) if (directory / "experiment.json").exists() else None, **kwargs)


def compare_residential_timing(first, second):
    """Timing comparison only, requiring identical, successful, entirely fresh work."""
    if not (first.get("benchmark_eligible") and second.get("benchmark_eligible") and
            first.get("case_set_signature") == second.get("case_set_signature")):
        raise ConfigurationError("Timing comparison requires identical successful configuration sets with cache disabled")
    return {"first_batch": first["batch_id"], "second_batch": second["batch_id"],
            "first_workers": first["max_workers"], "second_workers": second["max_workers"],
            "first_wall_seconds": first["wall_clock_seconds"], "second_wall_seconds": second["wall_clock_seconds"],
            "first_over_second_wall_time": first["wall_clock_seconds"] / second["wall_clock_seconds"]}
