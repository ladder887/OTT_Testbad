import importlib.util
import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


def load_module(name, filename):
    runtime_dir = Path(__file__).resolve().parents[1] / "06_runtime_metrics"
    if str(runtime_dir) not in sys.path:
        sys.path.insert(0, str(runtime_dir))
    path = runtime_dir / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SAMPLER = load_module("test_runtime_sampler", "runtime_sampler.py")
RAMP = load_module("test_runtime_ramp_module", "run_concurrency_ramp.py")


class RuntimeSamplerTest(unittest.TestCase):
    def test_remote_probe_excludes_the_docker_exec_wrapper(self):
        self.assertIn("not line.lstrip().startswith('docker exec ')", SAMPLER.REMOTE_SAMPLE)

    def test_baseline_reset_keeps_document_deduplication(self):
        sampler = SAMPLER.RuntimeSampler(
            ssh_user="ottadmin",
            ssh_key=Path("unused"),
        )
        self.assertEqual(sampler.max_events_per_sample, 10_000)
        sampler.seen_es_documents.add("index:document")
        sampler.all_ingest_lag_ms.extend([10.0, 20.0])
        sampler.all_graph_lag_ms.append(30.0)
        sampler.event_query_truncated = True

        sampler.reset_measurement_accumulators()

        self.assertEqual(sampler.seen_es_documents, {"index:document"})
        self.assertEqual(sampler.all_ingest_lag_ms, [])
        self.assertEqual(sampler.all_graph_lag_ms, [])
        self.assertFalse(sampler.event_query_truncated)

    def test_elasticsearch_error_body_is_not_reported_as_zero_events(self):
        sampler = SAMPLER.RuntimeSampler(
            ssh_user="ottadmin",
            ssh_key=Path("unused"),
        )
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            SAMPLER,
            "http_json",
            return_value=({"error": {"type": "illegal_argument_exception"}}, 1.0),
        ):
            with self.assertRaises(SAMPLER.RuntimeSampleError):
                sampler._query_elasticsearch(now - timedelta(seconds=5), now)

    def test_percentiles_and_counter_rates(self):
        self.assertEqual(SAMPLER.percentile_summary([1, 2, 3, 4])["p50"], 2.5)
        previous = {
            "sample_epoch_ms": 1_000,
            "cpu_total_ticks": 100,
            "cpu_idle_ticks": 50,
            "network_rx_bytes": 1_000,
            "network_tx_bytes": 2_000,
            "disk_read_sectors": 10,
            "disk_write_sectors": 20,
        }
        current = {
            "sample_epoch_ms": 3_000,
            "cpu_total_ticks": 300,
            "cpu_idle_ticks": 100,
            "network_rx_bytes": 1_001_000,
            "network_tx_bytes": 2_002_000,
            "disk_read_sectors": 110,
            "disk_write_sectors": 220,
        }

        result = SAMPLER.derive_node_rates(current, previous)

        self.assertEqual(result["cpu_used_percent"], 75.0)
        self.assertEqual(result["network_rx_mbps"], 4.0)
        self.assertEqual(result["network_tx_mbps"], 8.0)


class RuntimeRampTest(unittest.TestCase):
    def clients(self):
        clients = []
        sequence = 1
        for host in range(1, 11):
            for offset in range(10):
                clients.append(
                    RAMP.scenario.LogicalClient(
                        logical_client_id=f"lc{sequence:03d}",
                        physical_host_id=f"pi{host:02d}",
                        physical_host_ip=f"192.168.0.{130 + host}",
                        source_ip=f"192.168.0.{150 + sequence}",
                        account_email=f"user{sequence}@test.com",
                        device_id=f"device_{sequence:03d}",
                        edge_id=("edge-kr", "edge-jp", "edge-sg", "edge-us")[offset % 4],
                        edge_base_url="http://edge",
                        network_profile_id=f"P{offset % 5}",
                    )
                )
                sequence += 1
        return clients

    def test_balanced_selection_spreads_every_twenty_client_prefix(self):
        selected = RAMP.select_balanced_clients(self.clients(), 20)
        hosts = Counter(item.physical_host_id for item in selected)
        profiles = Counter(item.network_profile_id for item in selected)

        self.assertEqual(set(hosts.values()), {2})
        self.assertLessEqual(max(profiles.values()) - min(profiles.values()), 1)

    def test_stage_summary_requires_measured_concurrency_and_graph_recovery(self):
        samples = [
            {
                "phase": "workload",
                "interval_sec": 10.0,
                "achieved_active_clients": 20,
                "graph_cursor_lag_sec": 5.0,
                "elasticsearch": {"edge_document_count": 100, "status_4xx_count": 0},
                "neo4j": {},
                "nodes": [],
                "errors": [],
            }
            for _ in range(3)
        ]
        samples.append(
            {
                "phase": "recovery",
                "interval_sec": 10.0,
                "achieved_active_clients": 0,
                "graph_cursor_lag_sec": 0.0,
                "elasticsearch": {"edge_document_count": 0, "status_4xx_count": 0},
                "neo4j": {},
                "nodes": [],
                "errors": [],
            }
        )
        results = [
            {"ok": True, "http_failure_count": 0, "http_retry_count": 0}
            for _ in range(20)
        ]

        summary = RAMP.stage_summary(
            target=20,
            samples=samples,
            results=results,
            token_coverage={
                "expected_token_count": 20,
                "graph_token_count": 20,
                "tokens_with_segments": 20,
            },
            lag_summary={
                "ingest_lag_ms": {},
                "event_to_graph_lag_ms": {},
                "event_query_truncated": False,
            },
            recovery_completed=True,
            recovery_sec=20.0,
            minimum_concurrency_ratio=0.9,
            minimum_sustain_sec=30.0,
            maximum_cursor_lag_sec=2.0,
        )

        self.assertTrue(summary["passed"])
        samples[0]["achieved_active_clients"] = 1
        samples[1]["achieved_active_clients"] = 1
        samples[2]["achieved_active_clients"] = 1
        self.assertFalse(
            RAMP.stage_summary(
                target=20,
                samples=samples,
                results=results,
                token_coverage={
                    "expected_token_count": 20,
                    "graph_token_count": 20,
                    "tokens_with_segments": 20,
                },
                lag_summary={"event_query_truncated": False},
                recovery_completed=True,
                recovery_sec=20.0,
                minimum_concurrency_ratio=0.9,
                minimum_sustain_sec=30.0,
                maximum_cursor_lag_sec=2.0,
            )["passed"]
        )


if __name__ == "__main__":
    unittest.main()
