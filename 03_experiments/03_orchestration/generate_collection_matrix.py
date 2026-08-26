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
MAX_NORMAL_ATTACK_TOTAL_VARIATION = 0.15

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


def eligible_content_pool(
    contents: tuple[str, ...],
    scenario_id: str,
    variant: str,
) -> tuple[str, ...]:
    if scenario_id == "N1" and variant == "long":
        eligible = tuple(item for item in contents if item in RUNNER.LONG_VOD_CONTENT_IDS)
    elif scenario_id == "N1" and variant == "standard":
        eligible = tuple(item for item in contents if item in RUNNER.STANDARD_VOD_CONTENT_IDS)
    else:
        eligible = contents
    if not eligible:
        raise MatrixError(f"{scenario_id}/{variant} has no compatible content in {list(contents)}")
    return eligible


def planned_run_shape(
    scenario_id: str,
    variant: str,
    seed: int,
    smoke: bool,
    required_client_count: int,
    content_pool_size: int,
) -> tuple[int, int, list[dict[str, int]]]:
    rng = random.Random(seed)
    if scenario_id in {"A2"} or (scenario_id == "N1" and variant == "catalog_preview"):
        distinct_content_count = min(2 if smoke else rng.randint(2, 5), content_pool_size)
        session_count = distinct_content_count
    elif scenario_id == "N6" and variant == "household":
        distinct_content_count = required_client_count
        session_count = required_client_count
    elif scenario_id in {"A1", "A7"}:
        distinct_content_count = 1
        session_count = required_client_count - 1
    elif scenario_id == "N5":
        distinct_content_count = 1
        session_count = 2
    elif scenario_id in {"A6", "N6"} or (
        scenario_id == "N7" and variant == "popular_channel"
    ):
        distinct_content_count = 1
        session_count = required_client_count
    else:
        distinct_content_count = 1
        session_count = 1

    if scenario_id in {"A1", "A7"}:
        contributions = [
            {
                "network_profile_sessions": 0,
                "physical_host_sessions": 0,
                "edge_sessions": session_count,
            }
        ] + [
            {
                "network_profile_sessions": 1,
                "physical_host_sessions": 1,
                "edge_sessions": 0,
            }
            for _ in range(session_count)
        ]
    elif required_client_count == 1:
        contributions = [
            {
                "network_profile_sessions": session_count,
                "physical_host_sessions": session_count,
                "edge_sessions": session_count,
            }
        ]
    else:
        contributions = [
            {
                "network_profile_sessions": 1,
                "physical_host_sessions": 1,
                "edge_sessions": 1,
            }
            for _ in range(required_client_count)
        ]

    if len(contributions) != required_client_count:
        raise MatrixError(f"{scenario_id}/{variant} produced an invalid client contribution plan")
    return session_count, distinct_content_count, contributions


def plan_contents(
    allowed_contents: tuple[str, ...],
    eligible_contents: tuple[str, ...],
    distinct_content_count: int,
    session_count: int,
    usage: Counter[str],
    seed: int,
) -> tuple[list[str], list[str], dict[str, int]]:
    if distinct_content_count > len(eligible_contents):
        raise MatrixError(
            f"cannot plan {distinct_content_count} distinct contents from {len(eligible_contents)} candidates"
        )
    if distinct_content_count > 1 and session_count != distinct_content_count:
        raise MatrixError("multi-content runs must contribute one session per planned content")

    rng = random.Random(seed)
    tie = {content_id: rng.random() for content_id in allowed_contents}
    temporary = Counter(usage)
    planned: list[str] = []
    weights = [session_count] if distinct_content_count == 1 else [1] * distinct_content_count
    for weight in weights:
        candidates = [item for item in eligible_contents if item not in planned]
        selected = min(candidates, key=lambda item: (temporary[item], tie[item], item))
        planned.append(selected)
        temporary[selected] += weight
        usage[selected] += weight

    remaining = [item for item in allowed_contents if item not in planned]
    remaining.sort(key=lambda item: (temporary[item], tie[item], item))
    preferred = planned + remaining
    planned_counts = {
        content_id: (session_count if distinct_content_count == 1 else 1)
        for content_id in planned
    }
    return preferred, planned, planned_counts


