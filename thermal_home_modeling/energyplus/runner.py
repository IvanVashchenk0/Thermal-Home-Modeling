"""Run EnergyPlus in an isolated directory with durable process diagnostics."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import shutil
import subprocess
import time

from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.utilities.files import write_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    """Process outcome; complete stdout/stderr are captured in separate files."""

    command: tuple[str, ...]
    return_code: int | None
    runtime_seconds: float
    success: bool
    stdout_path: str
    stderr_path: str
    error_type: str | None = None
    error_message: str | None = None
    started_at: str = ""
    finished_at: str = ""


class EnergyPlusRunner:
    """Subprocess adapter; command may include a wrapper for testing only."""

    def __init__(self, command: tuple[str, ...] = ("energyplus",), timeout_seconds: float = 3600):
        if not command:
            raise ConfigurationError("EnergyPlus command cannot be empty")
        self.command = command
        self.timeout_seconds = timeout_seconds

    def run(self, model_path: Path, weather_file: Path, output_directory: Path, *, simulation_id: str = "-") -> ExecutionResult:
        """Execute an annual run and check return code, end marker, and error file.

        Existing nonempty output directories are rejected to prevent stale outputs
        from making a failed rerun look successful. Severe errors fail this study's
        quality gate even if EnergyPlus returns zero; warnings remain in .err.
        """
        for path in (model_path, weather_file):
            if not path.is_file():
                raise ConfigurationError(f"Missing EnergyPlus input: {path}")
        output_directory = output_directory.resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        if any(output_directory.iterdir()):
            raise ConfigurationError(f"EnergyPlus output directory must be empty: {output_directory}")
        executable = shutil.which(self.command[0])
        if executable is None:
            raise ConfigurationError(f"EnergyPlus executable not found: {self.command[0]!r}; configure ENERGYPLUS_EXE")
        # Preserve symlinks: resolving a virtualenv Python to the system binary
        # changes its environment and can also break executable wrapper layouts.
        command = (str(Path(executable).absolute()), *self.command[1:], "--annual", "--weather", str(weather_file.resolve()),
                   "--output-directory", str(output_directory), str(model_path.resolve()))
        stdout_path = output_directory / "stdout.log"
        stderr_path = output_directory / "stderr.log"
        start = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        return_code = None
        error_type = error_message = None
        log = logging.LoggerAdapter(logger, {"simulation_id": simulation_id})
        log.info("EnergyPlus started")
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                completed = subprocess.run(command, cwd=output_directory, stdout=stdout, stderr=stderr,
                                           timeout=self.timeout_seconds, check=False, shell=False)
            return_code = completed.returncode
        except subprocess.TimeoutExpired:
            error_type, error_message = "execution", f"EnergyPlus exceeded timeout of {self.timeout_seconds:g} seconds"
        except OSError as exc:
            error_type, error_message = "execution", f"Could not execute EnergyPlus: {exc}"
        runtime = time.perf_counter() - start
        if error_type is None:
            error_type, error_message = self._check_completion(output_directory, return_code)
        result = ExecutionResult(
            command, return_code, runtime, error_type is None,
            str(stdout_path), str(stderr_path), error_type, error_message,
            started_at, datetime.now(timezone.utc).isoformat(),
        )
        write_json(output_directory / "execution.json", asdict(result))
        log.info("EnergyPlus finished: success=%s return_code=%s runtime=%.3fs", result.success, return_code, runtime)
        return result

    @staticmethod
    def _check_completion(output_directory: Path, return_code: int | None) -> tuple[str | None, str | None]:
        error_file = output_directory / "eplusout.err"
        end_file = output_directory / "eplusout.end"
        errors = error_file.read_text(encoding="utf-8", errors="replace") if error_file.exists() else ""
        severe = [line.strip() for line in errors.splitlines() if re.search(r"\*\*\s*(Fatal|Severe)\s*\*\*", line, re.I)]
        if severe:
            return "simulation", "; ".join(severe[:5])
        if return_code != 0:
            return "execution", f"EnergyPlus exited with code {return_code}; inspect stdout.log, stderr.log, and eplusout.err"
        if not end_file.exists() or "EnergyPlus Completed Successfully" not in end_file.read_text(encoding="utf-8", errors="replace"):
            return "simulation", "Missing successful completion marker in eplusout.end"
        if not error_file.exists():
            return "simulation", "Missing eplusout.err; cannot verify simulation diagnostics"
        return None, None
