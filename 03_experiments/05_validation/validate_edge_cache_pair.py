"""Validate a controlled cold/warm manifest pair against Edge logs."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


class CacheValidationError(RuntimeError):
    """Raised when cache-pair evidence cannot be queried."""


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed":
        raise CacheValidationError(f"{path.name}: manifest is not completed")
    if not manifest.get("token_bindings"):
        raise CacheValidationError(f"{path.name}: manifest has no token binding")
    return manifest


def query_documents(elasticsearch: str, token_id: str) -> list[dict[str, Any]]:
    payload = {
        "size": 1000,
        "sort": [{"@timestamp": {"order": "asc", "unmapped_type": "date"}}],
        "query": {"term": {"cdn_token_id": token_id}},
    }
    request = urllib.request.Request(
        f"{elasticsearch.rstrip('/')}/access-gateway-nginx-*/_search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CacheValidationError(f"Elasticsearch query failed: {exc}") from exc
    return [hit.get("_source", {}) for hit in result.get("hits", {}).get("hits", [])]


def hls_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in documents
        if item.get("event_source") == "edge-nginx"
        and str(item.get("request_uri") or "").startswith("/hls/")
    ]


def analyze(
    cold_manifest: dict[str, Any],
    warm_manifest: dict[str, Any],
    cold_documents: list[dict[str, Any]],
    warm_documents: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    cold_hls = hls_documents(cold_documents)
    warm_hls = hls_documents(warm_documents)
    cold_binding = cold_manifest["token_bindings"][0]
    warm_binding = warm_manifest["token_bindings"][0]

    if cold_manifest.get("parameters", {}).get("cache_state") != "cold":
        errors.append("cold manifest cache_state is not cold")
    if warm_manifest.get("parameters", {}).get("cache_state") != "warm":
        errors.append("warm manifest cache_state is not warm")
    if cold_manifest.get("scenario_id") != warm_manifest.get("scenario_id"):
        errors.append("cold and warm scenarios differ")
    if cold_binding.get("content_id") != warm_binding.get("content_id"):
        errors.append("cold and warm content IDs differ")

    cold_clients = cold_manifest.get("parameters", {}).get("selected_clients", [])
    warm_clients = warm_manifest.get("parameters", {}).get("selected_clients", [])
    cold_edges = {str(item.get("edge_id") or "") for item in cold_clients}
    warm_edges = {str(item.get("edge_id") or "") for item in warm_clients}
    if cold_edges != warm_edges:
        errors.append(f"cold and warm assigned Edges differ: {cold_edges} != {warm_edges}")

    cold_uris = [str(item.get("request_uri") or "") for item in cold_hls]
    warm_uris = [str(item.get("request_uri") or "") for item in warm_hls]
    if not cold_hls or not warm_hls:
        errors.append("both manifests require HLS Edge documents")
    if Counter(cold_uris) != Counter(warm_uris):
        errors.append("cold and warm HLS resource sets differ")

    cold_statuses = Counter(str(item.get("cache_status") or "") for item in cold_hls)
    warm_statuses = Counter(str(item.get("cache_status") or "") for item in warm_hls)
    non_miss = [
        str(item.get("request_uri") or "")
        for item in cold_hls
        if str(item.get("cache_status") or "") != "MISS"
    ]
    non_hit = [
        str(item.get("request_uri") or "")
        for item in warm_hls
        if str(item.get("cache_status") or "") != "HIT"
    ]
    if non_miss:
        errors.append(f"cold run contains non-MISS HLS objects: {non_miss}")
    if non_hit:
        errors.append(f"warm run contains non-HIT HLS objects: {non_hit}")

    if cold_binding.get("cdn_token_id") == warm_binding.get("cdn_token_id"):
        warnings.append("cold and warm runs unexpectedly reused the same token")

    return {
        "passed": not errors,
        "scenario_id": cold_manifest.get("scenario_id"),
        "content_id": cold_binding.get("content_id"),
        "edge_ids": sorted(cold_edges),
        "cold_run_id": cold_manifest.get("run_id"),
        "warm_run_id": warm_manifest.get("run_id"),
        "cold_hls_document_count": len(cold_hls),
        "warm_hls_document_count": len(warm_hls),
        "cold_cache_statuses": dict(sorted(cold_statuses.items())),
        "warm_cache_statuses": dict(sorted(warm_statuses.items())),
        "errors": errors,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-manifest", type=Path, required=True)
    parser.add_argument("--warm-manifest", type=Path, required=True)
    parser.add_argument("--elasticsearch", default="http://192.168.0.120:9200")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cold = load_manifest(args.cold_manifest.resolve())
        warm = load_manifest(args.warm_manifest.resolve())
        cold_token = str(cold["token_bindings"][0]["cdn_token_id"])
        warm_token = str(warm["token_bindings"][0]["cdn_token_id"])
        report = analyze(
            cold,
            warm,
            query_documents(args.elasticsearch, cold_token),
            query_documents(args.elasticsearch, warm_token),
        )
    except (CacheValidationError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"cache-pair validation failed: {exc}", file=sys.stderr)
        return 2
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["output_path"] = str(output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
