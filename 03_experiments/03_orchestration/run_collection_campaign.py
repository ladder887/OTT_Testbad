"""Run a long collection matrix one gated, resumable batch at a time."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MATRIX_RUNNER = Path(__file__).with_name("run_collection_matrix.py")
GATE_RUNNER = REPOSITORY_ROOT / "03_experiments" / "06_runtime_metrics" / "check_collection_gate.py"
SPLIT_ORDER = ("train", "validation", "test")


class CampaignError(RuntimeError):
    """Raised when a campaign cannot continue without provenance risk."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_matrix(path: Path) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    if matrix.get("schema_version") != 2 or not matrix.get("matrix_id"):
        raise CampaignError("campaign execution requires a complete schema-v2 matrix")
    batches = matrix.get("batches")
    if not isinstance(batches, list) or not batches:
        raise CampaignError("matrix has no batches")
    batch_ids = [str(batch.get("batch_id") or "") for batch in batches]
    if not all(batch_ids) or len(batch_ids) != len(set(batch_ids)):
        raise CampaignError("matrix has duplicate or missing batch IDs")
    return matrix


def select_batches(
    matrix: dict[str, Any],
    splits: tuple[str, ...],
    batch_ids: tuple[str, ...],
    max_batches: int | None,
) -> list[dict[str, Any]]:
    batches = list(matrix["batches"])
    known_splits = {str(batch.get("data_split") or "") for batch in batches}
    unknown_splits = sorted(set(splits) - known_splits)
    if unknown_splits:
        raise CampaignError(f"unknown splits: {unknown_splits}")
    known_ids = {str(batch["batch_id"]) for batch in batches}
    unknown_ids = sorted(set(batch_ids) - known_ids)
    if unknown_ids:
        raise CampaignError(f"unknown batch IDs: {unknown_ids}")
    selected = [
        batch
        for batch in batches
        if (not splits or batch.get("data_split") in splits)
        and (not batch_ids or batch.get("batch_id") in batch_ids)
    ]
    if max_batches is not None:
        if max_batches < 1:
            raise CampaignError("max-batches must be at least 1")
        selected = selected[:max_batches]
    if not selected:
        raise CampaignError("batch selection is empty")
    return selected


def default_state_path(matrix: dict[str, Any]) -> Path:
    manifest_dir = resolve_repo_path(str(matrix["manifest_output_dir"]))
    return manifest_dir / f"{matrix['matrix_id']}.campaign_state.json"


def new_state(matrix: dict[str, Any], matrix_path: Path, matrix_sha256: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 1,
        "matrix_id": matrix["matrix_id"],
        "dataset_prefix": matrix["dataset_prefix"],
        "matrix_path": str(matrix_path),
        "matrix_sha256": matrix_sha256,
        "started_at": now,
        "updated_at": now,
        "completed_batch_ids": [],
        "attempts": [],
    }


def load_state(
    path: Path,
    matrix: dict[str, Any],
    matrix_path: Path,
    matrix_sha256: str,
) -> dict[str, Any]:
    if not path.exists():
        return new_state(matrix, matrix_path, matrix_sha256)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1:
        raise CampaignError(f"unsupported campaign state: {path}")
    if state.get("matrix_id") != matrix["matrix_id"]:
        raise CampaignError("campaign state belongs to a different matrix ID")
    if state.get("matrix_sha256") != matrix_sha256:
        raise CampaignError("matrix changed after campaign state was created")
    state.setdefault("completed_batch_ids", [])
    state.setdefault("attempts", [])
    return state


