import importlib.util
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


def load_runner_module():
    module_path = Path(__file__).resolve().parents[1] / "03_orchestration" / "run_scenario.py"
    spec = importlib.util.spec_from_file_location("run_scenario", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner_module()


class FakeExecutor:
    def __init__(self):
        self.counter = 0

    def run(self, assignment):
        self.counter += 1
        playbacks = []
        if assignment.spec["operation"] in {"issue", "vod", "live", "multi_vod"}:
            flow_count = len(assignment.spec.get("flows", [])) or 1
            for index in range(flow_count):
                token_jti = str(uuid.UUID(int=self.counter * 100 + index + 1))
                playback_id = str(uuid.UUID(int=self.counter * 1000 + index + 1))
                content_id = assignment.spec.get("content_id") or assignment.spec.get("flows", [{}])[index].get(
                    "content_id", "video_01"
                )
                playbacks.append(
                    {
                        "manifest_url": "http://edge/hls/video_01/master.m3u8?token=secret&sig=secret",
                        "token_binding": {
                            "token_jti": token_jti,
                            "cdn_token_id": f"cdn_{self.counter:024x}",
                            "playback_id": playback_id,
                            "content_id": content_id,
                            "issued_at": "2026-08-26T00:00:00Z",
                        },
                    }
                )
        traffic = {"failed_segments": 0, "rolling_playlist": True}
        return {
            "ok": True,
            "playbacks": playbacks,
            "traffic": traffic,
            "http_request_count": 10,
            "http_retry_count": 1,
            "http_failure_count": 0,
        }


class ScenarioCoordinatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clients = RUNNER.load_inventory(RUNNER.DEFAULT_INVENTORY)

    def run_scenario(self, scenario_id):
        with tempfile.TemporaryDirectory() as temp_dir:
            coordinator = RUNNER.ScenarioCoordinator(
                clients=self.clients,
                executor=FakeExecutor(),
                scenario_id=scenario_id,
                seed=12345,
                smoke=True,
                dataset_prefix="tnsm_100lc_20260826_smoke",
                output_dir=Path(temp_dir),
            )
            manifest, path = coordinator.execute()
            persisted = json.loads(path.read_text(encoding="utf-8"))
            return manifest, persisted

    def test_n1_writes_binding_and_retry_aware_counts(self):
        manifest, persisted = self.run_scenario("N1")

        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(len(manifest["logical_client_ids"]), 1)
        self.assertEqual(len(manifest["token_bindings"]), 1)
        self.assertEqual(manifest["observed_request_count"], 10)
        self.assertEqual(manifest["expected_request_count"], 9)
        self.assertEqual(manifest["parameters"]["cache_state"], "unspecified")
        self.assertNotIn("token=secret", json.dumps(persisted))

    def test_a1_uses_one_owner_and_two_real_consumers(self):
        manifest, _ = self.run_scenario("A1")

        self.assertEqual(len(manifest["logical_client_ids"]), 3)
        self.assertEqual(len(manifest["token_bindings"]), 1)
        binding = manifest["token_bindings"][0]
        self.assertEqual(len(binding["consumer_logical_client_ids"]), 2)
        self.assertNotIn(binding["owner_logical_client_id"], binding["consumer_logical_client_ids"])

    def test_every_supported_scenario_builds_a_completed_smoke_manifest(self):
        with mock.patch.object(RUNNER.time, "sleep", return_value=None):
            for scenario_id in sorted(RUNNER.SUPPORTED_SCENARIOS):
                with self.subTest(scenario_id=scenario_id):
                    manifest, _ = self.run_scenario(scenario_id)
                    self.assertEqual(manifest["status"], "completed")
                    self.assertGreater(len(manifest["logical_client_ids"]), 0)
                    self.assertGreater(len(manifest["token_bindings"]), 0)


if __name__ == "__main__":
    unittest.main()
