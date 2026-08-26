"""Snapshot graph aggregates and compare them across an Elasticsearch replay."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SnapshotError(RuntimeError):
    """Raised when the graph snapshot cannot satisfy its validation contract."""


def authorization(user: str, password: str) -> str:
    value = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {value}"


def query(
    base_url: str,
    user: str,
    password: str,
    statements: list[str],
) -> list[list[dict[str, Any]]]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/db/neo4j/tx/commit",
        data=json.dumps(
            {"statements": [{"statement": statement} for statement in statements]}
        ).encode("utf-8"),
        headers={
            "Authorization": authorization(user, password),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Neo4j snapshot query failed: {exc}") from exc
    if payload.get("errors"):
        raise SnapshotError(f"Neo4j snapshot query failed: {payload['errors']}")

    result_sets: list[list[dict[str, Any]]] = []
    for result in payload.get("results", []):
        columns = result.get("columns", [])
        result_sets.append(
            [
                dict(zip(columns, item.get("row", [])))
                for item in result.get("data", [])
            ]
        )
    return result_sets


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_graph_state(result_sets: list[list[dict[str, Any]]]) -> dict[str, Any]:
    if len(result_sets) != 4:
        raise SnapshotError(f"expected four Neo4j result sets, received {len(result_sets)}")
    nodes, relationships, sessions, contents = result_sets
    session_rows = sorted(sessions, key=lambda item: str(item.get("viewing_session_id") or ""))
    content_rows = sorted(contents, key=lambda item: str(item.get("content_id") or ""))

    session_mismatches = [
        row
        for row in session_rows
        if any(
            int(row.get(stored) or 0) != int(row.get(actual) or 0)
            for stored, actual in (
                ("stored_requests", "actual_requests"),
                ("stored_manifests", "actual_manifests"),
                ("stored_segments", "actual_segments"),
                ("stored_playback_starts", "actual_playback_starts"),
                ("stored_browse", "actual_browse"),
                ("stored_bytes", "actual_bytes"),
            )
        )
    ]
    content_mismatches = [
        row
        for row in content_rows
        if int(row.get("stored_requests") or 0) != int(row.get("actual_requests") or 0)
        or int(row.get("stored_bytes") or 0) != int(row.get("actual_bytes") or 0)
    ]

    fingerprint_payload = {
        "nodes": sorted(nodes, key=lambda item: str(item.get("label") or "")),
        "relationships": sorted(
            relationships,
            key=lambda item: str(item.get("relationship_type") or ""),
        ),
        "sessions": session_rows,
        "contents": content_rows,
    }
    return {
        "node_counts": {
            str(item["label"]): int(item["count"])
            for item in sorted(nodes, key=lambda item: str(item.get("label") or ""))
        },
        "relationship_counts": {
            str(item["relationship_type"]): int(item["count"])
            for item in sorted(
                relationships,
                key=lambda item: str(item.get("relationship_type") or ""),
            )
        },
        "viewing_session_count": len(session_rows),
        "viewing_session_aggregate_mismatch_count": len(session_mismatches),
        "content_count": len(content_rows),
        "content_aggregate_mismatch_count": len(content_mismatches),
        "fingerprint_sha256": stable_hash(fingerprint_payload),
    }


def capture(base_url: str, user: str, password: str) -> dict[str, Any]:
    statements = [
        """
        MATCH (node)
        RETURN labels(node)[0] AS label, count(*) AS count
        ORDER BY label
        """,
        """
        MATCH ()-[relationship]->()
        RETURN type(relationship) AS relationship_type, count(*) AS count
        ORDER BY relationship_type
        """,
        """
        MATCH (session:ViewingSession)
        OPTIONAL MATCH (session)-[:MAKES_REQUEST]->(request:Request)
        WITH session,
             count(DISTINCT request) AS actual_requests,
             sum(CASE WHEN request.kind = 'hls_manifest' THEN 1 ELSE 0 END) AS actual_manifests,
             sum(CASE WHEN request.kind = 'hls_segment' THEN 1 ELSE 0 END) AS actual_segments,
             sum(CASE WHEN request.kind = 'playback_start' THEN 1 ELSE 0 END) AS actual_playback_starts,
             sum(CASE WHEN request.kind = 'browse_content' THEN 1 ELSE 0 END) AS actual_browse,
             sum(coalesce(request.bytes_sent, 0)) AS actual_bytes
        OPTIONAL MATCH (session)-[:USES_CDN_TOKEN]->(token:CdnToken)
        OPTIONAL MATCH (session)-[:TARGETS_CONTENT]->(content:Content)
        OPTIONAL MATCH (session)-[:FROM_IP]->(ip:ClientIP)
        OPTIONAL MATCH (session)-[:ON_DEVICE]->(device:Device)
        RETURN session.viewing_session_id AS viewing_session_id,
               coalesce(session.request_count, 0) AS stored_requests,
               actual_requests,
               coalesce(session.total_manifest_requests, 0) AS stored_manifests,
               actual_manifests,
               coalesce(session.total_segment_requests, 0) AS stored_segments,
               actual_segments,
               coalesce(session.total_playback_start_requests, 0) AS stored_playback_starts,
               actual_playback_starts,
               coalesce(session.total_browse_requests, 0) AS stored_browse,
               actual_browse,
               coalesce(session.total_bytes, 0) AS stored_bytes,
               actual_bytes,
               collect(DISTINCT token.cdn_token_id) AS tokens,
               collect(DISTINCT content.content_id) AS contents,
               collect(DISTINCT ip.ip_address) AS ips,
               collect(DISTINCT device.device_id) AS devices
        ORDER BY viewing_session_id
        """,
        """
        MATCH (content:Content)
        OPTIONAL MATCH (request:Request)-[:TARGETS_CONTENT]->(content)
        RETURN content.content_id AS content_id,
               coalesce(content.request_count, 0) AS stored_requests,
               count(DISTINCT request) AS actual_requests,
               coalesce(content.total_bytes, 0) AS stored_bytes,
               sum(coalesce(request.bytes_sent, 0)) AS actual_bytes
        ORDER BY content_id
        """,
    ]
    return build_graph_state(query(base_url, user, password, statements))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--neo4j-http", default="http://192.168.0.120:7474")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="ottlab1234")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        graph_state = capture(args.neo4j_http, args.neo4j_user, args.neo4j_password)
        report = {
            "schema_version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "graph_state": graph_state,
        }
        errors: list[str] = []
        if graph_state["viewing_session_aggregate_mismatch_count"]:
            errors.append("ViewingSession stored aggregates do not match Request relationships")
        if graph_state["content_aggregate_mismatch_count"]:
            errors.append("Content stored aggregates do not match Request relationships")
        if args.compare:
            baseline = json.loads(args.compare.resolve().read_text(encoding="utf-8"))
            if graph_state != baseline.get("graph_state"):
                errors.append("graph state changed after replay")
        report["passed"] = not errors
        report["errors"] = errors
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (SnapshotError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"graph snapshot failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({**report, "output_path": str(output)}, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
