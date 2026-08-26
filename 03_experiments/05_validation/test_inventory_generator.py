from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


GENERATOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "03_orchestration"
    / "generate_logical_client_inventory.py"
)
SPEC = importlib.util.spec_from_file_location("logical_inventory_generator", GENERATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
GENERATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)


class InventoryGeneratorTest(unittest.TestCase):
    def test_inventory_is_complete_and_unique(self) -> None:
        clients = GENERATOR.build_inventory()
        self.assertEqual(len(clients), 100)
        self.assertEqual(len({client.source_ip for client in clients}), 100)
        self.assertEqual(clients[0].source_ip, "192.168.0.151")
        self.assertEqual(clients[-1].source_ip, "192.168.0.250")
        self.assertEqual(clients[0].physical_host_id, "pi01")
        self.assertEqual(clients[-1].physical_host_id, "pi10")
        self.assertNotIn("lc001", clients[0].device_id)
        self.assertEqual(len({client.device_id for client in clients}), 100)

    def test_generated_files_cover_all_hosts(self) -> None:
        clients = GENERATOR.build_inventory()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            GENERATOR.write_json(clients, output_dir / "logical_clients.json")
            GENERATOR.write_host_compose_files(clients, output_dir)

            payload = json.loads(
                (output_dir / "logical_clients.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(payload["clients"]), 100)
            compose_files = sorted(output_dir.glob("pi*/docker-compose.yml"))
            self.assertEqual(len(compose_files), 10)
            for compose_file in compose_files:
                text = compose_file.read_text(encoding="utf-8")
                self.assertEqual(text.count("    image:"), 10)
                self.assertIn("driver: ipvlan", text)


if __name__ == "__main__":
    unittest.main()
