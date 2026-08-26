import importlib.util
import hashlib
import sys
import unittest
from pathlib import Path


def load_validator_module():
    module_path = Path(__file__).with_name("validate_run_collection.py")
    spec = importlib.util.spec_from_file_location("validate_run_collection", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator_module()


class RunCollectionValidationTest(unittest.TestCase):
    def test_shared_token_requires_all_consumer_ips(self):
        token_jti = "11111111-1111-4111-8111-111111111111"
        token_id = f"cdn_{hashlib.sha256(token_jti.encode()).hexdigest()[:24]}"
        manifest = {
            "run_id": "run_a1",
            "scenario_id": "A1",
            "token_bindings": [
                {
                    "token_jti": token_jti,
                    "cdn_token_id": token_id,
                    "consumer_logical_client_ids": ["lc001", "lc002"],
                }
            ],
        }
        documents = [
            {"event_source": "ott-api", "event_kind": "token_issued", "cdn_token_id": token_id},
            {
                "event_source": "edge-nginx",
                "request_uri": "/hls/video_01/720p/seg_1.ts",
                "cdn_token_id": token_id,
                "client_ip": "192.168.0.151",
            },
            {
                "event_source": "edge-nginx",
                "request_uri": "/hls/video_01/720p/seg_2.ts",
                "cdn_token_id": token_id,
                "client_ip": "192.168.0.152",
            },
        ]
        graph = [
            {
                "token_id": token_id,
                "viewing_session_count": 2,
                "client_ips": ["192.168.0.151", "192.168.0.152"],
                "segment_client_ips": ["192.168.0.151", "192.168.0.152"],
                "device_ids": ["device_a", "device_b"],
                "segment_requests": 2,
            }
        ]

        report = VALIDATOR.analyze(
            manifest,
            documents,
            graph,
            [],
            {"lc001": "192.168.0.151", "lc002": "192.168.0.152"},
        )

        self.assertTrue(report["passed"])

    def test_consumer_without_graph_segment_requests_fails_validation(self):
        token_jti = "22222222-2222-4222-8222-222222222222"
        token_id = f"cdn_{hashlib.sha256(token_jti.encode()).hexdigest()[:24]}"
        manifest = {
            "run_id": "run_n1",
            "scenario_id": "N1",
            "token_bindings": [
                {
                    "token_jti": token_jti,
                    "cdn_token_id": token_id,
                    "consumer_logical_client_ids": ["lc001"],
                }
            ],
        }
        documents = [
            {"event_source": "ott-api", "event_kind": "token_issued", "cdn_token_id": token_id},
            {
                "event_source": "edge-nginx",
                "request_uri": "/hls/video_01/720p/seg_1.ts",
                "cdn_token_id": token_id,
                "client_ip": "192.168.0.151",
            },
        ]
        graph = [
            {
                "token_id": token_id,
                "viewing_session_count": 1,
                "client_ips": ["192.168.0.151"],
                "segment_client_ips": [],
                "device_ids": ["device_a"],
                "segment_requests": 0,
            }
        ]

        report = VALIDATOR.analyze(
            manifest,
            documents,
            graph,
            [],
            {"lc001": "192.168.0.151"},
        )

        self.assertFalse(report["passed"])
        self.assertTrue(any("no Neo4j segment requests" in item for item in report["errors"]))

    def test_raw_label_field_fails_validation(self):
        report = VALIDATOR.analyze(
            {"run_id": "bad", "scenario_id": "N1", "token_bindings": []},
            [{"scenario_id": "N1"}],
            [],
            [],
            {},
        )

        self.assertFalse(report["passed"])
        self.assertTrue(any("provenance leaked" in item for item in report["errors"]))


if __name__ == "__main__":
    unittest.main()
