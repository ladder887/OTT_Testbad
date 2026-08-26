"""Generate a leakage-aware, mixed normal/attack collection matrix."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = Path(__file__).with_name("run_scenario.py")
DEFAULT_INVENTORY = REPOSITORY_ROOT / "03_experiments" / "07_generated" / "logical_clients.json"
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "06_outputs" / "00_collection_plans"

CLIENT_SPLIT_HOSTS = {
    "train": tuple(f"pi{index:02d}" for index in range(1, 7)),
    "validation": ("pi07", "pi08"),
    "test": ("pi09", "pi10"),
}
VOD_CONTENT_SPLITS = {
    "train": tuple(f"video_{index:02d}" for index in range(1, 10)),
    "validation": tuple(f"video_{index:02d}" for index in range(10, 13)),
    "test": tuple(f"video_{index:02d}" for index in range(13, 16)),
}
LIVE_CONTENT_SPLITS = {
    "train": ("live_01",),
    "validation": ("live_02",),
    "test": ("live_03",),
}
NORMAL_VARIANTS = (
    ("N1", "preview"),
    ("N1", "standard"),
    ("N1", "long"),
    ("N1", "catalog_preview"),
    ("N2", "default"),
    ("N3", "default"),
    ("N4", "default"),
    ("N5", "default"),
    ("N6", "household"),
    ("N6", "flash_crowd"),
    ("N7", "single"),
    ("N7", "popular_channel"),
)
CALIBRATION_ATTACK_VARIANTS = (
    ("A1", "low_fanout"),
    ("A1", "high_fanout"),
    ("A2", "fast"),
    ("A2", "stealth"),
    ("A3", "low_parallel"),
    ("A3", "high_parallel"),
    ("A6", "low_rate"),
    ("A7", "low_fanout"),
    ("A7", "high_fanout"),
)
MAIN_ATTACK_VARIANTS = {
    "train": (
        ("A1", "high_fanout"),
        ("A2", "fast"),
        ("A3", "high_parallel"),
        ("A6", "low_rate"),
        ("A7", "high_fanout"),
    ),
    "validation": (
        ("A1", "high_fanout"),
        ("A2", "fast"),
        ("A3", "high_parallel"),
        ("A6", "low_rate"),
        ("A7", "high_fanout"),
    ),
    "test": (
        ("A1", "low_fanout"),
        ("A2", "stealth"),
        ("A3", "low_parallel"),
        ("A6", "low_rate"),
        ("A7", "low_fanout"),
    ),
}


class MatrixError(RuntimeError):
    """Raised when a collection matrix violates the experiment contract."""


def load_runner_module():
    spec = importlib.util.spec_from_file_location("collection_matrix_run_scenario", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise MatrixError(f"cannot load scenario runner: {RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner_module()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_seed(base_seed: int, *parts: str) -> int:
    payload = ":".join((str(base_seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big") & 0x7FFFFFFF


def client_pool_for_split(clients: list[Any], split: str) -> list[Any]:
    hosts = set(CLIENT_SPLIT_HOSTS[split])
    pool = [client for client in clients if client.physical_host_id in hosts]
    expected = 60 if split == "train" else 20
    if len(pool) != expected:
        raise MatrixError(f"{split} client pool has {len(pool)} clients; expected {expected}")
    return pool


def content_pool_for_run(split: str, scenario_id: str) -> tuple[str, ...]:
    return LIVE_CONTENT_SPLITS[split] if scenario_id in {"N7", "A7"} else VOD_CONTENT_SPLITS[split]


def attack_variants(phase: str, split: str) -> tuple[tuple[str, str], ...]:
    return CALIBRATION_ATTACK_VARIANTS if phase == "calibration" else MAIN_ATTACK_VARIANTS[split]


def build_templates(
    phase: str,
    split: str,
    repetitions: int,
    base_seed: int,
    smoke: bool,
) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    variants = tuple(NORMAL_VARIANTS) + tuple(attack_variants(phase, split))
    for repetition in range(1, repetitions + 1):
        for scenario_id, variant in variants:
            seed = stable_seed(base_seed, phase, split, scenario_id, variant, str(repetition))
            contents = content_pool_for_run(split, scenario_id)
            resolved_variant = RUNNER.resolve_scenario_variant(scenario_id, variant, seed)
            client_count = RUNNER.scenario_client_count(
                scenario_id,
                resolved_variant,
                seed,
                smoke,
                content_pool_size=len(contents) if scenario_id == "N6" else None,
            )
            templates.append(
                {
                    "scenario_id": scenario_id,
                    "scenario_variant": resolved_variant,
                    "class": "normal" if scenario_id.startswith("N") else "attack",
                    "attack_family": RUNNER.ATTACK_FAMILY.get(scenario_id),
                    "seed": seed,
                    "repetition": repetition,
                    "required_client_count": client_count,
                    "allowed_content_ids": list(contents),
                }
            )
    return templates


def interleave_templates(templates: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by_class = {
        class_name: [item for item in templates if item["class"] == class_name]
        for class_name in ("normal", "attack")
    }
    for values in by_class.values():
        rng.shuffle(values)
    queues = {key: deque(values) for key, values in by_class.items()}
    ordered: list[dict[str, Any]] = []
    turn = "normal"
    while queues["normal"] or queues["attack"]:
        if queues[turn]:
            ordered.append(queues[turn].popleft())
        turn = "attack" if turn == "normal" else "normal"
        if not queues[turn] and queues["normal" if turn == "attack" else "attack"]:
            turn = "normal" if turn == "attack" else "attack"
    return ordered


def pack_batches(templates: list[dict[str, Any]], target_clients: int) -> list[list[dict[str, Any]]]:
    remaining = list(templates)
    batches: list[list[dict[str, Any]]] = []
    while remaining:
        batch: list[dict[str, Any]] = []
        used = 0
        for required_class in ("normal", "attack"):
            index = next(
                (
                    position
                    for position, item in enumerate(remaining)
                    if item["class"] == required_class and used + item["required_client_count"] <= target_clients
                ),
                None,
            )
            if index is not None:
                item = remaining.pop(index)
                batch.append(item)
                used += item["required_client_count"]
        position = 0
        while position < len(remaining):
            item = remaining[position]
            if used + item["required_client_count"] <= target_clients:
                batch.append(remaining.pop(position))
                used += item["required_client_count"]
            else:
                position += 1
        if not batch:
            largest = max(item["required_client_count"] for item in remaining)
            raise MatrixError(f"target_clients={target_clients} is smaller than a required run size {largest}")
        batches.append(batch)
    for target in batches:
        target_classes = {item["class"] for item in target}
        for missing_class in {"normal", "attack"} - target_classes:
            target_size = sum(item["required_client_count"] for item in target)
            donors = [
                donor
                for donor in batches
                if donor is not target and sum(item["class"] == missing_class for item in donor) > 1
            ]
            movable = [
                (item["required_client_count"], donor, item)
                for donor in donors
                for item in donor
                if item["class"] == missing_class
                and target_size + item["required_client_count"] <= target_clients
            ]
            if movable:
                _, donor, item = min(movable, key=lambda value: value[0])
                donor.remove(item)
                target.append(item)
    return batches


def allocate_clients(
    pool: list[Any],
    count: int,
    unavailable: set[str],
    usage: Counter[str],
    seed: int,
) -> list[Any]:
    candidates = [client for client in pool if client.logical_client_id not in unavailable]
    if len(candidates) < count:
        raise MatrixError(f"only {len(candidates)} unreserved clients remain; {count} required")
    tie = random.Random(seed)
    tie_values = {client.logical_client_id: tie.random() for client in candidates}
    by_host: dict[str, list[Any]] = defaultdict(list)
    for client in candidates:
        by_host[client.physical_host_id].append(client)
    for values in by_host.values():
        values.sort(key=lambda item: (usage[item.logical_client_id], tie_values[item.logical_client_id]))
    selected: list[Any] = []
    while len(selected) < count:
        host_ids = sorted(
            (host_id for host_id, values in by_host.items() if values),
            key=lambda host_id: (
                min(usage[item.logical_client_id] for item in by_host[host_id]),
                sum(1 for item in selected if item.physical_host_id == host_id),
                host_id,
            ),
        )
        if not host_ids:
            break
        for host_id in host_ids:
            selected.append(by_host[host_id].pop(0))
            if len(selected) == count:
                break
    for client in selected:
        usage[client.logical_client_id] += 1
    return selected


def build_matrix(
    *,
    inventory: Path,
    dataset_prefix: str,
    phase: str,
    splits: tuple[str, ...],
    repetitions: int,
    target_clients: int,
    base_seed: int,
    smoke: bool,
    cache_state: str,
    stagger_min_sec: float,
    stagger_max_sec: float,
) -> dict[str, Any]:
    if repetitions < 1:
        raise MatrixError("repetitions must be at least 1")
    if target_clients < 2:
        raise MatrixError("target_clients must be at least 2 for a mixed batch")
    if stagger_min_sec < 0 or stagger_max_sec < stagger_min_sec:
        raise MatrixError("invalid stagger range")
    clients = RUNNER.load_inventory(inventory)
    matrix_id = f"{dataset_prefix}_{phase}_{base_seed}"
    usage: Counter[str] = Counter()
    batches: list[dict[str, Any]] = []
    sequence = 0
    for split in splits:
        pool = client_pool_for_split(clients, split)
        effective_target = min(target_clients, len(pool))
        templates = interleave_templates(
            build_templates(phase, split, repetitions, base_seed, smoke),
            stable_seed(base_seed, split, "order"),
        )
        packed = pack_batches(templates, effective_target)
        for batch_index, batch_templates in enumerate(packed, start=1):
            batch_id = f"{split}_b{batch_index:03d}"
            unavailable: set[str] = set()
            offset = 0.0
            rng = random.Random(stable_seed(base_seed, split, batch_id, "stagger"))
            runs: list[dict[str, Any]] = []
            for template in batch_templates:
                sequence += 1
                assigned = allocate_clients(
                    pool,
                    template["required_client_count"],
                    unavailable,
                    usage,
                    stable_seed(template["seed"], batch_id, "clients"),
                )
                client_ids = [client.logical_client_id for client in assigned]
                unavailable.update(client_ids)
                run_key = (
                    f"{split}_{template['scenario_id'].lower()}_"
                    f"{template['scenario_variant']}_r{template['repetition']:03d}_{sequence:04d}"
                )
                runs.append(
                    {
                        **template,
                        "run_key": run_key,
                        "data_split": split,
                        "batch_id": batch_id,
                        "start_offset_sec": round(offset, 3),
                        "reserved_client_ids": client_ids,
                    }
                )
                offset += rng.uniform(stagger_min_sec, stagger_max_sec)
            batches.append(
                {
                    "batch_id": batch_id,
                    "data_split": split,
                    "target_client_count": effective_target,
                    "planned_client_count": sum(item["required_client_count"] for item in runs),
                    "class_run_counts": dict(Counter(item["class"] for item in runs)),
                    "runs": runs,
                }
            )

    matrix = {
        "schema_version": 1,
        "matrix_id": matrix_id,
        "generated_at": utc_now(),
        "dataset_prefix": dataset_prefix,
        "phase": phase,
        "smoke": smoke,
        "base_seed": base_seed,
        "cache_state": cache_state,
        "target_client_count": target_clients,
        "stagger_range_sec": [stagger_min_sec, stagger_max_sec],
        "inventory_path": str(inventory),
        "manifest_output_dir": f"06_outputs/01_run_manifests/{dataset_prefix}",
        "split_contract": {
            split: {
                "physical_host_ids": list(CLIENT_SPLIT_HOSTS[split]),
                "vod_content_ids": list(VOD_CONTENT_SPLITS[split]),
                "live_content_ids": list(LIVE_CONTENT_SPLITS[split]),
            }
            for split in splits
        },
        "limitations": [
            "Three LIVE channels permit only a 1/1/1 content split; LIVE content-generalization evidence is limited.",
            "Planned client count is a reservation count; achieved simultaneous activity must be measured separately.",
        ],
        "batches": batches,
    }
    validate_matrix(matrix, clients)
    return matrix


def validate_matrix(matrix: dict[str, Any], clients: list[Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    by_id = {client.logical_client_id: client for client in clients}
    seen_run_keys: set[str] = set()
    seen_seeds: set[int] = set()
    scenario_counts: Counter[str] = Counter()
    for batch in matrix.get("batches", []):
        split = str(batch.get("data_split") or "")
        reserved_in_batch: set[str] = set()
        classes: set[str] = set()
        planned = 0
        for run in batch.get("runs", []):
            run_key = str(run.get("run_key") or "")
            seed = int(run.get("seed") or -1)
            scenario_id = str(run.get("scenario_id") or "")
            scenario_counts[scenario_id] += 1
            classes.add(str(run.get("class") or ""))
            if not run_key or run_key in seen_run_keys:
                errors.append(f"duplicate or missing run_key: {run_key or '<missing>'}")
            seen_run_keys.add(run_key)
            if seed in seen_seeds:
                errors.append(f"duplicate run seed: {seed}")
            seen_seeds.add(seed)
            ids = [str(item) for item in run.get("reserved_client_ids", [])]
            required = int(run.get("required_client_count") or 0)
            planned += required
            if len(ids) != required or len(ids) != len(set(ids)):
                errors.append(f"{run_key}: invalid logical-client reservation")
            overlap = sorted(set(ids).intersection(reserved_in_batch))
            if overlap:
                errors.append(f"{batch.get('batch_id')}: overlapping reservations {overlap}")
            reserved_in_batch.update(ids)
            allowed_hosts = set(CLIENT_SPLIT_HOSTS.get(split, ()))
            actual_hosts = {by_id[item].physical_host_id for item in ids if item in by_id}
            if len(actual_hosts) == 0 or not actual_hosts.issubset(allowed_hosts):
                errors.append(f"{run_key}: client reservation violates {split} host split")
            allowed_contents = set(content_pool_for_run(split, scenario_id)) if split in CLIENT_SPLIT_HOSTS else set()
            if set(run.get("allowed_content_ids", [])) != allowed_contents:
                errors.append(f"{run_key}: content pool violates {split} split")
        if planned != int(batch.get("planned_client_count") or -1):
            errors.append(f"{batch.get('batch_id')}: planned client count mismatch")
        if len(classes) < 2:
            warnings.append(f"{batch.get('batch_id')}: batch is not mixed normal/attack")
    missing_scenarios = sorted(RUNNER.SUPPORTED_SCENARIOS - set(scenario_counts))
    if missing_scenarios:
        errors.append(f"matrix is missing scenarios: {missing_scenarios}")
    if matrix.get("phase") == "main":
        for batch in matrix.get("batches", []):
            split = batch.get("data_split")
            for run in batch.get("runs", []):
                if run.get("scenario_id") in {"A1", "A2", "A3", "A7"}:
                    expected = {item for item in MAIN_ATTACK_VARIANTS[split] if item[0] == run["scenario_id"]}
                    if (run["scenario_id"], run["scenario_variant"]) not in expected:
                        errors.append(f"{run.get('run_key')}: attack variant holdout policy violation")
    if errors:
        raise MatrixError("; ".join(errors))
    return {
        "passed": True,
        "batch_count": len(matrix.get("batches", [])),
        "run_count": sum(len(batch.get("runs", [])) for batch in matrix.get("batches", [])),
        "scenario_run_counts": dict(sorted(scenario_counts.items())),
        "warnings": warnings,
    }


def parse_splits(value: str) -> tuple[str, ...]:
    splits = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    unknown = sorted(set(splits) - set(CLIENT_SPLIT_HOSTS))
    if not splits or unknown or len(splits) != len(set(splits)):
        raise MatrixError(f"invalid split list: {value}")
    return splits


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dataset-prefix", default="")
    parser.add_argument("--phase", choices=("calibration", "main"), default="calibration")
    parser.add_argument("--splits", default="")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--target-clients", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--cache-state",
        choices=("unspecified", "cold", "warmup", "warm", "mixed"),
        default="warm",
    )
    parser.add_argument("--stagger-min-sec", type=float)
    parser.add_argument("--stagger-max-sec", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        splits = parse_splits(args.splits or ("train" if args.phase == "calibration" else "train,validation,test"))
        dataset_prefix = args.dataset_prefix.strip() or (
            f"tnsm_100lc_{datetime.now(timezone.utc):%Y%m%d}_{args.phase}"
        )
        stagger_min = args.stagger_min_sec if args.stagger_min_sec is not None else (0.0 if args.smoke else 30.0)
        stagger_max = args.stagger_max_sec if args.stagger_max_sec is not None else (0.5 if args.smoke else 120.0)
        matrix = build_matrix(
            inventory=args.inventory.resolve(),
            dataset_prefix=dataset_prefix,
            phase=args.phase,
            splits=splits,
            repetitions=args.repetitions,
            target_clients=args.target_clients,
            base_seed=args.seed,
            smoke=args.smoke,
            cache_state=args.cache_state,
            stagger_min_sec=stagger_min,
            stagger_max_sec=stagger_max,
        )
        clients = RUNNER.load_inventory(args.inventory.resolve())
        report = validate_matrix(matrix, clients)
    except (MatrixError, OSError, ValueError) as exc:
        print(f"collection matrix generation failed: {exc}", file=sys.stderr)
        return 1
    output = args.output.resolve() if args.output else DEFAULT_OUTPUT_DIR / f"{matrix['matrix_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**report, "matrix_id": matrix["matrix_id"], "output_path": str(output)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
