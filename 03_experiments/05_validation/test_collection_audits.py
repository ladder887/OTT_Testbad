import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def load_module(name):
    module_path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COLLECTION = load_module("audit_pilot_collection")
DATASET = load_module("audit_session_dataset")
CACHE = load_module("validate_edge_cache_pair")


class CollectionAuditTest(unittest.TestCase):
    def test_hard_negative_relationships_are_accepted(self):
        flash_crowd = {
            "scenario_id": "N6",
            "parameters": {
                "scenario_variant": "flash_crowd",
                "consumer_count": 2,
                "shared_account": False,
                "shared_token": False,
                "shared_content": True,
                "actual_account_count": 2,
            },
            "token_bindings": [
                {"content_id": "video_01"},
                {"content_id": "video_01"},
            ],
        }
        popular_live = {
            "scenario_id": "N7",
            "parameters": {
                "scenario_variant": "popular_channel",
                "consumer_count": 2,
                "shared_token": False,
                "client_results": [
                    {"traffic": {"rolling_playlist": True}},
                    {"traffic": {"rolling_playlist": True}},
                ],
            },
            "token_bindings": [
                {"content_id": "live_01"},
                {"content_id": "live_01"},
            ],
        }

        self.assertEqual(COLLECTION.scenario_errors(flash_crowd), [])
        self.assertEqual(COLLECTION.scenario_errors(popular_live), [])

    def test_invalid_hard_negative_relationship_is_rejected(self):
        manifest = {
            "scenario_id": "N6",
            "parameters": {
                "scenario_variant": "flash_crowd",
                "consumer_count": 2,
                "shared_account": True,
                "shared_token": False,
                "shared_content": False,
                "actual_account_count": 1,
            },
            "token_bindings": [
                {"content_id": "video_01"},
                {"content_id": "video_02"},
            ],
        }

        errors = COLLECTION.scenario_errors(manifest)

        self.assertTrue(any("independent accounts" in item for item in errors))
        self.assertTrue(any("one shared content" in item for item in errors))

    def test_network_gate_accepts_all_applied_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory = {}
            paths = []
            for index, profile in enumerate(sorted(COLLECTION.EXPECTED_PROFILES)):
                client_id = f"lc{index + 1:03d}"
                host_id = f"pi{index + 1:02d}"
                edge_id = ("edge-kr", "edge-jp", "edge-sg", "edge-us")[index % 4]
                source_ip = f"192.168.0.{151 + index}"
                inventory[client_id] = {
                    "logical_client_id": client_id,
                    "physical_host_id": host_id,
                    "source_ip": source_ip,
                    "edge_id": edge_id,
                    "network_profile_id": profile,
                }
                expected_rtt, expected_loss = COLLECTION.EXPECTED_PROFILE_VALUES[profile]
                manifest = {
                    "run_id": f"run_{profile}",
                    "dataset_prefix": "tnsm_100lc_20260826_network",
                    "scenario_id": "N1",
                    "status": "completed",
                    "parameters": {
                        "selected_clients": [inventory[client_id]],
                        "client_results": [
                            {
                                "logical_client_id": client_id,
                                "http_retry_count": 0,
                                "http_failure_count": 0,
                                "network_impairment": {
                                    "profile_id": profile,
                                    "configured_added_rtt_ms": expected_rtt,
                                    "approximate_end_to_end_loss_percent": expected_loss,
                                },
                            }
                        ],
                    },
                    "token_bindings": [
                        {"cdn_token_id": f"cdn_{index:024x}", "content_id": "video_01"}
                    ],
                }
                path = root / f"run_{profile}.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                path.with_suffix(".validation.json").write_text(
                    json.dumps({"passed": True}), encoding="utf-8"
                )
                paths.append(path)

            report = COLLECTION.audit(paths, inventory, "network")

        self.assertTrue(report["passed"])
        self.assertEqual(set(report["applied_network_profiles"]), COLLECTION.EXPECTED_PROFILES)

    def test_missing_validation_fails_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.json"
            client = {
                "logical_client_id": "lc001",
                "physical_host_id": "pi01",
                "source_ip": "192.168.0.151",
                "edge_id": "edge-kr",
                "network_profile_id": "P0",
            }
            path.write_text(
                json.dumps(
                    {
                        "run_id": "run_1",
                        "dataset_prefix": "tnsm_100lc_20260826_test",
                        "scenario_id": "N1",
                        "status": "completed",
                        "parameters": {
                            "selected_clients": [client],
                            "client_results": [{"logical_client_id": "lc001"}],
                        },
                        "token_bindings": [{"cdn_token_id": "cdn_1", "content_id": "video_01"}],
                    }
                ),
                encoding="utf-8",
            )

            report = COLLECTION.audit([path], {"lc001": client}, "scenario")

        self.assertFalse(report["passed"])
        self.assertTrue(any("validation report is missing" in item for item in report["errors"]))


