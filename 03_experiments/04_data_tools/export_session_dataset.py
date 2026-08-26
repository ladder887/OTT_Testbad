"""Export labeled ViewingSession features by joining manifests to Neo4j tokens."""

from __future__ import annotations

import argparse
import base64
import csv
import glob
import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "06_outputs" / "02_datasets" / "session_features.csv"

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
METADATA_COLUMNS = (
    "sample_id",
    "run_id",
    "scenario_id",
    "attack_family",
    "label_binary",
    "cdn_token_id",
    "viewing_session_id",
    "logical_client_id",
    "physical_host_id",
    "content_id",
    "content_type",
    "client_ip",
    "start_time",
    "end_time",
)
FORBIDDEN_MODEL_FIELDS = {
    "scenario_id",
    "attack_family",
    "label_binary",
    "run_id",
    "logical_client_id",
    "physical_host_id",
    "network_profile_id",
    "cdn_token_id",
    "viewing_session_id",
    "client_ip",
    "content_id",
}


class ExportError(RuntimeError):
    """Raised when dataset export cannot satisfy its data contract."""


def basic_authorization(user: str, password: str) -> str:
    encoded = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def neo4j_query(
    base_url: str,
    user: str,
    password: str,
    statement: str,
    parameters: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = {"statements": [{"statement": statement, "parameters": parameters}]}
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/db/neo4j/tx/commit",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": basic_authorization(user, password),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ExportError(f"Neo4j query failed: {exc}") from exc
    if result.get("errors"):
        raise ExportError(f"Neo4j query failed: {result['errors']}")
    query_result = (result.get("results") or [{}])[0]
    columns = query_result.get("columns", [])
    return [dict(zip(columns, item.get("row", []))) for item in query_result.get("data", [])]


def expand_manifest_paths(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        candidate = Path(value)
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("*.json")))
            continue
        matches = [Path(item) for item in glob.glob(value)]
        if matches:
            paths.extend(sorted(matches))
        elif candidate.exists():
            paths.append(candidate)
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved.name.endswith(".validation.json") or resolved in unique:
            continue
        unique.append(resolved)
    if not unique:
        raise ExportError("no run manifest files matched")
    return unique


def load_manifests(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    token_labels: dict[str, dict[str, Any]] = {}
    ip_clients: dict[str, dict[str, Any]] = {}
    for path in paths:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("status") != "completed":
            continue
        selected = manifest.get("parameters", {}).get("selected_clients", [])
        for client in selected:
            source_ip = str(client.get("source_ip") or "")
            if source_ip:
                ip_clients[source_ip] = {
                    "logical_client_id": str(client.get("logical_client_id") or ""),
                    "physical_host_id": str(client.get("physical_host_id") or ""),
                }
        for binding in manifest.get("token_bindings", []):
            token_id = str(binding.get("cdn_token_id") or "")
            if not token_id:
                continue
            if token_id in token_labels:
                raise ExportError(f"duplicate token binding across manifests: {token_id}")
            token_labels[token_id] = {
                "run_id": str(manifest.get("run_id") or ""),
                "scenario_id": str(manifest.get("scenario_id") or ""),
                "attack_family": str(manifest.get("attack_family") or ""),
                "label_binary": 0 if str(manifest.get("scenario_id") or "").startswith("N") else 1,
            }
    if not token_labels:
        raise ExportError("completed manifests contain no token bindings")
    return token_labels, ip_clients


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "-"):
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def request_kind(request: dict[str, Any]) -> str:
    return str(request.get("kind") or "")


def rendition_from_path(path: str) -> str:
    match = re.search(r"/(\d{3,4}p)/", path)
    return match.group(1) if match else ""


