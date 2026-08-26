import importlib.util
import sys
import unittest
from pathlib import Path


def load_gate_module():
    path = Path(__file__).resolve().parents[1] / "06_runtime_metrics" / "check_collection_gate.py"
    spec = importlib.util.spec_from_file_location("check_collection_gate", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = load_gate_module()


class CollectionGateTest(unittest.TestCase):
    def healthy_metrics(self):
        return {
            "name": "ott-user1",
            "ntp_synchronized": True,
            "ntp_value": "yes",
            "clock_offset_ms": 50.0,
            "cpu_count": 4,
            "load_1m": 1.0,
            "memory_used_percent": 50.0,
            "disk_free_percent": 40.0,
            "docker_probe_ok": True,
            "running_container_count": 10,
            "minimum_running_containers": 10,
            "require_docker": True,
            "unhealthy_container_count": 0,
        }

    def evaluate(self, metrics):
        return GATE.evaluate_node(
            metrics,
            max_clock_offset_ms=1500.0,
            max_load_per_cpu=1.25,
            max_memory_used_percent=90.0,
            min_disk_free_percent=10.0,
        )

    def test_healthy_node_passes(self):
        metrics = self.healthy_metrics()
        self.assertEqual(self.evaluate(metrics), [])
        self.assertEqual(metrics["load_per_cpu"], 0.25)

    def test_clock_load_and_container_failures_are_reported(self):
        metrics = self.healthy_metrics()
        metrics.update(
            {
                "ntp_synchronized": False,
                "clock_offset_ms": 3000.0,
                "load_1m": 8.0,
                "running_container_count": 9,
            }
        )
        errors = self.evaluate(metrics)
        self.assertTrue(any("NTP" in item for item in errors))
        self.assertTrue(any("clock offset" in item for item in errors))
        self.assertTrue(any("load per CPU" in item for item in errors))
        self.assertTrue(any("running containers" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
