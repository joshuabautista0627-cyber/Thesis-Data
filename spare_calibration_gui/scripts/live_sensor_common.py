"""Shared provenance and atomic-output helpers for live-sensor commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping

from core.live_sensor_contracts import (
    ContractError,
    canonical_json_bytes,
    canonical_json_hash,
    load_json_object,
    validate_json_schema,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STUDY_SCHEMA = PROJECT_ROOT / "contracts" / "live_sensor_contract.schema.json"
MANUAL_ONLY_STUDY_SCHEMA = (
    PROJECT_ROOT / "contracts" / "live_sensor_manual_only_contract.schema.json"
)
MANUAL_ONLY_OUTPUT_NAMESPACE = "live_sensor_manual_only"


def utc_now_iso() -> str:
    """Return a strict UTC timestamp for manifests and logs."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: str | Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Hash one file without loading it into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    """Return SHA-256 for bytes."""

    return hashlib.sha256(value).hexdigest()


def code_hash(paths: Iterable[str | Path]) -> str:
    """Hash relative names and bytes for the files controlling one command."""

    digest = hashlib.sha256()
    for path in sorted((Path(item) for item in paths), key=lambda item: str(item)):
        selected = path if path.is_absolute() else PROJECT_ROOT / path
        try:
            relative = selected.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            relative = selected.name
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(selected.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_project_path(value: str, *, writable: bool = False) -> Path:
    """Resolve config paths and keep writable outputs inside the project."""

    candidate = Path(value)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (PROJECT_ROOT / candidate).resolve()
    )
    if writable:
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ContractError(
                f"configured writable path leaves the project: {resolved}"
            ) from exc
    return resolved


def is_manual_only_config(config: Mapping[str, Any]) -> bool:
    """Return whether a study declares the strict manual-only source policy."""

    firewall = config.get("dataset_firewall")
    return isinstance(firewall, Mapping) and firewall.get("mode") == "manual_only"


def assert_manual_only_source_firewall(config: Mapping[str, Any]) -> None:
    """Reject non-whitelisted sources and shared output roots before source reads."""

    if not is_manual_only_config(config):
        raise ContractError("manual-only dataset firewall is not enabled")
    firewall = config["dataset_firewall"]
    sources = config.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != {"manual"}:
        raise ContractError("manual-only config must contain exactly the manual source")
    manual = sources["manual"]
    if not isinstance(manual, Mapping) or manual.get("archive_role") != "model_primary":
        raise ContractError("manual source must have the model_primary role")
    selected_name = Path(str(manual.get("archive_path", ""))).name.casefold()
    allowed_names = {
        str(value).casefold() for value in firewall.get("allowed_archive_filenames", ())
    }
    if selected_name not in allowed_names:
        raise ContractError(
            f"source firewall rejected non-whitelisted archive: {selected_name or '<empty>'}"
        )
    forbidden_names = {
        str(value).casefold()
        for value in firewall.get("forbidden_archive_filenames", ())
    }
    if selected_name in forbidden_names:
        raise ContractError("source firewall rejected forbidden archive")
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise ContractError("manual-only config paths are missing")
    for key, value in paths.items():
        if key == "roi_layout":
            continue
        resolved = resolve_project_path(str(value), writable=True)
        if MANUAL_ONLY_OUTPUT_NAMESPACE not in {
            part.casefold() for part in resolved.parts
        }:
            raise ContractError(
                "manual-only writable paths must use the dedicated "
                f"{MANUAL_ONLY_OUTPUT_NAMESPACE} namespace: {resolved}"
            )


def assert_manual_only_artifact_lineage(value: Any) -> None:
    """Fail if a proposed manual-only artifact contains forbidden source lineage."""

    def visit(item: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                text_key = str(key)
                lowered = text_key.casefold()
                if lowered in {"automated", "auto calibration.zip"}:
                    raise ContractError(
                        "manual-only artifact contains forbidden lineage at "
                        + "/".join((*path, text_key))
                    )
                visit(child, (*path, text_key))
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, (*path, str(index)))
            return
        if isinstance(item, str) and "auto calibration.zip" in item.casefold():
            raise ContractError(
                "manual-only artifact contains forbidden archive lineage at "
                + "/".join(path)
            )

    visit(value)


def load_study_config(config_path: str | Path) -> tuple[dict[str, Any], str, Path]:
    """Schema-validate a legacy or strict manual-only study configuration."""

    selected = Path(config_path).resolve()
    config = load_json_object(selected)
    schema = MANUAL_ONLY_STUDY_SCHEMA if is_manual_only_config(config) else STUDY_SCHEMA
    config_hash = validate_json_schema(selected, schema)
    if is_manual_only_config(config):
        assert_manual_only_source_firewall(config)
    return config, config_hash, selected


def atomic_write_bytes(path: str | Path, value: bytes) -> Path:
    """Write bytes beside the destination and publish with one replace."""

    selected = Path(path)
    selected.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{selected.name}.", suffix=".tmp", dir=selected.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, selected)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return selected


def atomic_write_json(path: str | Path, value: Any) -> Path:
    """Write strict, sorted, human-readable JSON atomically."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    return atomic_write_bytes(path, (payload + "\n").encode("utf-8"))


