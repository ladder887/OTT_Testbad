"""Append one playback-token binding to an experiment run manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path


def expected_cdn_token_id(token_jti: str) -> str:
    digest = hashlib.sha256(token_jti.encode("utf-8")).hexdigest()
    return f"cdn_{digest[:24]}"


def validate_binding(manifest: dict, binding: dict[str, object]) -> None:
    token_jti = str(binding.get("token_jti") or "")
    playback_id = str(binding.get("playback_id") or "")
    uuid.UUID(token_jti)
    uuid.UUID(playback_id)

    cdn_token_id = str(binding.get("cdn_token_id") or "")
    if cdn_token_id != expected_cdn_token_id(token_jti):
        raise ValueError("cdn_token_id does not match token_jti")

    issued_at = str(binding.get("issued_at") or "")
    datetime.fromisoformat(issued_at.replace("Z", "+00:00"))

    owner = str(binding.get("owner_logical_client_id") or "")
    consumers = [str(item) for item in binding.get("consumer_logical_client_ids") or []]
    if not owner or not consumers:
        raise ValueError("owner and at least one consumer are required")

    run_clients = {str(item) for item in manifest.get("logical_client_ids") or []}
    if run_clients:
        unknown = ({owner, *consumers}) - run_clients
        if unknown:
            raise ValueError(f"binding contains clients outside this run: {sorted(unknown)}")


def record_token_binding(manifest_path: Path, binding: dict[str, object]) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_binding(manifest, binding)
    bindings = manifest.setdefault("token_bindings", [])
    token_jti = str(binding["token_jti"])

    if any(str(item.get("token_jti")) == token_jti for item in bindings):
        raise ValueError(f"token_jti already exists in manifest: {token_jti}")

    bindings.append(binding)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=manifest_path.parent,
        prefix=f".{manifest_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, manifest_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--token-jti", required=True)
    parser.add_argument("--cdn-token-id", required=True)
    parser.add_argument("--playback-id", required=True)
    parser.add_argument("--content-id", required=True)
    parser.add_argument("--owner", required=True, help="owner logical client, for example lc001")
    parser.add_argument(
        "--consumer",
        action="append",
        required=True,
        help="consumer logical client; repeat for token relay",
    )
    parser.add_argument("--issued-at", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    binding = {
        "token_jti": args.token_jti,
        "cdn_token_id": args.cdn_token_id,
        "playback_id": args.playback_id,
        "content_id": args.content_id,
        "owner_logical_client_id": args.owner,
        "consumer_logical_client_ids": list(dict.fromkeys(args.consumer)),
        "issued_at": args.issued_at,
    }
    record_token_binding(args.manifest, binding)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
