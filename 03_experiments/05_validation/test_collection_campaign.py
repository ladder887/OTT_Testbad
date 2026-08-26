import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "03_orchestration" / "run_collection_campaign.py"
SPEC = importlib.util.spec_from_file_location("test_run_collection_campaign", SCRIPT)
CAMPAIGN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CAMPAIGN
SPEC.loader.exec_module(CAMPAIGN)


class CollectionCampaignTest(unittest.TestCase):
    def matrix(self, manifest_dir="06_outputs/01_run_manifests/test"):
        return {
            "schema_version": 2,
            "matrix_id": "matrix-test",
            "dataset_prefix": "dataset-test",
            "phase": "main",
            "manifest_output_dir": manifest_dir,
            "batches": [
                {"batch_id": "train_b001", "data_split": "train", "runs": []},
                {"batch_id": "train_b002", "data_split": "train", "runs": []},
                {"batch_id": "test_b001", "data_split": "test", "runs": []},
            ],
        }

    def test_select_batches_preserves_order_and_limits(self):
        selected = CAMPAIGN.select_batches(self.matrix(), ("train",), (), 1)
        self.assertEqual([item["batch_id"] for item in selected], ["train_b001"])
        with self.assertRaisesRegex(CAMPAIGN.CampaignError, "unknown splits"):
            CAMPAIGN.select_batches(self.matrix(), ("validation",), (), None)

    def test_state_rejects_matrix_content_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            matrix_path = root / "matrix.json"
            matrix_path.write_text(json.dumps(self.matrix()), encoding="utf-8")
            state_path = root / "state.json"
            matrix_sha = CAMPAIGN.sha256_file(matrix_path)
            state = CAMPAIGN.new_state(self.matrix(), matrix_path, matrix_sha)
            CAMPAIGN.write_state(state_path, state)
            with self.assertRaisesRegex(CAMPAIGN.CampaignError, "matrix changed"):
                CAMPAIGN.load_state(state_path, self.matrix(), matrix_path, "different")

    def test_completed_execution_report_is_discovered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "batch.execution.json").write_text(
                json.dumps(
                    {
                        "matrix_id": "matrix-test",
                        "passed": True,
                        "batches": [{"batch_id": "train_b001", "passed": True}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                CAMPAIGN.discover_completed_batches(root, "matrix-test"),
                {"train_b001"},
            )

    def test_failed_or_interrupted_batch_blocks_implicit_retry(self):
        state = {
            "attempts": [
                {"batch_id": "train_b001", "status": "failed"},
                {"batch_id": "train_b002", "status": "gate_failed"},
            ]
        }
        self.assertEqual(CAMPAIGN.unresolved_attempts(state, set()), {"train_b001"})

    def test_main_split_order_requires_all_prior_batches(self):
        matrix = self.matrix()
        validation = [{"batch_id": "validation_b001", "data_split": "validation", "runs": []}]
        matrix["batches"].insert(2, validation[0])
        with self.assertRaisesRegex(CAMPAIGN.CampaignError, "before prior split"):
            CAMPAIGN.validate_split_order(matrix, validation, set())
        CAMPAIGN.validate_split_order(matrix, validation, {"train_b001", "train_b002"})

        with self.assertRaisesRegex(CAMPAIGN.CampaignError, "exists before prior splits"):
            CAMPAIGN.validate_split_order(matrix, [], {"test_b001"})

    def test_matrix_command_keeps_gate_and_validation_enabled(self):
        args = argparse.Namespace(
            ssh_user="ottadmin",
            ssh_key=Path("key"),
            remote_timeout_sec=1800.0,
            validation_wait_sec=180.0,
            live_setup_timeout_sec=60.0,
        )
        command = CAMPAIGN.matrix_command(Path("matrix.json"), "train_b001", Path("gate.json"), args)
        self.assertIn("--gate-report", command)
        self.assertIn("gate.json", command)
        self.assertNotIn("--skip-gate", command)
        self.assertNotIn("--skip-validation", command)

    def test_default_batch_timeout_includes_latest_start_offset(self):
        batch = {
            "runs": [
                {"start_offset_sec": 0.0},
                {"start_offset_sec": 3450.0},
            ]
        }
        args = argparse.Namespace(
            remote_timeout_sec=1800.0,
            validation_wait_sec=180.0,
            live_setup_timeout_sec=60.0,
        )
        self.assertEqual(CAMPAIGN.default_batch_timeout_sec(batch, args), 6090.0)


if __name__ == "__main__":
    unittest.main()