def segment_index_from_path(path: str) -> int | None:
    match = re.search(r"(?:seg(?:ment)?[_-]?)(\d+)\.ts(?:$|\?)", path, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def max_concurrency(intervals: list[tuple[datetime, datetime]]) -> int:
    events: list[tuple[datetime, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    active = 0
    maximum = 0
    for _, delta in sorted(events, key=lambda item: (item[0], -item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def build_rows(
    graph_rows: list[dict[str, Any]],
    token_labels: dict[str, dict[str, Any]],
    ip_clients: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for graph_row in graph_rows:
        token_id = str(graph_row.get("cdn_token_id") or "")
        session = dict(graph_row.get("session") or {})
        requests = [dict(item) for item in (graph_row.get("requests") or []) if isinstance(item, dict)]
        segment_requests = [item for item in requests if request_kind(item) == "hls_segment"]
        if not segment_requests:
            continue
        start = parse_datetime(session.get("start_time"))
        end = parse_datetime(session.get("end_time")) or start
        if start is None or end is None:
            continue
        entry = {
            "cdn_token_id": token_id,
            "session": session,
            "requests": requests,
            "segment_requests": segment_requests,
            "start": start,
            "end": end,
        }
        prepared.append(entry)
        by_token[token_id].append(entry)

    token_metrics: dict[str, dict[str, int]] = {}
    for token_id, entries in by_token.items():
        token_metrics[token_id] = {
            "token_session_count": len(entries),
            "token_unique_ips": len(
                {
                    str(item["session"].get("client_ip") or "")
                    for item in entries
                    if item["session"].get("client_ip")
                }
            ),
            "token_unique_devices": len(
                {
                    str(item["session"].get("observed_device_id") or "")
                    for item in entries
                    if item["session"].get("observed_device_id")
                }
            ),
            "token_concurrent_consumers_max": max_concurrency(
                [(item["start"], item["end"]) for item in entries]
            ),
        }

    dataset_rows: list[dict[str, Any]] = []
    for entry in prepared:
        token_id = entry["cdn_token_id"]
        label = token_labels.get(token_id)
        if label is None:
            continue
        session = entry["session"]
        requests = entry["requests"]
        segments = sorted(
            entry["segment_requests"],
            key=lambda item: parse_datetime(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        timestamps = [parse_datetime(item.get("timestamp")) for item in segments]
        valid_times = [item for item in timestamps if item is not None]
        intervals = [
            (valid_times[index] - valid_times[index - 1]).total_seconds()
            for index in range(1, len(valid_times))
        ]
        same_interval_count = sum(
            1
            for index in range(1, len(intervals))
            if math.isclose(intervals[index], intervals[index - 1], abs_tol=0.25)
        )
        renditions = [rendition_from_path(str(item.get("path") or "")) for item in segments]
        rendition_switches = sum(
            1
            for before, after in zip(renditions, renditions[1:])
            if before and after and before != after
        )
        indices = [segment_index_from_path(str(item.get("path") or "")) for item in segments]
        gaps = sum(
            1
            for before, after in zip(indices, indices[1:])
            if before is not None and after is not None and abs(after - before) != 1
        )
        client_ip = str(session.get("client_ip") or "")
        client_meta = ip_clients.get(client_ip, {})
        session_id = str(session.get("viewing_session_id") or "")
        row = {
            "sample_id": f"{label['run_id']}:{session_id}",
            **label,
            "cdn_token_id": token_id,
            "viewing_session_id": session_id,
            "logical_client_id": client_meta.get("logical_client_id", ""),
            "physical_host_id": client_meta.get("physical_host_id", ""),
            "content_id": str(session.get("content_id") or ""),
            "content_type": str(session.get("content_type") or ""),
            "client_ip": client_ip,
            "start_time": entry["start"].isoformat().replace("+00:00", "Z"),
            "end_time": entry["end"].isoformat().replace("+00:00", "Z"),
            "request_count": len(requests),
            "manifest_request_count": sum(1 for item in requests if request_kind(item) == "hls_manifest"),
            "segment_bytes_total": sum(int(item.get("bytes") or 0) for item in segments),
            "avg_segment_interval_sec": round(sum(intervals) / len(intervals), 6) if intervals else 0.0,
            "consecutive_same_interval_count": same_interval_count,
            "segment_count": len(segments),
            "status_4xx_count": sum(1 for item in requests if 400 <= int(item.get("status") or 0) < 500),
            "rendition_switch_count": rendition_switches,
            "segment_index_gap_count": gaps,
            **token_metrics[token_id],
        }
        dataset_rows.append(row)
    return sorted(dataset_rows, key=lambda item: (item["run_id"], item["sample_id"]))


def query_graph_sessions(
    base_url: str,
    user: str,
    password: str,
    token_ids: list[str],
) -> list[dict[str, Any]]:
    statement = """
    UNWIND $token_ids AS token_id
    MATCH (token:CdnToken {cdn_token_id: token_id})
    MATCH (session:ViewingSession)-[:USES_CDN_TOKEN]->(token)
    OPTIONAL MATCH (session)-[:FROM_IP]->(ip:ClientIP)
    OPTIONAL MATCH (session)-[:ON_DEVICE]->(device:Device)
    OPTIONAL MATCH (session)-[:MAKES_REQUEST]->(request:Request)
    WITH token_id, session,
         head(collect(DISTINCT ip.ip_address)) AS client_ip,
         head(collect(DISTINCT device.device_id)) AS observed_device_id,
         collect(DISTINCT {
        request_id: request.request_id,
        timestamp: toString(request.timestamp),
        kind: request.kind,
        path: request.path,
        status: request.status,
        bytes: request.bytes_sent,
        edge_id: request.edge_id,
        client_ip: request.client_ip,
        observed_device_id: request.observed_device_id
    }) AS requests
    RETURN token_id AS cdn_token_id,
           session {
               .*,
               client_ip: client_ip,
               observed_device_id: observed_device_id
           } AS session,
           requests
    ORDER BY token_id, session.start_time
    """
    return neo4j_query(base_url, user, password, statement, {"token_ids": token_ids})


def write_dataset(rows: list[dict[str, Any]], output: Path, manifests: list[Path]) -> Path:
    if not rows:
        raise ExportError("no segment-bearing ViewingSession samples were joined to the manifests")
    leaked_features = set(FEATURE_COLUMNS).intersection(FORBIDDEN_MODEL_FIELDS)
    if leaked_features:
        raise ExportError(f"forbidden model fields in feature allowlist: {sorted(leaked_features)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = list(METADATA_COLUMNS) + list(FEATURE_COLUMNS)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    metadata = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_path": str(output),
        "sha256": digest,
        "row_count": len(rows),
        "normal_rows": sum(1 for item in rows if int(item["label_binary"]) == 0),
        "attack_rows": sum(1 for item in rows if int(item["label_binary"]) == 1),
        "feature_columns": list(FEATURE_COLUMNS),
        "metadata_columns": list(METADATA_COLUMNS),
        "manifest_paths": [str(path) for path in manifests],
    }
    metadata_path = output.with_suffix(".metadata.json")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return metadata_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifests", nargs="+", required=True, help="manifest files, directories, or glob patterns")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--neo4j-http", default="http://192.168.0.120:7474")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="ottlab1234")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifests = expand_manifest_paths(args.manifests)
        token_labels, ip_clients = load_manifests(manifests)
        graph_rows = query_graph_sessions(
            args.neo4j_http,
            args.neo4j_user,
            args.neo4j_password,
            sorted(token_labels),
        )
        rows = build_rows(graph_rows, token_labels, ip_clients)
        output = args.output.resolve()
        metadata_path = write_dataset(rows, output, manifests)
    except (ExportError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"dataset export failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "dataset_path": str(output),
                "metadata_path": str(metadata_path),
                "row_count": len(rows),
                "normal_rows": sum(1 for item in rows if int(item["label_binary"]) == 0),
                "attack_rows": sum(1 for item in rows if int(item["label_binary"]) == 1),
                "feature_columns": list(FEATURE_COLUMNS),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
