"""Inventory actual HLS media before selecting experiment content."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ATTRIBUTE_PATTERN = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attribute_list(line: str) -> dict[str, str]:
    _, _, raw_attributes = line.partition(":")
    return {
        key: value.strip('"')
        for key, value in ATTRIBUTE_PATTERN.findall(raw_attributes)
    }


def parse_media_playlist(path: Path) -> dict[str, object]:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    segment_uris = [line for line in lines if not line.startswith("#")]

    def tag_value(prefix: str) -> str | None:
        for line in lines:
            if line.startswith(prefix):
                return line.split(":", 1)[1] if ":" in line else ""
        return None

    target_duration = tag_value("#EXT-X-TARGETDURATION")
    media_sequence = tag_value("#EXT-X-MEDIA-SEQUENCE")
    return {
        "path": path.as_posix(),
        "rendition": path.parent.name,
        "segment_count": len(segment_uris),
        "first_segment": segment_uris[0] if segment_uris else None,
        "last_segment": segment_uris[-1] if segment_uris else None,
        "target_duration_sec": int(target_duration) if target_duration and target_duration.isdigit() else None,
        "media_sequence": int(media_sequence) if media_sequence and media_sequence.isdigit() else None,
        "has_endlist": "#EXT-X-ENDLIST" in lines,
    }


def parse_master(path: Path) -> list[dict[str, object]]:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    variants: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = lines[index + 1] if index + 1 < len(lines) else ""
        if not uri or uri.startswith("#"):
            continue
        variants.append(
            {
                "uri": uri,
                "attributes": parse_attribute_list(line),
            }
        )
    return variants


def inventory_content(content_dir: Path, root: Path) -> dict[str, object]:
    master_path = content_dir / "master.m3u8"
    master_variants = parse_master(master_path) if master_path.exists() else []
    media_paths = sorted(
        path
        for path in content_dir.rglob("*.m3u8")
        if path.name != "master.m3u8"
    )
    errors: list[str] = []
    if not master_path.exists():
        errors.append("missing master.m3u8")
    if not media_paths:
        errors.append("no media playlists")
    for variant in master_variants:
        variant_uri = str(variant["uri"]).split("?", 1)[0]
        if not (content_dir / variant_uri).exists():
            errors.append(f"missing master variant: {variant_uri}")

    media_playlists = [parse_media_playlist(path) for path in media_paths]
    for playlist in media_playlists:
        playlist_path = Path(str(playlist["path"]))
        base_dir = playlist_path.parent
        for key in ("first_segment", "last_segment"):
            uri = playlist[key]
            if uri and not (base_dir / str(uri).split("?", 1)[0]).exists():
                errors.append(f"missing referenced {key}: {playlist_path.parent / str(uri)}")

    is_live = content_dir.name.startswith("live_") or any(
        not bool(playlist["has_endlist"]) for playlist in media_playlists
    )
    return {
        "content_id": content_dir.name,
        "content_type": "live" if is_live else "vod",
        "relative_path": content_dir.relative_to(root).as_posix(),
        "master_exists": master_path.exists(),
        "master_variants": master_variants,
        "media_playlists": media_playlists,
        "errors": sorted(set(errors)),
    }


def build_inventory(root: Path) -> dict[str, object]:
    content_dirs = sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ) if root.exists() else []
    contents = [inventory_content(path, root) for path in content_dirs]
    return {
        "schema_version": 1,
        "root": str(root.resolve()),
        "summary": {
            "contents": len(contents),
            "vod": sum(item["content_type"] == "vod" for item in contents),
            "live": sum(item["content_type"] == "live" for item in contents),
            "contents_with_errors": sum(bool(item["errors"]) for item in contents),
        },
        "contents": contents,
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
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_inventory(args.root)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")

    summary = payload["summary"]
    if summary["contents"] == 0 or summary["contents_with_errors"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
