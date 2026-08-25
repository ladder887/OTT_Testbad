import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def load_binding_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "04_data_tools"
        / "record_token_binding.py"
    )
    spec = importlib.util.spec_from_file_location("record_token_binding", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BINDING_MODULE = load_binding_module()


class RunManifestBindingTest(unittest.TestCase):
    def test_binding_is_written_once_without_service_log_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "run.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-001",
                        "logical_client_ids": ["lc001", "lc002"],
                        "token_bindings": [],
                    }
                ),
                encoding="utf-8",
            )
            token_jti = "11111111-1111-4111-8111-111111111111"
            binding = {
                "token_jti": token_jti,
                "cdn_token_id": BINDING_MODULE.expected_cdn_token_id(token_jti),
                "playback_id": "22222222-2222-4222-8222-222222222222",
                "content_id": "video_01",
                "owner_logical_client_id": "lc001",
                "consumer_logical_client_ids": ["lc001", "lc002"],
                "issued_at": "2026-08-19T00:00:00Z",
            }

            BINDING_MODULE.record_token_binding(manifest_path, binding)
            written = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(written["token_bindings"], [binding])
            with self.assertRaisesRegex(ValueError, "already exists"):
                BINDING_MODULE.record_token_binding(manifest_path, binding)

    def test_mismatched_token_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "run.json"
            manifest_path.write_text(
                json.dumps({"run_id": "run-001", "token_bindings": []}),
                encoding="utf-8",
            )
            binding = {
                "token_jti": "11111111-1111-4111-8111-111111111111",
                "cdn_token_id": "cdn_000000000000000000000000",
                "playback_id": "22222222-2222-4222-8222-222222222222",
                "content_id": "video_01",
                "owner_logical_client_id": "lc001",
                "consumer_logical_client_ids": ["lc001"],
                "issued_at": "2026-08-19T00:00:00Z",
            }

            with self.assertRaisesRegex(ValueError, "does not match token_jti"):
                BINDING_MODULE.record_token_binding(manifest_path, binding)


if __name__ == "__main__":
    unittest.main()
