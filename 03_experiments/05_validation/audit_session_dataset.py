"""Audit an exported session dataset for leakage and generator label proxies."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPOSITORY_ROOT / "06_outputs" / "02_datasets" / "session_features.csv"
FORBIDDEN_FEATURES = {
    "dataset_prefix",
    "data_split",
    "collection_matrix_id",
    "matrix_run_key",
    "scenario_id",
    "scenario_variant",
    "attack_family",
    "label_binary",
    "run_id",
    "logical_client_id",
    "physical_host_id",
    "account_id",
    "device_id",
    "network_profile_id",
    "cdn_token_id",
    "viewing_session_id",
    "client_ip",
    "edge_id",
    "content_id",
}
AUDITED_METADATA = (
    "scenario_id",
    "physical_host_id",
    "content_id",
    "content_type",
    "edge_id",
    "network_profile_id",
)


class DatasetAuditError(RuntimeError):
    """Raised when an exported dataset violates its storage contract."""


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def directionless_auc(labels: list[int], values: list[float]) -> float:
    positives = [value for value, label in zip(values, labels) if label == 1]
    negatives = [value for value, label in zip(values, labels) if label == 0]
    if not positives or not negatives or len(set(values)) < 2:
        return 0.5
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    auc = wins / (len(positives) * len(negatives))
    return max(auc, 1.0 - auc)


def summarize_values(labels: list[int], values: list[float]) -> dict[str, Any]:
    by_class = {
        label: [value for value, row_label in zip(values, labels) if row_label == label]
        for label in (0, 1)
    }
    return {
        "unique_value_count": len(set(values)),
        "directionless_roc_auc": round(directionless_auc(labels, values), 6),
        "normal": {
            "min": min(by_class[0]) if by_class[0] else None,
            "max": max(by_class[0]) if by_class[0] else None,
            "mean": round(sum(by_class[0]) / len(by_class[0]), 6) if by_class[0] else None,
        },
        "attack": {
            "min": min(by_class[1]) if by_class[1] else None,
            "max": max(by_class[1]) if by_class[1] else None,
            "mean": round(sum(by_class[1]) / len(by_class[1]), 6) if by_class[1] else None,
        },
    }


def load_contract(dataset: Path) -> tuple[list[str], dict[str, Any]]:
    metadata_path = dataset.with_suffix(".metadata.json")
    if not metadata_path.exists():
        raise DatasetAuditError(f"dataset metadata is missing: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    feature_columns = [str(item) for item in metadata.get("feature_columns", [])]
    if not feature_columns:
        raise DatasetAuditError("metadata contains no feature_columns")
    leaked = sorted(set(feature_columns).intersection(FORBIDDEN_FEATURES))
    if leaked:
        raise DatasetAuditError(f"forbidden fields are declared as model features: {leaked}")
    return feature_columns, metadata


def audit(dataset: Path, high_auc_threshold: float) -> dict[str, Any]:
    feature_columns, metadata = load_contract(dataset)
    with dataset.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise DatasetAuditError("dataset is empty")
    missing_columns = [column for column in feature_columns if column not in rows[0]]
    if missing_columns:
        raise DatasetAuditError(f"dataset is missing feature columns: {missing_columns}")

    try:
        labels = [int(row["label_binary"]) for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        raise DatasetAuditError(f"invalid label_binary: {exc}") from exc
    if set(labels) != {0, 1}:
        raise DatasetAuditError(f"both classes are required, found {sorted(set(labels))}")

    errors: list[str] = []
    warnings: list[str] = []
    feature_reports: dict[str, dict[str, Any]] = {}
    high_auc_features: list[str] = []
    constant_features: list[str] = []
    for feature in feature_columns:
        parsed = [finite_float(row.get(feature)) for row in rows]
        missing_count = sum(value is None for value in parsed)
        values = [value if value is not None else 0.0 for value in parsed]
        report = summarize_values(labels, values)
        report["missing_count"] = missing_count
        feature_reports[feature] = report
        if missing_count:
            warnings.append(f"{feature}: {missing_count} missing/non-finite values were treated as zero")
        if report["unique_value_count"] <= 1:
            constant_features.append(feature)
        if report["directionless_roc_auc"] >= high_auc_threshold:
            high_auc_features.append(feature)

    if high_auc_features:
        warnings.append(
            f"single-feature AUC >= {high_auc_threshold:.2f}: {sorted(high_auc_features)}"
        )
    if constant_features:
        warnings.append(f"constant features in this collection: {sorted(constant_features)}")

    metadata_distribution: dict[str, Any] = {}
    for field in AUDITED_METADATA:
        if field not in rows[0]:
            warnings.append(f"metadata field is unavailable for proxy audit: {field}")
            continue
        per_class = {
            "normal": Counter(row.get(field, "") for row, label in zip(rows, labels) if label == 0),
            "attack": Counter(row.get(field, "") for row, label in zip(rows, labels) if label == 1),
        }
        normal_values = {item for item in per_class["normal"] if item}
        attack_values = {item for item in per_class["attack"] if item}
        only_normal = sorted(normal_values - attack_values)
        only_attack = sorted(attack_values - normal_values)
        metadata_distribution[field] = {
            "normal": dict(sorted(per_class["normal"].items())),
            "attack": dict(sorted(per_class["attack"].items())),
            "only_normal": only_normal,
            "only_attack": only_attack,
        }
        if field in {"edge_id", "network_profile_id", "content_type"} and (only_normal or only_attack):
            warnings.append(
                f"{field} has class-exclusive values; normal_only={only_normal}, attack_only={only_attack}"
            )

    run_labels: dict[str, set[int]] = defaultdict(set)
    token_runs: dict[str, set[str]] = defaultdict(set)
    sample_ids: set[str] = set()
    for row, label in zip(rows, labels):
        sample_id = str(row.get("sample_id") or "")
        if not sample_id or sample_id in sample_ids:
            errors.append(f"duplicate or missing sample_id: {sample_id or '<missing>'}")
        sample_ids.add(sample_id)
        run_id = str(row.get("run_id") or "")
        token_id = str(row.get("cdn_token_id") or "")
        run_labels[run_id].add(label)
        token_runs[token_id].add(run_id)
    mixed_runs = sorted(run_id for run_id, values in run_labels.items() if len(values) > 1)
    duplicate_tokens = sorted(token_id for token_id, values in token_runs.items() if len(values) > 1)
    if mixed_runs:
        errors.append(f"runs contain mixed labels: {mixed_runs}")
    if duplicate_tokens:
        errors.append(f"tokens span multiple runs: {duplicate_tokens}")

    metadata_row_count = int(metadata.get("row_count") or 0)
    if metadata_row_count != len(rows):
        errors.append(
            f"metadata row_count={metadata_row_count} does not match dataset rows={len(rows)}"
        )

    return {
        "passed": not errors,
        "dataset_path": str(dataset),
        "row_count": len(rows),
        "class_counts": dict(sorted(Counter(labels).items())),
        "run_count": len(run_labels),
        "token_count": len(token_runs),
        "feature_count": len(feature_columns),
        "high_auc_threshold": high_auc_threshold,
        "high_auc_features": sorted(high_auc_features),
        "constant_features": sorted(constant_features),
        "feature_reports": feature_reports,
        "metadata_distribution": metadata_distribution,
        "errors": errors,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--high-auc-threshold", type=float, default=0.95)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = audit(args.dataset.resolve(), args.high_auc_threshold)
    except (DatasetAuditError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"dataset audit failed: {exc}", file=sys.stderr)
        return 2
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["output_path"] = str(output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
