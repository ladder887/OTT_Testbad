"""Execute collection-matrix batches with logical-client reservation locks."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_RUNNER = Path(__file__).with_name("run_scenario.py")
COLLECTION_VALIDATOR = REPOSITORY_ROOT / "03_experiments" / "05_validation" / "validate_run_collection.py"
DEFAULT_LOCK_DIR = REPOSITORY_ROOT / "06_outputs" / "00_collection_plans" / ".client_reservations"
DEFAULT_GATE_DIR = REPOSITORY_ROOT / "06_outputs" / "03_runtime_metrics"


class MatrixExecutionError(RuntimeError):
    """Raised when a matrix cannot be executed without corrupting provenance."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MatrixExecutionError(f"invalid gate timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_recent_gate_report(path: Path | None, max_age_min: float) -> tuple[Path, dict[str, Any]]:
    if path is None:
        candidates = sorted(DEFAULT_GATE_DIR.glob("collection_gate_*.json"), key=lambda item: item.stat().st_mtime)
        if not candidates:
            raise MatrixExecutionError(
                "no collection gate report exists; run check_collection_gate.py before --execute"
            )
        path = candidates[-1]
    resolved = path.resolve()
    report = json.loads(resolved.read_text(encoding="utf-8"))
    if not report.get("passed"):
        raise MatrixExecutionError(f"collection gate did not pass: {resolved}")
    sampled_at = parse_datetime(report.get("sampled_at"))
    age_sec = (datetime.now(timezone.utc) - sampled_at).total_seconds()
    if age_sec < -60.0:
        raise MatrixExecutionError(f"collection gate timestamp is in the future: {resolved}")
    if age_sec > max_age_min * 60.0:
        raise MatrixExecutionError(
            f"collection gate is {age_sec / 60.0:.1f} minutes old; maximum is {max_age_min:.1f}"
        )
    return resolved, report


class ClientReservation(AbstractContextManager["ClientReservation"]):
    def __init__(self, lock_dir: Path, client_ids: list[str], owner: dict[str, Any]) -> None:
        self.lock_dir = lock_dir
        self.client_ids = sorted(client_ids)
        self.owner = owner
        self.acquired: list[Path] = []

    def __enter__(self) -> "ClientReservation":
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            for client_id in self.client_ids:
                path = self.lock_dir / f"{client_id}.lock"
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    payload = {**self.owner, "logical_client_id": client_id, "reserved_at": utc_now()}
                    os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
                finally:
                    os.close(descriptor)
                self.acquired.append(path)
        except FileExistsError as exc:
            self.release()
            existing = Path(exc.filename) if exc.filename else self.lock_dir
            detail = existing.read_text(encoding="utf-8", errors="replace").strip() if existing.is_file() else ""
            raise MatrixExecutionError(
                f"logical client is already reserved: {existing.name}; owner={detail or 'unknown'}"
            ) from exc
        return self

    def release(self) -> None:
        for path in reversed(self.acquired):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.acquired.clear()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def resolve_repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def load_matrix(path: Path) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    if matrix.get("schema_version") != 1 or not matrix.get("matrix_id"):
        raise MatrixExecutionError("unsupported or incomplete collection matrix")
    run_keys: set[str] = set()
    for batch in matrix.get("batches", []):
        batch_clients: set[str] = set()
        for run in batch.get("runs", []):
            run_key = str(run.get("run_key") or "")
            if not run_key or run_key in run_keys:
                raise MatrixExecutionError(f"duplicate or missing matrix run key: {run_key or '<missing>'}")
            run_keys.add(run_key)
            ids = [str(item) for item in run.get("reserved_client_ids", [])]
            if len(ids) != int(run.get("required_client_count") or 0) or len(ids) != len(set(ids)):
                raise MatrixExecutionError(f"{run_key}: invalid logical-client reservation")
            overlap = set(ids).intersection(batch_clients)
            if overlap:
                raise MatrixExecutionError(f"{batch.get('batch_id')}: overlapping clients {sorted(overlap)}")
            batch_clients.update(ids)
    return matrix