def build_templates(
    phase: str,
    split: str,
    repetitions: int,
    base_seed: int,
    smoke: bool,
) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    content_usage = {"normal": Counter(), "attack": Counter()}
    variants = tuple(NORMAL_VARIANTS) + tuple(attack_variants(phase, split))
    for repetition in range(1, repetitions + 1):
        for scenario_id, variant in variants:
            seed = stable_seed(base_seed, phase, split, scenario_id, variant, str(repetition))
            contents = content_pool_for_run(split, scenario_id)
            resolved_variant = RUNNER.resolve_scenario_variant(scenario_id, variant, seed)
            class_name = "normal" if scenario_id.startswith("N") else "attack"
            client_count = RUNNER.scenario_client_count(
                scenario_id,
                resolved_variant,
                seed,
                smoke,
                content_pool_size=len(contents) if scenario_id == "N6" else None,
            )
            session_count, distinct_content_count, client_contributions = planned_run_shape(
                scenario_id,
                resolved_variant,
                seed,
                smoke,
                client_count,
                len(contents),
            )
            eligible_contents = eligible_content_pool(contents, scenario_id, resolved_variant)
            preferred_contents, planned_contents, planned_content_counts = plan_contents(
                contents,
                eligible_contents,
                distinct_content_count,
                session_count,
                content_usage[class_name],
                stable_seed(seed, "contents"),
            )
            templates.append(
                {
                    "scenario_id": scenario_id,
                    "scenario_variant": resolved_variant,
                    "class": class_name,
                    "attack_family": RUNNER.ATTACK_FAMILY.get(scenario_id),
                    "seed": seed,
                    "repetition": repetition,
                    "required_client_count": client_count,
                    "allowed_content_ids": list(contents),
                    "preferred_content_ids": preferred_contents,
                    "planned_content_ids": planned_contents,
                    "planned_content_session_counts": planned_content_counts,
                    "planned_session_count": session_count,
                    "client_session_contributions": client_contributions,
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
                continue

            swaps: list[tuple[int, list[dict[str, Any]], dict[str, Any], dict[str, Any]]] = []
            opposite_class = "attack" if missing_class == "normal" else "normal"
            for donor in donors:
                donor_size = sum(item["required_client_count"] for item in donor)
                for incoming in donor:
                    if incoming["class"] != missing_class:
                        continue
                    for outgoing in target:
                        if outgoing["class"] != opposite_class:
                            continue
                        target_after = (
                            target_size
                            - outgoing["required_client_count"]
                            + incoming["required_client_count"]
                        )
                        donor_after = (
                            donor_size
                            - incoming["required_client_count"]
                            + outgoing["required_client_count"]
                        )
                        if target_after <= target_clients and donor_after <= target_clients:
                            swaps.append(
                                (
                                    abs(target_after - donor_after),
                                    donor,
                                    incoming,
                                    outgoing,
                                )
                            )
            if swaps:
                _, donor, incoming, outgoing = min(
                    swaps,
                    key=lambda value: (
                        value[0],
                        value[2]["required_client_count"],
                        value[2]["seed"],
                    ),
                )
                donor.remove(incoming)
                target.remove(outgoing)
                donor.append(outgoing)
                target.append(incoming)
    return batches


def projected_balance_cost(
    counter: Counter[str],
    categories: tuple[str, ...],
    value: str,
    increment: int,
) -> float:
    if increment <= 0:
        return 0.0
    total = sum(counter[item] for item in categories) + increment
    target_ratio = 1.0 / len(categories)
    return sum(
        (
            (counter[item] + (increment if item == value else 0)) / total
            - target_ratio
        )
        ** 2
        for item in categories
    )


def allocate_clients(
    pool: list[Any],
    contributions: list[dict[str, int]],
    unavailable: set[str],
    usage: Counter[str],
    class_usage: dict[str, Counter[str]],
    class_client_usage: Counter[str],
    seed: int,
) -> list[Any]:
    count = len(contributions)
    candidates = [client for client in pool if client.logical_client_id not in unavailable]
    if len(candidates) < count:
        raise MatrixError(f"only {len(candidates)} unreserved clients remain; {count} required")

    category_values = {
        "network_profile_sessions": tuple(sorted({item.network_profile_id for item in pool})),
        "physical_host_sessions": tuple(sorted({item.physical_host_id for item in pool})),
        "edge_sessions": tuple(sorted({item.edge_id for item in pool})),
    }
    attribute_names = {
        "network_profile_sessions": "network_profile_id",
        "physical_host_sessions": "physical_host_id",
        "edge_sessions": "edge_id",
    }
    rng = random.Random(seed)
    tie_values = {client.logical_client_id: rng.random() for client in candidates}
    selected: list[Any] = []
    selected_ids: set[str] = set()
    selected_hosts: Counter[str] = Counter()

    for contribution in contributions:
        available = [item for item in candidates if item.logical_client_id not in selected_ids]

        def score(client: Any) -> tuple[Any, ...]:
            balance_cost = 0.0
            direct_sample_weight = 0
            for metric, attribute in attribute_names.items():
                increment = int(contribution.get(metric, 0))
                direct_sample_weight = max(direct_sample_weight, increment)
                balance_cost += projected_balance_cost(
                    class_usage[metric],
                    category_values[metric],
                    str(getattr(client, attribute)),
                    increment,
                )
            return (
                selected_hosts[client.physical_host_id],
                round(balance_cost, 12),
                class_client_usage[client.logical_client_id] + direct_sample_weight,
                usage[client.logical_client_id],
                tie_values[client.logical_client_id],
                client.logical_client_id,
            )

        chosen = min(available, key=score)
        selected.append(chosen)
        selected_ids.add(chosen.logical_client_id)
        selected_hosts[chosen.physical_host_id] += 1
        direct_sample_weight = 0
        for metric, attribute in attribute_names.items():
            increment = int(contribution.get(metric, 0))
            class_usage[metric][str(getattr(chosen, attribute))] += increment
            direct_sample_weight = max(direct_sample_weight, increment)
        class_client_usage[chosen.logical_client_id] += direct_sample_weight
        usage[chosen.logical_client_id] += 1
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
        balance_usage = {
            class_name: {
                "network_profile_sessions": Counter(),
                "physical_host_sessions": Counter(),
                "edge_sessions": Counter(),
            }
            for class_name in ("normal", "attack")
        }
        class_client_usage = {class_name: Counter() for class_name in ("normal", "attack")}
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
                    template["client_session_contributions"],
                    unavailable,
                    usage,
                    balance_usage[template["class"]],
                    class_client_usage[template["class"]],
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
        "schema_version": 2,
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
            "cache_state records a prepared condition; the matrix runner does not warm or flush VOD Edge caches.",
        ],
        "batches": batches,
    }
    matrix["planned_balance"] = summarize_matrix_balance(matrix, clients)
    validate_matrix(matrix, clients)
    return matrix


