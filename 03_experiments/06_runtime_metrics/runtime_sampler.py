"""Collect synchronized node, Elasticsearch, and Neo4j runtime samples."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import math
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from check_collection_gate import CONTROL_NODE, NODES, Node  # noqa: E402


REMOTE_SAMPLE = r"""
import json
import os
import subprocess
import time


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=10)
        return result.returncode, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ''


def memory_used_percent():
    values = {}
    with open('/proc/meminfo', encoding='ascii') as handle:
        for line in handle:
            key, value = line.split(':', 1)
            values[key] = int(value.strip().split()[0])
    total = values.get('MemTotal', 0)
    available = values.get('MemAvailable', 0)
    return round(100.0 * (total - available) / total, 3) if total else 100.0


with open('/proc/stat', encoding='ascii') as handle:
    cpu_values = [int(value) for value in handle.readline().split()[1:]]
cpu_total = sum(cpu_values)
cpu_idle = cpu_values[3] + (cpu_values[4] if len(cpu_values) > 4 else 0)

rx_bytes = 0
tx_bytes = 0
with open('/proc/net/dev', encoding='ascii') as handle:
    for line in handle:
        if ':' not in line:
            continue
        interface, values = line.split(':', 1)
        if interface.strip() != 'eth0':
            continue
        fields = values.split()
        rx_bytes += int(fields[0])
        tx_bytes += int(fields[8])

disk_read_sectors = 0
disk_write_sectors = 0
for entry in os.scandir('/sys/class/block'):
    name = entry.name
    if name.startswith(('loop', 'ram', 'zram')) or os.path.exists(os.path.join(entry.path, 'partition')):
        continue
    try:
        fields = open(os.path.join(entry.path, 'stat'), encoding='ascii').read().split()
        disk_read_sectors += int(fields[2])
        disk_write_sectors += int(fields[6])
    except (OSError, ValueError, IndexError):
        pass

ps_rc, process_lines = command(['ps', '-eo', 'args='])
active_playbacks = 0
if ps_rc == 0:
    active_playbacks = sum(
        1 for line in process_lines.splitlines()
        if 'python /app/client_agent.py run-spec' in line
    )

docker_rc, docker_ids = command(['docker', 'ps', '--quiet'])
checkpoint_rc, checkpoint_raw = command([
    'docker', 'exec', 'ott-graph-pipeline-central',
    'cat', '/var/lib/graph-pipeline/checkpoint.json'
])
checkpoint_timestamp = ''
if checkpoint_rc == 0 and checkpoint_raw:
    try:
        checkpoint_timestamp = str(json.loads(checkpoint_raw).get('es_last_timestamp') or '')
    except (ValueError, TypeError):
        pass

print(json.dumps({
    'sample_epoch_ms': time.time_ns() // 1_000_000,
    'hostname': os.uname().nodename,
    'cpu_count': os.cpu_count() or 1,
    'cpu_total_ticks': cpu_total,
    'cpu_idle_ticks': cpu_idle,
    'load_1m': round(os.getloadavg()[0], 4),
    'memory_used_percent': memory_used_percent(),
    'network_rx_bytes': rx_bytes,
    'network_tx_bytes': tx_bytes,
    'disk_read_sectors': disk_read_sectors,
    'disk_write_sectors': disk_write_sectors,
    'active_playback_processes': active_playbacks,
    'running_container_count': len(docker_ids.splitlines()) if docker_rc == 0 and docker_ids else 0,
    'graph_checkpoint_timestamp': checkpoint_timestamp,
}, sort_keys=True))
"""


class RuntimeSampleError(RuntimeError):
    """Raised when a runtime source cannot be sampled."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "-"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    return round(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction, 3)


def percentile_summary(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": round(max(values), 3) if values else None,
    }


def remote_sample_command() -> str:
    encoded = base64.b64encode(REMOTE_SAMPLE.encode("utf-8")).decode("ascii")
    program = f"import base64;exec(base64.b64decode('{encoded}').decode())"
    return f"python3 -c {shlex.quote(program)}"


