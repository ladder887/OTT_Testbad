"""Verify that every deployed LIVE media playlist advances over time."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from inventory_hls import build_inventory, parse_media_playlist


def playlist_advanced(
    before: dict[str, object],
    after: dict[str, object],
) -> bool:
    before_sequence = before.get("media_sequence")
    after_sequence = after.get("media_sequence")
    if isinstance(before_sequence, int) and isinstance(after_sequence, int):
        if after_sequence > before_sequence:
            return True
    return bool(
        before.get("last_segment")
        and after.get("last_segment")
        and before["last_segment"] != after["last_segment"]
    )


def capture_live_playlists(root: Path) -> dict[str, dict[str, object]]:
    inventory = build_inventory(root)
    snapshots: dict[str, dict[str, object]] = {}
    for content in inventory["contents"]:
        if content["content_type"] != "live":
            continue
        for playlist in content["media_playlists"]:
            path = Path(str(playlist["path"]))
            key = f"{content['content_id']}/{path.parent.name}/{path.name}"
            snapshots[key] = parse_media_playlist(path)
    return snapshots


def verify_live_playlists(
    root: Path,
    *,
    wait_seconds: float,
    minimum_live: int,
) -> dict[str, object]:
    inventory = build_inventory(root)
    live_contents = [
        content
        for content in inventory["contents"]
        if content["content_type"] == "live"
    ]
    before = capture_live_playlists(root)
    time.sleep(wait_seconds)
    after = capture_live_playlists(root)

    results = []
    for key, before_snapshot in sorted(before.items()):
        after_snapshot = after.get(key)
        advanced = bool(
            after_snapshot
            and playlist_advanced(before_snapshot, after_snapshot)
        )
        results.append(
            {
                "playlist": key,
                "before_media_sequence": before_snapshot.get("media_sequence"),
                "after_media_sequence": (
                    after_snapshot.get("media_sequence")
                    if after_snapshot
                    else None
                ),
                "before_last_segment": before_snapshot.get("last_segment"),
                "after_last_segment": (
                    after_snapshot.get("last_segment")
                    if after_snapshot
                    else None
                ),
                "advanced": advanced,
            }
        )

    failures = [
        result["playlist"]
        for result in results
        if not result["advanced"]
    ]
    return {
        "schema_version": 1,
        "root": str(root.resolve()),
        "wait_seconds": wait_seconds,
        "minimum_live": minimum_live,
        "live_contents": len(live_contents),
        "playlists_checked": len(results),
        "failed_playlists": failures,
        "passed": (
            len(live_contents) >= minimum_live
            and bool(results)
            and not failures
        ),
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    default_root = (
        Path(__file__).resolve().parents[2]
        / "01_platform"
        / "01_origin"
        / "hls"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--wait-seconds", type=float, default=12.0)
    parser.add_argument("--minimum-live", type=int, default=1)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.wait_seconds <= 0:
        raise SystemExit("--wait-seconds must be greater than 0")
    if args.minimum_live < 1:
        raise SystemExit("--minimum-live must be at least 1")

    payload = verify_live_playlists(
        args.root,
        wait_seconds=args.wait_seconds,
        minimum_live=args.minimum_live,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
