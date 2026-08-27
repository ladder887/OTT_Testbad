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

    def run_scenario(self, scenario_id, variant="default"):
        with tempfile.TemporaryDirectory() as temp_dir:
            coordinator = RUNNER.ScenarioCoordinator(
                clients=self.clients,
                executor=FakeExecutor(),
                scenario_id=scenario_id,
                seed=12345,
                smoke=True,
                dataset_prefix="tnsm_100lc_20260826_smoke",
                output_dir=Path(temp_dir),
                variant=variant,
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
        manifest, _ = self.run_scenario("A1", "low_fanout")

        self.assertEqual(len(manifest["logical_client_ids"]), 3)
        self.assertEqual(len(manifest["token_bindings"]), 1)
        binding = manifest["token_bindings"][0]
        self.assertEqual(len(binding["consumer_logical_client_ids"]), 2)
        self.assertNotIn(binding["owner_logical_client_id"], binding["consumer_logical_client_ids"])
        phases = [item["traffic"]["phases"][0] for item in manifest["parameters"]["consumers"]]
        self.assertTrue(all(phase["start_mode"] == "fraction" for phase in phases))
        self.assertEqual([phase["start_fraction"] for phase in phases], [0.0, 0.25])
        self.assertTrue(all(0.0 <= phase["start_fraction"] < 0.5 for phase in phases))

    def test_n1_catalog_preview_uses_multiple_contents_and_separate_tokens(self):
        manifest, _ = self.run_scenario("N1", "catalog_preview")

        self.assertEqual(manifest["parameters"]["scenario_variant"], "catalog_preview")
        self.assertGreaterEqual(manifest["parameters"]["content_count"], 2)
        self.assertEqual(len(manifest["logical_client_ids"]), 1)
        self.assertEqual(len(manifest["token_bindings"]), manifest["parameters"]["content_count"])
        self.assertEqual(
            len({item["content_id"] for item in manifest["token_bindings"]}),
            manifest["parameters"]["content_count"],
        )

    def test_n6_flash_crowd_uses_one_content_with_independent_tokens(self):
        manifest, _ = self.run_scenario("N6", "flash_crowd")

        count = manifest["parameters"]["consumer_count"]
        self.assertEqual(count, 2)
        self.assertEqual(len(manifest["token_bindings"]), count)
        self.assertEqual(len({item["content_id"] for item in manifest["token_bindings"]}), 1)
        self.assertEqual(
            {item["owner_logical_client_id"] for item in manifest["token_bindings"]},
            set(manifest["logical_client_ids"]),
        )
        self.assertFalse(manifest["parameters"]["shared_account"])
        self.assertFalse(manifest["parameters"]["shared_token"])

    def test_n7_popular_channel_uses_independent_live_tokens(self):
        manifest, _ = self.run_scenario("N7", "popular_channel")

        count = manifest["parameters"]["consumer_count"]
        self.assertEqual(count, 2)
        self.assertEqual(len(manifest["token_bindings"]), count)
        self.assertEqual(len({item["content_id"] for item in manifest["token_bindings"]}), 1)
        self.assertFalse(manifest["parameters"]["shared_token"])

    def test_attack_strength_variants_are_explicit(self):
        a1_high, _ = self.run_scenario("A1", "high_fanout")
        a2_fast, _ = self.run_scenario("A2", "fast")
        a3_high, _ = self.run_scenario("A3", "high_parallel")

        self.assertEqual(a1_high["parameters"]["consumer_count"], 3)
        self.assertEqual(a2_fast["parameters"]["download_variant"], "fast")
        self.assertEqual(a3_high["parameters"]["worker_count"], 3)

    def test_invalid_variant_is_rejected(self):
        with self.assertRaisesRegex(RUNNER.CoordinatorError, "unsupported variant"):
            self.run_scenario("A2", "not_a_variant")

    def test_reserved_clients_and_content_split_are_enforced(self):
        reserved = tuple(client.logical_client_id for client in self.clients[70:72])
        selected_clients = [client for client in self.clients if client.logical_client_id in reserved]
        with tempfile.TemporaryDirectory() as temp_dir:
            coordinator = RUNNER.ScenarioCoordinator(
                clients=selected_clients,
                executor=FakeExecutor(),
                scenario_id="N6",
                seed=12345,
                smoke=True,
                dataset_prefix="tnsm_100lc_20260826_smoke",
                output_dir=Path(temp_dir),
                variant="household",
                reserved_client_ids=reserved,
                content_ids=("video_10", "video_11", "video_12"),
                data_split="validation",
                matrix_id="matrix-test",
                matrix_run_key="validation-n6-001",
            )
            manifest, _ = coordinator.execute()

        self.assertEqual(set(manifest["logical_client_ids"]), set(reserved))
        self.assertEqual(manifest["parameters"]["data_split"], "validation")
        self.assertEqual(manifest["parameters"]["collection_matrix_id"], "matrix-test")
        self.assertTrue(
            {item["content_id"] for item in manifest["parameters"]["members"]}.issubset(
                {"video_10", "video_11", "video_12"}
            )
        )

    def test_reserved_client_order_controls_the_token_owner(self):
        ordered_clients = [self.clients[13], self.clients[1], self.clients[22]]
        reserved = tuple(client.logical_client_id for client in ordered_clients)
        with tempfile.TemporaryDirectory() as temp_dir:
            coordinator = RUNNER.ScenarioCoordinator(
                clients=ordered_clients,
                executor=FakeExecutor(),
                scenario_id="A1",
                seed=12345,
                smoke=True,
                dataset_prefix="tnsm_100lc_20260826_smoke",
                output_dir=Path(temp_dir),
                variant="low_fanout",
                reserved_client_ids=reserved,
                content_ids=("video_01", "video_02"),
            )
            manifest, _ = coordinator.execute()

        self.assertEqual(manifest["parameters"]["owner_logical_client_id"], reserved[0])
        self.assertEqual(manifest["logical_client_ids"], list(reserved))

    def test_preferred_and_planned_contents_are_executed_exactly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            coordinator = RUNNER.ScenarioCoordinator(
                clients=[self.clients[70]],
                executor=FakeExecutor(),
                scenario_id="A2",
                seed=12345,
                smoke=True,
                dataset_prefix="tnsm_100lc_20260826_smoke",
                output_dir=Path(temp_dir),
                variant="stealth",
                content_ids=("video_10", "video_11", "video_12"),
                preferred_content_ids=("video_12", "video_10", "video_11"),
                planned_content_ids=("video_12", "video_10"),
            )
            manifest, _ = coordinator.execute()

        self.assertEqual(manifest["parameters"]["content_ids"], ["video_12", "video_10"])
        self.assertEqual(
            {item["content_id"] for item in manifest["token_bindings"]},
            {"video_10", "video_12"},
        )

    def test_long_view_uses_only_long_content_inside_the_reserved_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            coordinator = RUNNER.ScenarioCoordinator(
                clients=[self.clients[80]],
                executor=FakeExecutor(),
                scenario_id="N1",
                seed=12345,
                smoke=True,
                dataset_prefix="tnsm_100lc_20260826_smoke",
                output_dir=Path(temp_dir),
                variant="long",
                content_ids=("video_13", "video_14", "video_15"),
            )
            manifest, _ = coordinator.execute()

        self.assertIn(manifest["parameters"]["content_id"], {"video_13", "video_14"})

    def test_auto_variant_is_reproducible_for_the_same_seed(self):
        first, _ = self.run_scenario("N1", "auto")
        second, _ = self.run_scenario("N1", "auto")

        self.assertEqual(
            first["parameters"]["scenario_variant"],
            second["parameters"]["scenario_variant"],
        )

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
