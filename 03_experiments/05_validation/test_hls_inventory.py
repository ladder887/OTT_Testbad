from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


INVENTORY_PATH = (
    Path(__file__).resolve().parents[1]
    / "04_data_tools"
    / "inventory_hls.py"
)
SPEC = importlib.util.spec_from_file_location("hls_inventory", INVENTORY_PATH)
assert SPEC is not None and SPEC.loader is not None
INVENTORY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INVENTORY
SPEC.loader.exec_module(INVENTORY)
sys.modules.setdefault("inventory_hls", INVENTORY)

LIVE_VERIFY_PATH = (
    Path(__file__).resolve().parents[1]
    / "04_data_tools"
    / "verify_live_hls.py"
)
LIVE_VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_live_hls",
    LIVE_VERIFY_PATH,
)
assert LIVE_VERIFY_SPEC is not None and LIVE_VERIFY_SPEC.loader is not None
LIVE_VERIFY = importlib.util.module_from_spec(LIVE_VERIFY_SPEC)
sys.modules[LIVE_VERIFY_SPEC.name] = LIVE_VERIFY
LIVE_VERIFY_SPEC.loader.exec_module(LIVE_VERIFY)


class HlsInventoryTest(unittest.TestCase):
    def test_vod_and_live_are_classified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_content(root / "video_001", endlist=True)
            self._write_content(root / "live_001", endlist=False)

            payload = INVENTORY.build_inventory(root)
            self.assertEqual(payload["summary"]["contents"], 2)
            self.assertEqual(payload["summary"]["vod"], 1)
            self.assertEqual(payload["summary"]["live"], 1)
            self.assertEqual(payload["summary"]["contents_with_errors"], 0)

    def test_missing_master_variant_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content_dir = root / "video_001"
            self._write_content(content_dir, endlist=True)
            (content_dir / "master.m3u8").write_text(
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
                "720p/playlist.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080\n"
                "1080p/playlist.m3u8\n",
                encoding="utf-8",
            )

            payload = INVENTORY.build_inventory(root)
            self.assertEqual(payload["summary"]["contents_with_errors"], 1)
            self.assertIn(
                "missing master variant: 1080p/playlist.m3u8",
                payload["contents"][0]["errors"],
            )

    def test_live_playlist_advance_detection(self) -> None:
        self.assertTrue(
            LIVE_VERIFY.playlist_advanced(
                {"media_sequence": 10, "last_segment": "seg_10.ts"},
                {"media_sequence": 11, "last_segment": "seg_11.ts"},
            )
        )
        self.assertFalse(
            LIVE_VERIFY.playlist_advanced(
                {"media_sequence": 10, "last_segment": "seg_10.ts"},
                {"media_sequence": 10, "last_segment": "seg_10.ts"},
            )
        )

    @staticmethod
    def _write_content(content_dir: Path, *, endlist: bool) -> None:
        rendition_dir = content_dir / "720p"
        rendition_dir.mkdir(parents=True)
        (content_dir / "master.m3u8").write_text(
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
            "720p/playlist.m3u8\n",
            encoding="utf-8",
        )
        for segment in ("seg_00001.ts", "seg_00002.ts"):
            (rendition_dir / segment).write_bytes(b"test")
        lines = [
            "#EXTM3U",
            "#EXT-X-TARGETDURATION:6",
            "#EXT-X-MEDIA-SEQUENCE:1",
            "#EXTINF:6.0,",
            "seg_00001.ts",
            "#EXTINF:6.0,",
            "seg_00002.ts",
        ]
        if endlist:
            lines.append("#EXT-X-ENDLIST")
        (rendition_dir / "playlist.m3u8").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