def total_variation(left: Counter[str], right: Counter[str], categories: set[str]) -> float:
    left_total = sum(left[item] for item in categories)
    right_total = sum(right[item] for item in categories)
    if left_total <= 0 or right_total <= 0:
        return 1.0
    return 0.5 * sum(
        abs((left[item] / left_total) - (right[item] / right_total))
        for item in categories
    )


def summarize_matrix_balance(matrix: dict[str, Any], clients: list[Any]) -> dict[str, Any]:
    by_id = {client.logical_client_id: client for client in clients}
    counters: dict[str, dict[str, dict[str, Counter[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(Counter))
    )
    sessions: dict[str, Counter[str]] = defaultdict(Counter)
    for batch in matrix.get("batches", []):
        split = str(batch.get("data_split") or "")
        for run in batch.get("runs", []):
            class_name = str(run.get("class") or "")
            sessions[split][class_name] += int(run.get("planned_session_count") or 0)
            ids = [str(item) for item in run.get("reserved_client_ids", [])]
            contributions = list(run.get("client_session_contributions", []))
            for client_id, contribution in zip(ids, contributions):
                client = by_id.get(client_id)
                if client is None:
                    continue
                counters[split][class_name]["network_profile_session_counts"][
                    client.network_profile_id
                ] += int(contribution.get("network_profile_sessions", 0))
                counters[split][class_name]["physical_host_session_counts"][
                    client.physical_host_id
                ] += int(contribution.get("physical_host_sessions", 0))
                counters[split][class_name]["edge_session_counts"][client.edge_id] += int(
                    contribution.get("edge_sessions", 0)
                )
            counters[split][class_name]["content_session_counts"].update(
                {
                    str(content_id): int(count)
                    for content_id, count in run.get("planned_content_session_counts", {}).items()
                }
            )

    summary: dict[str, Any] = {}
    for split, contract in matrix.get("split_contract", {}).items():
        pool = [client for client in clients if client.physical_host_id in set(contract["physical_host_ids"])]
        categories = {
            "network_profile_session_counts": sorted({item.network_profile_id for item in pool}),
            "physical_host_session_counts": sorted({item.physical_host_id for item in pool}),
            "edge_session_counts": sorted({item.edge_id for item in pool}),
            "content_session_counts": sorted(
                set(contract.get("vod_content_ids", [])) | set(contract.get("live_content_ids", []))
            ),
        }
        class_rows: dict[str, Any] = {}
        for class_name in ("normal", "attack"):
            class_rows[class_name] = {
                "planned_session_count": sessions[split][class_name],
                **{
                    metric: {
                        category: counters[split][class_name][metric][category]
                        for category in values
                    }
                    for metric, values in categories.items()
                },
            }
        class_rows["normal_attack_total_variation"] = {
            metric: round(
                total_variation(
                    counters[split]["normal"][metric],
                    counters[split]["attack"][metric],
                    set(values),
                ),
                6,
            )
            for metric, values in categories.items()
        }
        summary[split] = class_rows
    return summary


def validate_matrix(matrix: dict[str, Any], clients: list[Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    schema_version = int(matrix.get("schema_version") or 0)
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
            unknown_ids = sorted(set(ids) - set(by_id))
            if unknown_ids:
                errors.append(f"{run_key}: unknown logical clients {unknown_ids}")
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
            if schema_version >= 2:
                preferred = [str(item) for item in run.get("preferred_content_ids", [])]
                planned_contents = [str(item) for item in run.get("planned_content_ids", [])]
                planned_counts = {
                    str(content_id): int(count)
                    for content_id, count in run.get("planned_content_session_counts", {}).items()
                }
                planned_sessions = int(run.get("planned_session_count") or 0)
                contributions = list(run.get("client_session_contributions", []))
                if len(preferred) != len(allowed_contents) or set(preferred) != allowed_contents:
                    errors.append(f"{run_key}: preferred content order is not a permutation of the split pool")
                if not planned_contents or len(planned_contents) != len(set(planned_contents)):
                    errors.append(f"{run_key}: planned contents are empty or duplicated")
                if not set(planned_contents).issubset(allowed_contents):
                    errors.append(f"{run_key}: planned contents violate the split pool")
                if set(planned_counts) != set(planned_contents) or any(
                    count <= 0 for count in planned_counts.values()
                ):
                    errors.append(f"{run_key}: invalid planned content session counts")
                if sum(planned_counts.values()) != planned_sessions or planned_sessions <= 0:
                    errors.append(f"{run_key}: planned content counts do not match planned sessions")
                eligible = set(
                    eligible_content_pool(
                        tuple(run.get("allowed_content_ids", [])),
                        scenario_id,
                        str(run.get("scenario_variant") or ""),
                    )
                )
                if not set(planned_contents).issubset(eligible):
                    errors.append(f"{run_key}: planned contents do not support the scenario variant")
                if len(contributions) != required:
                    errors.append(f"{run_key}: client contribution count does not match reservation")
                else:
                    metrics = (
                        "network_profile_sessions",
                        "physical_host_sessions",
                        "edge_sessions",
                    )
                    for contribution in contributions:
                        if any(
                            not isinstance(contribution.get(metric), int)
                            or int(contribution.get(metric, -1)) < 0
                            for metric in metrics
                        ):
                            errors.append(f"{run_key}: invalid client session contribution")
                            break
                    for metric in metrics:
                        if sum(int(item.get(metric, 0)) for item in contributions) != planned_sessions:
                            errors.append(f"{run_key}: {metric} does not match planned sessions")
        if planned != int(batch.get("planned_client_count") or -1):
            errors.append(f"{batch.get('batch_id')}: planned client count mismatch")
        if len(classes) < 2:
            message = f"{batch.get('batch_id')}: batch is not mixed normal/attack"
            if schema_version >= 2:
                errors.append(message)
            else:
                warnings.append(message)
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
    balance = summarize_matrix_balance(matrix, clients) if schema_version >= 2 else {}
    if schema_version >= 2:
        if matrix.get("planned_balance") != balance:
            errors.append("stored planned_balance does not match matrix reservations")
        for split, contract in matrix.get("split_contract", {}).items():
            split_balance = balance.get(split, {})
            expected_profiles = {
                client.network_profile_id
                for client in clients
                if client.physical_host_id in set(contract.get("physical_host_ids", []))
            }
            expected_edges = {
                client.edge_id
                for client in clients
                if client.physical_host_id in set(contract.get("physical_host_ids", []))
            }
            expected_hosts = set(contract.get("physical_host_ids", []))
            expected_contents = set(contract.get("vod_content_ids", [])) | set(
                contract.get("live_content_ids", [])
            )
            coverage_contract = {
                "network_profile_session_counts": expected_profiles,
                "edge_session_counts": expected_edges,
                "physical_host_session_counts": expected_hosts,
                "content_session_counts": expected_contents,
            }
            for class_name in ("normal", "attack"):
                class_balance = split_balance.get(class_name, {})
                for metric, expected_values in coverage_contract.items():
                    missing = sorted(
                        value
                        for value in expected_values
                        if int(class_balance.get(metric, {}).get(value, 0)) <= 0
                    )
                    if missing:
                        errors.append(f"{split}/{class_name}: {metric} misses {missing}")
            variation = split_balance.get("normal_attack_total_variation", {})
            for metric in (
                "network_profile_session_counts",
                "edge_session_counts",
                "physical_host_session_counts",
                "content_session_counts",
            ):
                value = float(variation.get(metric, 1.0))
                if value > MAX_NORMAL_ATTACK_TOTAL_VARIATION:
                    errors.append(
                        f"{split}: normal/attack {metric} total variation {value:.3f} exceeds "
                        f"{MAX_NORMAL_ATTACK_TOTAL_VARIATION:.3f}"
                    )
    if errors:
        raise MatrixError("; ".join(errors))
    return {
        "passed": True,
        "batch_count": len(matrix.get("batches", [])),
        "run_count": sum(len(batch.get("runs", [])) for batch in matrix.get("batches", [])),
        "scenario_run_counts": dict(sorted(scenario_counts.items())),
        "planned_balance": balance,
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
        default="unspecified",
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
    output = (
        args.output.resolve()
        if args.output
        else DEFAULT_OUTPUT_DIR / f"{matrix['matrix_id']}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {**report, "matrix_id": matrix["matrix_id"], "output_path": str(output)},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
