"""Sequential orchestration: checkpoint every completed attempt before yielding."""

from collections.abc import Iterable, Iterator
from dataclasses import asdict
import json
import logging
from pathlib import Path
import time
from uuid import uuid4

from thermal_home_modeling import __version__
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.energyplus.model_editor import EpJSONModelEditor, ModelEditor
from thermal_home_modeling.energyplus.result_parser import ResultParser, SQLiteResultParser
from thermal_home_modeling.energyplus.runner import EnergyPlusRunner, ExecutionResult
from thermal_home_modeling.errors import ConfigurationError, EnergyPlusExecutionError, EnergyPlusSimulationError, StorageError, ThermalModelingError
from thermal_home_modeling.models.simulation_input import SimulationInput
from thermal_home_modeling.models.simulation_result import SimulationResult
from thermal_home_modeling.storage.result_store import ResultStore
from thermal_home_modeling.utilities.files import file_digest, fingerprint, write_json

logger = logging.getLogger(__name__)


class BatchRunner:
    """One simulation at a time with injected adapters for future extensions.

    A campaign is single-writer. Use a separate results directory after changing
    model/weather/configuration. Storage errors stop work because continuing would
    risk losing results; individual simulation failures are recorded and continued.
    """

    def __init__(self, settings: Settings, *, editor: ModelEditor | None = None,
                 runner: EnergyPlusRunner | None = None, parser: ResultParser | None = None,
                 store: ResultStore | None = None, data_source: str = "energyplus"):
        self.settings = settings
        self.editor = editor or EpJSONModelEditor(settings.baseline_parameters, settings.outputs)
        self.runner = runner or EnergyPlusRunner((settings.energyplus_exe,), settings.timeout_seconds)
        self.parser = parser or SQLiteResultParser(settings.outputs)
        self.store = store or ResultStore(settings.paths.results)
        self.data_source = data_source
        # Reuse digests for unchanged shared EPWs; a new stat triggers rehashing.
        self._digests: dict[tuple[Path, int, int, int], str] = {}

    def _asset_signature(self, path: Path) -> dict[str, str]:
        path = path.resolve()
        try:
            stat = path.stat()
            key = (path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if key not in self._digests:
                self._digests[key] = file_digest(path)
            return {"path": str(path), "sha256": self._digests[key]}
        except OSError as exc:
            # Missing assets must still lead to a saved per-simulation failure.
            return {"path": str(path), "unavailable": type(exc).__name__}

    def _signature(self, simulation: SimulationInput) -> str:
        return fingerprint({
            "input": simulation.to_dict(), "baseline": self._asset_signature(self.settings.baseline_model),
            "baseline_parameters": self._asset_signature(self.settings.baseline_parameters),
            "weather": self._asset_signature(self.settings.weather_path(simulation.weather_file)),
            "outputs": asdict(self.settings.outputs), "command": self.runner.command,
            "timeout_seconds": self.runner.timeout_seconds, "package_version": __version__,
            "editor": type(self.editor).__qualname__, "parser": type(self.parser).__qualname__,
            "data_source": self.data_source,
        })

    def run(self, simulations: Iterable[SimulationInput], *, resume: bool = True, export_every: int = 100) -> Iterator[SimulationResult]:
        """Yield durable result rows; exhaust or close the iterator to export.

        Resume reuses successful identical inputs/assets/settings; failed runs are
        retried in fresh attempt directories. A successful ID/signature conflict
        stops the campaign instead of silently mixing incompatible datasets.
        """
        if type(export_every) is not int or export_every < 1:
            raise ConfigurationError("export_every must be a positive integer")
        pending_exports = 0
        try:
            for simulation in simulations:
                signature = self._signature(simulation)
                prior = self.store.load(simulation.simulation_id)
                if prior and prior.success and prior.run_signature != signature:
                    raise ConfigurationError(f"Successful checkpoint {simulation.simulation_id} has different inputs/assets/settings; use a new results directory")
                if prior and prior.success and resume:
                    logger.info("Reusing successful checkpoint", extra={"simulation_id": simulation.simulation_id})
                    yield prior
                    continue
                result = self._run_one(simulation, signature)
                self.store.save(result)
                pending_exports += 1
                if pending_exports >= export_every:
                    self.store.export()
                    pending_exports = 0
                yield result
        finally:
            # Includes KeyboardInterrupt and an invalid later experiment axis.
            self.store.export()

    def _run_one(self, simulation: SimulationInput, signature: str) -> SimulationResult:
        start = time.perf_counter()
        attempt_id = uuid4().hex
        attempt_directory = self.settings.paths.simulation_outputs / simulation.simulation_id / attempt_id
        output_directory = attempt_directory / "energyplus"
        model_path = self.settings.paths.generated_models / simulation.simulation_id / f"{attempt_id}{self.editor.suffix}"
        log = logging.LoggerAdapter(logger, {"simulation_id": simulation.simulation_id})
        execution: ExecutionResult | None = None
        metrics = {}
        error_type = error_message = None
        log.info("Simulation started; attempt=%s", attempt_id)
        try:
            attempt_directory.mkdir(parents=True, exist_ok=False)
            write_json(attempt_directory / "input.json", simulation.to_dict())
            weather = self.settings.weather_path(simulation.weather_file)
            if not weather.is_file():
                raise ConfigurationError(f"Missing EPW weather file: {weather}")
            self.editor.build_model(self.settings.baseline_model, simulation, model_path)
            log.info("Model generated: %s", model_path)
            execution = self.runner.run(model_path, weather, output_directory, simulation_id=simulation.simulation_id)
            if not execution.success:
                error_class = EnergyPlusSimulationError if execution.error_type == "simulation" else EnergyPlusExecutionError
                raise error_class(execution.error_message)
            log.info("Parsing results from %s", output_directory)
            metrics = asdict(self.parser.parse(output_directory))
        except ThermalModelingError as exc:
            if isinstance(exc, StorageError):
                raise
            error_type, error_message = exc.category, str(exc)
            log.error("Simulation failed (%s): %s", error_type, error_message)
        except Exception as exc:
            error_type, error_message = "internal", f"{type(exc).__name__}: {exc}"
            log.exception("Unexpected simulation failure")
        result = SimulationResult.from_input(
            simulation, success=error_type is None, runtime_seconds=time.perf_counter() - start,
            output_directory=str(output_directory), attempt_id=attempt_id, run_signature=signature,
            error_type=error_type, error_message=error_message,
            energyplus_return_code=execution.return_code if execution else None,
            energyplus_runtime_seconds=execution.runtime_seconds if execution else None,
            energy_basis=self.settings.outputs.energy_basis,
            metric_sources_json=json.dumps(asdict(self.settings.outputs), sort_keys=True),
            data_source=self.data_source, **metrics,
        )
        try:
            write_json(attempt_directory / "result.json", result.to_dict())
        except OSError as exc:
            raise StorageError(f"Could not preserve attempt result at {attempt_directory}: {exc}") from exc
        log.info("Simulation finished: success=%s runtime=%.3fs", result.success, result.runtime_seconds)
        return result
