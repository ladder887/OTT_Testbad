import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "03_orchestration"


def load_module(name, filename):
    path = ORCHESTRATION_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR = load_module("test_generate_collection_matrix", "generate_collection_matrix.py")
MATRIX_RUNNER = load_module("test_run_collection_matrix", "run_collection_matrix.py")
SPLIT_AUDITOR = load_module(
    "test_audit_dataset_splits",
    str(Path("..") / "05_validation" / "audit_dataset_splits.py"),
)


class CollectionMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = GENERATOR.DEFAULT_INVENTORY
        cls.clients = GENERATOR.RUNNER.load_inventory(cls.inventory)
        cls.by_id = {client.logical_client_id: client for client in cls.clients}

    def build(self, phase="main", splits=("train", "validation", "test")):
        return GENERATOR.build_matrix(
            inventory=self.inventory,
            dataset_prefix="tnsm_100lc_20260826_matrix_test",
            phase=phase,
            splits=splits,
            repetitions=4,
            target_clients=20,
            base_seed=20260826,
            smoke=True,
            cache_state="warm",
            stagger_min_sec=0.0,
            stagger_max_sec=0.1,
        )

    def test_main_matrix_reserves_disjoint_clients_and_content_by_split(self):
        matrix = self.build()
        report = GENERATOR.validate_matrix(matrix, self.clients)

        self.assertTrue(report["passed"])
        for batch in matrix["batches"]:
            reserved = []
            for run in batch["runs"]:
                reserved.extend(run["reserved_client_ids"])
                hosts = {self.by_id[item].physical_host_id for item in run["reserved_client_ids"]}
                self.assertTrue(hosts.issubset(set(GENERATOR.CLIENT_SPLIT_HOSTS[run["data_split"]])))
                expected_contents = set(
                    GENERATOR.content_pool_for_run(run["data_split"], run["scenario_id"])
                )
                self.assertEqual(set(run["allowed_content_ids"]), expected_contents)
                self.assertEqual(set(run["preferred_content_ids"]), expected_contents)
                self.assertEqual(len(run["preferred_content_ids"]), len(expected_contents))
                self.assertTrue(set(run["planned_content_ids"]).issubset(expected_contents))
                self.assertEqual(
                    sum(run["planned_content_session_counts"].values()),
                    run["planned_session_count"],
                )
                self.assertEqual(
                    len(run["client_session_contributions"]),
                    run["required_client_count"],
                )
            self.assertEqual(len(reserved), len(set(reserved)))
            self.assertLessEqual(batch["planned_client_count"], batch["target_client_count"])
            self.assertEqual({run["class"] for run in batch["runs"]}, {"normal", "attack"})

        for split in ("train", "validation", "test"):
            balance = report["planned_balance"][split]
            for class_name in ("normal", "attack"):
                self.assertTrue(all(balance[class_name]["network_profile_session_counts"].values()))
                self.assertTrue(all(balance[class_name]["edge_session_counts"].values()))
                self.assertTrue(all(balance[class_name]["content_session_counts"].values()))
            for metric in (
                "network_profile_session_counts",
                "edge_session_counts",
                "physical_host_session_counts",
                "content_session_counts",
            ):
                self.assertLessEqual(balance["normal_attack_total_variation"][metric], 0.15)

    def test_low_strength_attack_variants_are_reserved_for_test(self):
        matrix = self.build()
        variants = {
            split: {
                (run["scenario_id"], run["scenario_variant"])
                for batch in matrix["batches"]
                if batch["data_split"] == split
                for run in batch["runs"]
                if run["class"] == "attack"
            }
            for split in ("train", "validation", "test")
        }

        self.assertIn(("A2", "fast"), variants["train"])
        self.assertNotIn(("A2", "stealth"), variants["train"])
        self.assertIn(("A2", "stealth"), variants["test"])
        self.assertNotIn(("A2", "fast"), variants["test"])

    def test_main_matrix_rejects_too_few_runs_for_train_content_overlap(self):
        with self.assertRaisesRegex(GENERATOR.MatrixError, "content_session_counts misses"):
            GENERATOR.build_matrix(
                inventory=self.inventory,
                dataset_prefix="tnsm_100lc_20260826_too_small",
                phase="main",
                splits=("train",),
                repetitions=1,
                target_clients=20,
                base_seed=20260826,
                smoke=True,
                cache_state="warm",
                stagger_min_sec=0.0,
                stagger_max_sec=0.1,
            )

    def test_calibration_matrix_contains_all_attack_strengths(self):
        matrix = self.build(phase="calibration", splits=("train",))
        variants = {
            (run["scenario_id"], run["scenario_variant"])
            for batch in matrix["batches"]
            for run in batch["runs"]
            if run["class"] == "attack"
        }
        self.assertEqual(variants, set(GENERATOR.CALIBRATION_ATTACK_VARIANTS))

    def test_client_lock_rejects_overlap_and_cleans_partial_reservations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_dir = Path(temp_dir)
            owner = {"matrix_id": "m1", "batch_id": "b1", "run_key": "r1"}
            with MATRIX_RUNNER.ClientReservation(lock_dir, ["lc001"], owner):
                with self.assertRaisesRegex(MATRIX_RUNNER.MatrixExecutionError, "already reserved"):
                    with MATRIX_RUNNER.ClientReservation(
                        lock_dir,
                        ["lc002", "lc001"],
                        {**owner, "run_key": "r2"},
                    ):
                        pass
                self.assertFalse((lock_dir / "lc002.lock").exists())
            self.assertFalse((lock_dir / "lc001.lock").exists())

    def test_matrix_runner_rejects_overlapping_batch(self):
        matrix = self.build(phase="calibration", splits=("train",))
        matrix["batches"][0]["runs"][1]["reserved_client_ids"][0] = (
            matrix["batches"][0]["runs"][0]["reserved_client_ids"][0]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "matrix.json"
            path.write_text(json.dumps(matrix), encoding="utf-8")
            with self.assertRaisesRegex(MATRIX_RUNNER.MatrixExecutionError, "overlapping clients"):
                MATRIX_RUNNER.load_matrix(path)

    def test_matrix_runner_rejects_incomplete_schema_v2_plan(self):
        matrix = self.build(phase="calibration", splits=("train",))
        matrix["batches"][0]["runs"][0]["preferred_content_ids"] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "matrix.json"
            path.write_text(json.dumps(matrix), encoding="utf-8")
            with self.assertRaisesRegex(MATRIX_RUNNER.MatrixExecutionError, "incomplete schema-v2"):
                MATRIX_RUNNER.load_matrix(path)

    def test_matrix_runner_requires_a_recent_passed_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "gate.json"
            path.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "sampled_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            resolved, report = MATRIX_RUNNER.load_recent_gate_report(path, 15.0)
            self.assertEqual(resolved, path.resolve())
            self.assertTrue(report["passed"])

            path.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "sampled_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MATRIX_RUNNER.MatrixExecutionError, "minutes old"):
                MATRIX_RUNNER.load_recent_gate_report(path, 15.0)

    def test_matrix_runner_selects_one_failed_run_for_repair(self):
        matrix = self.build(phase="calibration", splits=("train",))
        batch = matrix["batches"][0]
        selected = batch["runs"][1]

        repaired = MATRIX_RUNNER.select_run_subset([batch], (selected["run_key"],))

        self.assertEqual([run["run_key"] for run in repaired[0]["runs"]], [selected["run_key"]])
        self.assertEqual(repaired[0]["full_batch_run_count"], len(batch["runs"]))
        self.assertEqual(
            repaired[0]["planned_client_count"],
            len(selected["reserved_client_ids"]),
        )
        self.assertEqual(repaired[0]["runs"][0]["start_offset_sec"], 0.0)
        self.assertTrue(repaired[0]["run_repair"])

    def test_matrix_batch_resolves_one_live_channel_from_its_split(self):
        matrix = self.build()
        expected = {"train": ["live_01"], "validation": ["live_02"], "test": ["live_03"]}
        for batch in matrix["batches"]:
            if any(run["scenario_id"] in {"N7", "A7"} for run in batch["runs"]):
                self.assertEqual(
                    MATRIX_RUNNER.active_live_ids_for_batch(batch),
                    expected[batch["data_split"]],
                )

    def test_live_playlist_parser_requires_sequence_and_returns_latest_segment(self):
        state = MATRIX_RUNNER.parse_live_playlist(
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:42\n#EXTINF:2.0,\nseg_00042.ts\n"
            "#EXTINF:2.0,\nseg_00043.ts\n"
        )
        self.assertEqual(state, (42, "seg_00043.ts"))
        with self.assertRaisesRegex(MATRIX_RUNNER.MatrixExecutionError, "no sequence"):
            MATRIX_RUNNER.parse_live_playlist("#EXTM3U\n")

            path.write_text(
                json.dumps({"passed": False, "sampled_at": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MATRIX_RUNNER.MatrixExecutionError, "did not pass"):
                MATRIX_RUNNER.load_recent_gate_report(path, 15.0)

    def test_split_audit_accepts_isolated_batch_provenance(self):
        rows = [
            {
                "dataset_prefix": "tnsm_100lc_20260826_main",
                "collection_matrix_id": "matrix-main",
                "data_split": "train",
                "run_id": "run-normal",
                "matrix_run_key": "train-n1-001",
                "cdn_token_id": "token-normal",
                "logical_client_id": "lc001",
                "account_id": "account-normal",
                "device_id": "device-normal",
                "client_ip": "192.168.0.151",
                "physical_host_id": "pi01",
                "content_id": "video_01",
                "scenario_id": "N1",
                "scenario_variant": "standard",
                "label_binary": "0",
                "timing_scaled": "false",
                "start_time": "2026-08-26T00:00:00Z",
            },
            {
                "dataset_prefix": "tnsm_100lc_20260826_main",
                "collection_matrix_id": "matrix-main",
                "data_split": "train",
                "run_id": "run-attack",
                "matrix_run_key": "train-a2-001",
                "cdn_token_id": "token-attack",
                "logical_client_id": "lc002",
                "account_id": "account-attack",
                "device_id": "device-attack",
                "client_ip": "192.168.0.152",
                "physical_host_id": "pi01",
                "content_id": "video_02",
                "scenario_id": "A2",
                "scenario_variant": "fast",
                "label_binary": "1",
                "timing_scaled": "false",
                "start_time": "2026-08-26T00:01:00Z",
            },
        ]
        report = SPLIT_AUDITOR.audit_rows(
            rows,
            required_splits={"train"},
            enforce_main_contract=False,
            require_scenario_coverage=False,
        )
        self.assertTrue(report["passed"])

    def test_split_audit_rejects_account_crossing_splits(self):
        base = {
            "dataset_prefix": "tnsm_100lc_20260826_main",
            "collection_matrix_id": "matrix-main",
            "account_id": "shared-account",
            "scenario_variant": "standard",
            "label_binary": "0",
            "timing_scaled": "false",
            "scenario_id": "N1",
        }
        rows = [
            {
                **base,
                "data_split": "train",
                "run_id": "run-train",
                "matrix_run_key": "train-n1",
                "cdn_token_id": "token-train",
                "logical_client_id": "lc001",
                "device_id": "device-train",
                "client_ip": "192.168.0.151",
                "physical_host_id": "pi01",
                "content_id": "video_01",
            },
            {
                **base,
                "data_split": "test",
                "run_id": "run-test",
                "matrix_run_key": "test-n1",
                "cdn_token_id": "token-test",
                "logical_client_id": "lc081",
                "device_id": "device-test",
                "client_ip": "192.168.0.231",
                "physical_host_id": "pi09",
                "content_id": "video_13",
            },
        ]
        report = SPLIT_AUDITOR.audit_rows(
            rows,
            required_splits={"train", "test"},
            enforce_main_contract=False,
            require_scenario_coverage=False,
        )
        self.assertFalse(report["passed"])
        self.assertIn("account_id", report["cross_split_groups"])

    def test_main_split_audit_rejects_class_exclusive_content(self):
        rows = []
        for index, label in enumerate((0, 0, 1, 1), start=1):
            rows.append(
                {
                    "dataset_prefix": "tnsm_100lc_20260826_main",
                    "collection_matrix_id": "matrix-main",
                    "data_split": "train",
                    "run_id": f"run-{index}",
                    "matrix_run_key": f"train-run-{index}",
                    "cdn_token_id": f"token-{index}",
                    "logical_client_id": f"lc{index:03d}",
                    "account_id": f"account-{index}",
                    "device_id": f"device-{index}",
                    "client_ip": f"192.168.0.{150 + index}",
                    "physical_host_id": "pi01",
                    "content_id": "video_01" if label == 0 else "video_02",
                    "content_type": "vod",
                    "edge_id": "edge-kr",
                    "network_profile_id": "P0",
                    "scenario_id": "N1" if label == 0 else "A1",
                    "scenario_variant": "standard" if label == 0 else "high_fanout",
                    "label_binary": str(label),
                    "timing_scaled": "false",
                    "start_time": f"2026-08-26T00:0{index}:00Z",
                }
            )

        report = SPLIT_AUDITOR.audit_rows(
            rows,
            required_splits={"train"},
            enforce_main_contract=True,
            require_scenario_coverage=False,
        )

        self.assertFalse(report["passed"])
        self.assertTrue(any("content_id has class-exclusive values" in item for item in report["errors"]))


if __name__ == "__main__":
    unittest.main()
