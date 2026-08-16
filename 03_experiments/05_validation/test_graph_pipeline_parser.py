import importlib.util
import sys
import types
import unittest
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


class GraphPipelineParserTest(unittest.TestCase):
    def setUp(self):
        self.pipeline = GraphPipeline.__new__(GraphPipeline)

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

    def test_api_token_event_preserves_stable_hash_and_request_kind(self):
        parsed = self.pipeline.parse_nginx_log(
            {
                "@timestamp": "2026-07-27T10:00:01Z",
                "client_ip": "192.168.0.151",
                "request_uri": "/api/playback/start",
                "query_string": "content_id=video_01&run_id=run-001",
                "request_method": "POST",
                "cdn_token_id": "cdn_0123456789abcdef01234567",
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
        self.assertEqual(content_id, "video_01")
        self.assertEqual(parsed["cdn_token_id"], "cdn_0123456789abcdef01234567")
        self.assertEqual(parsed["run_id"], "run-001")


if __name__ == "__main__":
    unittest.main()
