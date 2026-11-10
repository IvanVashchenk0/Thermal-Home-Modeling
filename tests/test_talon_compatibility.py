"""Allocation, path, and interrupted-export regression checks."""

import json
from pathlib import Path

import pandas as pd
import pytest

from test_residential import residential, demo
from thermal_home_modeling.cli import _worker_request
from thermal_home_modeling.config.mvp_config import MVPPaths
from thermal_home_modeling.errors import StorageError
from thermal_home_modeling.experiments import cpu_resources
from thermal_home_modeling.experiments.mvp_batch import worker_limit
from thermal_home_modeling.experiments.residential import run_residential_batch, resume_residential_batch
from thermal_home_modeling.storage.residential_integrity import recover_exports, verify_batch
from thermal_home_modeling.storage.result_store import export_dataframe


def test_residential_paths_override_and_local_default(tmp_path, monkeypatch):
    monkeypatch.delenv("THM_INPUT_ROOT", raising=False)
    monkeypatch.delenv("THM_OUTPUT_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    original = MVPPaths.default()
    assert original.input == tmp_path / "home/Desktop/Academics/Fall 26/ME 487/input"
    assert original.output == tmp_path / "home/Desktop/Academics/Fall 26/ME 487/output"
    monkeypatch.setenv("THM_INPUT_ROOT", str(tmp_path / "linux inputs"))
    monkeypatch.setenv("THM_OUTPUT_ROOT", str(tmp_path / "linux outputs"))
    selected = MVPPaths.default()
    assert selected.input == (tmp_path / "linux inputs").resolve()
    assert selected.output == (tmp_path / "linux outputs").resolve()


def test_custom_paths_run_and_resume(residential, tmp_path, monkeypatch):
    args, runner, cases = residential
    monkeypatch.setenv("THM_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("THM_OUTPUT_ROOT", str(tmp_path / "outputs"))
    args.pop("paths")
    first = run_residential_batch(cases[:2], **args)
    assert first.directory.parent == (tmp_path / "outputs/batches").resolve()
    assert all(Path(path).is_relative_to(tmp_path / "inputs") for path in first.frame.generated_input)
    assert all(Path(path).is_relative_to(tmp_path / "outputs") for path in first.frame.output_directory)
    resumed = resume_residential_batch(first.directory, **{k: v for k, v in args.items() if k not in ("catalog", "mappings", "assumptions")})
    assert resumed.summary["cached_cases"] == 2
    assert runner.run.call_count == 2


@pytest.mark.parametrize("allocation", [1, 4, 8, 16, 32, 36])
def test_slurm_auto_and_manual_worker_caps(allocation, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", str(allocation))
    monkeypatch.setattr(cpu_resources.os, "sched_getaffinity", lambda _: set(range(64)), raising=False)
    monkeypatch.setattr(cpu_resources, "_cgroup_quota", lambda: None)
    assert worker_limit("parallel", None, 1728) == allocation
    assert worker_limit("parallel", 100, 1728) == allocation
    assert worker_limit("parallel", 3, 1728) == min(3, allocation)
    assert worker_limit("parallel", None, 2) == min(2, allocation)
    assert worker_limit("sequential", None, 1728) == 1


def test_conflicting_missing_invalid_cpu_limits(monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "36")
    monkeypatch.setattr(cpu_resources.os, "sched_getaffinity", lambda _: set(range(16)), raising=False)
    monkeypatch.setattr(cpu_resources, "_cgroup_quota", lambda: 8)
    assert cpu_resources.cpu_limits()["available_cpus"] == worker_limit("parallel", None, 100) == 8
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "invalid")
    assert cpu_resources.cpu_limits()["available_cpus"] == 1
    monkeypatch.delenv("SLURM_CPUS_PER_TASK")
    monkeypatch.setattr(cpu_resources.os, "sched_getaffinity", lambda _: (_ for _ in ()).throw(OSError()), raising=False)
    monkeypatch.setattr(cpu_resources, "_cgroup_quota", lambda: None)
    monkeypatch.setattr(cpu_resources.os, "cpu_count", lambda: 128)
    assert cpu_resources.cpu_limits()["available_cpus"] == 1
    assert worker_limit("parallel", None, 100) == 1
    assert _worker_request("36") == 36 and _worker_request("auto") is None


def test_cgroup_quota_uses_parent_and_floor(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    (tmp_path / "cpu.max").write_text("160000 100000")
    (child / "cpu.max").write_text("max 100000")
    monkeypatch.setattr(cpu_resources, "_cgroup_directories", lambda: [(child, "v2"), (tmp_path, "v2")])
    assert cpu_resources._cgroup_quota() == 1


def test_verify_and_recover_interrupted_export(residential, monkeypatch):
    args, _, cases = residential
    batch = run_residential_batch(cases[:2], **args)
    assert verify_batch(batch.directory)["complete"]
    original_to_csv = pd.DataFrame.to_csv
    def interrupted(*_args, **_kwargs):
        raise OSError("interrupted CSV write")
    with monkeypatch.context() as patch:
        patch.setattr(pd.DataFrame, "to_csv", interrupted)
        with pytest.raises(StorageError):
            export_dataframe(batch.frame.iloc[:1], batch.directory, stem="batch_results")
    manifest_path = batch.directory / "batch_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["results"] = []  # An interruption can leave the manifest behind indexed records.
    manifest_path.write_text(json.dumps(manifest))
    assert not verify_batch(batch.directory)["complete"]
    report = recover_exports(batch.directory)
    assert report["complete"] and report["indexed_records"] == 2
    assert len(pd.read_csv(batch.directory / "batch_results.csv")) == 2
    assert len(pd.read_parquet(batch.directory / "batch_results.parquet")) == 2
    assert original_to_csv is pd.DataFrame.to_csv


def test_verifier_detects_missing_failed_and_duplicate_records(residential):
    args, _, cases = residential
    batch = run_residential_batch(cases[:2], **args)
    records = sorted((batch.directory / "records").glob("*.json"))
    first = json.loads(records[0].read_text())
    second = json.loads(records[1].read_text())
    second["simulation_id"] = first["simulation_id"]
    records[1].write_text(json.dumps(second))
    with pytest.raises(StorageError, match="duplicate simulation ID"):
        verify_batch(batch.directory)
    records[1].unlink()
    assert not verify_batch(batch.directory)["complete"]
    second["simulation_id"] = batch.frame.iloc[1].simulation_id
    second["success"] = False
    records[1].write_text(json.dumps(second))
    report = verify_batch(batch.directory)
    assert not report["complete"] and report["failed_simulations"] == [second["simulation_id"]]
