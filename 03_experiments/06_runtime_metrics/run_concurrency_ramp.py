"""Run balanced 20/40/60/80/100-client VOD load stages with runtime sampling."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATION_DIR = REPOSITORY_ROOT / "03_experiments" / "03_orchestration"
RUNTIME_DIR = Path(__file__).resolve().parent
for module_dir in (ORCHESTRATION_DIR, RUNTIME_DIR):
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

import run_scenario as scenario  # noqa: E402
from run_collection_matrix import (  # noqa: E402
    ClientReservation,
    DEFAULT_LOCK_DIR,
    configure_live_channels,
)
from runtime_sampler import RuntimeSampleError, RuntimeSampler, parse_datetime, utc_now  # noqa: E402


DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "06_outputs" / "03_runtime_metrics"
GATE_SCRIPT = RUNTIME_DIR / "check_collection_gate.py"


class RampError(RuntimeError):
    """Raised when a ramp stage cannot produce defensible measurements."""


def parse_levels(value: str) -> tuple[int, ...]:
    try:
        levels = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise RampError("ramp levels must be comma-separated integers") from exc
    if not levels or any(level < 1 or level > 100 for level in levels):
        raise RampError("ramp levels must be between 1 and 100")
    if tuple(sorted(set(levels))) != levels:
        raise RampError("ramp levels must be unique and increasing")
    return levels


def select_balanced_clients(
    clients: list[scenario.LogicalClient],
    target: int,
) -> list[scenario.LogicalClient]:
    """Balance every prefix across hosts, network profiles, and Edges."""
    if target > len(clients):
        raise RampError(f"requested {target} clients from an inventory of {len(clients)}")
    by_host: dict[str, list[scenario.LogicalClient]] = defaultdict(list)
    for client in clients:
        by_host[client.physical_host_id].append(client)
    host_ids = sorted(by_host)
    for values in by_host.values():
        values.sort(key=lambda item: item.logical_client_id)
    selected: list[scenario.LogicalClient] = []
    selected_ids: set[str] = set()
    profile_counts: Counter[str] = Counter()
    edge_counts: Counter[str] = Counter()
    while len(selected) < target:
        progressed = False
        for host_id in host_ids:
            candidates = [item for item in by_host[host_id] if item.logical_client_id not in selected_ids]
            if not candidates:
                continue
            candidate = min(
                candidates,
                key=lambda item: (
                    profile_counts[item.network_profile_id],
                    edge_counts[item.edge_id],
                    item.logical_client_id,
                ),
            )
            selected.append(candidate)
            selected_ids.add(candidate.logical_client_id)
            profile_counts[candidate.network_profile_id] += 1
            edge_counts[candidate.edge_id] += 1
            progressed = True
            if len(selected) == target:
                break
        if not progressed:
            break
    if len(selected) != target:
        raise RampError(f"balanced selection stopped at {len(selected)} of {target} clients")
    return selected


def build_assignments(
    clients: list[scenario.LogicalClient],
    *,
    seed: int,
    segments_min: int,
    segments_max: int,
    delay_min_sec: float,
    delay_max_sec: float,
) -> tuple[list[scenario.Assignment], list[dict[str, Any]]]:
    assignments: list[scenario.Assignment] = []
    plans: list[dict[str, Any]] = []
    contents = tuple(scenario.STANDARD_VOD_CONTENT_IDS)
    for index, client in enumerate(clients):
        client_seed = seed + index * 1009 + int(client.logical_client_id[2:])
        rng = random.Random(client_seed)
        content_id = contents[rng.randrange(len(contents))]
        segment_count = rng.randint(segments_min, segments_max)
        camouflage = scenario.choose_camouflage(rng, content_id, attack=False)
        phase = scenario.vod_phase(
            segment_count,
            delay=(delay_min_sec, delay_max_sec),
            initial_buffer_count=3,
        )
        spec = {
            **scenario.common_spec(client_seed, camouflage, "vod"),
            "content_id": content_id,
            "browse": camouflage["browse"],
            "phases": [phase],
        }
        assignments.append(scenario.Assignment(client=client, spec=spec, role="runtime_ramp_viewer"))
        plans.append(
            {
                "logical_client_id": client.logical_client_id,
                "physical_host_id": client.physical_host_id,
                "source_ip": client.source_ip,
                "edge_id": client.edge_id,
                "network_profile_id": client.network_profile_id,
                "content_id": content_id,
                "segment_count": segment_count,
                "delay_min_sec": delay_min_sec,
                "delay_max_sec": delay_max_sec,
                "browse": camouflage["browse"],
                "ua_mode": camouflage["ua_mode"],
                "referrer_mode": camouflage["referrer_mode"],
                "seed": client_seed,
            }
        )
    return assignments, plans


def summarize_remote_result(
    assignment: scenario.Assignment,
    result: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "logical_client_id": assignment.client.logical_client_id,
        "physical_host_id": assignment.client.physical_host_id,
        "source_ip": assignment.client.source_ip,
        "edge_id": assignment.client.edge_id,
        "network_profile_id": assignment.client.network_profile_id,
        "ok": result is not None and not error and bool(result.get("ok")),
        "error": error,
    }
    if result is None:
        return summary
    bindings = [
        dict(item.get("token_binding") or {})
        for item in result.get("playbacks", [])
        if item.get("token_binding")
    ]
    summary.update(
        {
            "started_epoch": result.get("started_epoch"),
            "ended_epoch": result.get("ended_epoch"),
            "http_request_count": int(result.get("http_request_count") or 0),
            "http_retry_count": int(result.get("http_retry_count") or 0),
            "http_failure_count": int(result.get("http_failure_count") or 0),
            "request_elapsed_ms_mean": result.get("request_elapsed_ms_mean"),
            "traffic": result.get("traffic"),
            "token_bindings": bindings,
            "network_impairment": result.get("network_impairment"),
        }
    )
    return summary


def distribution(values: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(item[key]) for item in values).items()))


def sample_time(value: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise RampError(f"invalid runtime sample timestamp: {value}")
    return parsed


def stage_summary(
    *,
    target: int,
    samples: list[dict[str, Any]],
    results: list[dict[str, Any]],
    token_coverage: dict[str, Any],
    lag_summary: dict[str, Any],
    recovery_completed: bool,
    recovery_sec: float | None,
    minimum_concurrency_ratio: float,
    minimum_sustain_sec: float,
    maximum_cursor_lag_sec: float,
) -> dict[str, Any]:
    workload_samples = [item for item in samples if item.get("phase") == "workload"]
    threshold = max(1, math.ceil(target * minimum_concurrency_ratio))
    peak_active = max((int(item.get("achieved_active_clients") or 0) for item in workload_samples), default=0)
    sustained_sec = round(
        sum(
            float(item.get("interval_sec") or 0.0)
            for item in workload_samples
            if int(item.get("achieved_active_clients") or 0) >= threshold
        ),
        3,
    )
    measured_samples = [item for item in samples if item.get("phase") != "baseline"]
    edge_documents = sum(
        int(item.get("elasticsearch", {}).get("edge_document_count") or 0)
        for item in measured_samples
    )
    status_4xx = sum(
        int(item.get("elasticsearch", {}).get("status_4xx_count") or 0)
        for item in measured_samples
    )
    http_failures = sum(int(item.get("http_failure_count") or 0) for item in results)
    http_retries = sum(int(item.get("http_retry_count") or 0) for item in results)
    sample_errors = sorted({error for item in samples for error in item.get("errors", [])})
    cursor_lags = [
        float(item["graph_cursor_lag_sec"])
        for item in samples
        if item.get("graph_cursor_lag_sec") is not None
    ]
    max_node_values: dict[str, dict[str, float]] = {}
    for sample in samples:
        for node in sample.get("nodes", []):
            if node.get("error"):
                continue
            node_values = max_node_values.setdefault(node["name"], {})
            for field in (
                "cpu_used_percent",
                "memory_used_percent",
                "load_1m",
                "network_rx_mbps",
                "network_tx_mbps",
                "disk_read_mb_per_sec",
                "disk_write_mb_per_sec",
            ):
                if node.get(field) is not None:
                    node_values[field] = round(
                        max(float(node_values.get(field, 0.0)), float(node[field])),
                        4,
                    )
    errors: list[str] = []
    success_count = sum(1 for item in results if item.get("ok"))
    if success_count != target:
        errors.append(f"{success_count} of {target} client workloads completed successfully")
    if peak_active < threshold:
        errors.append(f"peak active clients {peak_active} is below required {threshold}")
    if sustained_sec < minimum_sustain_sec:
        errors.append(
            f"active clients stayed at or above {threshold} for {sustained_sec:.1f}s; "
            f"required {minimum_sustain_sec:.1f}s"
        )
    if edge_documents <= target:
        errors.append(f"only {edge_documents} Edge documents were measured for {target} clients")
    if edge_documents and status_4xx / edge_documents > 0.01:
        errors.append(f"Edge 4xx ratio {status_4xx / edge_documents:.4f} exceeds 0.01")
    if http_failures:
        errors.append(f"clients reported {http_failures} HTTP failures")
    if lag_summary.get("event_query_truncated"):
        errors.append("Elasticsearch runtime query was truncated")
    if sample_errors:
        errors.extend(sample_errors)
    if not recovery_completed:
        errors.append("Graph Pipeline did not recover before the stage timeout")
    expected_tokens = int(token_coverage.get("expected_token_count") or 0)
    segment_tokens = int(token_coverage.get("tokens_with_segments") or 0)
    if expected_tokens != target or segment_tokens != expected_tokens:
        errors.append(
            f"Neo4j segment-token coverage is {segment_tokens}/{expected_tokens}; expected {target}/{target}"
        )
    final_cursor_lag = cursor_lags[-1] if cursor_lags else None
    if final_cursor_lag is None or final_cursor_lag > maximum_cursor_lag_sec:
        errors.append(
            f"final Graph cursor lag is {final_cursor_lag}; maximum {maximum_cursor_lag_sec:.1f}s"
        )
    return {
        "passed": not errors,
        "target_clients": target,
        "successful_client_count": success_count,
        "peak_active_clients": peak_active,
        "minimum_active_clients": threshold,
        "sustained_at_threshold_sec": sustained_sec,
        "edge_document_count": edge_documents,
        "edge_status_4xx_count": status_4xx,
        "edge_status_4xx_ratio": round(status_4xx / edge_documents, 6) if edge_documents else None,
        "client_http_retry_count": http_retries,
        "client_http_failure_count": http_failures,
        "graph_recovery_completed": recovery_completed,
        "graph_recovery_sec": round(recovery_sec, 3) if recovery_sec is not None else None,
        "final_graph_cursor_lag_sec": final_cursor_lag,
        "maximum_graph_cursor_lag_sec": round(max(cursor_lags), 3) if cursor_lags else None,
        "token_coverage": token_coverage,
        **lag_summary,
        "node_maxima": dict(sorted(max_node_values.items())),
        "errors": errors,
    }


def append_json_line(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_gate_until_pass(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.gate_wait_timeout_sec
    last_detail = ""
    while True:
        completed = subprocess.run(
            [
                sys.executable,
                str(GATE_SCRIPT),
                "--ssh-user",
                args.ssh_user,
                "--ssh-key",
                str(args.ssh_key.expanduser()),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            result = {}
        if completed.returncode == 0 and result.get("passed"):
            return result
        last_detail = completed.stderr.strip() or completed.stdout[-1000:]
        if time.monotonic() >= deadline:
            raise RampError(f"collection gate did not recover: {last_detail}")
        time.sleep(args.gate_retry_sec)


def run_stage(
    args: argparse.Namespace,
    *,
    ramp_id: str,
    target: int,
    all_clients: list[scenario.LogicalClient],
    output_dir: Path,
) -> dict[str, Any]:
    selected = select_balanced_clients(all_clients, target)
    assignments, plans = build_assignments(
        selected,
        seed=args.seed + target * 100_003,
        segments_min=args.segments_min,
        segments_max=args.segments_max,
        delay_min_sec=args.delay_min_sec,
        delay_max_sec=args.delay_max_sec,
    )
    samples_path = output_dir / f"{ramp_id}_{target:03d}.samples.jsonl"
    if samples_path.exists():
        raise RampError(f"runtime sample file already exists: {samples_path}")
    gate = None if args.skip_gate else run_gate_until_pass(args)
    sampler = RuntimeSampler(
        ssh_user=args.ssh_user,
        ssh_key=args.ssh_key,
        ssh_timeout_sec=args.ssh_timeout_sec,
        neo4j_password=args.neo4j_password,
        api_timeout_sec=args.api_timeout_sec,
    )
    samples: list[dict[str, Any]] = []

    def take_sample(phase: str, workload: dict[str, Any]) -> dict[str, Any]:
        sample = sampler.sample(phase=phase, workload=workload)
        samples.append(sample)
        append_json_line(samples_path, sample)
        print(
            json.dumps(
                {
                    "level": target,
                    "phase": phase,
                    "active": sample["achieved_active_clients"],
                    "completed": workload.get("completed"),
                    "failed": workload.get("failed"),
                    "cursor_lag_sec": sample.get("graph_cursor_lag_sec"),
                    "sample_errors": len(sample.get("errors", [])),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return sample

    take_sample("baseline", {"target": target, "submitted": 0, "completed": 0, "failed": 0})
    sampler.reset_measurement_accumulators()
    executor = scenario.RemoteExecutor(args.ssh_user, args.ssh_key, args.remote_timeout_sec)
    results_by_client: dict[str, dict[str, Any]] = {}
    errors_by_client: dict[str, str] = {}
    owner = {"ramp_id": ramp_id, "level": target, "pid": str(os.getpid())}
    stage_started = time.monotonic()
    with ClientReservation(
        DEFAULT_LOCK_DIR,
        [item.client.logical_client_id for item in assignments],
        owner,
    ):
        with concurrent.futures.ThreadPoolExecutor(max_workers=target) as pool:
            future_assignments = {
                pool.submit(executor.run, assignment): assignment
                for assignment in assignments
            }
            while not all(future.done() for future in future_assignments):
                time.sleep(args.sample_interval_sec)
                completed_count = sum(future.done() for future in future_assignments)
                failed_count = sum(
                    future.done() and future.exception() is not None
                    for future in future_assignments
                )
                take_sample(
                    "workload",
                    {
                        "target": target,
                        "submitted": target,
                        "completed": completed_count,
                        "failed": failed_count,
                    },
                )
            for future, assignment in future_assignments.items():
                try:
                    results_by_client[assignment.client.logical_client_id] = future.result()
                except Exception as exc:
                    errors_by_client[assignment.client.logical_client_id] = str(exc)
            take_sample(
                "workload",
                {
                    "target": target,
                    "submitted": target,
                    "completed": target,
                    "failed": len(errors_by_client),
                },
            )
    workload_ended = time.monotonic()
    result_rows = [
        summarize_remote_result(
            assignment,
            results_by_client.get(assignment.client.logical_client_id),
            errors_by_client.get(assignment.client.logical_client_id),
        )
        for assignment in assignments
    ]
    token_ids = [
        str(binding.get("cdn_token_id"))
        for result in result_rows
        for binding in result.get("token_bindings", [])
        if binding.get("cdn_token_id")
    ]
    recovery_deadline = time.monotonic() + args.recovery_timeout_sec
    recovery_completed = False
    consecutive_recovered = 0
    token_coverage: dict[str, Any] = {
        "expected_token_count": len(set(token_ids)),
        "graph_token_count": 0,
        "tokens_with_segments": 0,
    }
    while time.monotonic() < recovery_deadline:
        time.sleep(args.sample_interval_sec)
        sample = take_sample(
            "recovery",
            {
                "target": target,
                "submitted": target,
                "completed": target,
                "failed": len(errors_by_client),
            },
        )
        try:
            token_coverage = sampler.query_token_coverage(token_ids)
        except RuntimeSampleError as exc:
            sample["errors"].append(str(exc))
        cursor_lag = sample.get("graph_cursor_lag_sec")
        recovered_now = (
            sample.get("achieved_active_clients") == 0
            and cursor_lag is not None
            and float(cursor_lag) <= args.maximum_cursor_lag_sec
            and token_coverage.get("expected_token_count") == target
            and token_coverage.get("tokens_with_segments") == target
            and not sample.get("errors")
        )
        consecutive_recovered = consecutive_recovered + 1 if recovered_now else 0
        if consecutive_recovered >= 2:
            recovery_completed = True
            break
    recovery_sec = time.monotonic() - workload_ended if recovery_completed else None
    summary = stage_summary(
        target=target,
        samples=samples,
        results=result_rows,
        token_coverage=token_coverage,
        lag_summary=sampler.aggregate_lags(),
        recovery_completed=recovery_completed,
        recovery_sec=recovery_sec,
        minimum_concurrency_ratio=args.minimum_concurrency_ratio,
        minimum_sustain_sec=args.minimum_sustain_sec,
        maximum_cursor_lag_sec=args.maximum_cursor_lag_sec,
    )
    return {
        "schema_version": 1,
        "ramp_id": ramp_id,
        "target_clients": target,
        "started_at": samples[0]["sampled_at"],
        "ended_at": utc_now(),
        "duration_sec": round(time.monotonic() - stage_started, 3),
        "gate": gate,
        "sample_count": len(samples),
        "samples_path": str(samples_path),
        "workload": {
            "scenario": "N1-like sequential VOD system workload",
            "segments_min": args.segments_min,
            "segments_max": args.segments_max,
            "delay_min_sec": args.delay_min_sec,
            "delay_max_sec": args.delay_max_sec,
            "client_ids": [item.logical_client_id for item in selected],
            "physical_host_counts": distribution(plans, "physical_host_id"),
            "network_profile_counts": distribution(plans, "network_profile_id"),
            "edge_counts": distribution(plans, "edge_id"),
            "content_counts": distribution(plans, "content_id"),
            "plans": plans,
            "results": result_rows,
        },
        "summary": summary,
        "passed": summary["passed"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default="20,40,60,80,100")
    parser.add_argument("--seed", type=int, default=2026082601)
    parser.add_argument("--segments-min", type=int, default=30)
    parser.add_argument("--segments-max", type=int, default=40)
    parser.add_argument("--delay-min-sec", type=float, default=5.2)
    parser.add_argument("--delay-max-sec", type=float, default=6.8)
    parser.add_argument("--sample-interval-sec", type=float, default=5.0)
    parser.add_argument("--minimum-concurrency-ratio", type=float, default=0.90)
    parser.add_argument("--minimum-sustain-sec", type=float, default=30.0)
    parser.add_argument("--maximum-cursor-lag-sec", type=float, default=2.0)
    parser.add_argument("--recovery-timeout-sec", type=float, default=900.0)
    parser.add_argument("--inter-stage-idle-sec", type=float, default=30.0)
    parser.add_argument("--gate-wait-timeout-sec", type=float, default=600.0)
    parser.add_argument("--gate-retry-sec", type=float, default=30.0)
    parser.add_argument("--ssh-user", default="ottadmin")
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh" / "ott_lab_ed25519")
    parser.add_argument("--ssh-timeout-sec", type=float, default=25.0)
    parser.add_argument("--remote-timeout-sec", type=float, default=1200.0)
    parser.add_argument("--api-timeout-sec", type=float, default=20.0)
    parser.add_argument("--neo4j-password", default="ottlab1234")
    parser.add_argument("--ramp-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-gate", action="store_true", help="debugging only")
    parser.add_argument("--continue-on-failure", action="store_true", help="debugging only")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> tuple[int, ...]:
    levels = parse_levels(args.levels)
    if args.segments_min < 1 or args.segments_max < args.segments_min:
        raise RampError("segment range is invalid")
    if args.delay_min_sec < 0 or args.delay_max_sec < args.delay_min_sec:
        raise RampError("delay range is invalid")
    if args.sample_interval_sec < 1:
        raise RampError("sample interval must be at least one second")
    if not 0 < args.minimum_concurrency_ratio <= 1:
        raise RampError("minimum concurrency ratio must be in (0, 1]")
    if args.minimum_sustain_sec < args.sample_interval_sec:
        raise RampError("minimum sustain time must cover at least one sample interval")
    return levels


def main() -> int:
    args = parse_args()
    try:
        levels = validate_args(args)
        clients = scenario.load_inventory(scenario.DEFAULT_INVENTORY)
        ramp_id = args.ramp_id or f"tnsm_100lc_{datetime.now(timezone.utc):%Y%m%dT%H%M}_runtime_ramp"
        output = args.output.resolve() if args.output else DEFAULT_OUTPUT_DIR / f"{ramp_id}.json"
        if output.exists():
            raise RampError(f"ramp report already exists: {output}")
        plan = {
            "ramp_id": ramp_id,
            "levels": list(levels),
            "selected_clients": {
                str(level): [item.logical_client_id for item in select_balanced_clients(clients, level)]
                for level in levels
            },
        }
        if args.dry_run:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0

        live_state = configure_live_channels(
            [],
            ssh_user=args.ssh_user,
            ssh_key=args.ssh_key.expanduser().resolve(),
            timeout_sec=60.0,
        )
        revision = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        report: dict[str, Any] = {
            "schema_version": 1,
            "ramp_id": ramp_id,
            "started_at": utc_now(),
            "ended_at": None,
            "repository_revision": revision,
            "levels": list(levels),
            "live_setup": live_state,
            "stages": [],
            "passed": False,
        }
        write_json(output, report)
        for index, level in enumerate(levels):
            stage = run_stage(
                args,
                ramp_id=ramp_id,
                target=level,
                all_clients=clients,
                output_dir=output.parent,
            )
            report["stages"].append(stage)
            write_json(output, report)
            if not stage["passed"] and not args.continue_on_failure:
                break
            if index < len(levels) - 1:
                time.sleep(args.inter_stage_idle_sec)
        report["ended_at"] = utc_now()
        report["passed"] = len(report["stages"]) == len(levels) and all(
            stage["passed"] for stage in report["stages"]
        )
        write_json(output, report)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "completed_levels": [stage["target_clients"] for stage in report["stages"]],
                    "output_path": str(output),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report["passed"] else 1
    except (RampError, RuntimeSampleError, OSError, ValueError) as exc:
        print(f"runtime ramp failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
