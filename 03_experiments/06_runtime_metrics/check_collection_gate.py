"""Block collection when clocks, node load, containers, or endpoints are unhealthy."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "06_outputs" / "03_runtime_metrics"


@dataclass(frozen=True)
class Node:
    name: str
    ip: str
    role: str
    minimum_running_containers: int
    require_docker: bool = True


NODES = (
    Node("ott-origin", "192.168.0.101", "origin", 4),
    Node("ott-edge-1", "192.168.0.111", "edge", 2),
    Node("ott-edge-2", "192.168.0.112", "edge", 2),
    Node("ott-edge-3", "192.168.0.113", "edge", 2),
    Node("ott-edge-4", "192.168.0.114", "edge", 2),
    Node("ott-storage", "192.168.0.120", "storage", 3),
    Node("ott-processing", "192.168.0.130", "processing", 1),
    *(Node(f"ott-user{index}", f"192.168.0.{130 + index}", "client", 10) for index in range(1, 11)),
)
CONTROL_NODE = Node("ott-control", "192.168.0.150", "control", 0, False)
ENDPOINTS = (
    ("origin-api", "http://192.168.0.101:3001/health"),
    ("origin-nginx", "http://192.168.0.101:8080/health"),
    ("edge-1", "http://192.168.0.111/health"),
    ("edge-2", "http://192.168.0.112/health"),
    ("edge-3", "http://192.168.0.113/health"),
    ("edge-4", "http://192.168.0.114/health"),
    ("elasticsearch", "http://192.168.0.120:9200"),
    ("neo4j", "http://192.168.0.120:7474"),
)

REMOTE_PROBE = r"""
import json
import os
import subprocess
import time


def read_meminfo():
    values = {}
    with open('/proc/meminfo', encoding='ascii') as handle:
        for line in handle:
            key, value = line.split(':', 1)
            values[key] = int(value.strip().split()[0])
    total = values.get('MemTotal', 0)
    available = values.get('MemAvailable', 0)
    return round(100.0 * (total - available) / total, 3) if total else 100.0


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=10)
        return result.returncode, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ''


stat = os.statvfs('/')
disk_total = stat.f_blocks * stat.f_frsize
disk_free = stat.f_bavail * stat.f_frsize
ntp_rc, ntp_value = command(['timedatectl', 'show', '--property=NTPSynchronized', '--value'])
docker_rc, docker_ids = command(['docker', 'ps', '--quiet'])
unhealthy_rc, unhealthy_ids = command([
    'docker', 'ps', '--quiet', '--filter', 'health=unhealthy'
])
print(json.dumps({
    'sample_epoch_ms': time.time_ns() // 1_000_000,
    'hostname': os.uname().nodename,
    'ntp_synchronized': ntp_rc == 0 and ntp_value == 'yes',
    'ntp_value': ntp_value or 'unknown',
    'cpu_count': os.cpu_count() or 1,
    'load_1m': round(os.getloadavg()[0], 4),
    'memory_used_percent': read_meminfo(),
    'disk_free_percent': round(100.0 * disk_free / disk_total, 3) if disk_total else 0.0,
    'running_container_count': len(docker_ids.splitlines()) if docker_rc == 0 and docker_ids else 0,
    'unhealthy_container_count': len(unhealthy_ids.splitlines()) if unhealthy_rc == 0 and unhealthy_ids else 0,
    'docker_probe_ok': docker_rc == 0,
}, sort_keys=True))
"""


class GateError(RuntimeError):
    """Raised when a node cannot be measured."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def remote_probe_command() -> str:
    encoded = base64.b64encode(REMOTE_PROBE.encode("utf-8")).decode("ascii")
    program = f"import base64;exec(base64.b64decode('{encoded}').decode())"
    return f"python3 -c {shlex.quote(program)}"


def probe_node(node: Node, ssh_user: str, ssh_key: Path, timeout_sec: float) -> dict[str, Any]:
    before_ms = time.time_ns() // 1_000_000
    started = time.monotonic()
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-i",
        str(ssh_key),
        f"{ssh_user}@{node.ip}",
        remote_probe_command(),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GateError(f"{node.name}: SSH probe failed: {exc}") from exc
    after_ms = time.time_ns() // 1_000_000
    if completed.returncode != 0:
        raise GateError(f"{node.name}: SSH probe failed: {completed.stderr.strip() or completed.stdout[-500:]}")
    try:
        metrics = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"{node.name}: invalid probe output: {completed.stdout[-500:]}") from exc
    midpoint_ms = (before_ms + after_ms) / 2.0
    metrics.update(
        {
            **asdict(node),
            "ssh_round_trip_ms": round((time.monotonic() - started) * 1000.0, 3),
            "clock_offset_ms": round(float(metrics["sample_epoch_ms"]) - midpoint_ms, 3),
        }
    )
    return metrics


