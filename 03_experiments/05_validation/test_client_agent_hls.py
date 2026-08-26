import importlib.util
import sys
import unittest
from pathlib import Path


def load_agent_module():
    module_path = Path(__file__).resolve().parents[1] / "01_client_runtime" / "client_agent.py"
    spec = importlib.util.spec_from_file_location("client_agent", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AGENT = load_agent_module()


class ClientAgentHlsTest(unittest.TestCase):
    def test_network_profiles_generate_bidirectional_netem_commands(self):
        commands = AGENT.network_profile_commands("P3")

        self.assertEqual(AGENT.network_profile_commands("P0"), [])
        self.assertEqual(commands[0], ["ip", "link", "add", "ifb0", "type", "ifb"])
        self.assertTrue(any(command[:4] == ["tc", "qdisc", "add", "dev"] for command in commands))
        self.assertTrue(any("mirred" in command for command in commands))
        netem_commands = [command for command in commands if "netem" in command]
        self.assertEqual(len(netem_commands), 2)
        self.assertTrue(all("45ms" in command for command in netem_commands))
        self.assertTrue(all("0.5%" in command for command in netem_commands))

    def test_unknown_network_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            AGENT.network_profile_commands("P9")

    def test_signed_child_url_inherits_only_token_and_signature(self):
        parent = "http://edge/hls/video_01/master.m3u8?token=abc&sig=def&ignored=value"
        child = AGENT.signed_child_url(parent, "720p/playlist.m3u8")

        self.assertEqual(
            child,
            "http://edge/hls/video_01/720p/playlist.m3u8?token=abc&sig=def",
        )

    def test_master_and_media_playlists_preserve_sequences(self):
        master_url = "http://edge/hls/video_01/master.m3u8?token=abc&sig=def"
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
720p/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080p/playlist.m3u8
"""
        variants = AGENT.parse_master_playlist(master, master_url)

        self.assertEqual([item.height for item in variants], [720, 1080])
        self.assertIn("token=abc", AGENT.choose_variant(variants, "720p").url)

        media = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:41
#EXTINF:6.0,
seg_00041.ts
#EXTINF:5.5,
seg_00042.ts
"""
        parsed = AGENT.parse_media_playlist(media, variants[0].url)

        self.assertEqual(parsed.media_sequence, 41)
        self.assertEqual([item.sequence for item in parsed.segments], [41, 42])
        self.assertEqual(parsed.segments[1].duration_sec, 5.5)
        self.assertIn("sig=def", parsed.segments[1].url)

    def test_live_end_list_is_parsed(self):
        parsed = AGENT.parse_media_playlist(
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:6,\nseg_1.ts\n#EXT-X-ENDLIST\n",
            "http://edge/hls/live_01/720p/playlist.m3u8?token=a&sig=b",
        )

        self.assertTrue(parsed.end_list)


if __name__ == "__main__":
    unittest.main()
