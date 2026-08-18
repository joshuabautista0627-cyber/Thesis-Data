"""Build the v4 hybrid bundle from the selected v2 and v3 components.

The component models are re-verified through the strict bundle loader before
composition.  Their archived grouped metrics and the new candidate-search
reports are copied into one disclosed experimental artifact.  This builder
does not reinterpret the recovery evidence as physical validation.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np

from core.experimental_live_sensor import load_experimental_bundle
from scripts.build_experimental_manual_recovery import (
    _canonical_hash,
    _publish,
    _write_json,
)
from scripts.live_sensor_common import PROJECT_ROOT, sha256_file


SCHEMA_VERSION = "3.0.0"
SPEC_PATH = PROJECT_ROOT / "config" / "manual_only_experimental_hybrid_v4.json"
V2_BUNDLE = PROJECT_ROOT / "models" / "live_sensor_experimental_manual_recovery_v2"
V3_BUNDLE = PROJECT_ROOT / "models" / "live_sensor_experimental_event_signal_v3"
FORCE_BENCHMARK = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "hybrid_candidate_benchmark.json"
)
LOCALIZATION_BENCHMARK = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "event_localization_candidate_benchmark.json"
)
RUN_ROOT = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "experimental_hybrid"
)
DEFAULT_BUNDLE = PROJECT_ROOT / "models" / "live_sensor_experimental_hybrid_v4"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _assert_shared_preprocessing(v2: Mapping[str, Any], v3: Mapping[str, Any]) -> None:
    for key in ("signed_center", "signed_scale", "active_center", "active_scale"):
        left = np.asarray(v2.get(key), dtype=float)
        right = np.asarray(v3.get(key), dtype=float)
        if left.shape != (9,) or right.shape != (9,) or not np.allclose(
            left, right, rtol=0.0, atol=1e-12
        ):
            raise ValueError(f"v2/v3 preprocessing differs for {key}")
    if v2.get("feature_order") != v3.get("feature_order"):
        raise ValueError("v2/v3 feature order differs")
    if v2.get("temporal_filter") != v3.get("temporal_filter"):
        raise ValueError("v2/v3 temporal filter differs")
    for key in (
        "fallback_threshold",
        "unloaded_warmup_quantile",
        "minimum_warmup_frames",
        "acquire_frames",
        "clear_frames",
    ):
        if v2["contact"].get(key) != v3["contact"].get(key):
            raise ValueError(f"v2/v3 contact setting differs: {key}")


def _write_bundle(
    directory: Path,
    spec: Mapping[str, Any],
    source: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    v2_preprocessing = _read(V2_BUNDLE / "preprocessing.json")
    v3_preprocessing = _read(V3_BUNDLE / "preprocessing.json")
    _assert_shared_preprocessing(v2_preprocessing, v3_preprocessing)
    preprocessing = dict(v3_preprocessing)
    preprocessing["schema_version"] = SCHEMA_VERSION

    force_model = _read(V2_BUNDLE / "force_model.json")
    force_model["schema_version"] = SCHEMA_VERSION
    force_model["selection"] = (
        "v2 monotonic isotonic retained: the expanded grouped search did not "
        "establish a material or stable improvement"
    )
    event_model = _read(V3_BUNDLE / "event_model.json")
    event_model["schema_version"] = SCHEMA_VERSION
    event_model["force_output"] = "approximate_isotonic_frame_and_event_peak"
    event_model["force_peak_rule"] = (
        "maximum in-support isotonic estimate across included raw-contact frames"
    )
    localization_model = _read(V3_BUNDLE / "localization_model.json")
    localization_model["schema_version"] = SCHEMA_VERSION
    localization_model["selection"] = (
        "retained after grouped comparison against logistic regression, "
        "shrinkage LDA, and Extra Trees"
    )

    aggregate = metrics["aggregate"]
    release = {
        "schema_version": SCHEMA_VERSION,
        "status": "experimental",
        "model_eligible_under_authoritative_plan": False,
        "validated_claim_allowed": False,
        "posthoc_outer_fold_reuse": True,
        "physical_validation_complete": False,
        "usability_validation_complete": False,
        "force_output_available": True,
        "force_resolution_established": False,
        "numerical_recovery_checks": {
            "max_no_contact_frame_fpr_at_most_0_05": aggregate[
                "max_no_contact_frame_fpr"
            ]
            <= 0.05,
            "min_contact_recall_at_least_0_90": aggregate["min_contact_recall"]
            >= 0.90,
            "event_localization_macro_f1_at_least_0_891": aggregate[
                "event_localization_macro_f1"
            ]
            >= 0.891,
            "force_mae_at_most_0_75N": aggregate[
                "mean_conditional_force_mae_N"
            ]
            <= 0.75,
            "force_resolution_established": False,
        },
        "blocking_reasons": [
            "The six existing outer TEST folds were reused after inspection; no untouched confirmation data remain.",
            "The force estimator failed the combined improvement/rank/response-resolution gate.",
            "The authoritative all-ROI common safe-range procedure remains failed.",
            "Known-force physical, latency/soak, and representative-user validation were not performed.",
        ],
    }

    _write_json(directory / "preprocessing.json", preprocessing)
    _write_json(directory / "force_model.json", force_model)
    _write_json(directory / "event_model.json", event_model)
    _write_json(directory / "localization_model.json", localization_model)
    _write_json(directory / "metrics.json", metrics)
    _write_json(directory / "release_decision.json", release)
    (directory / "replay_demo.jsonl").write_bytes(
        (V3_BUNDLE / "replay_demo.jsonl").read_bytes()
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": spec["bundle_id"],
        "sensor_mode": "hybrid_force_event",
        "status": "experimental",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": source,
        "spec_hash": _canonical_hash(spec),
        "runtime": "reviewed_numpy_json",
        "manual_only": True,
        "operating_range_N": spec["operating_range_N"],
        "inference_inputs": {
            "camera_roi_features": [
                "signed_delta_v_sum",
                "active_fraction",
            ],
            "load_cell": "forbidden",
            "printer": "forbidden",
        },
        "files": {},
    }
    for path in sorted(directory.iterdir()):
        if path.name in {"manifest.json", "SHA256SUMS"}:
            continue
        manifest["files"][path.name] = {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    _write_json(directory / "manifest.json", manifest)
    (directory / "SHA256SUMS").write_text(
        "\n".join(
            f"{sha256_file(path)}  {path.name}"
            for path in sorted(directory.iterdir())
            if path.name != "SHA256SUMS"
        )
        + "\n",
        encoding="utf-8",
    )


def build(
    bundle_path: Path = DEFAULT_BUNDLE, spec_path: Path = SPEC_PATH
) -> tuple[Path, Path]:
    v2 = load_experimental_bundle(V2_BUNDLE)
    v3 = load_experimental_bundle(V3_BUNDLE)
    if v2.sensor_mode != "frame_force" or v3.sensor_mode != "event_signal":
        raise ValueError("unexpected source bundle modes")
    if v2.release_decision.get("validated_claim_allowed") is not False:
        raise ValueError("v2 disclosure contract changed")
    if v3.release_decision.get("validated_claim_allowed") is not False:
        raise ValueError("v3 disclosure contract changed")
    spec = _read(spec_path)
    force_benchmark = _read(FORCE_BENCHMARK)
    localization_benchmark = _read(LOCALIZATION_BENCHMARK)
    if localization_benchmark.get("selected_family") != "argmax":
        raise ValueError("the reviewed localization winner changed")
    force_aggregate = dict(v2.metrics["aggregate"])
    event_aggregate = dict(v3.metrics["aggregate"])
    aggregate = {
        **event_aggregate,
        "force_output_available": True,
        "force_output_status": "approximate_unvalidated",
        "operating_range_N": force_aggregate["operating_range_N"],
        "mean_conditional_force_mae_N": force_aggregate[
            "mean_conditional_force_mae_N"
        ],
        "cross_validated_p95_absolute_error_N": force_aggregate[
            "cross_validated_p95_absolute_error_N"
        ],
        "mean_force_spearman_rho": force_aggregate["mean_force_spearman_rho"],
        "force_resolution_established": False,
        "expanded_force_search_mae_N": force_benchmark["aggregate"][
            "mean_conditional_force_mae_N"
        ],
        "expanded_force_search_relative_gain_over_constant": force_benchmark[
            "aggregate"
        ]["relative_mae_gain_over_constant"],
        "localization_selected_family": "accumulated_argmax",
    }
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "status": "posthoc_experimental",
        "disclosure": spec["disclosure"],
        "aggregate": aggregate,
        "event_source_metrics": v3.metrics,
        "force_source_metrics": v2.metrics,
        "force_candidate_benchmark": force_benchmark,
        "localization_candidate_benchmark": localization_benchmark,
    }
    source = {
        "dataset_policy": "manual_only",
        "v2_bundle_id": v2.bundle_id,
        "v2_manifest_sha256": sha256_file(V2_BUNDLE / "manifest.json"),
        "v3_bundle_id": v3.bundle_id,
        "v3_manifest_sha256": sha256_file(V3_BUNDLE / "manifest.json"),
        "force_benchmark_sha256": sha256_file(FORCE_BENCHMARK),
        "localization_benchmark_sha256": sha256_file(LOCALIZATION_BENCHMARK),
        "source_manifest_hash": force_benchmark["data_quality"][
            "source_manifest_hash"
        ],
        "split_hash": force_benchmark["data_quality"]["split_hash"],
    }
    run_hash = hashlib.sha256(
        json.dumps(
            {"spec_hash": _canonical_hash(spec), **source}, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()[:16]
    run_id = f"experimental-hybrid-{run_hash}"
    run_directory = RUN_ROOT / run_id
    if not run_directory.exists():
        RUN_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=run_id + "-", dir=RUN_ROOT) as temporary:
            temp_run = Path(temporary)
            _write_json(temp_run / "metrics.json", metrics)
            _write_json(
                temp_run / "run_manifest.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "status": "posthoc_experimental",
                    "source": source,
                    "spec_hash": _canonical_hash(spec),
                },
            )
            os.replace(temp_run, run_directory)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=bundle_path.name + "-", dir=bundle_path.parent
    ) as temporary:
        temp_bundle = Path(temporary) / "bundle"
        _write_bundle(temp_bundle, spec, source, metrics)
        _publish(temp_bundle, bundle_path)
    return run_directory, bundle_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run, bundle = build(args.bundle.resolve(), args.spec.resolve())
    print(json.dumps({"run": str(run), "bundle": str(bundle)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
