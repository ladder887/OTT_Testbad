"""Validate one run manifest against Elasticsearch and the Neo4j graph."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = REPOSITORY_ROOT / "03_experiments" / "07_generated" / "logical_clients.json"
FORBIDDEN_FIELDS = {
    "scenario_id",
    "run_id",
    "label",
    "dataset_label",
    "logical_client_id",
    "physical_host_id",
    "network_profile_id",
    "token_scenario_id",
    "token_run_id",
    "token_label",
    "token_dataset_label",
    "token_logical_client_id",
    "token_physical_host_id",
    "token_network_profile_id",
}


class ValidationError(RuntimeError):
    """Raised when a remote validation query fails."""


def json_request(
    url: str,
    payload: dict[str, Any],
    *,
    authorization: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValidationError(f"request failed for {url}: {exc}") from exc


def expected_cdn_token_id(token_jti: str) -> str:
    return f"cdn_{hashlib.sha256(token_jti.encode('utf-8')).hexdigest()[:24]}"


def load_ip_mapping(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row["logical_client_id"]): str(row["source_ip"])
        for row in payload.get("clients", [])
    }


def query_elasticsearch(base_url: str, token_ids: list[str]) -> list[dict[str, Any]]:
    url = (
        f"{base_url.rstrip('/')}/"
        "access-gateway-nginx-*,ott-api-events-*/_search?allow_no_indices=true&ignore_unavailable=true"
    )
    payload = {
        "size": 10000,
        "sort": [{"@timestamp": {"order": "asc", "unmapped_type": "date"}}],
        "query": {"terms": {"cdn_token_id": token_ids}},
    }
    response = json_request(url, payload)
    return [hit.get("_source", {}) for hit in response.get("hits", {}).get("hits", [])]


def neo4j_authorization(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def query_neo4j(
    base_url: str,
    user: str,
    password: str,
    token_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    statement = """
    UNWIND $token_ids AS token_id
    OPTIONAL MATCH (token:CdnToken {cdn_token_id: token_id})
    OPTIONAL MATCH (session:ViewingSession)-[:USES_CDN_TOKEN]->(token)
    OPTIONAL MATCH (session)-[:FROM_IP]->(ip:ClientIP)
    OPTIONAL MATCH (session)-[:ON_DEVICE]->(device:Device)
    RETURN token_id,
           count(DISTINCT session) AS viewing_session_count,
           collect(DISTINCT ip.ip_address) AS client_ips,
           collect(DISTINCT CASE
               WHEN coalesce(session.total_segment_requests, 0) > 0 THEN ip.ip_address
           END) AS segment_client_ips,
           collect(DISTINCT device.device_id) AS device_ids,
           sum(coalesce(session.total_segment_requests, 0)) AS segment_requests
    ORDER BY token_id
    """
    leakage_statement = """
    MATCH (node)
    WITH node, [key IN keys(node) WHERE key IN $forbidden_fields] AS leaked
    WHERE size(leaked) > 0
    RETURN DISTINCT labels(node) AS labels, leaked
    LIMIT 20
    """
    payload = {
        "statements": [
            {"statement": statement, "parameters": {"token_ids": token_ids}},
            {
                "statement": leakage_statement,
                "parameters": {"forbidden_fields": sorted(FORBIDDEN_FIELDS)},
            },
        ]
    }
    response = json_request(
        f"{base_url.rstrip('/')}/db/neo4j/tx/commit",
        payload,
        authorization=neo4j_authorization(user, password),
    )
    errors = response.get("errors", [])
    if errors:
        raise ValidationError(f"Neo4j query failed: {errors}")
    results = response.get("results", [])
    columns = results[0].get("columns", []) if results else []
    rows = [dict(zip(columns, item.get("row", []))) for item in (results[0].get("data", []) if results else [])]
    leaked = []
    if len(results) > 1:
        for item in results[1].get("data", []):
            labels, fields = item.get("row", [[], []])
            leaked.append(f"{labels}:{fields}")
    return rows, leaked


def analyze(
    manifest: dict[str, Any],
    documents: list[dict[str, Any]],
    graph_rows: list[dict[str, Any]],
    graph_leakage: list[str],
    ip_mapping: dict[str, str],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    bindings = manifest.get("token_bindings", [])
    token_ids = [str(item.get("cdn_token_id") or "") for item in bindings]
    if not bindings:
        errors.append("manifest contains no token bindings")

    for binding in bindings:
        token_jti = str(binding.get("token_jti") or "")
        token_id = str(binding.get("cdn_token_id") or "")
        if not token_jti or token_id != expected_cdn_token_id(token_jti):
            errors.append(f"invalid token binding hash: {token_id or '<missing>'}")

    leaked_documents = []
    for index, document in enumerate(documents):
        leaked = sorted(FORBIDDEN_FIELDS.intersection(document))
        if leaked:
            leaked_documents.append({"document_index": index, "fields": leaked})
        request_uri = str(document.get("request_uri") or "")
        if request_uri.startswith("/hls/") and ("?" in request_uri or "token=" in request_uri):
            errors.append(f"signed query leaked into request_uri at document {index}")
    if leaked_documents:
        errors.append(f"experiment provenance leaked into {len(leaked_documents)} Elasticsearch documents")
    if graph_leakage:
        errors.append(f"experiment provenance leaked into Neo4j properties: {graph_leakage}")

    edge_documents = [
        item
        for item in documents
        if item.get("event_source") == "edge-nginx" and str(item.get("request_uri") or "").startswith("/hls/")
    ]
    api_documents = [
        item
        for item in documents
        if item.get("event_source") == "ott-api" and item.get("event_kind") == "token_issued"
    ]
    graph_by_token = {str(row.get("token_id")): row for row in graph_rows}
    token_reports = []
    actual_all_ips: set[str] = set()

    for binding in bindings:
        token_id = str(binding["cdn_token_id"])
        token_edge = [item for item in edge_documents if str(item.get("cdn_token_id")) == token_id]
        token_api = [item for item in api_documents if str(item.get("cdn_token_id")) == token_id]
        actual_ips = {
            str(item.get("client_ip"))
            for item in token_edge
            if item.get("client_ip") not in (None, "", "-")
        }
        actual_all_ips.update(actual_ips)
        consumer_ids = [str(item) for item in binding.get("consumer_logical_client_ids", [])]
        expected_ips = {ip_mapping[item] for item in consumer_ids if item in ip_mapping}
        missing_ips = sorted(expected_ips - actual_ips)
        unexpected_ips = sorted(actual_ips - expected_ips)
        if not token_api:
            errors.append(f"{token_id}: token_issued API event is missing")
        if not token_edge:
            errors.append(f"{token_id}: HLS Edge events are missing")
        if missing_ips:
            errors.append(f"{token_id}: expected consumer IPs not observed: {missing_ips}")
        if unexpected_ips:
            warnings.append(f"{token_id}: additional HLS source IPs observed: {unexpected_ips}")

        graph = graph_by_token.get(token_id, {})
        if int(graph.get("viewing_session_count") or 0) == 0:
            errors.append(f"{token_id}: Neo4j ViewingSession join is missing")
        graph_ips = {str(item) for item in (graph.get("client_ips") or []) if item}
        if not expected_ips.issubset(graph_ips):
            errors.append(f"{token_id}: expected consumer IPs are missing in Neo4j: {sorted(expected_ips - graph_ips)}")
        graph_segment_ips = {
            str(item) for item in (graph.get("segment_client_ips") or []) if item
        }
        if not expected_ips.issubset(graph_segment_ips):
            errors.append(
                f"{token_id}: expected consumers have no Neo4j segment requests: "
                f"{sorted(expected_ips - graph_segment_ips)}"
            )
        token_reports.append(
            {
                "cdn_token_id": token_id,
                "expected_consumer_count": len(consumer_ids),
                "expected_ips": sorted(expected_ips),
                "edge_ips": sorted(actual_ips),
                "edge_hls_documents": len(token_edge),
                "api_token_documents": len(token_api),
                "neo4j_viewing_sessions": int(graph.get("viewing_session_count") or 0),
                "neo4j_segment_requests": int(graph.get("segment_requests") or 0),
                "neo4j_ips": sorted(graph_ips),
                "neo4j_segment_ips": sorted(graph_segment_ips),
                "neo4j_device_count": len([item for item in (graph.get("device_ids") or []) if item]),
            }
        )

    scenario_id = str(manifest.get("scenario_id") or "")
    parameters = manifest.get("parameters", {})
    scenario_variant = str(parameters.get("scenario_variant") or "")
    if scenario_id in {"A1", "A7"}:
        if len(bindings) != 1:
            errors.append(f"{scenario_id} must have exactly one shared token binding")
        elif len(bindings[0].get("consumer_logical_client_ids", [])) < 2:
            errors.append(f"{scenario_id} requires at least two token consumers")
        if len(actual_all_ips) < 2:
            errors.append(f"{scenario_id} requires at least two observed HLS source IPs")
        if token_reports and token_reports[0]["neo4j_device_count"] < 2:
            errors.append(f"{scenario_id} requires at least two observed consumer devices")
    if scenario_id == "A6":
        if len(bindings) < 2:
            errors.append("A6 requires at least two real token bindings")
        if len(actual_all_ips) < 4:
            errors.append("A6 requires four observed HLS source IPs")
        if sum(item["neo4j_device_count"] for item in token_reports) < 4:
            errors.append("A6 requires four observed consumer devices")
    if scenario_id == "N6" and len(bindings) < 2:
        errors.append("N6 requires separate playback tokens for at least two normal viewers")
    if scenario_id == "N6" and scenario_variant == "flash_crowd":
        expected_viewers = int(parameters.get("consumer_count") or 0)
        if len(bindings) != expected_viewers or len(actual_all_ips) < 2:
            errors.append("N6 flash_crowd requires separate observed viewers and tokens")
        if len({str(item.get("content_id") or "") for item in bindings} - {""}) != 1:
            errors.append("N6 flash_crowd token bindings must target one content")
    if scenario_id == "N7" and scenario_variant == "popular_channel":
        expected_viewers = int(parameters.get("consumer_count") or 0)
        if expected_viewers < 2 or len(bindings) != expected_viewers:
            errors.append("N7 popular_channel requires at least two independent token bindings")
        if len(actual_all_ips) < 2:
            errors.append("N7 popular_channel requires at least two observed HLS source IPs")
        if len({str(item.get("content_id") or "") for item in bindings} - {""}) != 1:
            errors.append("N7 popular_channel token bindings must target one LIVE content")

    return {
        "run_id": manifest.get("run_id"),
        "scenario_id": scenario_id,
        "passed": not errors,
        "elasticsearch_documents": len(documents),
        "edge_hls_documents": len(edge_documents),
        "api_token_documents": len(api_documents),
        "token_reports": token_reports,
        "leaked_documents": leaked_documents,
        "errors": errors,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--elasticsearch", default="http://192.168.0.120:9200")
    parser.add_argument("--neo4j-http", default="http://192.168.0.120:7474")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="ottlab1234")
    parser.add_argument("--wait-sec", type=float, default=120.0)
    parser.add_argument("--poll-sec", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed":
        print("manifest status must be completed before collection validation", file=sys.stderr)
        return 2
    token_ids = [str(item.get("cdn_token_id")) for item in manifest.get("token_bindings", [])]
    if not token_ids:
        print("manifest has no token bindings", file=sys.stderr)
        return 2
    ip_mapping = load_ip_mapping(args.inventory.resolve())
    deadline = time.monotonic() + max(0.0, args.wait_sec)
    report: dict[str, Any] | None = None

    while True:
        try:
            documents = query_elasticsearch(args.elasticsearch, token_ids)
            graph_rows, graph_leakage = query_neo4j(
                args.neo4j_http,
                args.neo4j_user,
                args.neo4j_password,
                token_ids,
            )
            report = analyze(manifest, documents, graph_rows, graph_leakage, ip_mapping)
        except (ValidationError, OSError, ValueError) as exc:
            report = {
                "run_id": manifest.get("run_id"),
                "scenario_id": manifest.get("scenario_id"),
                "passed": False,
                "errors": [str(exc)],
                "warnings": [],
            }
        if report.get("passed") or time.monotonic() >= deadline:
            break
        time.sleep(max(0.1, args.poll_sec))

    output_path = args.output.resolve() if args.output else manifest_path.with_suffix(".validation.json")
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**report, "output_path": str(output_path)}, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
