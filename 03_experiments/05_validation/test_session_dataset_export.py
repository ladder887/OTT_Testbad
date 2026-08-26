import importlib.util
import json
import sys
import tempfile
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
    def test_manifest_resolution_ignores_campaign_control_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "20260827_run.json"
            manifest.write_text("{}", encoding="utf-8")
            (root / "20260827_run.validation.json").write_text("{}", encoding="utf-8")
            (root / "matrix.execution.json").write_text("{}", encoding="utf-8")
            (root / "matrix.campaign_state.json").write_text("{}", encoding="utf-8")

            self.assertEqual(EXPORTER.expand_manifest_paths([str(root)]), [manifest.resolve()])

    def test_manifest_split_provenance_is_kept_outside_feature_columns(self):
        manifest = {
            "status": "completed",
            "run_id": "run_1",
            "dataset_prefix": "tnsm_100lc_20260826_main",
            "scenario_id": "A2",
            "attack_family": "M2",
            "parameters": {
                "data_split": "test",
                "collection_matrix_id": "matrix-main",
                "matrix_run_key": "test-a2-001",
                "scenario_variant": "stealth",
                "cache_state": "warm",
                "timing_scaled": False,
                "selected_clients": [],
            },
            "token_bindings": [{"cdn_token_id": "cdn_111111111111111111111111"}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            labels, _ = EXPORTER.load_manifests([path])

        label = labels["cdn_111111111111111111111111"]
        self.assertEqual(label["data_split"], "test")
        self.assertEqual(label["scenario_variant"], "stealth")
        self.assertEqual(label["collection_matrix_id"], "matrix-main")
        self.assertTrue(
            {"data_split", "scenario_variant", "collection_matrix_id"}.isdisjoint(
                EXPORTER.FEATURE_COLUMNS
            )
        )
        self.assertTrue(
            set(EXPORTER.INFRASTRUCTURE_CONTEXT_COLUMNS).isdisjoint(EXPORTER.FEATURE_COLUMNS)
        )

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
                    "account_id": "account_a",
                    "edge_id": "edge-kr",
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
                        "response_time_ms": 20,
                        "cache_status": "MISS",
                    },
                    {
                        "timestamp": "2026-08-26T00:00:06Z",
                        "kind": "hls_segment",
                        "path": "/hls/video_01/720p/seg_00002.ts",
                        "status": 200,
                        "bytes": 1000,
                        "response_time_ms": 10,
                        "cache_status": "HIT",
                    },
                    {
                        "timestamp": "2026-08-26T00:00:12Z",
                        "kind": "hls_segment",
                        "path": "/hls/video_01/1080p/seg_00003.ts",
                        "status": 200,
                        "bytes": 2000,
                        "response_time_ms": 30,
                        "cache_status": "HIT",
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
            "192.168.0.151": {
                "logical_client_id": "lc001",
                "physical_host_id": "pi01",
                "edge_id": "edge-kr",
                "network_profile_id": "P0",
            }
        }

        rows = EXPORTER.build_rows(graph_rows, labels, clients)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segment_count"], 3)
        self.assertEqual(rows[0]["avg_segment_interval_sec"], 6.0)
        self.assertEqual(rows[0]["consecutive_same_interval_count"], 1)
        self.assertEqual(rows[0]["rendition_switch_count"], 1)
        self.assertEqual(rows[0]["segment_index_gap_count"], 0)
        self.assertEqual(rows[0]["token_unique_ips"], 1)
        self.assertEqual(rows[0]["account_session_count_10m"], 1)
        self.assertEqual(rows[0]["content_unique_segments_10m"], 3)
        self.assertAlmostEqual(rows[0]["response_time_avg_ms"], 20.0)
        self.assertAlmostEqual(rows[0]["cache_hit_ratio"], 2 / 3, places=6)
        self.assertEqual(rows[0]["network_profile_id"], "P0")

    def test_time_window_features_join_account_and_content_sessions(self):
        labels = {}
        graph_rows = []
        clients = {}
        for index, (token_id, account_id, client_ip, start_index, content_id) in enumerate(
            [
                ("cdn_a", "account_a", "192.168.0.151", 0, "video_01"),
                ("cdn_b", "account_a", "192.168.0.152", 10, "video_02"),
                ("cdn_c", "account_b", "192.168.0.153", 2, "video_01"),
            ]
        ):
            labels[token_id] = {
                "run_id": f"run_{index}",
                "scenario_id": "N1",
                "attack_family": "",
                "label_binary": 0,
            }
            clients[client_ip] = {
                "logical_client_id": f"lc{index + 1:03d}",
                "physical_host_id": "pi01",
                "edge_id": "edge-kr",
                "network_profile_id": "P0",
            }
            graph_rows.append(
                {
                    "cdn_token_id": token_id,
                    "session": {
                        "viewing_session_id": f"vs_{index}",
                        "client_ip": client_ip,
                        "observed_device_id": f"device_{index}",
                        "account_id": account_id,
                        "content_id": content_id,
                        "content_type": "vod",
                        "start_time": f"2026-08-26T00:0{index}:00Z",
                        "end_time": f"2026-08-26T00:0{index}:06Z",
                    },
                    "requests": [
                        {
                            "timestamp": f"2026-08-26T00:0{index}:00Z",
                            "kind": "hls_segment",
                            "path": f"/hls/{content_id}/720p/seg_{start_index:05d}.ts",
                            "status": 200,
                            "bytes": 1000,
                        },
                        {
                            "timestamp": f"2026-08-26T00:0{index}:06Z",
                            "kind": "hls_segment",
                            "path": f"/hls/{content_id}/720p/seg_{start_index + 1:05d}.ts",
                            "status": 200,
                            "bytes": 1000,
                        },
                    ],
                }
            )

        rows = EXPORTER.build_rows(graph_rows, labels, clients)
        by_session = {row["viewing_session_id"]: row for row in rows}

        self.assertEqual(by_session["vs_1"]["account_session_count_10m"], 2)
        self.assertEqual(by_session["vs_1"]["account_unique_contents_10m"], 2)
        self.assertEqual(by_session["vs_2"]["content_session_count_10m"], 2)
        self.assertEqual(by_session["vs_2"]["content_unique_accounts_10m"], 2)

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