def probe_local_control(node: Node, timeout_sec: float) -> dict[str, Any]:
    before_ms = time.time_ns() // 1_000_000
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", REMOTE_PROBE],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GateError(f"{node.name}: local probe failed: {exc}") from exc
    after_ms = time.time_ns() // 1_000_000
    if completed.returncode != 0:
        raise GateError(f"{node.name}: local probe failed: {completed.stderr.strip()}")
    try:
        metrics = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"{node.name}: invalid local probe output") from exc
    metrics.update(
        {
            **asdict(node),
            "ssh_round_trip_ms": 0.0,
            "clock_offset_ms": round(float(metrics["sample_epoch_ms"]) - (before_ms + after_ms) / 2.0, 3),
            "probe_duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
    )
    return metrics


def evaluate_node(
    metrics: dict[str, Any],
    *,
    max_clock_offset_ms: float,
    max_load_per_cpu: float,
    max_memory_used_percent: float,
    min_disk_free_percent: float,
) -> list[str]:
    errors: list[str] = []
    name = str(metrics.get("name") or "unknown")
    if not metrics.get("ntp_synchronized"):
        errors.append(f"{name}: NTP is not synchronized ({metrics.get('ntp_value', 'unknown')})")
    if abs(float(metrics.get("clock_offset_ms") or 0.0)) > max_clock_offset_ms:
        errors.append(
            f"{name}: clock offset {metrics.get('clock_offset_ms')} ms exceeds {max_clock_offset_ms} ms"
        )
    cpu_count = max(1, int(metrics.get("cpu_count") or 1))
    load_per_cpu = float(metrics.get("load_1m") or 0.0) / cpu_count
    metrics["load_per_cpu"] = round(load_per_cpu, 4)
    if load_per_cpu > max_load_per_cpu:
        errors.append(f"{name}: 1-minute load per CPU {load_per_cpu:.3f} exceeds {max_load_per_cpu}")
    if float(metrics.get("memory_used_percent") or 0.0) > max_memory_used_percent:
        errors.append(
            f"{name}: memory use {metrics.get('memory_used_percent')}% exceeds {max_memory_used_percent}%"
        )
    if float(metrics.get("disk_free_percent") or 0.0) < min_disk_free_percent:
        errors.append(
            f"{name}: root disk free {metrics.get('disk_free_percent')}% is below {min_disk_free_percent}%"
        )
    if metrics.get("require_docker", True):
        if not metrics.get("docker_probe_ok"):
            errors.append(f"{name}: Docker cannot be queried")
        running = int(metrics.get("running_container_count") or 0)
        minimum = int(metrics.get("minimum_running_containers") or 0)
        if running < minimum:
            errors.append(f"{name}: {running} running containers; at least {minimum} required")
        if int(metrics.get("unhealthy_container_count") or 0) > 0:
            errors.append(f"{name}: one or more containers report unhealthy")
    return errors


def probe_endpoint(name: str, url: str, timeout_sec: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            status = int(response.status)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"name": name, "url": url, "passed": False, "error": str(exc)}
    return {
        "name": name,
        "url": url,
        "status": status,
        "latency_ms": round((time.monotonic() - started) * 1000.0, 3),
        "passed": status == 200,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-user", default="ottadmin")
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh" / "ott_lab_ed25519")
    parser.add_argument("--ssh-timeout-sec", type=float, default=30.0)
    parser.add_argument("--endpoint-timeout-sec", type=float, default=10.0)
    parser.add_argument("--max-clock-offset-ms", type=float, default=1500.0)
    parser.add_argument("--max-load-per-cpu", type=float, default=1.25)
    parser.add_argument("--max-memory-used-percent", type=float, default=90.0)
    parser.add_argument("--min-disk-free-percent", type=float, default=10.0)
    parser.add_argument("--skip-endpoints", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key = args.ssh_key.expanduser().resolve()
    node_reports: list[dict[str, Any]] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(NODES) + 1) as pool:
        futures = {
            pool.submit(probe_node, node, args.ssh_user, key, args.ssh_timeout_sec): node for node in NODES
        }
        futures[pool.submit(probe_local_control, CONTROL_NODE, args.ssh_timeout_sec)] = CONTROL_NODE
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                metrics = future.result()
                errors.extend(
                    evaluate_node(
                        metrics,
                        max_clock_offset_ms=args.max_clock_offset_ms,
                        max_load_per_cpu=args.max_load_per_cpu,
                        max_memory_used_percent=args.max_memory_used_percent,
                        min_disk_free_percent=args.min_disk_free_percent,
                    )
                )
                node_reports.append(metrics)
            except GateError as exc:
                errors.append(str(exc))
                node_reports.append({**asdict(node), "probe_failed": True, "error": str(exc)})
    endpoint_reports = [] if args.skip_endpoints else [
        probe_endpoint(name, url, args.endpoint_timeout_sec) for name, url in ENDPOINTS
    ]
    errors.extend(
        f"{item['name']}: endpoint failed: {item.get('error') or item.get('status')}"
        for item in endpoint_reports
        if not item.get("passed")
    )
    report = {
        "schema_version": 1,
        "sampled_at": utc_now(),
        "passed": not errors,
        "thresholds": {
            "max_clock_offset_ms": args.max_clock_offset_ms,
            "max_load_per_cpu": args.max_load_per_cpu,
            "max_memory_used_percent": args.max_memory_used_percent,
            "min_disk_free_percent": args.min_disk_free_percent,
        },
        "nodes": sorted(node_reports, key=lambda item: item["ip"]),
        "endpoints": endpoint_reports,
        "errors": errors,
    }
    output = args.output.resolve() if args.output else (
        DEFAULT_OUTPUT_DIR / f"collection_gate_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "node_count": len(node_reports),
                "endpoint_count": len(endpoint_reports),
                "errors": errors,
                "output_path": str(output),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
