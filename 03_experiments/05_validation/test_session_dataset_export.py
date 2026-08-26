import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


def load_exporter_module():
    module_path = Path(__file__).resolve().parents[1] / "04_data_tools" / "export_session_dataset.py"
    spec = importlib.util.spec_from_file_location("export_session_dataset", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXPORTER = load_exporter_module()


class SessionDatasetExportTest(unittest.TestCase):
    def test_graph_query_uses_a_node_map_projection(self):
        with mock.patch.object(EXPORTER, "neo4j_query", return_value=[]) as query:
            rows = EXPORTER.query_graph_sessions(
                "http://neo4j.test:7474",
                "neo4j",
                "password",
                ["cdn_1"],
            )

        self.assertEqual(rows, [])
        statement = query.call_args.args[3]
        self.assertIn("session {", statement)
        self.assertIn(".*", statement)
        self.assertNotIn("properties(session) +", statement)

    def test_features_are_derived_from_requests_and_token_relations(self):
        token_id = "cdn_111111111111111111111111"
        graph_rows = [
            {
                "cdn_token_id": token_id,
                "session": {
                    "viewing_session_id": "vs_1",
                    "client_ip": "192.168.0.151",
                    "observed_device_id": "device_a",
                    "content_id": "video_01",
                    "content_type": "vod",
                    "start_time": "2026-08-26T00:00:00Z",
                    "end_time": "2026-08-26T00:00:12Z",
                },
                "requests": [
                    {
                        "timestamp": "2026-08-26T00:00:00Z",
                        "kind": "hls_manifest",
                        "path": "/hls/video_01/720p/playlist.m3u8",
                        "status": 200,
                        "bytes": 100,
                    },
                    {
                        "timestamp": "2026-08-26T00:00:00Z",
                        "kind": "hls_segment",
                        "path": "/hls/video_01/720p/seg_00001.ts",
                        "status": 200,
                        "bytes": 1000,
                    },
                    {
                        "timestamp": "2026-08-26T00:00:06Z",
                        "kind": "hls_segment",
                        "path": "/hls/video_01/720p/seg_00002.ts",
                        "status": 200,
                        "bytes": 1000,
                    },
                    {
                        "timestamp": "2026-08-26T00:00:12Z",
                        "kind": "hls_segment",
                        "path": "/hls/video_01/1080p/seg_00003.ts",
                        "status": 200,
                        "bytes": 2000,
                    },
                ],
            }
        ]
        labels = {
            token_id: {
                "run_id": "run_1",
                "scenario_id": "N3",
                "attack_family": "",
                "label_binary": 0,
            }
        }
        clients = {
            "192.168.0.151": {"logical_client_id": "lc001", "physical_host_id": "pi01"}
        }

        rows = EXPORTER.build_rows(graph_rows, labels, clients)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segment_count"], 3)
        self.assertEqual(rows[0]["avg_segment_interval_sec"], 6.0)
        self.assertEqual(rows[0]["consecutive_same_interval_count"], 1)
        self.assertEqual(rows[0]["rendition_switch_count"], 1)
        self.assertEqual(rows[0]["segment_index_gap_count"], 0)
        self.assertEqual(rows[0]["token_unique_ips"], 1)

    def test_control_plane_only_session_is_not_exported(self):
        rows = EXPORTER.build_rows(
            [
                {
                    "cdn_token_id": "cdn_x",
                    "session": {
                        "viewing_session_id": "vs_owner",
                        "start_time": "2026-08-26T00:00:00Z",
                        "end_time": "2026-08-26T00:00:01Z",
                    },
                    "requests": [{"kind": "playback_start"}],
                }
            ],
            {
                "cdn_x": {
                    "run_id": "run_1",
                    "scenario_id": "A1",
                    "attack_family": "M1",
                    "label_binary": 1,
                }
            },
            {},
        )

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