def parse_json_output(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MatrixExecutionError(f"scenario runner returned invalid JSON: {text[-500:]}") from exc


def execute_one_run(
    matrix: dict[str, Any],
    run: dict[str, Any],
    *,
    batch_started_monotonic: float,
    manifest_output_dir: Path,
    lock_dir: Path,
    ssh_user: str,
    ssh_key: Path,
    remote_timeout_sec: float,
    validate_collection: bool,
    validation_wait_sec: float,
) -> dict[str, Any]:
    offset = max(0.0, float(run.get("start_offset_sec") or 0.0))
    remaining = batch_started_monotonic + offset - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    started_at = utc_now()
    owner = {
        "matrix_id": matrix["matrix_id"],
        "batch_id": run["batch_id"],
        "run_key": run["run_key"],
        "pid": os.getpid(),
    }
    command = [
        sys.executable,
        str(SCENARIO_RUNNER),
        "--scenario",
        run["scenario_id"],
        "--variant",
        run["scenario_variant"],
        "--seed",
        str(run["seed"]),
        "--dataset-prefix",
        matrix["dataset_prefix"],
        "--output-dir",
        str(manifest_output_dir),
        "--cache-state",
        matrix.get("cache_state", "unspecified"),
        "--reserved-client-ids",
        ",".join(run["reserved_client_ids"]),
        "--content-ids",
        ",".join(run["allowed_content_ids"]),
        "--data-split",
        run["data_split"],
        "--matrix-id",
        matrix["matrix_id"],
        "--matrix-run-key",
        run["run_key"],
        "--ssh-user",
        ssh_user,
        "--ssh-key",
        str(ssh_key),
        "--remote-timeout-sec",
        str(remote_timeout_sec),
    ]
    if matrix.get("smoke"):
        command.append("--smoke")

    result: dict[str, Any] = {
        "run_key": run["run_key"],
        "scenario_id": run["scenario_id"],
        "scenario_variant": run["scenario_variant"],
        "data_split": run["data_split"],
        "reserved_client_ids": run["reserved_client_ids"],
        "started_at": started_at,
        "status": "running",
    }
    try:
        with ClientReservation(lock_dir, run["reserved_client_ids"], owner):
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(60.0, remote_timeout_sec + 120.0),
            )
            if completed.returncode != 0:
                result.update(
                    {
                        "status": "failed",
                        "return_code": completed.returncode,
                        "error": completed.stderr.strip() or completed.stdout.strip()[-1000:],
                    }
                )
                return result
            scenario_result = parse_json_output(completed.stdout)
            result.update(
                {
                    "status": "completed",
                    "run_id": scenario_result.get("run_id"),
                    "manifest_path": scenario_result.get("manifest_path"),
                    "observed_request_count": scenario_result.get("observed_request_count"),
                }
            )
            if validate_collection:
                validation = subprocess.run(
                    [
                        sys.executable,
                        str(COLLECTION_VALIDATOR),
                        "--manifest",
                        str(scenario_result["manifest_path"]),
                        "--wait-sec",
                        str(validation_wait_sec),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=max(60.0, validation_wait_sec + 60.0),
                )
                result["validation_return_code"] = validation.returncode
                try:
                    validation_result = parse_json_output(validation.stdout)
                    result["validation_passed"] = bool(validation_result.get("passed"))
                    result["validation_path"] = validation_result.get("output_path")
                    if not result["validation_passed"]:
                        result["status"] = "validation_failed"
                        result["validation_errors"] = validation_result.get("errors", [])
                except MatrixExecutionError:
                    result["status"] = "validation_failed"
                    result["validation_passed"] = False
                    result["validation_errors"] = [validation.stderr.strip() or validation.stdout[-1000:]]
    except (MatrixExecutionError, subprocess.TimeoutExpired, OSError) as exc:
        result.update({"status": "failed", "error": str(exc)})
    finally:
        result["ended_at"] = utc_now()
    return result


def select_batches(matrix: dict[str, Any], batch_ids: tuple[str, ...], all_batches: bool) -> list[dict[str, Any]]:
    batches = list(matrix.get("batches", []))
    if all_batches:
        return batches
    requested = set(batch_ids)
    selected = [batch for batch in batches if batch.get("batch_id") in requested]
    missing = sorted(requested - {str(batch.get("batch_id")) for batch in selected})
    if missing:
        raise MatrixExecutionError(f"unknown batch IDs: {missing}")
    if not selected:
        raise MatrixExecutionError("specify --batch-id or --all-batches with --execute")
    return selected


def execute_batch(
    matrix: dict[str, Any],
    batch: dict[str, Any],
    args: argparse.Namespace,
    manifest_output_dir: Path,
) -> dict[str, Any]:
    started_at = utc_now()
    started_monotonic = time.monotonic()
    runs = list(batch.get("runs", []))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(runs))) as pool:
        futures = [
            pool.submit(
                execute_one_run,
                matrix,
                run,
                batch_started_monotonic=started_monotonic,
                manifest_output_dir=manifest_output_dir,
                lock_dir=args.lock_dir.resolve(),
                ssh_user=args.ssh_user,
                ssh_key=args.ssh_key.expanduser().resolve(),
                remote_timeout_sec=args.remote_timeout_sec,
                validate_collection=not args.skip_validation,
                validation_wait_sec=args.validation_wait_sec,
            )
            for run in runs
        ]
        results = [future.result() for future in futures]
    status_counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "batch_id": batch["batch_id"],
        "data_split": batch["data_split"],
        "planned_client_count": batch["planned_client_count"],
        "started_at": started_at,
        "ended_at": utc_now(),
        "status_counts": dict(sorted(status_counts.items())),
        "passed": all(result.get("status") == "completed" for result in results),
        "runs": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="run traffic; without this flag only validate and summarize")
    parser.add_argument("--batch-id", action="append", default=[])
    parser.add_argument("--all-batches", action="store_true")
    parser.add_argument("--ssh-user", default="ottadmin")
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh" / "ott_lab_ed25519")
    parser.add_argument("--remote-timeout-sec", type=float, default=1800.0)
    parser.add_argument("--validation-wait-sec", type=float, default=180.0)
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--lock-dir", type=Path, default=DEFAULT_LOCK_DIR)
    parser.add_argument("--gate-report", type=Path, help="passed collection-gate JSON; newest report is used by default")
    parser.add_argument("--max-gate-age-min", type=float, default=15.0)
    parser.add_argument("--skip-gate", action="store_true", help="explicitly bypass the pre-collection gate")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        matrix_path = args.matrix.resolve()
        matrix = load_matrix(matrix_path)
        if not args.execute:
            summary = {
                "matrix_id": matrix["matrix_id"],
                "dataset_prefix": matrix["dataset_prefix"],
                "batch_count": len(matrix.get("batches", [])),
                "run_count": sum(len(batch.get("runs", [])) for batch in matrix.get("batches", [])),
                "planned_client_counts": {
                    batch["batch_id"]: batch["planned_client_count"] for batch in matrix.get("batches", [])
                },
                "validated": True,
                "executed": False,
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        batches = select_batches(matrix, tuple(args.batch_id), args.all_batches)
        gate_path = None
        gate_report = None
        if not args.skip_gate:
            gate_path, gate_report = load_recent_gate_report(args.gate_report, args.max_gate_age_min)
    except (MatrixExecutionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"collection matrix rejected: {exc}", file=sys.stderr)
        return 2

    manifest_output_dir = resolve_repo_path(matrix["manifest_output_dir"]).resolve()
    manifest_output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "matrix_id": matrix["matrix_id"],
        "dataset_prefix": matrix["dataset_prefix"],
        "matrix_path": str(matrix_path),
        "gate_report_path": str(gate_path) if gate_path else None,
        "gate_sampled_at": gate_report.get("sampled_at") if gate_report else None,
        "gate_bypassed": bool(args.skip_gate),
        "started_at": utc_now(),
        "batches": [],
    }
    for batch in batches:
        batch_report = execute_batch(matrix, batch, args, manifest_output_dir)
        report["batches"].append(batch_report)
        if not batch_report["passed"]:
            break
    report["ended_at"] = utc_now()
    report["passed"] = len(report["batches"]) == len(batches) and all(
        item["passed"] for item in report["batches"]
    )
    batch_label = "-".join(batch["batch_id"] for batch in batches)
    report_path = args.report.resolve() if args.report else (
        manifest_output_dir
        / f"{matrix['matrix_id']}.{batch_label}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.execution.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "matrix_id": matrix["matrix_id"],
                "passed": report["passed"],
                "executed_batch_count": len(report["batches"]),
                "report_path": str(report_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
