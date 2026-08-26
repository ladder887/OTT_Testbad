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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "06_outputs" / "02_datasets" / "session_features.csv"

F0_F1_FEATURE_COLUMNS = (
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
F2_RELATION_FEATURE_COLUMNS = (
    "account_session_count_10m",
    "account_active_sessions_max_10m",
    "account_unique_devices_10m",
    "account_unique_ips_10m",
    "account_unique_tokens_10m",
    "account_unique_contents_10m",
    "content_session_count_10m",
    "content_concurrent_sessions_max_10m",
    "content_unique_accounts_10m",
    "content_unique_devices_10m",
    "content_unique_ips_10m",
)
F3_BEHAVIOR_FEATURE_COLUMNS = (
    "segment_interval_stddev_sec",
    "segment_interval_p95_sec",
    "segment_interval_cv",
    "segment_request_burst_1s_max",
    "segment_request_burst_5s_max",
    "segment_request_concurrency_max",
    "unique_segment_count",
    "segment_span",
    "segment_duplicate_ratio",
    "segment_skipped_ratio",
    "segment_out_of_order_ratio",
    "content_unique_segments_10m",
    "content_segment_span_10m",
    "content_segment_duplicate_ratio_10m",
    "content_segment_range_fill_ratio_10m",
    "manifest_poll_interval_avg_sec",
    "manifest_poll_interval_stddev_sec",
)
INFRASTRUCTURE_CONTEXT_COLUMNS = (
    "response_time_avg_ms",
    "response_time_p95_ms",
    "cache_hit_ratio",
)
F4_LIFECYCLE_FEATURE_COLUMNS = (
    "token_age_at_session_start_sec",
    "token_age_at_session_end_sec",
    "token_ttl_remaining_at_session_end_sec",
)
FEATURE_COLUMNS = (
    F0_F1_FEATURE_COLUMNS
    + F2_RELATION_FEATURE_COLUMNS
    + F3_BEHAVIOR_FEATURE_COLUMNS
    + F4_LIFECYCLE_FEATURE_COLUMNS
)
METADATA_COLUMNS = (
    "sample_id",
    "run_id",
    "dataset_prefix",
    "data_split",
    "collection_matrix_id",
    "matrix_run_key",
    "scenario_id",
    "scenario_variant",
    "attack_family",
    "label_binary",
    "cdn_token_id",
    "viewing_session_id",
    "logical_client_id",
    "physical_host_id",
    "account_id",
    "device_id",
    "content_id",
    "content_type",
    "client_ip",
    "edge_id",
    "network_profile_id",
    "cache_state",
    "timing_scaled",
    "start_time",
    "end_time",
)
FORBIDDEN_MODEL_FIELDS = {
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
        if resolved.name.endswith(
            (".validation.json", ".execution.json", ".campaign_state.json")
        ) or resolved in unique:
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
        parameters = manifest.get("parameters", {})
        selected = parameters.get("selected_clients", [])
        for client in selected:
            source_ip = str(client.get("source_ip") or "")
            if source_ip:
                ip_clients[source_ip] = {
                    "logical_client_id": str(client.get("logical_client_id") or ""),
                    "physical_host_id": str(client.get("physical_host_id") or ""),
                    "edge_id": str(client.get("edge_id") or ""),
                    "network_profile_id": str(client.get("network_profile_id") or ""),
                }
        for binding in manifest.get("token_bindings", []):
            token_id = str(binding.get("cdn_token_id") or "")
            if not token_id:
                continue
            if token_id in token_labels:
                raise ExportError(f"duplicate token binding across manifests: {token_id}")
            token_labels[token_id] = {
                "run_id": str(manifest.get("run_id") or ""),
                "dataset_prefix": str(manifest.get("dataset_prefix") or ""),
                "data_split": str(parameters.get("data_split") or ""),
                "collection_matrix_id": str(parameters.get("collection_matrix_id") or ""),
                "matrix_run_key": str(parameters.get("matrix_run_key") or ""),
                "scenario_id": str(manifest.get("scenario_id") or ""),
                "scenario_variant": str(parameters.get("scenario_variant") or ""),
                "attack_family": str(manifest.get("attack_family") or ""),
                "label_binary": 0 if str(manifest.get("scenario_id") or "").startswith("N") else 1,
                "cache_state": str(parameters.get("cache_state") or ""),
                "timing_scaled": bool(parameters.get("timing_scaled", False)),
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


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def population_stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    average = mean(values)
    return math.sqrt(sum((item - average) ** 2 for item in values) / len(values))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def max_events_in_window(timestamps: list[datetime], window_sec: float) -> int:
    if not timestamps:
        return 0
    ordered = sorted(timestamps)
    left = 0
    maximum = 0
    for right, timestamp in enumerate(ordered):
        while (timestamp - ordered[left]).total_seconds() > window_sec:
            left += 1
        maximum = max(maximum, right - left + 1)
    return maximum


def segment_identity(request: dict[str, Any]) -> tuple[str, int] | None:
    path = str(request.get("path") or "")
    index = segment_index_from_path(path)
    if index is None:
        return None
    return rendition_from_path(path), index


def segment_sequence_metrics(requests: list[dict[str, Any]]) -> dict[str, float | int]:
    identities = [segment_identity(item) for item in requests]
    valid = [item for item in identities if item is not None]
    indices = [item[1] for item in valid]
    unique = set(valid)
    duplicate_count = max(0, len(valid) - len(unique))
    skipped = sum(1 for before, after in zip(indices, indices[1:]) if after - before > 1)
    out_of_order = sum(1 for before, after in zip(indices, indices[1:]) if after < before)
    segment_span = max(indices) - min(indices) + 1 if indices else 0
    transitions = max(0, len(indices) - 1)
    return {
        "unique_segment_count": len(unique),
        "segment_span": segment_span,
        "segment_duplicate_ratio": safe_ratio(duplicate_count, len(valid)),
        "segment_skipped_ratio": safe_ratio(skipped, transitions),
        "segment_out_of_order_ratio": safe_ratio(out_of_order, transitions),
    }


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


def request_concurrency(requests: list[dict[str, Any]]) -> int:
    intervals: list[tuple[datetime, datetime]] = []
    for request in requests:
        ended = parse_datetime(request.get("timestamp"))
        if ended is None:
            continue
        duration_ms = max(0.0, safe_float(request.get("response_time_ms")))
        started = ended - timedelta(milliseconds=duration_ms)
        intervals.append((started, ended))
    return max_concurrency(intervals)


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
            "account_id": str(session.get("account_id") or ""),
            "content_id": str(session.get("content_id") or ""),
            "client_ip": str(session.get("client_ip") or ""),
            "device_id": str(session.get("observed_device_id") or ""),
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

    def trailing_entries(entry: dict[str, Any], field: str) -> list[dict[str, Any]]:
        value = entry[field]
        if not value:
            return []
        window_start = entry["end"] - timedelta(minutes=10)
        return [
            candidate
            for candidate in prepared
            if candidate[field] == value
            and candidate["start"] <= entry["end"]
            and candidate["end"] >= window_start
        ]

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
        account_entries = trailing_entries(entry, "account_id")
        content_entries = trailing_entries(entry, "content_id")
        window_start = entry["end"] - timedelta(minutes=10)
        content_window_segments = [
            request
            for candidate in content_entries
            for request in candidate["segment_requests"]
            if (
                (timestamp := parse_datetime(request.get("timestamp"))) is not None
                and window_start <= timestamp <= entry["end"]
            )
        ]
        content_identities = [
            identity
            for request in content_window_segments
            if (identity := segment_identity(request)) is not None
        ]
        content_indices = [identity[1] for identity in content_identities]
        content_unique_identities = set(content_identities)
        content_unique_indices = set(content_indices)
        content_span = max(content_indices) - min(content_indices) + 1 if content_indices else 0

        segment_metrics = segment_sequence_metrics(segments)
        segment_times = [item for item in valid_times]
        manifest_times = sorted(
            timestamp
            for request in requests
            if request_kind(request) == "hls_manifest"
            and (timestamp := parse_datetime(request.get("timestamp"))) is not None
        )
        manifest_intervals = [
            (after - before).total_seconds()
            for before, after in zip(manifest_times, manifest_times[1:])
        ]
        response_times = [max(0.0, safe_float(item.get("response_time_ms"))) for item in segments]
        known_cache = [
            str(item.get("cache_status") or "").upper()
            for item in segments
            if str(item.get("cache_status") or "").upper() not in {"", "-"}
        ]
        issued_at = parse_datetime(session.get("token_issued_at"))
        ttl_remaining_values = [
            safe_float(item.get("token_ttl_remaining_sec"))
            for item in segments
            if item.get("token_ttl_remaining_sec") not in (None, "", "-")
        ]
        actual_edge_id = str(session.get("edge_id") or "") or next(
            (str(item.get("edge_id") or "") for item in segments if item.get("edge_id")),
            str(client_meta.get("edge_id") or ""),
        )
        row = {
            "sample_id": f"{label['run_id']}:{session_id}",
            **label,
            "cdn_token_id": token_id,
            "viewing_session_id": session_id,
            "logical_client_id": client_meta.get("logical_client_id", ""),
            "physical_host_id": client_meta.get("physical_host_id", ""),
            "account_id": entry["account_id"],
            "device_id": entry["device_id"],
            "content_id": str(session.get("content_id") or ""),
            "content_type": str(session.get("content_type") or ""),
            "client_ip": client_ip,
            "edge_id": actual_edge_id,
            "network_profile_id": client_meta.get("network_profile_id", ""),
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
            "account_session_count_10m": len(account_entries),
            "account_active_sessions_max_10m": max_concurrency(
                [(item["start"], item["end"]) for item in account_entries]
            ),
            "account_unique_devices_10m": len({item["device_id"] for item in account_entries if item["device_id"]}),
            "account_unique_ips_10m": len({item["client_ip"] for item in account_entries if item["client_ip"]}),
            "account_unique_tokens_10m": len({item["cdn_token_id"] for item in account_entries if item["cdn_token_id"]}),
            "account_unique_contents_10m": len({item["content_id"] for item in account_entries if item["content_id"]}),
            "content_session_count_10m": len(content_entries),
            "content_concurrent_sessions_max_10m": max_concurrency(
                [(item["start"], item["end"]) for item in content_entries]
            ),
            "content_unique_accounts_10m": len({item["account_id"] for item in content_entries if item["account_id"]}),
            "content_unique_devices_10m": len({item["device_id"] for item in content_entries if item["device_id"]}),
            "content_unique_ips_10m": len({item["client_ip"] for item in content_entries if item["client_ip"]}),
            "segment_interval_stddev_sec": round(population_stddev(intervals), 6),
            "segment_interval_p95_sec": round(percentile(intervals, 0.95), 6),
            "segment_interval_cv": round(safe_ratio(population_stddev(intervals), mean(intervals)), 6),
            "segment_request_burst_1s_max": max_events_in_window(segment_times, 1.0),
            "segment_request_burst_5s_max": max_events_in_window(segment_times, 5.0),
            "segment_request_concurrency_max": request_concurrency(segments),
            **{key: round(value, 6) if isinstance(value, float) else value for key, value in segment_metrics.items()},
            "content_unique_segments_10m": len(content_unique_identities),
            "content_segment_span_10m": content_span,
            "content_segment_duplicate_ratio_10m": round(
                safe_ratio(len(content_identities) - len(content_unique_identities), len(content_identities)),
                6,
            ),
            "content_segment_range_fill_ratio_10m": round(
                safe_ratio(len(content_unique_indices), content_span),
                6,
            ),
            "manifest_poll_interval_avg_sec": round(mean(manifest_intervals), 6),
            "manifest_poll_interval_stddev_sec": round(population_stddev(manifest_intervals), 6),
            "response_time_avg_ms": round(mean(response_times), 6),
            "response_time_p95_ms": round(percentile(response_times, 0.95), 6),
            "cache_hit_ratio": round(
                safe_ratio(sum(1 for value in known_cache if value == "HIT"), len(known_cache)),
                6,
            ),
            "token_age_at_session_start_sec": round(
                max(0.0, (entry["start"] - issued_at).total_seconds()) if issued_at else 0.0,
                6,
            ),
            "token_age_at_session_end_sec": round(
                max(0.0, (entry["end"] - issued_at).total_seconds()) if issued_at else 0.0,
                6,
            ),
            "token_ttl_remaining_at_session_end_sec": round(
                ttl_remaining_values[-1] if ttl_remaining_values else 0.0,
                6,
            ),
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
    OPTIONAL MATCH (session)-[:SERVED_BY]->(edge:Edge)
    OPTIONAL MATCH (session)-[:MAKES_REQUEST]->(request:Request)
    WITH token_id, session,
         head(collect(DISTINCT ip.ip_address)) AS client_ip,
         head(collect(DISTINCT device.device_id)) AS observed_device_id,
         head(collect(DISTINCT edge.edge_id)) AS edge_id,
         collect(DISTINCT {
        request_id: request.request_id,
        timestamp: toString(request.timestamp),
        kind: request.kind,
        path: request.path,
        status: request.status,
        bytes: request.bytes_sent,
        edge_id: request.edge_id,
        response_time_ms: request.response_time_ms,
        cache_status: request.cache_status,
        token_ttl_remaining_sec: request.token_ttl_remaining_sec,
        client_ip: request.client_ip,
        observed_device_id: request.observed_device_id
    }) AS requests
    RETURN token_id AS cdn_token_id,
           session {
               .*,
               client_ip: client_ip,
               observed_device_id: observed_device_id,
               edge_id: edge_id
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
    columns = (
        list(METADATA_COLUMNS)
        + list(FEATURE_COLUMNS)
        + list(INFRASTRUCTURE_CONTEXT_COLUMNS)
    )
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
        "infrastructure_context_columns": list(INFRASTRUCTURE_CONTEXT_COLUMNS),
        "feature_groups": {
            "F0_F1": list(F0_F1_FEATURE_COLUMNS),
            "F2_relation": list(F2_RELATION_FEATURE_COLUMNS),
            "F3_behavior": list(F3_BEHAVIOR_FEATURE_COLUMNS),
            "F4_lifecycle": list(F4_LIFECYCLE_FEATURE_COLUMNS),
        },
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