def probe_remote_node(node: Node, ssh_user: str, ssh_key: Path, timeout_sec: float) -> dict[str, Any]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-i",
        str(ssh_key),
        f"{ssh_user}@{node.ip}",
        remote_sample_command(),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout_sec)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeSampleError(f"{node.name}: runtime SSH probe failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout[-500:]
        raise RuntimeSampleError(f"{node.name}: runtime SSH probe failed: {detail}")
    try:
        sample = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeSampleError(f"{node.name}: invalid runtime probe output") from exc
    sample.update({"name": node.name, "ip": node.ip, "role": node.role})
    return sample


def probe_local_node(node: Node, timeout_sec: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", REMOTE_SAMPLE],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeSampleError(f"{node.name}: local runtime probe failed: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeSampleError(f"{node.name}: local runtime probe failed: {completed.stderr.strip()}")
    try:
        sample = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeSampleError(f"{node.name}: invalid local runtime probe output") from exc
    sample.update({"name": node.name, "ip": node.ip, "role": node.role})
    return sample


def derive_node_rates(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(current)
    result.update(
        {
            "cpu_used_percent": None,
            "network_rx_mbps": None,
            "network_tx_mbps": None,
            "disk_read_mb_per_sec": None,
            "disk_write_mb_per_sec": None,
        }
    )
    if not previous:
        return result
    elapsed = (float(current["sample_epoch_ms"]) - float(previous["sample_epoch_ms"])) / 1000.0
    if elapsed <= 0:
        return result
    total_delta = int(current["cpu_total_ticks"]) - int(previous["cpu_total_ticks"])
    idle_delta = int(current["cpu_idle_ticks"]) - int(previous["cpu_idle_ticks"])
    if total_delta > 0:
        result["cpu_used_percent"] = round(100.0 * (total_delta - idle_delta) / total_delta, 3)
    result["network_rx_mbps"] = round(
        max(0, int(current["network_rx_bytes"]) - int(previous["network_rx_bytes"]))
        * 8.0
        / elapsed
        / 1_000_000.0,
        4,
    )
    result["network_tx_mbps"] = round(
        max(0, int(current["network_tx_bytes"]) - int(previous["network_tx_bytes"]))
        * 8.0
        / elapsed
        / 1_000_000.0,
        4,
    )
    result["disk_read_mb_per_sec"] = round(
        max(0, int(current["disk_read_sectors"]) - int(previous["disk_read_sectors"]))
        * 512.0
        / elapsed
        / 1_000_000.0,
        4,
    )
    result["disk_write_mb_per_sec"] = round(
        max(0, int(current["disk_write_sectors"]) - int(previous["disk_write_sectors"]))
        * 512.0
        / elapsed
        / 1_000_000.0,
        4,
    )
    return result


def http_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_sec: float,
    authorization: str | None = None,
) -> tuple[dict[str, Any], float]:
    headers = {"Content-Type": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeSampleError(f"runtime API request failed for {url}: {exc}") from exc
    return body, round((time.monotonic() - started) * 1000.0, 3)


def event_epoch(source: dict[str, Any]) -> float | None:
    raw_epoch = source.get("event_time_epoch")
    if raw_epoch not in (None, "", "-"):
        try:
            return float(raw_epoch)
        except (TypeError, ValueError):
            pass
    parsed = parse_datetime(source.get("timestamp") or source.get("event.created"))
    return parsed.timestamp() if parsed else None


class RuntimeSampler:
    def __init__(
        self,
        *,
        ssh_user: str,
        ssh_key: Path,
        ssh_timeout_sec: float = 25.0,
        elasticsearch_url: str = "http://192.168.0.120:9200",
        neo4j_http: str = "http://192.168.0.120:7474",
        neo4j_user: str = "neo4j",
        neo4j_password: str = "ottlab1234",
        api_timeout_sec: float = 20.0,
        max_events_per_sample: int = 20000,
    ) -> None:
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key.expanduser().resolve()
        self.ssh_timeout_sec = ssh_timeout_sec
        self.elasticsearch_url = elasticsearch_url.rstrip("/")
        self.neo4j_http = neo4j_http.rstrip("/")
        self.api_timeout_sec = api_timeout_sec
        self.max_events_per_sample = max_events_per_sample
        token = base64.b64encode(f"{neo4j_user}:{neo4j_password}".encode("utf-8")).decode("ascii")
        self.neo4j_authorization = f"Basic {token}"
        self.previous_nodes: dict[str, dict[str, Any]] = {}
        self.previous_sample_at: datetime | None = None
        self.previous_graph_totals: tuple[int, int] | None = None
        self.seen_es_documents: set[str] = set()
        self.seen_graph_requests: set[str] = set()
        self.latest_es_timestamp: datetime | None = None
        self.all_ingest_lag_ms: list[float] = []
        self.all_graph_lag_ms: list[float] = []
        self.event_query_truncated = False

    def _probe_nodes(self) -> tuple[list[dict[str, Any]], list[str]]:
        raw_samples: list[dict[str, Any]] = []
        errors: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(NODES) + 1) as pool:
            futures = {
                pool.submit(
                    probe_remote_node,
                    node,
                    self.ssh_user,
                    self.ssh_key,
                    self.ssh_timeout_sec,
                ): node
                for node in NODES
            }
            futures[pool.submit(probe_local_node, CONTROL_NODE, self.ssh_timeout_sec)] = CONTROL_NODE
            for future in concurrent.futures.as_completed(futures):
                node = futures[future]
                try:
                    raw_samples.append(future.result())
                except RuntimeSampleError as exc:
                    errors.append(str(exc))
                    raw_samples.append({"name": node.name, "ip": node.ip, "role": node.role, "error": str(exc)})
        samples: list[dict[str, Any]] = []
        for raw in sorted(raw_samples, key=lambda item: item["ip"]):
            if "error" in raw:
                samples.append(raw)
                continue
            samples.append(derive_node_rates(raw, self.previous_nodes.get(raw["name"])))
            self.previous_nodes[raw["name"]] = raw
        return samples, errors

    def _query_elasticsearch(self, since: datetime, until: datetime) -> dict[str, Any]:
        overlap_since = since - timedelta(seconds=2)
        payload = {
            "size": self.max_events_per_sample,
            "track_total_hits": True,
            "_source": [
                "@timestamp",
                "timestamp",
                "event.created",
                "event_time_epoch",
                "bytes_sent",
                "status",
                "http.response.status_code",
                "cache_status",
                "edge_server",
                "edge_service",
            ],
            "sort": [{"@timestamp": {"order": "asc", "unmapped_type": "date"}}],
            "query": {
                "range": {
                    "@timestamp": {
                        "gte": overlap_since.isoformat().replace("+00:00", "Z"),
                        "lte": until.isoformat().replace("+00:00", "Z"),
                    }
                }
            },
        }
        body, latency_ms = http_json(
            f"{self.elasticsearch_url}/access-gateway-nginx-*,ott-api-events-*/_search",
            payload,
            timeout_sec=self.api_timeout_sec,
        )
        hits_block = body.get("hits", {})
        total = hits_block.get("total", 0)
        total_value = int(total.get("value", 0) if isinstance(total, dict) else total or 0)
        hits = list(hits_block.get("hits", []))
        truncated = total_value > len(hits)
        self.event_query_truncated = self.event_query_truncated or truncated
        edge_count = 0
        api_count = 0
        bytes_total = 0
        status_4xx = 0
        cache_statuses: Counter[str] = Counter()
        edge_ids: Counter[str] = Counter()
        ingest_lags: list[float] = []
        for hit in hits:
            key = f"{hit.get('_index', '')}:{hit.get('_id', '')}"
            if key in self.seen_es_documents:
                continue
            self.seen_es_documents.add(key)
            source = hit.get("_source", {})
            ingested = parse_datetime(source.get("@timestamp"))
            if ingested and (self.latest_es_timestamp is None or ingested > self.latest_es_timestamp):
                self.latest_es_timestamp = ingested
            event_time = event_epoch(source)
            if ingested and event_time is not None:
                ingest_lags.append((ingested.timestamp() - event_time) * 1000.0)
            index_name = str(hit.get("_index") or "")
            if "access-gateway-nginx" in index_name:
                edge_count += 1
                try:
                    bytes_total += int(source.get("bytes_sent") or 0)
                except (TypeError, ValueError):
                    pass
                raw_status = source.get("status")
                if raw_status is None:
                    raw_status = source.get("http.response.status_code")
                try:
                    status_4xx += 1 if 400 <= int(raw_status) < 500 else 0
                except (TypeError, ValueError):
                    pass
                cache_statuses[str(source.get("cache_status") or "UNKNOWN").upper()] += 1
                edge_ids[str(source.get("edge_server") or source.get("edge_service") or "unknown")] += 1
            else:
                api_count += 1
        self.all_ingest_lag_ms.extend(ingest_lags)
        interval = max(0.001, (until - since).total_seconds())
        cache_denominator = cache_statuses.get("HIT", 0) + cache_statuses.get("MISS", 0)
        return {
            "query_latency_ms": latency_ms,
            "query_total_hits_with_overlap": total_value,
            "new_document_count": edge_count + api_count,
            "edge_document_count": edge_count,
            "api_document_count": api_count,
            "edge_requests_per_sec": round(edge_count / interval, 4),
            "edge_throughput_mbps": round(bytes_total * 8.0 / interval / 1_000_000.0, 4),
            "edge_bytes": bytes_total,
            "status_4xx_count": status_4xx,
            "cache_hit_ratio": (
                round(cache_statuses.get("HIT", 0) / cache_denominator, 6)
                if cache_denominator
                else None
            ),
            "cache_status_counts": dict(sorted(cache_statuses.items())),
            "edge_counts": dict(sorted(edge_ids.items())),
            "ingest_lag_ms": percentile_summary(ingest_lags),
            "truncated": truncated,
        }

    def _query_neo4j(self, since: datetime, interval_sec: float) -> dict[str, Any]:
        statement_payload = {
            "statements": [
                {"statement": "MATCH (r:Request) RETURN count(r) AS count"},
                {"statement": "MATCH (vs:ViewingSession) RETURN count(vs) AS count"},
                {
                    "statement": (
                        "MATCH (r:Request) "
                        "WHERE r.graph_ingested_at >= datetime($since) "
                        "RETURN r.request_id AS request_id, "
                        "r.timestamp.epochMillis AS event_ms, "
                        "r.graph_ingested_at.epochMillis AS graph_ms "
                        "LIMIT $limit"
                    ),
                    "parameters": {
                        "since": (since - timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
                        "limit": self.max_events_per_sample,
                    },
                },
            ]
        }
        body, latency_ms = http_json(
            f"{self.neo4j_http}/db/neo4j/tx/commit",
            statement_payload,
            timeout_sec=self.api_timeout_sec,
            authorization=self.neo4j_authorization,
        )
        if body.get("errors"):
            raise RuntimeSampleError(f"Neo4j runtime query failed: {body['errors']}")
        results = body.get("results", [])
        if len(results) != 3:
            raise RuntimeSampleError("Neo4j runtime query returned an incomplete response")
        request_total = int(results[0]["data"][0]["row"][0])
        session_total = int(results[1]["data"][0]["row"][0])
        graph_lags: list[float] = []
        for item in results[2].get("data", []):
            request_id, event_ms, graph_ms = item.get("row", [None, None, None])
            if not request_id or request_id in self.seen_graph_requests:
                continue
            self.seen_graph_requests.add(str(request_id))
            if event_ms is not None and graph_ms is not None:
                graph_lags.append(float(graph_ms) - float(event_ms))
        self.all_graph_lag_ms.extend(graph_lags)
        request_rate = None
        session_rate = None
        if self.previous_graph_totals is not None and interval_sec > 0:
            request_rate = round(max(0, request_total - self.previous_graph_totals[0]) / interval_sec, 4)
            session_rate = round(max(0, session_total - self.previous_graph_totals[1]) / interval_sec, 4)
        self.previous_graph_totals = (request_total, session_total)
        return {
            "query_latency_ms": latency_ms,
            "request_node_count": request_total,
            "viewing_session_node_count": session_total,
            "request_upserts_per_sec": request_rate,
            "session_upserts_per_sec": session_rate,
            "new_graph_latency_count": len(graph_lags),
            "event_to_graph_lag_ms": percentile_summary(graph_lags),
        }

    def sample(self, *, phase: str, workload: dict[str, Any] | None = None) -> dict[str, Any]:
        sampled_at = datetime.now(timezone.utc)
        since = self.previous_sample_at or (sampled_at - timedelta(seconds=2))
        interval_sec = max(0.001, (sampled_at - since).total_seconds())
        nodes, node_errors = self._probe_nodes()
        source_errors = list(node_errors)
        try:
            elasticsearch = self._query_elasticsearch(since, sampled_at)
        except RuntimeSampleError as exc:
            source_errors.append(str(exc))
            elasticsearch = {"error": str(exc)}
        try:
            neo4j = self._query_neo4j(since, interval_sec)
        except RuntimeSampleError as exc:
            source_errors.append(str(exc))
            neo4j = {"error": str(exc)}

        active_clients = sum(
            int(node.get("active_playback_processes") or 0)
            for node in nodes
            if node.get("role") == "client"
        )
        processing = next((node for node in nodes if node.get("role") == "processing"), {})
        checkpoint = parse_datetime(processing.get("graph_checkpoint_timestamp"))
        cursor_lag = None
        if checkpoint and self.latest_es_timestamp:
            cursor_lag = round(max(0.0, (self.latest_es_timestamp - checkpoint).total_seconds()), 3)
        sample = {
            "sampled_at": sampled_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "interval_sec": round(interval_sec, 3),
            "phase": phase,
            "workload": dict(workload or {}),
            "achieved_active_clients": active_clients,
            "graph_checkpoint_timestamp": processing.get("graph_checkpoint_timestamp") or None,
            "graph_cursor_lag_sec": cursor_lag,
            "nodes": nodes,
            "elasticsearch": elasticsearch,
            "neo4j": neo4j,
            "errors": source_errors,
        }
        self.previous_sample_at = sampled_at
        return sample

    def query_token_coverage(self, token_ids: list[str]) -> dict[str, Any]:
        unique_ids = sorted(set(token_ids))
        if not unique_ids:
            return {"expected_token_count": 0, "graph_token_count": 0, "tokens_with_segments": 0}
        payload = {
            "statements": [
                {
                    "statement": (
                        "UNWIND $token_ids AS token_id "
                        "OPTIONAL MATCH (tok:CdnToken {cdn_token_id: token_id}) "
                        "OPTIONAL MATCH (tok)<-[:USES_CDN_TOKEN]-(vs:ViewingSession)"
                        "-[:MAKES_REQUEST]->(r:Request {kind: 'hls_segment'}) "
                        "WITH token_id, tok, count(r) AS segment_count "
                        "RETURN count(tok) AS graph_token_count, "
                        "sum(CASE WHEN segment_count > 0 THEN 1 ELSE 0 END) AS tokens_with_segments"
                    ),
                    "parameters": {"token_ids": unique_ids},
                }
            ]
        }
        body, latency_ms = http_json(
            f"{self.neo4j_http}/db/neo4j/tx/commit",
            payload,
            timeout_sec=self.api_timeout_sec,
            authorization=self.neo4j_authorization,
        )
        if body.get("errors"):
            raise RuntimeSampleError(f"Neo4j token coverage query failed: {body['errors']}")
        row = body["results"][0]["data"][0]["row"]
        return {
            "expected_token_count": len(unique_ids),
            "graph_token_count": int(row[0]),
            "tokens_with_segments": int(row[1]),
            "query_latency_ms": latency_ms,
        }

    def aggregate_lags(self) -> dict[str, Any]:
        return {
            "ingest_lag_ms": percentile_summary(self.all_ingest_lag_ms),
            "event_to_graph_lag_ms": percentile_summary(self.all_graph_lag_ms),
            "event_query_truncated": self.event_query_truncated,
        }
