"""Small public CLI for a single case, two-case experiment, smoke, and export."""

import argparse
import json
import logging
import os
from pathlib import Path
import signal

from thermal_home_modeling.config.experiment_config import phoenix_example, small_parameter_space
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.errors import ThermalModelingError
from thermal_home_modeling.experiments.batch_runner import BatchRunner
from thermal_home_modeling.experiments.experiment_generator import generate_experiments
from thermal_home_modeling.storage.result_store import ResultStore
from thermal_home_modeling.utilities.logging_config import configure_logging

logger = logging.getLogger(__name__)


def _worker_request(value: str) -> int | None:
    if value.casefold() == "auto":
        return None
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--workers requires a positive integer or auto") from exc
    if workers < 1:
        raise argparse.ArgumentTypeError("--workers requires a positive integer or auto")
    return workers


def main(argv: list[str] | None = None) -> int:
    """Run the selected minimal workflow; nonzero exit means something failed."""
    parser = argparse.ArgumentParser(description="Thermal Home Modeling: single-run reliability before scale")
    parser.add_argument("mode", nargs="?", choices=("single", "small", "smoke", "export", "mvp-real", "mvp-batch", "catalog-import", "weather-fetch", "residential", "residential-batch", "residential-verify", "residential-recover"), default="single")
    parser.add_argument("--config", type=Path, help="Residential experiment JSON")
    parser.add_argument("--count", type=int, help="Number of unique configurations to sample")
    parser.add_argument("--sampling", choices=("full_factorial", "random", "balanced"))
    parser.add_argument("--seed", type=int)
    parser.add_argument("--fresh", action="store_true", help="Disable cache reuse; same as --rerun")
    parser.add_argument("--background", action="store_true", help="Submit a detached persistent residential worker")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report exact counts; do not execute")
    parser.add_argument("--save-config", type=Path, help="Save the resolved experiment specification")
    parser.add_argument("--resume-job", type=Path, help="Resume a detached job, including preflight interruptions")
    parser.add_argument("--job-status", type=Path, help="Read a detached job status")
    parser.add_argument("--stop-job", type=Path, help="Stop scheduling work; retain active case checkpoints")
    parser.add_argument("--workbook", type=Path)
    parser.add_argument("--walls", default=None, help="Residential wall IDs, comma separated")
    parser.add_argument("--attic-levels", default=None, help="Residential nominal insulation R-values, comma separated")
    parser.add_argument("--house", default=None, help="Comma-separated residential house IDs")
    parser.add_argument("--city", default=None, help="Residential city ID")
    parser.add_argument("--resume-batch", type=Path)
    parser.add_argument("--batch", type=Path, help="Saved residential batch for verification or export recovery")
    parser.add_argument("--orientation", type=float, default=0, help="North axis in degrees for mvp-real (default: 0)")
    parser.add_argument("--weather", type=Path, help="EPW path for mvp-real (default: bundled Chicago when available)")
    parser.add_argument("--scope", choices=("annual",), default="annual", help="Implemented MVP simulation scope")
    parser.add_argument("--root", type=Path, help="Project root (default: this source checkout)")
    parser.add_argument("--rerun", action="store_true", help="Rerun identical successful inputs in fresh attempt directories")
    parser.add_argument("--cities", default=None, help="Comma-separated installed city names, unique prefixes, or station IDs for mvp-batch")
    parser.add_argument("--orientations", default=None, help="Comma-separated batch north axes (commercial default 0,90,180,270; residential 0,90)")
    parser.add_argument("--mode", dest="execution_mode", choices=("sequential", "parallel"), default=None, help="Batch execution mode")
    parser.add_argument("--workers", type=_worker_request, default=4, help="Positive worker limit or auto; capped by allocated CPUs")
    parser.add_argument("--allow-large-batch", action="store_true", help="Explicitly allow more than 20 configurations")
    parser.add_argument("--compare-batch", type=Path, help="Previous batch_summary.json for an equivalent uncached sequential/parallel timing comparison")
    args = parser.parse_args(argv)
    args.rerun = args.rerun or args.fresh
    configure_logging()
    if os.environ.get("SLURM_JOB_ID") and args.mode in ("residential", "residential-batch"):
        def interrupt_job(signum, _frame):
            raise KeyboardInterrupt(f"Slurm signal {signum}")
        signal.signal(signal.SIGTERM, interrupt_job)
        signal.signal(signal.SIGUSR1, interrupt_job)
    try:
        settings = Settings.from_env(args.root)
        if args.mode in ("residential-verify", "residential-recover"):
            from thermal_home_modeling.storage.residential_integrity import verify_batch, recover_exports
            if args.batch is None:
                raise ValueError(f"{args.mode} requires --batch")
            report = recover_exports(args.batch) if args.mode == "residential-recover" else verify_batch(args.batch)
            print(json.dumps(report, indent=2))
            return 0 if report["complete"] else 1
        if args.mode == "weather-fetch":
            from thermal_home_modeling.weather.research import fetch_research_weather
            logger.info("Research weather files: %s", fetch_research_weather())
            return 0
        if args.mode == "catalog-import":
            from thermal_home_modeling.catalogs.workbook import import_catalog
            if args.workbook is None:
                raise ValueError("catalog-import requires --workbook")
            report = import_catalog(args.workbook)
            logger.info("Catalog revision %s: %s changed cells, %s issues. Full report: catalogs/revisions/%s/import_report.json",
                        report['revision'], len(report['changed_cells']), len(report['issues']), report['revision'])
            return 0
        if args.mode in ("residential", "residential-batch"):
            from thermal_home_modeling.catalogs.workbook import load_catalog
            from thermal_home_modeling.config.mvp_config import MVPPaths
            from thermal_home_modeling.energyplus.installation import detect_energyplus
            from thermal_home_modeling.energyplus.residential_model import load_v0_configuration
            from thermal_home_modeling.experiments.residential import (generate_residential_cases, resolve_residential_weather,
                run_residential_batch, resume_residential_batch, prepare_residential_cases, compare_residential_timing)
            from thermal_home_modeling.experiments.residential_design import default_design, design_from_spec
            from thermal_home_modeling.experiments.campaign_jobs import submit_campaign, submit_resume, retry_job, job_status
            from thermal_home_modeling.utilities.files import write_json
            if args.job_status:
                print(json.dumps(job_status(args.job_status), indent=2))
                return 0
            if args.stop_job:
                if not (args.stop_job / "request.json").is_file():
                    raise ValueError("Not a saved residential job")
                (args.stop_job / "stop_requested").touch()
                logger.info("Stop requested; active cases will finish and retain raw artifacts")
                return 0
            installation = detect_energyplus(settings.energyplus_exe)
            options = dict(mode=args.execution_mode or ("sequential" if args.workers == 1 else "parallel"),
                           workers=args.workers, reuse=not (args.rerun or args.fresh), settings=settings, installation=installation)
            if args.resume_job:
                if any([args.config, args.count, args.sampling, args.cities, args.city, args.house, args.walls, args.attic_levels, args.orientations, args.resume_batch]):
                    raise ValueError("Resume job uses saved selections; do not combine it with new inputs")
                if args.dry_run: raise ValueError("Use --job-status to inspect a saved job")
                job = retry_job(args.resume_job, **options)
                logger.info("Persistent resumed job: %s", job)
                return 0
            if args.resume_batch:
                if any([args.config, args.count, args.sampling, args.cities, args.city, args.house, args.walls, args.attic_levels, args.orientations]):
                    raise ValueError("Resume uses the saved case set; do not combine it with new selections")
                if args.dry_run:
                    raise ValueError("Use --dry-run with an experiment configuration; resume uses its saved case set")
                if args.background:
                    job = submit_resume(args.resume_batch, **options)
                    logger.info("Persistent job: %s", job)
                    return 0
                outcome = resume_residential_batch(args.resume_batch, **options)
            else:
                mappings, assumptions = load_v0_configuration()
                catalog = load_catalog()
                if args.mode == "residential":
                    weather = args.weather or resolve_residential_weather(installation, mappings, args.city or "washington_dc")
                    cases = generate_residential_cases(weather, cities=[args.city or "washington_dc"], houses=[args.house or "1S"],
                        walls=(args.walls or "W3").split(","), attic_levels=[float(x) for x in (args.attic_levels or "49").split(",")], orientations=[args.orientation])
                    experiment = None
                else:
                    spec = json.loads(args.config.read_text()) if args.config else default_design(catalog, mappings)
                    if not isinstance(spec, dict): raise ValueError("Experiment JSON must be an object")
                    for key, values in (("city_id", args.cities or args.city), ("house_id", args.house), ("wall_id", args.walls),
                                        ("attic_r_ip", args.attic_levels), ("orientation_deg", args.orientations)):
                        if values is not None:
                            spec.setdefault("fixed", {}).pop(key, None)
                            spec.setdefault("parameters", {})[key] = [float(x) if key in ("attic_r_ip", "orientation_deg") else x.strip() for x in values.split(",")]
                    if args.weather:
                        selected_cities = spec.get("parameters", {}).get("city_id", [spec.get("fixed", {}).get("city_id", "washington_dc")])
                        if len(selected_cities) != 1: raise ValueError("--weather requires exactly one selected city")
                        spec["weather_files"] = {selected_cities[0]: str(args.weather.resolve())}
                    if args.seed is not None: spec["seed"] = args.seed
                    if args.count is not None:
                        spec["count"] = args.count
                        if spec.get("sampling", "full_factorial") == "full_factorial": spec["sampling"] = "random"
                    if args.sampling:
                        spec["sampling"] = args.sampling
                        if args.sampling == "full_factorial":
                            if args.count is not None: raise ValueError("--count cannot be combined with full_factorial")
                            spec.pop("count", None)
                    design = design_from_spec(spec, catalog, mappings, assumptions, installation)
                    cases = design.cases
                    experiment = {"specification": design.spec, "design": design.summary()}
                    logger.info("Experiment: %s", json.dumps(design.summary(), indent=2))
                    if args.save_config: write_json(args.save_config, design.spec)
                plans = prepare_residential_cases(cases, catalog, mappings, assumptions, installation, MVPPaths.default(), reuse=options["reuse"])
                cached = sum(p["cached_result"] is not None for p in plans)
                logger.info("Exact preflight: %s configurations; %s new; %s cached", len(plans), len(plans)-cached, cached)
                if args.dry_run: return 0
                if args.background:
                    job = submit_campaign(cases, experiment=experiment, **options)
                    logger.info("Persistent job: %s (browser/terminal may close)", job)
                    return 0
                outcome = run_residential_batch(cases, experiment=experiment, **options)
            if args.compare_batch:
                comparison = compare_residential_timing(json.loads(args.compare_batch.read_text()), outcome.summary)
                write_json(outcome.directory / "timing_comparison.json", comparison)
                logger.info("Timing comparison: %s", comparison)
            logger.info("Residential batch: %s\n%s", outcome.directory, json.dumps(outcome.summary, indent=2))
            return 1 if outcome.summary["failed"] else 0
        if args.mode == "mvp-batch":
            from thermal_home_modeling.energyplus.installation import detect_energyplus
            from thermal_home_modeling.experiments.mvp_batch import measured_speedup, run_mvp_batch, select_cities
            from thermal_home_modeling.weather.discovery import discover_weather
            installation = detect_energyplus(settings.energyplus_exe)
            weather = select_cities(discover_weather(installation.weather_data), args.cities or "Chicago")
            previous = json.loads(args.compare_batch.read_text()) if args.compare_batch else None
            outcome = run_mvp_batch(weather, [float(value) for value in (args.orientations or "0,90,180,270").split(",")],
                                    mode=args.execution_mode or "sequential", workers=args.workers, reuse=not (args.rerun or args.fresh),
                                    allow_large=args.allow_large_batch, settings=settings, installation=installation)
            summary = outcome.summary
            logger.info("Batch %s: %s succeeded, %s failed, %s cached, %s newly executed; %.2f s wall time; max workers %s",
                        outcome.batch_id, summary["succeeded"], summary["failed"], summary["cached_cases"],
                        summary["newly_executed_cases"], summary["wall_clock_seconds"], summary["max_workers"])
            logger.info("EnergyPlus runtime sum: %.2f s; mean: %s s; median: %s s; new successful cases/min: %.2f",
                        summary["sum_energyplus_runtime_seconds"], summary["mean_energyplus_runtime_seconds"],
                        summary["median_energyplus_runtime_seconds"], summary["new_successful_simulations_per_minute"])
            if previous:
                speedup = measured_speedup(previous, summary)
                logger.info("Observed speedup: %s", f"{speedup:.2f}×" if speedup is not None else "unavailable: requires equivalent, successful, wholly new sequential/parallel batches")
            logger.info("Batch manifest/summary: %s\nCSV: %s\nParquet: %s", outcome.directory, summary["csv_path"], summary["parquet_path"])
            return 1 if summary["failed"] else 0
        if args.mode == "mvp-real":
            from thermal_home_modeling.experiments.mvp_real import format_summary, run_real_mvp
            result = run_real_mvp(args.orientation, settings=settings, weather_file=args.weather,
                                  scope=args.scope, reuse=not args.rerun)
            logger.info("\n%s", format_summary(result))
            return 0 if result.success else 1
        if args.mode == "smoke":
            from thermal_home_modeling.smoke.workflow import run_smoke
            directory = run_smoke(settings.paths.data / "smoke")
            logger.info("Synthetic smoke passed; fixed test values only. Results: %s", directory)
            return 0
        configure_logging(settings.paths.results / "pipeline.log")
        if args.mode == "export":
            logger.info("Exported datasets: %s", ResultStore(settings.paths.results).export())
            return 0
        cases = [phoenix_example()] if args.mode == "single" else generate_experiments([phoenix_example()], small_parameter_space())
        failures = 0
        for result in BatchRunner(settings).run(cases, resume=not args.rerun):
            logger.info("Result: %s", json.dumps(result.to_dict(), indent=2), extra={"simulation_id": result.simulation_id})
            failures += not result.success
        return 1 if failures else 0
    except (ThermalModelingError, OSError, ValueError) as exc:
        logger.error("Workflow failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted; completed simulation checkpoints are preserved")
        return 130