class DatasetAuditTest(unittest.TestCase):
    def test_reports_single_feature_label_proxy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset = Path(temp_dir) / "dataset.csv"
            columns = [
                "sample_id",
                "run_id",
                "scenario_id",
                "label_binary",
                "cdn_token_id",
                "feature_proxy",
                "feature_constant",
            ]
            with dataset.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for index, label in enumerate((0, 0, 1, 1)):
                    writer.writerow(
                        {
                            "sample_id": f"sample_{index}",
                            "run_id": f"run_{index}",
                            "scenario_id": "N1" if label == 0 else "A1",
                            "label_binary": label,
                            "cdn_token_id": f"cdn_{index}",
                            "feature_proxy": label,
                            "feature_constant": 1,
                        }
                    )
            dataset.with_suffix(".metadata.json").write_text(
                json.dumps(
                    {
                        "row_count": 4,
                        "feature_columns": ["feature_proxy", "feature_constant"],
                    }
                ),
                encoding="utf-8",
            )

            report = DATASET.audit(dataset, 0.95)

        self.assertTrue(report["passed"])
        self.assertEqual(report["high_auc_features"], ["feature_proxy"])
        self.assertEqual(report["constant_features"], ["feature_constant"])


class EdgeCachePairValidationTest(unittest.TestCase):
    def test_accepts_matching_miss_then_hit_pair(self):
        def manifest(state, run_id, token_id):
            return {
                "run_id": run_id,
                "scenario_id": "N1",
                "parameters": {
                    "cache_state": state,
                    "selected_clients": [{"edge_id": "edge-sg"}],
                },
                "token_bindings": [{"cdn_token_id": token_id, "content_id": "video_01"}],
            }

        def documents(status):
            return [
                {
                    "event_source": "edge-nginx",
                    "request_uri": "/hls/video_01/master.m3u8",
                    "cache_status": status,
                },
                {
                    "event_source": "edge-nginx",
                    "request_uri": "/hls/video_01/720p/seg_00000.ts",
                    "cache_status": status,
                },
            ]

        report = CACHE.analyze(
            manifest("cold", "cold_run", "cdn_cold"),
            manifest("warm", "warm_run", "cdn_warm"),
            documents("MISS"),
            documents("HIT"),
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["cold_cache_statuses"], {"MISS": 2})
        self.assertEqual(report["warm_cache_statuses"], {"HIT": 2})

    def test_rejects_warm_miss(self):
        cold = {
            "run_id": "cold",
            "scenario_id": "N1",
            "parameters": {"cache_state": "cold", "selected_clients": [{"edge_id": "edge-kr"}]},
            "token_bindings": [{"cdn_token_id": "cdn_1", "content_id": "video_01"}],
        }
        warm = {
            "run_id": "warm",
            "scenario_id": "N1",
            "parameters": {"cache_state": "warm", "selected_clients": [{"edge_id": "edge-kr"}]},
            "token_bindings": [{"cdn_token_id": "cdn_2", "content_id": "video_01"}],
        }
        document = {
            "event_source": "edge-nginx",
            "request_uri": "/hls/video_01/master.m3u8",
            "cache_status": "MISS",
        }

        report = CACHE.analyze(cold, warm, [document], [document])

        self.assertFalse(report["passed"])
        self.assertTrue(any("warm run contains non-HIT" in item for item in report["errors"]))


if __name__ == "__main__":
    unittest.main()