def package_versions(names: Iterable[str]) -> dict[str, str | None]:
    """Return installed versions without importing large analysis libraries."""

    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def platform_manifest() -> dict[str, Any]:
    """Return stable environment provenance for one run."""

    return {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "operating_system": platform.platform(),
        "machine": platform.machine(),
        "packages": package_versions(
            (
                "numpy",
                "opencv-python",
                "pandas",
                "scipy",
                "scikit-learn",
                "xgboost",
                "matplotlib",
                "pyarrow",
                "jsonschema",
                "onnx",
                "skl2onnx",
            )
        ),
    }


@dataclass(slots=True)
class RunLogger:
    """Incremental JSONL logger finalized atomically with its run directory."""

    path: Path
    command: str
    run_id: str
    _handle: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")

    def emit(self, level: str, event: str, **fields: Any) -> None:
        record = {
            "timestamp": utc_now_iso(),
            "command": self.command,
            "run_id": self.run_id,
            "level": level,
            "event": event,
            **fields,
        }
        self._handle.write(
            json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True)
            + "\n"
        )
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def make_run_id(command: str, identity: Mapping[str, Any]) -> str:
    """Create an idempotent run ID from all controlling inputs."""

    token = canonical_json_hash({"command": command, **identity})[:16]
    return f"{command}-{token}"


def create_temporary_run_directory(parent: Path, run_id: str) -> Path:
    """Create a private run directory inside a validated project output root."""

    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".tmp", dir=parent))


def publish_run_directory(temporary: Path, final: Path) -> tuple[Path, bool]:
    """Atomically publish a completed run, or reuse an identical prior run."""

    if final.exists():
        existing = final / "run_manifest.json"
        proposed = temporary / "run_manifest.json"
        if not existing.exists() or not proposed.exists():
            raise ContractError(
                f"run destination already exists without comparable manifest: {final}"
            )
        if canonical_json_hash(load_json_object(existing)) != canonical_json_hash(
            load_json_object(proposed)
        ):
            raise ContractError(
                f"run destination collision for {final}; inputs or outputs differ"
            )
        shutil.rmtree(temporary)
        return final, True
    os.replace(temporary, final)
    return final, False


def output_file_hashes(root: Path, *, exclude: Iterable[str] = ()) -> dict[str, str]:
    """Hash every regular output file below a run directory."""

    excluded = set(exclude)
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in excluded
    }


def elapsed_seconds(started: float) -> float:
    """Return monotonic elapsed seconds rounded for manifests."""

    return round(time.perf_counter() - started, 6)


__all__ = [
    "PROJECT_ROOT",
    "RunLogger",
    "atomic_write_bytes",
    "atomic_write_json",
    "assert_manual_only_artifact_lineage",
    "assert_manual_only_source_firewall",
    "canonical_json_bytes",
    "canonical_json_hash",
    "code_hash",
    "create_temporary_run_directory",
    "elapsed_seconds",
    "load_study_config",
    "make_run_id",
    "is_manual_only_config",
    "output_file_hashes",
    "platform_manifest",
    "publish_run_directory",
    "resolve_project_path",
    "sha256_bytes",
    "sha256_file",
    "utc_now_iso",
]
