import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


def load_graph_pipeline_module():
    sys.modules.setdefault(
        "elasticsearch",
        types.SimpleNamespace(Elasticsearch=object),
    )
    sys.modules.setdefault(
        "neo4j",
        types.SimpleNamespace(GraphDatabase=object),
    )

    module_path = (
        Path(__file__).resolve().parents[2]
        / "01_platform"
        / "05_graph_pipeline"
        / "service"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location("graph_pipeline_main", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GRAPH_PIPELINE_MODULE = load_graph_pipeline_module()
GraphPipeline = GRAPH_PIPELINE_MODULE.GraphPipeline


class FakeResult:
    def __init__(self, record=None):
        self.record = record

    def single(self):
        return self.record


class FakeSession:
    def __init__(self, active_viewing_session_id=None, has_playback_start=False):
        self.active_viewing_session_id = active_viewing_session_id
        self.has_playback_start = has_playback_start
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return False

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if "vs.viewing_session_id AS viewing_session_id" in query:
            if self.active_viewing_session_id:
                return FakeResult(
                    {
                        "viewing_session_id": self.active_viewing_session_id,
                        "has_playback_start": self.has_playback_start,
                    }
                )
            return FakeResult()
        return FakeResult()


class FakeDriver:
    def __init__(self, session):
        self.fake_session = session

    def session(self, **_kwargs):
        return self.fake_session


class GraphPipelineParserTest(unittest.TestCase):
    def setUp(self):
        self.pipeline = GraphPipeline.__new__(GraphPipeline)
        self.pipeline.neo4j_database = "neo4j"
        self.pipeline.viewing_session_idle_timeout_sec = 120
        self.pipeline.live_viewing_session_idle_timeout_sec = 45

    def test_query_real_ip_cannot_override_edge_observed_client_ip(self):
        parsed = self.pipeline.parse_nginx_log(
            {
                "@timestamp": "2026-07-27T10:00:00Z",
                "client_ip": "192.168.0.151",
                "remote_addr": "192.168.0.151",
                "request_uri": "/hls/video_01/720p/seg_00001.ts",
                "query_string": "token=sample&real_ip=10.0.0.9",
                "request_method": "GET",
                "status": 200,
            }
        )

        self.assertEqual(parsed["client_ip"], "192.168.0.151")
        self.assertEqual(parsed["query_params"]["real_ip"], "10.0.0.9")

    def test_api_token_event_preserves_operational_identity_without_provenance(self):
        parsed = self.pipeline.parse_nginx_log(
            {
                "@timestamp": "2026-07-27T10:00:01Z",
                "event_source": "ott-api",
                "client_ip": "192.168.0.151",
                "request_uri": "/api/playback/start",
                "query_string": "run_id=run-001&scenario_id=A1&label=attack",
                "request_method": "POST",
                "cdn_token_id": "cdn_0123456789abcdef01234567",
                "token_playback_id": "playback-001",
                "token_content_id": "video_01",
                "status": 200,
            }
        )

        request_kind = GraphPipeline._classify_request_kind(
            parsed["content_path"],
            parsed["method"],
        )
        content_id = GraphPipeline._extract_content_id_from_request(
            request_kind,
            parsed["content_path"],
            parsed["query_params"],
            parsed["referer"],
        )

        self.assertEqual(request_kind, "playback_start")
        self.assertEqual(content_id, "")
        self.assertEqual(parsed["token_content_id"], "video_01")
        self.assertEqual(parsed["cdn_token_id"], "cdn_0123456789abcdef01234567")
        self.assertEqual(parsed["playback_session_id"], "playback-001")
        self.assertNotIn("run_id", parsed)
        self.assertNotIn("scenario_id", parsed)
        self.assertNotIn("label", parsed)

    def test_edge_proxy_api_event_is_not_duplicated_in_graph(self):
        parsed = self.pipeline.parse_nginx_log(
            {
                "event_time_epoch": "1787065200.123",
                "event_source": "edge-nginx",
                "client_ip": "192.168.0.151",
                "request_uri": "/api/playback/start",
                "request_method": "POST",
                "status": 200,
                "request_id": "edge-kr-1787065200.123-1-1",
            }
        )
        fake_session = FakeSession()
        self.pipeline.driver = FakeDriver(fake_session)

        stats = self.pipeline.build_knowledge_graph([parsed])

        self.assertEqual(dict(stats), {})
        self.assertEqual(fake_session.calls, [])

    def test_epoch_event_time_and_request_seconds_are_normalized(self):
        epoch = 1787065200.123
        parsed = self.pipeline.parse_nginx_log(
            {
                "@timestamp": "2026-08-18T13:00:06Z",
                "timestamp": "2026-08-18T22:00:00+09:00",
                "event_time_epoch": str(epoch),
                "client_ip": "192.168.0.151",
                "request_uri": "/hls/video_01/720p/seg_00001.ts",
                "request_method": "GET",
                "request_time_sec": "0.004",
                "status": 200,
            }
        )

        expected = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        self.assertEqual(parsed["timestamp"], expected)
        self.assertEqual(parsed["response_time_ms"], 4.0)

    def test_token_relay_consumers_get_different_viewing_session_keys(self):
        common = {
            "playback_session_id": "playback-001",
            "cdn_token_id": "cdn_0123456789abcdef01234567",
            "observed_device_id": "device_browser",
            "content_id": "video_01",
            "request_id": "req-001",
        }
        first = GraphPipeline._build_viewing_session_key(
            "user_7",
            common["playback_session_id"],
            common["cdn_token_id"],
            "192.168.0.151",
            common["observed_device_id"],
            common["content_id"],
            common["request_id"],
        )
        second = GraphPipeline._build_viewing_session_key(
            "user_7",
            common["playback_session_id"],
            common["cdn_token_id"],
            "192.168.0.152",
            common["observed_device_id"],
            common["content_id"],
            common["request_id"],
        )

        self.assertNotEqual(first, second)

    def test_browse_and_playback_share_consumer_content_session_key(self):
        browse_key = GraphPipeline._build_viewing_session_key(
            "user_7",
            "-",
            "",
            "192.168.0.151",
            "device_browser",
            "video_01",
            "req_browse",
        )
        playback_key = GraphPipeline._build_viewing_session_key(
            "user_7",
            "playback-001",
            "cdn_0123456789abcdef01234567",
            "192.168.0.151",
            "device_browser",
            "video_01",
            "req_playback",
        )

        self.assertEqual(browse_key, playback_key)

    def test_active_session_is_reused_within_idle_timeout(self):
        fake_session = FakeSession(active_viewing_session_id="vs_existing")
        resolved = self.pipeline._resolve_viewing_session_id(
            fake_session,
            "vsk_example",
            "2026-08-19T00:00:00.000Z",
            "req_example",
        )

        self.assertEqual(resolved, "vs_existing")
        _, parameters = fake_session.calls[0]
        self.assertEqual(parameters["threshold"], "2026-08-18T23:58:00Z")

    def test_live_session_uses_shorter_idle_timeout(self):
        fake_session = FakeSession()
        self.pipeline._resolve_viewing_session_id(
            fake_session,
            "vsk_live",
            "2026-08-19T00:00:00.000Z",
            "req_live",
            idle_timeout_sec=self.pipeline.live_viewing_session_idle_timeout_sec,
        )

        _, parameters = fake_session.calls[0]
        self.assertEqual(parameters["threshold"], "2026-08-18T23:59:15Z")

    def test_repeated_playback_start_opens_a_new_session(self):
        fake_session = FakeSession(
            active_viewing_session_id="vs_existing",
            has_playback_start=True,
        )
        resolved = self.pipeline._resolve_viewing_session_id(
            fake_session,
            "vsk_example",
            "2026-08-19T00:00:00.000Z",
            "req_second_playback",
            start_new_playback=True,
        )

        self.assertNotEqual(resolved, "vs_existing")

    def test_first_playback_reuses_prior_browse_session(self):
        fake_session = FakeSession(
            active_viewing_session_id="vs_browse",
            has_playback_start=False,
        )
        resolved = self.pipeline._resolve_viewing_session_id(
            fake_session,
            "vsk_example",
            "2026-08-19T00:00:00.000Z",
            "req_first_playback",
            start_new_playback=True,
        )

        self.assertEqual(resolved, "vs_browse")

    def test_failed_hls_request_is_written_to_raw_graph(self):
        parsed = self.pipeline.parse_nginx_log(
            {
                "event_time_epoch": "1787065200.123",
                "client_ip": "192.168.0.151",
                "edge_server": "edge-kr",
                "request_uri": "/hls/video_01/720p/seg_00001.ts",
                "request_method": "GET",
                "status": 403,
                "cdn_token_id": "-",
                "request_id": "edge-kr-1787065200.123-1-1",
                "http_user_agent": "OTT-Test/1.0",
            }
        )
        fake_session = FakeSession()
        self.pipeline.driver = FakeDriver(fake_session)

        stats = self.pipeline.build_knowledge_graph([parsed])

        self.assertEqual(stats["Request"], 1)
        self.assertEqual(stats["CdnToken"], 0)
        request_writes = [
            parameters
            for query, parameters in fake_session.calls
            if "MERGE (r:Request" in query
        ]
        self.assertEqual(request_writes[0]["status"], 403)


if __name__ == "__main__":
    unittest.main()
