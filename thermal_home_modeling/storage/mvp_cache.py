"""Reuse only complete, intact real runs with matching effective input signatures."""

import json
import logging
from pathlib import Path

from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.energyplus.result_parser import SQLiteResultParser
from thermal_home_modeling.config.output_config import OutputConfig
from thermal_home_modeling.config.mvp_config import MVP_METRICS
from thermal_home_modeling.errors import ResultParsingError
from thermal_home_modeling.models.mvp import MVPResult
from thermal_home_modeling.utilities.files import file_digest, fingerprint

logger = logging.getLogger(__name__)


def find_cached_mvp(directory: Path, *, input_signature: str = "",
                    comparison_signature: str = "", orientation_deg: float | None = None) -> MVPResult | None:
    """Find an exact run or an orientation-compatible run, rejecting legacy guesses.

    Generated input and SQL hashes, completion diagnostics, and manifest signatures
    must still match. Legacy runs lacking signatures remain readable, not reusable.
    """
    if not input_signature and not comparison_signature:
        return None
    files = sorted(directory.glob("mvp_*/result.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in files:
        try:
            result = MVPResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if result.success is not True or result.data_source != "real_energyplus":
                continue
            if input_signature and result.input_signature != input_signature:
                continue
            if comparison_signature and result.comparison_signature != comparison_signature:
                continue
            if orientation_deg is not None and result.orientation_deg != orientation_deg:
                continue
            raw = Path(result.output_directory)
            generated = Path(result.generated_input)
            if raw.resolve() != (path.parent / "energyplus").resolve():
                continue
            manifest = json.loads((generated.parent / "run_manifest.json").read_text(encoding="utf-8"))
            if (manifest.get("input_signature") != result.input_signature
                    or manifest.get("comparison_signature") != result.comparison_signature
                    or fingerprint(manifest.get("comparison_inputs")) != result.comparison_signature
                    or fingerprint({"comparison_signature": result.comparison_signature,
                                    "orientation_deg": result.orientation_deg}) != result.input_signature):
                continue
            if (file_digest(generated) != manifest["generated_sha256"]
                    or not result.sql_sha256 or file_digest(raw / "eplusout.sql") != result.sql_sha256):
                continue
            if EnergyPlusRunner._check_completion(raw, result.return_code)[0] is not None:
                continue
            values = SQLiteResultParser(OutputConfig()).parse_annual_meters(
                raw, required=("Electricity:Facility",),
                optional=tuple(spec[0] for spec in MVP_METRICS.values() if spec[0] != "Electricity:Facility"),
            )
            actual_metrics = {key: values[spec[0]] for key, spec in MVP_METRICS.items() if spec[0] in values}
            if result.metrics != actual_metrics:
                continue
            result.reused = True
            return result
        except (OSError, ValueError, TypeError, KeyError, ResultParsingError) as exc:
            logger.warning("Ignoring unusable cached result %s: %s", path, exc)
    return None
