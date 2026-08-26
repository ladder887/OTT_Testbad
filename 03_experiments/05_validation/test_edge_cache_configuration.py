import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def location_body(config: str, marker: str) -> str:
    start = config.index(marker)
    opening = config.index("{", start)
    depth = 0
    for index in range(opening, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[opening + 1 : index]
    raise AssertionError(f"unterminated location block: {marker}")


class EdgeCacheConfigurationTest(unittest.TestCase):
    def check_config(self, path):
        config = path.read_text(encoding="utf-8")
        live_manifest = location_body(
            config,
            "location ~ ^/hls/live_.*/(master\\.m3u8|.*/playlist\\.m3u8)$",
        )
        live_segment = location_body(config, "location ~ ^/hls/live_.*/.*/seg_.*\\.ts$")
        vod = location_body(config, "location /hls")

        self.assertIn("proxy_cache off;", live_manifest)
        self.assertIn("proxy_cache cdn_cache;", live_segment)
        self.assertIn('proxy_cache_key "$scheme$proxy_host$uri";', live_segment)
        self.assertIn("proxy_cache cdn_cache;", vod)
        self.assertIn('proxy_cache_key "$scheme$proxy_host$uri";', vod)
        self.assertRegex(vod, re.compile(r"proxy_cache_valid\s+200\s+206\s+60m;"))

    def test_remote_edge_cache_contract(self):
        self.check_config(
            REPOSITORY_ROOT / "02_deployment" / "01_edge-common" / "nginx.remote.conf"
        )

    def test_local_compose_cache_contract(self):
        self.check_config(
            REPOSITORY_ROOT / "01_platform" / "02_access_gateway" / "nginx.conf"
        )


if __name__ == "__main__":
    unittest.main()
