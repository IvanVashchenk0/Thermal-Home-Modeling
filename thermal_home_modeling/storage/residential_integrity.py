"""Verify residential batch indexes and rebuild their derived CSV/Parquet views."""

import json
import math
from pathlib import Path

import pandas as pd

from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.errors import StorageError
from thermal_home_modeling.storage.result_store import export_dataframe
from thermal_home_modeling.utilities.files import file_digest, write_json

NUMERIC_COLUMNS = ("heating_load_j", "cooling_load_j", "runtime_seconds", "original_runtime_seconds")


def _records(directory: Path) -> tuple[dict, list[dict]]:
    try:
        manifest = json.loads((directory / "batch_manifest.json").read_text())
        if manifest.get("workflow") != "residential_v0":
            raise ValueError("not a residential batch")
        files = sorted((directory / "records").glob("*.json"))
        rows = []
        seen_indexes = set()
        for file in files:
            index = int(file.stem)
            if index in seen_indexes or index < 0 or index >= len(manifest["cases"]):
                raise ValueError(f"unexpected or duplicate record index: {file.name}")
            seen_indexes.add(index)
            row = json.loads(file.read_text())
            if row["batch_id"] != manifest["batch_id"] or row["input_signature"] != manifest["input_signatures"][index]:
                raise ValueError(f"record identity differs from manifest: {file.name}")
            for key, value in manifest["cases"][index].items():
                if row[key] != value:
                    raise ValueError(f"record input differs from saved case: {file.name}: {key}")
            rows.append(row)
        ids = [row["simulation_id"] for row in rows]
        signatures = [row["input_signature"] for row in rows]
        if len(ids) != len(set(ids)) or len(signatures) != len(set(signatures)):
            raise ValueError("duplicate simulation ID or input signature")
        return manifest, rows
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StorageError(f"Invalid residential batch records: {exc}") from exc


def _frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for field in NUMERIC_COLUMNS:
        if field in frame:
            frame[field] = frame[field].astype("Float64")
    return frame


def _equal(left, right) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not isinstance(left, bool) and not isinstance(right, bool):
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def _compare(label: str, actual: pd.DataFrame, expected: pd.DataFrame, errors: list[str]) -> None:
    if len(actual) != len(expected):
        errors.append(f"{label}: {len(actual)} rows; expected {len(expected)}")
        return
    if set(actual.columns) != set(expected.columns):
        errors.append(f"{label}: columns differ from indexed records")
        return
    if actual.simulation_id.duplicated().any() or set(actual.simulation_id) != set(expected.simulation_id):
        errors.append(f"{label}: simulation IDs are missing or duplicated")
        return
    actual = actual.set_index("simulation_id")
    expected = expected.set_index("simulation_id")
    for simulation_id, row in expected.iterrows():
        for column, value in row.items():
            if not _equal(actual.at[simulation_id, column], value):
                errors.append(f"{label}: {simulation_id} {column} differs from indexed record")
                return


def verify_batch(batch: Path) -> dict:
    """Check saved cases, indexes, exports, and successful raw artifacts."""
    directory = Path(batch).expanduser().resolve()
    manifest, rows = _records(directory)
    errors = []
    if manifest.get("results") != rows:
        errors.append("batch manifest results differ from indexed records")
    if len(rows) != len(manifest["cases"]):
        errors.append(f"{len(rows)} indexed records for {len(manifest['cases'])} cases")
    if len(manifest["input_signatures"]) != len(manifest["cases"]) or len(set(manifest["input_signatures"])) != len(manifest["cases"]):
        errors.append("manifest input signatures are missing or duplicated")
    expected = _frame(rows)
    for label, path, reader in (("CSV", directory / "batch_results.csv", pd.read_csv),
                                ("Parquet", directory / "batch_results.parquet", pd.read_parquet)):
        if not path.is_file():
            errors.append(f"{label} export is missing")
            continue
        try:
            _compare(label, reader(path), expected, errors)
        except Exception as exc:
            errors.append(f"{label} export cannot be read: {exc}")
    failed = [row["simulation_id"] for row in rows if not row["success"]]
    if failed:
        errors.append(f"{len(failed)} simulations failed")
    for row in rows:
        if not row["success"]:
            continue
        try:
            model, raw, result_path = (Path(row[key]) for key in ("generated_input", "output_directory", "result_path"))
            result = json.loads(result_path.read_text())
            if (result["simulation_id"] != row["simulation_id"] or result["input_signature"] != row["input_signature"]
                    or not result["success"] or result["inputs"] != {key: row[key] for key in result["inputs"]}):
                raise ValueError("result identity or inputs differ")
            if file_digest(model) != result["model_sha256"] or file_digest(raw / "eplusout.sql") != result["sql_sha256"]:
                raise ValueError("model or SQL checksum differs")
            if not (raw / "eplusout.end").is_file() or not (raw / "eplusout.err").is_file():
                raise ValueError("EnergyPlus completion files are missing")
            if EnergyPlusRunner._check_completion(raw, result["return_code"])[0]:
                raise ValueError("EnergyPlus completion check failed")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{row['simulation_id']}: raw artifact traceability failed: {exc}")
    return {"batch": str(directory), "expected_cases": len(manifest["cases"]), "indexed_records": len(rows),
            "failed_simulations": failed, "complete": not errors, "errors": errors}


def recover_exports(batch: Path) -> dict:
    """Explicitly rebuild both derived exports after the original writer has stopped."""
    directory = Path(batch).expanduser().resolve()
    manifest, rows = _records(directory)
    if not rows:
        raise StorageError("No indexed records to recover")
    export_dataframe(_frame(rows), directory, stem="batch_results")
    manifest["results"] = rows
    write_json(directory / "batch_manifest.json", manifest)
    return verify_batch(directory)
