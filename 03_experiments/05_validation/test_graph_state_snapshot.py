import importlib.util
import sys
import unittest
from pathlib import Path


def load_snapshot_module():
    module_path = Path(__file__).with_name("snapshot_graph_state.py")
    spec = importlib.util.spec_from_file_location("snapshot_graph_state", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SNAPSHOT = load_snapshot_module()


class GraphStateSnapshotTest(unittest.TestCase):
    def test_matching_aggregates_produce_a_stable_state(self):
        result_sets = [
            [{"label": "Request", "count": 2}, {"label": "ViewingSession", "count": 1}],
            [{"relationship_type": "MAKES_REQUEST", "count": 2}],
            [
                {
                    "viewing_session_id": "vs_1",
                    "stored_requests": 2,
                    "actual_requests": 2,
                    "stored_manifests": 1,
                    "actual_manifests": 1,
                    "stored_segments": 1,
                    "actual_segments": 1,
                    "stored_playback_starts": 0,
                    "actual_playback_starts": 0,
                    "stored_browse": 0,
                    "actual_browse": 0,
                    "stored_bytes": 100,
                    "actual_bytes": 100,
                    "tokens": ["cdn_1"],
                    "contents": ["video_01"],
                    "ips": ["192.168.0.151"],
                    "devices": ["device_1"],
                }
            ],
            [
                {
                    "content_id": "video_01",
                    "stored_requests": 2,
                    "actual_requests": 2,
                    "stored_bytes": 100,
                    "actual_bytes": 100,
                }
            ],
        ]

        first = SNAPSHOT.build_graph_state(result_sets)
        second = SNAPSHOT.build_graph_state(result_sets)

        self.assertEqual(first, second)
        self.assertEqual(first["viewing_session_aggregate_mismatch_count"], 0)
        self.assertEqual(first["content_aggregate_mismatch_count"], 0)

    def test_mismatched_stored_counter_is_reported(self):
        state = SNAPSHOT.build_graph_state(
            [
                [{"label": "ViewingSession", "count": 1}],
                [],
                [
                    {
                        "viewing_session_id": "vs_1",
                        "stored_requests": 4,
                        "actual_requests": 2,
                    }
                ],
                [],
            ]
        )

        self.assertEqual(state["viewing_session_aggregate_mismatch_count"], 1)


if __name__ == "__main__":
    unittest.main()
