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
        self.assertNotIn("proxy_buffering off;", vod)

    def test_remote_edge_cache_contract(self):
        self.check_config(
            REPOSITORY_ROOT / "02_deployment" / "01_edge-common" / "nginx.remote.conf"
        )

    def test_local_compose_cache_contract(self):
        self.check_config(
            REPOSITORY_ROOT / "01_platform" / "02_access_gateway" / "nginx.conf"
        )

    def test_origin_distinguishes_rolling_playlists_from_immutable_media(self):
        config = (REPOSITORY_ROOT / "01_platform" / "01_origin" / "nginx.conf").read_text(
            encoding="utf-8"
        )
        live_manifest = location_body(
            config,
            "location ~ ^/hls/live_.*/(master\\.m3u8|.*/playlist\\.m3u8)$",
        )
        live_segment = location_body(config, "location ~ ^/hls/live_.*/.*/seg_.*\\.ts$")
        vod = location_body(config, "location /hls")

        self.assertIn('Cache-Control "no-store, no-cache, must-revalidate"', live_manifest)
        self.assertIn('Cache-Control "public, max-age=300, immutable"', live_segment)
        self.assertIn('Cache-Control "public, max-age=3600, immutable"', vod)

    def test_platform_deployment_reloads_bind_mounted_origin_config(self):
        playbook = (
            REPOSITORY_ROOT
            / "02_deployment"
            / "09_remote-management"
            / "playbooks"
            / "02_deploy_platform.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("Validate the bind-mounted Origin Nginx configuration", playbook)
        self.assertIn(
            "argv: [docker, compose, up, -d, --force-recreate, origin-nginx]",
            playbook,
        )
        self.assertIn("argv: [docker, exec, ott-origin-nginx, nginx, -t]", playbook)
        self.assertIn("argv: [docker, exec, ott-origin-nginx, nginx, -s, reload]", playbook)


if __name__ == "__main__":
    unittest.main()