def write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def parse_json_output(text: str, command_name: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CampaignError(f"{command_name} returned invalid JSON: {text[-500:]}") from exc
    if not isinstance(value, dict):
        raise CampaignError(f"{command_name} returned a non-object JSON value")
    return value


def discover_completed_batches(manifest_dir: Path, matrix_id: str) -> set[str]:
    completed: set[str] = set()
    if not manifest_dir.exists():
        return completed
    for path in manifest_dir.glob("*.execution.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("matrix_id") != matrix_id or not report.get("passed"):
            continue
        batches = report.get("batches") or []
        for batch in batches:
            if batch.get("passed") and batch.get("batch_id"):
                completed.add(str(batch["batch_id"]))
    return completed


def unresolved_attempts(state: dict[str, Any], completed: set[str]) -> set[str]:
    unresolved: set[str] = set()
    for attempt in state.get("attempts", []):
        batch_id = str(attempt.get("batch_id") or "")
        if not batch_id or batch_id in completed:
            continue
        if attempt.get("status") in {"running", "failed", "interrupted"}:
            unresolved.add(batch_id)
    return unresolved


def validate_split_order(
    matrix: dict[str, Any],
    selected: list[dict[str, Any]],
    completed: set[str],
) -> None:
    if matrix.get("phase") != "main":
        return
    all_batches = list(matrix["batches"])
    by_split = {
        split: {str(batch["batch_id"]) for batch in all_batches if batch.get("data_split") == split}
        for split in SPLIT_ORDER
    }
    for index, split in enumerate(SPLIT_ORDER[1:], start=1):
        if completed.intersection(by_split[split]):
            missing_prior = set().union(*(by_split[item] for item in SPLIT_ORDER[:index])) - completed
            if missing_prior:
                raise CampaignError(
                    f"completed {split} data exists before prior splits finished: {sorted(missing_prior)}"
                )
    available = set(completed)
    for batch in selected:
        split = str(batch.get("data_split") or "")
        if split not in SPLIT_ORDER:
            raise CampaignError(f"main batch has invalid split: {split}")
        index = SPLIT_ORDER.index(split)
        required = set().union(*(by_split[item] for item in SPLIT_ORDER[:index])) if index else set()
        missing_prior = required - available
        if missing_prior:
            raise CampaignError(
                f"cannot execute {split} before prior split batches complete: {sorted(missing_prior)}"
            )
        available.add(str(batch["batch_id"]))


def gate_command() -> list[str]:
    return [sys.executable, str(GATE_RUNNER)]


def matrix_command(
    matrix_path: Path,
    batch_id: str,
    gate_report_path: Path,
    args: argparse.Namespace,
) -> list[str]:
    return [
        sys.executable,
        str(MATRIX_RUNNER),
        "--matrix",
        str(matrix_path),
        "--batch-id",
        batch_id,
        "--execute",
        "--gate-report",
        str(gate_report_path),
        "--ssh-user",
        args.ssh_user,
        "--ssh-key",
        str(args.ssh_key.expanduser().resolve()),
        "--remote-timeout-sec",
        str(args.remote_timeout_sec),
        "--validation-wait-sec",
        str(args.validation_wait_sec),
        "--live-setup-timeout-sec",
        str(args.live_setup_timeout_sec),
    ]


def emit_progress(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "at": utc_now(), **fields}, sort_keys=True), file=sys.stderr, flush=True)


def default_batch_timeout_sec(batch: dict[str, Any], args: argparse.Namespace) -> float:
    latest_start_offset = max(
        (float(run.get("start_offset_sec") or 0.0) for run in batch.get("runs", [])),
        default=0.0,
    )
    return (
        latest_start_offset
        + args.remote_timeout_sec
        + args.validation_wait_sec
        + args.live_setup_timeout_sec
        + 600.0
    )


