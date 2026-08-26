"""Run a small non-publication ML check on an exported session dataset."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPOSITORY_ROOT / "06_outputs" / "02_datasets" / "session_features.csv"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "06_outputs" / "03_training_smoke" / "report.json"
FEATURE_COLUMNS = (
    "request_count",
    "manifest_request_count",
    "segment_bytes_total",
    "avg_segment_interval_sec",
    "consecutive_same_interval_count",
    "segment_count",
    "status_4xx_count",
    "rendition_switch_count",
    "segment_index_gap_count",
    "token_session_count",
    "token_unique_ips",
    "token_unique_devices",
    "token_concurrent_consumers_max",
)
FORBIDDEN_FIELDS = {
    "scenario_id",
    "attack_family",
    "run_id",
    "logical_client_id",
    "physical_host_id",
    "network_profile_id",
    "cdn_token_id",
    "viewing_session_id",
    "client_ip",
    "content_id",
    "label_binary",
}


class SmokeTrainingError(RuntimeError):
    """Raised when the smoke training contract is not satisfied."""


def load_dataset(path: Path) -> tuple[list[list[float]], list[int], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SmokeTrainingError("dataset is empty")
    missing = [feature for feature in FEATURE_COLUMNS if feature not in rows[0]]
    if missing:
        raise SmokeTrainingError(f"dataset is missing feature columns: {missing}")
    leaked = sorted(set(FEATURE_COLUMNS).intersection(FORBIDDEN_FIELDS))
    if leaked:
        raise SmokeTrainingError(f"feature allowlist contains forbidden fields: {leaked}")
    features: list[list[float]] = []
    labels: list[int] = []
    for index, row in enumerate(rows):
        try:
            features.append([float(row[feature]) for feature in FEATURE_COLUMNS])
            labels.append(int(row["label_binary"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise SmokeTrainingError(f"invalid numeric value in dataset row {index + 2}: {exc}") from exc
    return features, labels, rows


def metrics(y_true: Any, probabilities: Any) -> dict[str, float]:
    from sklearn.metrics import (
        average_precision_score,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    predictions = (probabilities >= 0.5).astype(int)
    return {
        "pr_auc": round(float(average_precision_score(y_true, probabilities)), 6),
        "roc_auc": round(float(roc_auc_score(y_true, probabilities)), 6),
        "f1": round(float(f1_score(y_true, predictions, zero_division=0)), 6),
        "precision": round(float(precision_score(y_true, predictions, zero_division=0)), 6),
        "recall": round(float(recall_score(y_true, predictions, zero_division=0)), 6),
    }


def run_training(features: list[list[float]], labels: list[int], seed: int) -> dict[str, Any]:
    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import StratifiedKFold, cross_val_predict
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise SmokeTrainingError(
            "scikit-learn is not installed; run: "
            "python -m pip install -r 03_experiments/04_data_tools/requirements-training-smoke.txt"
        ) from exc

    class_counts = Counter(labels)
    if set(class_counts) != {0, 1}:
        raise SmokeTrainingError(f"both normal and attack classes are required: {dict(class_counts)}")
    minimum_class = min(class_counts.values())
    if minimum_class < 2:
        raise SmokeTrainingError(
            f"at least two samples per class are required for smoke cross-validation: {dict(class_counts)}"
        )
    split_count = min(3, minimum_class)
    splitter = StratifiedKFold(n_splits=split_count, shuffle=True, random_state=seed)
    x = np.asarray(features, dtype=float)
    y = np.asarray(labels, dtype=int)
    models = {
        "logistic_regression": Pipeline(
            [
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=2000, class_weight="balanced", random_state=seed)),
            ]
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=200,
            max_depth=6,
            min_samples_leaf=1,
            class_weight="balanced",
            random_state=seed,
            n_jobs=-1,
        ),
    }
    reports: dict[str, Any] = {}
    for name, model in models.items():
        probabilities = cross_val_predict(
            model,
            x,
            y,
            cv=splitter,
            method="predict_proba",
            n_jobs=1,
        )[:, 1]
        reports[name] = metrics(y, probabilities)
    return {
        "split": "stratified_cross_validation",
        "folds": split_count,
        "class_counts": {str(key): value for key, value in sorted(class_counts.items())},
        "models": reports,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=20260826)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        features, labels, rows = load_dataset(args.dataset.resolve())
        training = run_training(features, labels, args.seed)
    except (SmokeTrainingError, OSError) as exc:
        print(f"training smoke failed: {exc}", file=sys.stderr)
        return 1
    report = {
        "evaluation_scope": "pipeline_smoke_only_not_a_research_result",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_path": str(args.dataset.resolve()),
        "row_count": len(rows),
        "feature_columns": list(FEATURE_COLUMNS),
        "seed": args.seed,
        **training,
        "limitations": [
            "This smoke run only verifies export, leakage guard, fitting, and metric calculation.",
            "It does not replace account/device/host/content/time holdout evaluation.",
            "No journal claim may use these smoke metrics.",
        ],
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**report, "output_path": str(output)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
