"""Validate the deployed Edge/API telemetry contract before data collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


FORBIDDEN_FIELDS = {
    "token_label",
    "token_run_id",
    "token_scenario_id",
    "token_dataset_label",
    "token_logical_client_id",
    "token_physical_host_id",
    "token_network_profile_id",
}


def token_id_from_jti(jti: object) -> str:
    value = str(jti or "").strip()
    if not value or value == "-":
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"cdn_{digest[:24]}"


def search_documents(base_url: str, index_pattern: str, size: int) -> list[dict]:
    url = f"{base_url.rstrip('/')}/{index_pattern}/_search?allow_no_indices=true"
    body = json.dumps(
        {
            "size": size,
            "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
            "query": {"range": {"@timestamp": {"gte": "now-30m"}}},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    return [item.get("_source", {}) for item in payload.get("hits", {}).get("hits", [])]


def parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def validate_forbidden_fields(documents: list[dict], source: str) -> list[str]:
    errors = []
    for index, document in enumerate(documents):
        present = sorted(FORBIDDEN_FIELDS.intersection(document))
        if present:
            errors.append(f"{source}[{index}] contains forbidden fields: {present}")
    return errors


def validate_edge_documents(documents: list[dict]) -> tuple[list[str], set[str]]:
    errors = validate_forbidden_fields(documents, "edge")
    hls_token_ids: set[str] = set()
    hls_documents = [doc for doc in documents if str(doc.get("uri", "")).startswith("/hls/")]
    if not hls_documents:
        errors.append("no HLS Edge event found in the last 30 minutes")
        return errors, hls_token_ids

    required = {
        "@timestamp",
        "event_time_epoch",
        "client_ip",
        "edge_server",
        "request_uri",
        "status",
        "request_time_sec",
        "request_id",
        "token_jti",
        "cdn_token_id",
        "token_playback_id",
        "observed_device_id",
    }
    for index, document in enumerate(hls_documents):
        missing = sorted(field for field in required if field not in document)
        if missing:
            errors.append(f"edge HLS[{index}] is missing fields: {missing}")
            continue

        request_uri = str(document.get("request_uri", ""))
        if "?" in request_uri or "token=" in request_uri:
            errors.append(f"edge HLS[{index}] exposes query/token in request_uri")
        if document.get("query_string") not in (None, "-"):
            errors.append(f"edge HLS[{index}] stores query_string")
        if document.get("session_token") not in (None, "-"):
            errors.append(f"edge HLS[{index}] stores the raw playback token")
        if document.get("event_source") not in (None, "edge-nginx"):
            errors.append(f"edge HLS[{index}] has an invalid event_source")

        edge_id = str(document.get("edge_server", ""))
        if not str(document.get("request_id", "")).startswith(f"{edge_id}-"):
            errors.append(f"edge HLS[{index}] request_id does not include edge_server")

        event_time = parse_iso(document.get("@timestamp"))
        try:
            raw_time = datetime.fromtimestamp(
                float(document.get("event_time_epoch")),
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OSError):
            raw_time = None
        if event_time is None or raw_time is None:
            errors.append(f"edge HLS[{index}] has an invalid event timestamp")
        elif abs((event_time - raw_time).total_seconds()) > 0.01:
            errors.append(f"edge HLS[{index}] @timestamp is not the original event time")
        ingested_time = parse_iso(document.get("event", {}).get("ingested"))
        if ingested_time is None:
            errors.append(f"edge HLS[{index}] has no Elasticsearch ingestion time")
        elif event_time and ingested_time < event_time:
            errors.append(f"edge HLS[{index}] ingestion time precedes event time")

        cdn_token_id = str(document.get("cdn_token_id", ""))
        expected_token_id = token_id_from_jti(document.get("token_jti"))
        if expected_token_id and cdn_token_id != expected_token_id:
            errors.append(f"edge HLS[{index}] cdn_token_id does not match token_jti")
        if cdn_token_id.startswith("cdn_"):
            hls_token_ids.add(cdn_token_id)

    return errors, hls_token_ids


def validate_api_documents(documents: list[dict]) -> tuple[list[str], set[str]]:
    errors = validate_forbidden_fields(documents, "api")
    issued = [doc for doc in documents if doc.get("event_kind") == "token_issued"]
    if not issued:
        errors.append("no token_issued API event found in the last 30 minutes")
        return errors, set()

    required = {
        "@timestamp",
        "event_time_epoch",
        "token_jti",
        "cdn_token_id",
        "token_playback_id",
        "token_owner_account_id",
        "token_content_id",
        "observed_device_id",
    }
    token_ids = set()
    for index, document in enumerate(issued):
        missing = sorted(field for field in required if field not in document)
        if missing:
            errors.append(f"api token_issued[{index}] is missing fields: {missing}")
        if document.get("query_string") not in (None, "-"):
            errors.append(f"api token_issued[{index}] stores experiment/query metadata")
        if document.get("event_source") != "ott-api":
            errors.append(f"api token_issued[{index}] has an invalid event_source")

        event_time = parse_iso(document.get("@timestamp"))
        try:
            raw_time = datetime.fromtimestamp(
                float(document.get("event_time_epoch")),
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OSError):
            raw_time = None
        if event_time is None or raw_time is None:
            errors.append(f"api token_issued[{index}] has an invalid event timestamp")
        elif abs((event_time - raw_time).total_seconds()) > 0.01:
            errors.append(f"api token_issued[{index}] @timestamp is not the original event time")
        ingested_time = parse_iso(document.get("event", {}).get("ingested"))
        if ingested_time is None:
            errors.append(f"api token_issued[{index}] has no Elasticsearch ingestion time")
        elif event_time and ingested_time < event_time:
            errors.append(f"api token_issued[{index}] ingestion time precedes event time")

        token_id = str(document.get("cdn_token_id", ""))
        expected_token_id = token_id_from_jti(document.get("token_jti"))
        if expected_token_id and token_id != expected_token_id:
            errors.append(f"api token_issued[{index}] cdn_token_id does not match token_jti")
        if token_id.startswith("cdn_"):
            token_ids.add(token_id)
    return errors, token_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elasticsearch", default="http://192.168.0.120:9200")
    parser.add_argument("--size", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        edge_documents = search_documents(
            args.elasticsearch,
            "access-gateway-nginx-*",
            args.size,
        )
        api_documents = search_documents(
            args.elasticsearch,
            "ott-api-events-*",
            args.size,
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"telemetry query failed: {exc}", file=sys.stderr)
        return 2

    edge_errors, edge_token_ids = validate_edge_documents(edge_documents)
    api_errors, api_token_ids = validate_api_documents(api_documents)
    errors = edge_errors + api_errors
    if edge_token_ids and api_token_ids and not edge_token_ids.intersection(api_token_ids):
        errors.append("no cdn_token_id joins a token_issued event to an HLS Edge event")

    summary = {
        "edge_documents_checked": len(edge_documents),
        "api_documents_checked": len(api_documents),
        "joined_token_ids": len(edge_token_ids.intersection(api_token_ids)),
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