def run_campaign(
    matrix: dict[str, Any],
    matrix_path: Path,
    batches: list[dict[str, Any]],
    state_path: Path,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], bool]:
    matrix_sha256 = sha256_file(matrix_path)
    state = load_state(state_path, matrix, matrix_path, matrix_sha256)
    manifest_dir = resolve_repo_path(str(matrix["manifest_output_dir"]))
    completed = set(str(item) for item in state["completed_batch_ids"])
    completed.update(discover_completed_batches(manifest_dir, matrix["matrix_id"]))
    state["completed_batch_ids"] = sorted(completed)
    selected_ids = [str(batch["batch_id"]) for batch in batches]
    validate_split_order(matrix, batches, completed)
    blocked = unresolved_attempts(state, completed).intersection(selected_ids)
    if blocked and not args.allow_partial_batch_retry:
        raise CampaignError(
            "partial or failed batches require cleanup review before retry: "
            f"{sorted(blocked)}; pass --allow-partial-batch-retry only after resolving duplicate-data risk"
        )
    write_state(state_path, state)

    for batch in batches:
        batch_id = str(batch["batch_id"])
        if batch_id in completed:
            emit_progress("batch_skipped_completed", batch_id=batch_id)
            continue
        attempt = {
            "batch_id": batch_id,
            "data_split": batch.get("data_split"),
            "started_at": utc_now(),
            "status": "gate_running",
        }
        state["attempts"].append(attempt)
        write_state(state_path, state)
        emit_progress("gate_started", batch_id=batch_id)
        try:
            gate = subprocess.run(
                gate_command(),
                check=False,
                capture_output=True,
                text=True,
                timeout=args.gate_timeout_sec,
            )
            gate_summary = parse_json_output(gate.stdout, "collection gate")
        except (OSError, subprocess.TimeoutExpired, CampaignError) as exc:
            attempt.update({"status": "gate_failed", "ended_at": utc_now(), "error": str(exc)})
            write_state(state_path, state)
            emit_progress("gate_failed", batch_id=batch_id, error=str(exc))
            return state, False
        attempt["gate_report_path"] = gate_summary.get("output_path")
        if gate.returncode != 0 or not gate_summary.get("passed") or not attempt["gate_report_path"]:
            error = gate_summary.get("errors") or gate.stderr.strip() or "collection gate failed"
            attempt.update({"status": "gate_failed", "ended_at": utc_now(), "error": error})
            write_state(state_path, state)
            emit_progress("gate_failed", batch_id=batch_id, error=error)
            return state, False

        timeout = args.batch_timeout_sec or default_batch_timeout_sec(batch, args)
        attempt.update({"status": "running", "batch_timeout_sec": timeout})
        write_state(state_path, state)
        emit_progress(
            "batch_started",
            batch_id=batch_id,
            gate_report_path=attempt["gate_report_path"],
            batch_timeout_sec=timeout,
        )
        try:
            execution = subprocess.run(
                matrix_command(matrix_path, batch_id, Path(str(attempt["gate_report_path"])), args),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            execution_summary = parse_json_output(execution.stdout, "matrix runner")
        except KeyboardInterrupt:
            attempt.update({"status": "interrupted", "ended_at": utc_now()})
            write_state(state_path, state)
            raise
        except (OSError, subprocess.TimeoutExpired, CampaignError) as exc:
            attempt.update({"status": "failed", "ended_at": utc_now(), "error": str(exc)})
            write_state(state_path, state)
            emit_progress("batch_failed", batch_id=batch_id, error=str(exc))
            return state, False
        attempt["execution_report_path"] = execution_summary.get("report_path")
        if execution.returncode != 0 or not execution_summary.get("passed"):
            error = execution.stderr.strip() or execution.stdout[-1000:] or "matrix batch failed"
            attempt.update({"status": "failed", "ended_at": utc_now(), "error": error})
            write_state(state_path, state)
            emit_progress("batch_failed", batch_id=batch_id, error=error)
            return state, False
        attempt.update({"status": "completed", "ended_at": utc_now()})
        completed.add(batch_id)
        state["completed_batch_ids"] = sorted(completed)
        write_state(state_path, state)
        emit_progress("batch_completed", batch_id=batch_id, report_path=attempt["execution_report_path"])

    return state, True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="execute selected batches; otherwise print a plan")
    parser.add_argument("--split", action="append", choices=("train", "validation", "test"), default=[])
    parser.add_argument("--batch-id", action="append", default=[])
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--allow-partial-batch-retry", action="store_true")
    parser.add_argument("--ssh-user", default="ottadmin")
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh" / "ott_lab_ed25519")
    parser.add_argument("--remote-timeout-sec", type=float, default=1800.0)
    parser.add_argument("--validation-wait-sec", type=float, default=180.0)
    parser.add_argument("--live-setup-timeout-sec", type=float, default=60.0)
    parser.add_argument("--gate-timeout-sec", type=float, default=300.0)
    parser.add_argument("--batch-timeout-sec", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        matrix_path = args.matrix.resolve()
        matrix = load_matrix(matrix_path)
        batches = select_batches(matrix, tuple(args.split), tuple(args.batch_id), args.max_batches)
        state_path = args.state.resolve() if args.state else default_state_path(matrix).resolve()
        if not args.execute:
            print(
                json.dumps(
                    {
                        "matrix_id": matrix["matrix_id"],
                        "dataset_prefix": matrix["dataset_prefix"],
                        "selected_batch_count": len(batches),
                        "selected_batch_ids": [batch["batch_id"] for batch in batches],
                        "state_path": str(state_path),
                        "executed": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        state, passed = run_campaign(matrix, matrix_path, batches, state_path, args)
    except KeyboardInterrupt:
        print("collection campaign interrupted", file=sys.stderr)
        return 130
    except (CampaignError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"collection campaign rejected: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "matrix_id": matrix["matrix_id"],
                "passed": passed,
                "selected_batch_count": len(batches),
                "completed_batch_count": len(state["completed_batch_ids"]),
                "state_path": str(state_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
