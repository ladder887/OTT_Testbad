"""Audit collection-reserved dataset splits for provenance leakage."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPOSITORY_ROOT / "06_outputs" / "02_datasets" / "session_features.csv"
SPLIT_HOSTS = {
    "train": {f"pi{index:02d}" for index in range(1, 7)},
    "validation": {"pi07", "pi08"},
    "test": {"pi09", "pi10"},
}
VOD_CONTENTS = {
    "train": {f"video_{index:02d}" for index in range(1, 10)},
    "validation": {f"video_{index:02d}" for index in range(10, 13)},
    "test": {f"video_{index:02d}" for index in range(13, 16)},
}
LIVE_CONTENTS = {
    "train": {"live_01"},
    "validation": {"live_02"},
    "test": {"live_03"},
}
PROVENANCE_GROUP_FIELDS = (
    "run_id",
    "matrix_run_key",
    "cdn_token_id",
    "logical_client_id",
    "account_id",
    "device_id",
    "client_ip",
    "physical_host_id",
    "content_id",
)
REQUIRED_SCENARIOS = {f"N{index}" for index in range(1, 8)} | {"A1", "A2", "A3", "A6", "A7"}
MAIN_ATTACK_VARIANTS = {
    "train": {
        "A1": "high_fanout",
        "A2": "fast",
        "A3": "high_parallel",
        "A6": "low_rate",
        "A7": "high_fanout",
    },
    "validation": {
        "A1": "high_fanout",
        "A2": "fast",
        "A3": "high_parallel",
        "A6": "low_rate",
        "A7": "high_fanout",
    },
    "test": {
        "A1": "low_fanout",
        "A2": "stealth",
        "A3": "low_parallel",
        "A6": "low_rate",
        "A7": "low_fanout",
    },
}


class SplitAuditError(RuntimeError):
    """Raised when a dataset cannot be audited."""


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def audit_rows(
    rows: list[dict[str, Any]],
    *,
    required_splits: set[str],
    enforce_main_contract: bool,
    require_scenario_coverage: bool,
) -> dict[str, Any]:
    if not rows:
        raise SplitAuditError("dataset is empty")
    required_columns = {
        "data_split",
        "run_id",
        "matrix_run_key",
        "scenario_id",
        "scenario_variant",
        "label_binary",
        "content_id",
        "physical_host_id",
    }
    missing = sorted(required_columns - set(rows[0]))
    if missing:
        raise SplitAuditError(f"dataset is missing split-provenance columns: {missing}")

    errors: list[str] = []
    warnings: list[str] = []
    observed_splits = {str(row.get("data_split") or "") for row in rows}
    if "" in observed_splits:
        errors.append("one or more rows have no data_split")
        observed_splits.discard("")
    missing_splits = sorted(required_splits - observed_splits)
    unexpected_splits = sorted(observed_splits - required_splits)
    if missing_splits:
        errors.append(f"required splits are missing: {missing_splits}")
    if unexpected_splits:
        errors.append(f"unexpected splits are present: {unexpected_splits}")

    crossings: dict[str, dict[str, list[str]]] = {}
    for field in PROVENANCE_GROUP_FIELDS:
        values: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            value = str(row.get(field) or "")
            split = str(row.get("data_split") or "")
            if value and split:
                values[value].add(split)
        leaked = {value: sorted(splits) for value, splits in values.items() if len(splits) > 1}
        if leaked:
            crossings[field] = leaked
            examples = list(leaked.items())[:3]
            errors.append(f"{field} crosses split boundaries: {examples}")

    split_reports: dict[str, Any] = {}
    for split in sorted(observed_splits):
        split_rows = [row for row in rows if row.get("data_split") == split]
        scenarios = Counter(str(row.get("scenario_id") or "") for row in split_rows)
        labels = Counter(str(row.get("label_binary") or "") for row in split_rows)
        hosts = {str(row.get("physical_host_id") or "") for row in split_rows if row.get("physical_host_id")}
        contents = {str(row.get("content_id") or "") for row in split_rows if row.get("content_id")}
        if enforce_main_contract:
            invalid_hosts = sorted(hosts - SPLIT_HOSTS[split])
            invalid_contents = sorted(
                content
                for content in contents
                if content not in (LIVE_CONTENTS[split] if content.startswith("live_") else VOD_CONTENTS[split])
            )
            if invalid_hosts:
                errors.append(f"{split}: hosts violate reserved split: {invalid_hosts}")
            if invalid_contents:
                errors.append(f"{split}: contents violate reserved split: {invalid_contents}")
            for row in split_rows:
                scenario_id = str(row.get("scenario_id") or "")
                if scenario_id in MAIN_ATTACK_VARIANTS[split]:
                    expected = MAIN_ATTACK_VARIANTS[split][scenario_id]
                    actual = str(row.get("scenario_variant") or "")
                    if actual != expected:
                        errors.append(
                            f"{split}: {scenario_id} uses {actual or '<missing>'}; expected held-out policy {expected}"
                        )
                        break
        if require_scenario_coverage:
            missing_scenarios = sorted(REQUIRED_SCENARIOS - set(scenarios))
            if missing_scenarios:
                errors.append(f"{split}: scenarios missing from dataset: {missing_scenarios}")
        if not {"0", "1"}.issubset(labels):
            errors.append(f"{split}: both normal and attack samples are required")
        split_reports[split] = {
            "row_count": len(split_rows),
            "class_counts": dict(sorted(labels.items())),
            "scenario_counts": dict(sorted(scenarios.items())),
            "physical_host_ids": sorted(hosts),
            "content_ids": sorted(contents),
            "run_count": len({str(row.get("run_id") or "") for row in split_rows}),
        }

    scaled_rows = sum(parse_bool(row.get("timing_scaled")) for row in rows)
    if enforce_main_contract and scaled_rows:
        errors.append(f"main dataset contains {scaled_rows} timing-scaled smoke rows")

    train_times = [
        parsed
        for row in rows
        if row.get("data_split") in {"train", "validation"}
        and (parsed := parse_datetime(row.get("start_time"))) is not None
    ]
    test_times = [
        parsed
        for row in rows
        if row.get("data_split") == "test"
        and (parsed := parse_datetime(row.get("start_time"))) is not None
    ]
    temporal_test_is_future = bool(train_times and test_times and min(test_times) > max(train_times))
    if enforce_main_contract and test_times and train_times and not temporal_test_is_future:
        warnings.append("test rows are not strictly later than every train/validation row; future-time is not established")

    prefixes = sorted({str(row.get("dataset_prefix") or "") for row in rows if row.get("dataset_prefix")})
    if len(prefixes) != 1:
        errors.append(f"expected one dataset_prefix, found {prefixes}")
    matrix_ids = sorted(
        {str(row.get("collection_matrix_id") or "") for row in rows if row.get("collection_matrix_id")}
    )
    if not matrix_ids:
        errors.append("collection_matrix_id is missing from every row")

    return {
        "passed": not errors,
        "row_count": len(rows),
        "required_splits": sorted(required_splits),
        "dataset_prefixes": prefixes,
        "collection_matrix_ids": matrix_ids,
        "cross_split_groups": crossings,
        "timing_scaled_row_count": scaled_rows,
        "temporal_test_is_strictly_future": temporal_test_is_future,
        "splits": split_reports,
        "errors": errors,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--mode", choices=("batch", "main"), default="main")
    parser.add_argument("--required-splits", default="train,validation,test")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset = args.dataset.resolve()
    required_splits = {item.strip() for item in args.required_splits.split(",") if item.strip()}
    try:
        with dataset.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        report = audit_rows(
            rows,
            required_splits=required_splits,
            enforce_main_contract=args.mode == "main",
            require_scenario_coverage=args.mode == "main",
        )
    except (OSError, SplitAuditError, ValueError) as exc:
        print(f"dataset split audit failed: {exc}", file=sys.stderr)
        return 2
    output = args.output.resolve() if args.output else dataset.with_suffix(".split-audit.json")
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**report, "output_path": str(output)}, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
