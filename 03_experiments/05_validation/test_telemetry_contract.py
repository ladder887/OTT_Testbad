import importlib.util
import unittest
from pathlib import Path


def load_validator_module():
    module_path = Path(__file__).with_name("validate_telemetry_contract.py")
    spec = importlib.util.spec_from_file_location("validate_telemetry_contract", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator_module()


class TelemetryContractTest(unittest.TestCase):
    def test_valid_edge_and_api_documents_join_by_cdn_token_id(self):
        token_jti = "11111111-1111-4111-8111-111111111111"
        token_id = VALIDATOR.token_id_from_jti(token_jti)
        edge = {
            "@timestamp": "2026-08-19T00:00:00.123Z",
            "event_time_epoch": "1787097600.123",
            "event_source": "edge-nginx",
            "client_ip": "192.168.0.151",
            "edge_server": "edge-kr",
            "uri": "/hls/video_01/720p/seg_00001.ts",
            "request_uri": "/hls/video_01/720p/seg_00001.ts",
            "query_string": "-",
            "status": "200",
            "request_time_sec": "0.004",
            "request_id": "edge-kr-1787097600.123-1-1",
            "token_jti": token_jti,
            "cdn_token_id": token_id,
            "token_playback_id": "playback-1",
            "session_token": "-",
        }
        api = {
            "@timestamp": "2026-08-19T00:00:00.000Z",
            "event_time_epoch": 1787097600.0,
            "event_source": "ott-api",
            "event_kind": "token_issued",
            "query_string": "-",
            "token_jti": token_jti,
            "cdn_token_id": token_id,
            "token_playback_id": "playback-1",
            "token_owner_account_id": "1",
            "token_content_id": "video_01",
        }

        edge_errors, edge_ids = VALIDATOR.validate_edge_documents([edge])
        api_errors, api_ids = VALIDATOR.validate_api_documents([api])

        self.assertEqual(edge_errors, [])
        self.assertEqual(api_errors, [])
        self.assertEqual(edge_ids.intersection(api_ids), {token_id})

    def test_forbidden_label_field_is_rejected(self):
        errors = VALIDATOR.validate_forbidden_fields(
            [{"token_label": "attack"}],
            "edge",
        )
        self.assertEqual(len(errors), 1)

    def test_mismatched_jti_and_token_id_is_rejected(self):
        api = {
            "@timestamp": "2026-08-19T00:00:00.000Z",
            "event_time_epoch": 1787097600.0,
            "event_source": "ott-api",
            "event_kind": "token_issued",
            "query_string": "-",
            "token_jti": "11111111-1111-4111-8111-111111111111",
            "cdn_token_id": "cdn_000000000000000000000000",
            "token_playback_id": "playback-1",
            "token_owner_account_id": "1",
            "token_content_id": "video_01",
        }

        errors, _ = VALIDATOR.validate_api_documents([api])

        self.assertTrue(any("does not match token_jti" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
