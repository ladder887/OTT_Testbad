"""
Graph Pipeline - ViewingSession-centered log normalization and graph loading.

This service reads access events from a local log file or Elasticsearch,
normalizes them into the canonical schema, groups them into ViewingSession
entities, and writes the resulting graph to Neo4j. Model training and
inference are separate concerns and are not performed by this service.

Final raw graph nodes:
- Account
- ViewingSession
- CdnToken
- ClientIP
- Device
- Edge
- Content
- Segment
- Request
- Referrer

Core relationships:
- (Account)-[:HAS_VIEWING_SESSION]->(ViewingSession)
- (ViewingSession)-[:USES_CDN_TOKEN]->(CdnToken)
- (ViewingSession)-[:TARGETS_CONTENT]->(Content)
- (ViewingSession)-[:FROM_IP]->(ClientIP)
- (ViewingSession)-[:ON_DEVICE]->(Device)
- (ViewingSession)-[:SERVED_BY]->(Edge)
- (ViewingSession)-[:REFERRED_BY]->(Referrer)
- (ViewingSession)-[:MAKES_REQUEST]->(Request)
- (Request)-[:TARGETS_CONTENT]->(Content)
- (Request)-[:FOR_SEGMENT]->(Segment)
- (Segment)-[:BELONGS_TO]->(Content)

Request-level token/edge/ip/referrer values are stored as Request properties
instead of separate request-level relationships.
"""
import os
import time
import json
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import urlparse, parse_qsl, unquote
from elasticsearch import Elasticsearch
from neo4j import GraphDatabase

log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(level=log_level, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class GraphPipeline:
    def __init__(self):
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = os.environ["NEO4J_PASSWORD"]
        self.neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
        self.driver = GraphDatabase.driver(self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password))

        self.log_source = os.getenv("LOG_SOURCE", "file").lower()
        self.log_file = "/var/log/access-gateway/access.log"
        self.last_position = 0

        self.elasticsearch_url = os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
        self.elasticsearch_index = os.getenv(
            "ELASTICSEARCH_INDEX",
            "access-gateway-nginx-*,scrubber-nginx-*,filebeat-*,"
            ".ds-access-gateway-nginx-*,.ds-scrubber-nginx-*,.ds-filebeat-*",
        )
        self.es_poll_size = int(os.getenv("ES_POLL_SIZE", "500"))
        self.es_allowed_lateness_sec = max(
            0,
            int(os.getenv("ES_ALLOWED_LATENESS_SEC", "180")),
        )
        self.es_scroll_keepalive = os.getenv("ES_SCROLL_KEEPALIVE", "1m")
        self.es_default_start_timestamp = os.getenv("ES_START_TIMESTAMP", "1970-01-01T00:00:00Z")
        self.es_last_timestamp = self.es_default_start_timestamp
        self.es_seen_ids_at_last_timestamp = set()
        self.es_seen_documents = {}
        self.state_file = os.getenv(
            "GRAPH_PIPELINE_STATE_FILE",
            "/var/lib/graph-pipeline/checkpoint.json",
        )
        self.pending_file_position = None
        self.pending_es_cursor = None
        self.es_client = None
        self.enable_operational_audit = os.getenv(
            "ENABLE_OPERATIONAL_AUDIT", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.viewing_session_idle_timeout_sec = max(
            1,
            int(os.getenv("VIEWING_SESSION_IDLE_TIMEOUT_SEC", "120")),
        )
        self.live_viewing_session_idle_timeout_sec = max(
            1,
            int(os.getenv("LIVE_VIEWING_SESSION_IDLE_TIMEOUT_SEC", "45")),
        )

        if self.log_source in ("elasticsearch", "es"):
            self.es_client = Elasticsearch(self.elasticsearch_url, verify_certs=False)
        elif self.log_source != "file":
            logger.warning(f"Unknown LOG_SOURCE={self.log_source}, fallback to file mode")
            self.log_source = "file"

        self._load_checkpoint()
        logger.info(
            "Graph Pipeline initialized "
            f"(source={self.log_source}, neo4j_db={self.neo4j_database}, "
            f"operational_audit={self.enable_operational_audit})"
        )
        self._create_indexes()

    def _load_checkpoint(self):
        """Restore the last committed input position after a container restart."""
        if not self.state_file or not os.path.exists(self.state_file):
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as state_handle:
                state = json.load(state_handle)

            if state.get("source") == "file":
                self.last_position = max(0, int(state.get("file_position", 0)))
            elif state.get("source") in ("elasticsearch", "es"):
                timestamp = self._normalize_iso_timestamp(state.get("es_last_timestamp"))
                if timestamp:
                    self.es_last_timestamp = timestamp
                self.es_seen_ids_at_last_timestamp = {
                    str(doc_id)
                    for doc_id in state.get("es_seen_ids_at_last_timestamp", [])
                    if doc_id
                }
                saved_documents = state.get("es_seen_documents", {})
                if isinstance(saved_documents, dict):
                    self.es_seen_documents = {
                        str(doc_id): str(doc_timestamp)
                        for doc_id, doc_timestamp in saved_documents.items()
                        if doc_id and self._normalize_iso_timestamp(doc_timestamp)
                    }
                if not self.es_seen_documents:
                    self.es_seen_documents = {
                        str(doc_id): self.es_last_timestamp
                        for doc_id in self.es_seen_ids_at_last_timestamp
                    }

            logger.info(f"Loaded Graph Pipeline checkpoint from {self.state_file}")
        except Exception as exc:
            logger.warning(f"Checkpoint load failed; starting from configured defaults: {exc}")

    def _commit_checkpoint(self):
        """Commit a staged cursor only after the graph write has succeeded."""
        if self.pending_file_position is None and self.pending_es_cursor is None:
            return

        if self.pending_file_position is not None:
            self.last_position = self.pending_file_position
            self.pending_file_position = None

        if self.pending_es_cursor is not None:
            timestamp, seen_documents = self.pending_es_cursor
            self.es_last_timestamp = timestamp
            self.es_seen_documents = dict(seen_documents)
            self.es_seen_ids_at_last_timestamp = {
                doc_id
                for doc_id, doc_timestamp in self.es_seen_documents.items()
                if doc_timestamp == timestamp
            }
            self.pending_es_cursor = None

        state = {
            "schema_version": 2,
            "source": self.log_source,
            "file_position": self.last_position,
            "es_last_timestamp": self.es_last_timestamp,
            "es_seen_ids_at_last_timestamp": sorted(self.es_seen_ids_at_last_timestamp),
            "es_seen_documents": dict(sorted(self.es_seen_documents.items())),
            "es_allowed_lateness_sec": self.es_allowed_lateness_sec,
            "committed_at": datetime.utcnow().isoformat() + "Z",
        }

        state_dir = os.path.dirname(self.state_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        temp_file = f"{self.state_file}.tmp"
        with open(temp_file, "w", encoding="utf-8") as state_handle:
            json.dump(state, state_handle, ensure_ascii=False, sort_keys=True)
            state_handle.write("\n")
        os.replace(temp_file, self.state_file)
        logger.debug(f"Committed Graph Pipeline checkpoint to {self.state_file}")

    def _create_indexes(self):
        """Create indexes for the final ViewingSession graph schema."""
        with self.driver.session(database=self.neo4j_database) as session:
            indexes = [
                "CREATE INDEX account_id IF NOT EXISTS FOR (a:Account) ON (a.account_id)",
                "CREATE INDEX viewing_session_id IF NOT EXISTS FOR (vs:ViewingSession) ON (vs.viewing_session_id)",
                "CREATE INDEX viewing_session_key IF NOT EXISTS FOR (vs:ViewingSession) ON (vs.session_key)",
                "CREATE INDEX cdn_token_id IF NOT EXISTS FOR (t:CdnToken) ON (t.cdn_token_id)",
                "CREATE INDEX client_ip_address IF NOT EXISTS FOR (ip:ClientIP) ON (ip.ip_address)",
                "CREATE INDEX device_id IF NOT EXISTS FOR (d:Device) ON (d.device_id)",
                "CREATE INDEX edge_id IF NOT EXISTS FOR (e:Edge) ON (e.edge_id)",
                "CREATE INDEX content_content_id IF NOT EXISTS FOR (c:Content) ON (c.content_id)",
                "CREATE INDEX content_type IF NOT EXISTS FOR (c:Content) ON (c.type)",
                "CREATE INDEX segment_segment_id IF NOT EXISTS FOR (s:Segment) ON (s.segment_id)",
                "CREATE INDEX request_id IF NOT EXISTS FOR (r:Request) ON (r.request_id)",
                "CREATE INDEX request_kind IF NOT EXISTS FOR (r:Request) ON (r.kind)",
                "CREATE INDEX request_target_content IF NOT EXISTS FOR (r:Request) ON (r.target_content_id)",
                "CREATE INDEX request_graph_ingested_at IF NOT EXISTS FOR (r:Request) ON (r.graph_ingested_at)",
                "CREATE INDEX referrer_domain IF NOT EXISTS FOR (r:Referrer) ON (r.domain)",
            ]
            for idx in indexes:
                try:
                    session.run(idx)
                except Exception as e:
                    logger.debug(f"Index: {e}")

    @staticmethod
    def _extract_domain(url):
        """URL에서 도메인 추출"""
        try:
            if not url or url == "-":
                return None
            parsed = urlparse(url if url.startswith("http") else f"http://{url}")
            return parsed.netloc or None
        except:
            return None

    @staticmethod
    def _extract_filename(path):
        """경로에서 파일명만 추출 (쿼리 스트링 제거)"""
        if not path or path == "-":
            return None
        # 쿼리 스트링 제거
        path_without_query = path.split("?")[0]
        # 마지막 / 이후가 파일명
        filename = path_without_query.split("/")[-1] if "/" in path_without_query else path_without_query
        return filename if filename else None

    @staticmethod
    def classify_resource_type(url):
        """URL에서 리소스 타입 분류"""
        if not url or url == "-":
            return "other", "other"
        
        url_lower = url.lower()
        
        # 비디오 파일
        if any(ext in url_lower for ext in ['.mp4', '.m3u8', '.ts', '.webm', '.mkv']):
            return "video", "video"
        
        # HTML
        if '.html' in url_lower or url.endswith('/') or 'index' in url_lower:
            return "html", "web_resource"
        
        # CSS
        if '.css' in url_lower:
            return "css", "web_resource"
        
        # JavaScript
        if '.js' in url_lower and '.json' not in url_lower:
            return "js", "web_resource"
        
        # API
        if '/api/' in url_lower or '.json' in url_lower:
            return "api", "api"
        
        # 이미지
        if any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']):
            return "image", "static"
        
        # 기타
        return "other", "other"

    @staticmethod
    def _extract_title(path):
        """경로에서 타이틀 추출 (예: /hls/funny/episode1.m3u8 -> funny)"""
        if not path or path == "-":
            return "unknown"
        parts = path.strip("/").split("/")
        # /hls/타이틀/파일명 구조 가정
        if len(parts) >= 2:
            return parts[1]  # hls 다음 경로를 타이틀로
        return "unknown"

    @staticmethod
    def _strip_query(path):
        """요청 경로에서 query string 제거"""
        if not path or path == "-":
            return ""
        return str(path).split("?", 1)[0]

    @classmethod
    def _classify_request_kind(cls, path, method):
        """탐지에 사용하는 핵심 요청만 kind로 분류"""
        request_path = cls._strip_query(path)
        if not request_path:
            return None

        method_upper = str(method or "").upper().strip()

        if request_path.startswith("/hls/"):
            if request_path.endswith(".m3u8"):
                return "hls_manifest"
            if request_path.endswith(".ts"):
                return "hls_segment"
            return None

        if method_upper == "POST" and request_path == "/api/playback/start":
            return "playback_start"

        if method_upper == "GET" and request_path.startswith("/api/browse/content/"):
            return "browse_content"

        return None

    @staticmethod
    def _extract_watch_content_id_from_referer(referer):
        """watch 페이지 referer에서 콘텐츠 ID 추출"""
        if not referer or referer == "-":
            return ""

        try:
            parsed = urlparse(referer if str(referer).startswith("http") else f"http://{referer}")
            parts = [unquote(part).strip() for part in str(parsed.path or "").strip("/").split("/") if part]
            if len(parts) >= 2 and parts[0] == "watch":
                return parts[1]
        except Exception:
            return ""

        return ""

    @classmethod
    def _extract_content_id_from_request(cls, request_kind, request_path, query_params, referer):
        """요청 정보에서 대상 콘텐츠 ID 추출"""
        params = query_params if isinstance(query_params, dict) else {}

        query_content_id = str(params.get("content_id", "")).strip()
        if query_content_id:
            return query_content_id

        if request_kind == "browse_content":
            prefix = "/api/browse/content/"
            if request_path.startswith(prefix):
                raw = request_path[len(prefix) :].strip("/")
                if raw:
                    return unquote(raw.split("/")[0]).strip()

        if request_kind == "playback_start":
            return cls._extract_watch_content_id_from_referer(referer)

        if request_kind in ("hls_manifest", "hls_segment") and request_path.startswith("/hls/"):
            hls_parts = [part for part in request_path[len("/hls/") :].split("/") if part]
            if hls_parts:
                return unquote(hls_parts[0]).strip()

        return ""

    @staticmethod
    def _infer_region_from_edge(edge_id):
        """Edge ID 접미사로 지역 추정"""
        if not edge_id:
            return "UNKNOWN"
        edge_id = edge_id.lower()
        if edge_id.endswith("-kr"):
            return "KR"
        if edge_id.endswith("-jp"):
            return "JP"
        if edge_id.endswith("-sg"):
            return "SG"
        if edge_id.endswith("-us"):
            return "US"
        return "UNKNOWN"

    @staticmethod
    def _safe_graph_id(value, fallback="unknown"):
        text = str(value or "").strip()
        if not text or text == "-":
            text = fallback
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in text)
        return safe[:160] or fallback

    @classmethod
    def _build_viewing_session_key(
        cls,
        account_id,
        playback_session_id,
        cdn_token_id,
        client_ip,
        observed_device_id,
        content_id,
        request_id,
    ):
        consumer_parts = [
            value
            for value in (client_ip, observed_device_id)
            if value and value != "-"
        ]
        consumer_id = "|".join(consumer_parts) or "consumer_unknown"
        owner_scope = account_id if account_id and account_id != "-" else None
        if not owner_scope:
            owner_scope = (
                playback_session_id
                if playback_session_id and playback_session_id != "-"
                else cdn_token_id
            )
        seed_parts = [
            value
            for value in (owner_scope, consumer_id, content_id)
            if value and value != "-"
        ]
        if not seed_parts:
            seed_parts = [request_id]
        seed = "|".join(seed_parts)
        return "vsk_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _new_viewing_session_id(cls, session_key, timestamp, request_id):
        seed = f"{session_key}|{timestamp}|{request_id}"
        return "vs_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _parse_event_datetime(value):
        if not value or value == "-":
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _epoch_to_iso_timestamp(cls, value):
        try:
            if value in (None, "", "-"):
                return None
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
            return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            return None

    def _resolve_viewing_session_id(
        self,
        session,
        session_key,
        timestamp,
        request_id,
        idle_timeout_sec=None,
        start_new_playback=False,
    ):
        event_time = self._parse_event_datetime(timestamp)
        if event_time is None:
            return self._new_viewing_session_id(session_key, timestamp, request_id)

        if idle_timeout_sec is None:
            idle_timeout_sec = getattr(self, "viewing_session_idle_timeout_sec", 120)
        idle_timeout_sec = max(1, int(idle_timeout_sec))
        threshold = event_time - timedelta(seconds=idle_timeout_sec)
        result = session.run(
            """
            MATCH (vs:ViewingSession {session_key: $session_key})
            WHERE vs.last_time >= datetime($threshold)
              AND vs.start_time <= datetime($timestamp)
            RETURN
                vs.viewing_session_id AS viewing_session_id,
                coalesce(vs.has_playback_start, false) AS has_playback_start
            ORDER BY vs.last_time DESC
            LIMIT 1
            """,
            session_key=session_key,
            threshold=threshold.isoformat().replace("+00:00", "Z"),
            timestamp=event_time.isoformat().replace("+00:00", "Z"),
        )
        record = result.single()
        if (
            record
            and record["viewing_session_id"]
            and not (start_new_playback and record["has_playback_start"])
        ):
            return record["viewing_session_id"]
        return self._new_viewing_session_id(session_key, timestamp, request_id)

    @staticmethod
    def _infer_content_type(content_id, request_path=""):
        value = str(content_id or "").lower()
        path = str(request_path or "").lower()
        if value.startswith("live_") or "/hls/live_" in path:
            return "live"
        return "vod"

    @classmethod
    def _resolve_request_content_id(
        cls,
        request_kind,
        request_path,
        query_params,
        referer,
        token_content_id,
    ):
        token_content = str(token_content_id or "").strip()
        if token_content == "-":
            token_content = ""
        if request_kind == "playback_start" and token_content:
            return token_content
        extracted = cls._extract_content_id_from_request(
            request_kind,
            request_path,
            query_params,
            referer,
        )
        return extracted or token_content

    @staticmethod
    def _classify_device(user_agent):
        ua = str(user_agent or "").lower()
        if not ua or ua == "-":
            return "unknown"
        if "python" in ua or "curl" in ua or "wget" in ua:
            return "script"
        if "smart-tv" in ua or "tizen" in ua or "webos" in ua:
            return "tv"
        if "mobile" in ua or "iphone" in ua or "android" in ua:
            return "mobile"
        if "chrome" in ua or "safari" in ua or "firefox" in ua or "edge" in ua:
            return "browser"
        return "other"

    @classmethod
    def _device_id_from_user_agent(cls, user_agent):
        ua = str(user_agent or "").strip()
        if not ua or ua == "-":
            return "device_unknown"
        digest = hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16]
        return f"device_{digest}"

    @classmethod
    def _normalize_referrer(cls, referer):
        raw = str(referer or "").strip()
        if not raw or raw == "-":
            return "empty", "empty", False
        lowered = raw.lower()
        if lowered.startswith("chrome-extension://") or lowered.startswith("moz-extension://"):
            return "browser_extension", "extension", False
        domain = cls._extract_domain(raw)
        if not domain:
            return "malformed", "malformed", False
        domain_lower = domain.lower()
        host_only = domain_lower.split(":", 1)[0]
        legitimate_hosts = {"localhost", "127.0.0.1", "ott.local", "myott.com", "www.myott.com"}
        is_private_lab = host_only.startswith("192.168.") or host_only.startswith("10.") or host_only.startswith("172.")
        is_legitimate = host_only in legitimate_hosts or is_private_lab or "myott" in host_only
        return domain_lower, "legitimate" if is_legitimate else "foreign", is_legitimate

    @staticmethod
    def _normalize_iso_timestamp(value):
        """Elasticsearch range cursor로 쓸 수 있는 ISO8601 문자열만 반환"""
        if isinstance(value, str):
            candidate = value.strip()
            if len(candidate) >= 19 and candidate[4] == "-" and candidate[10] == "T":
                return candidate
        return None

    def _elasticsearch_query_start(self):
        cursor_time = self._parse_event_datetime(self.es_last_timestamp)
        if cursor_time is None or self.es_allowed_lateness_sec <= 0:
            return self.es_last_timestamp
        return (
            cursor_time - timedelta(seconds=self.es_allowed_lateness_sec)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @classmethod
    def _latest_iso_timestamp(cls, values, fallback):
        candidates = []
        for value in values:
            parsed = cls._parse_event_datetime(value)
            if parsed is not None:
                candidates.append((parsed, value))
        if not candidates:
            return fallback
        return max(candidates, key=lambda item: item[0])[1]

    def _prune_es_seen_documents(self, seen_documents, watermark):
        watermark_time = self._parse_event_datetime(watermark)
        if watermark_time is None:
            return dict(seen_documents)
        cutoff = watermark_time - timedelta(seconds=self.es_allowed_lateness_sec)
        return {
            doc_id: doc_timestamp
            for doc_id, doc_timestamp in seen_documents.items()
            if (
                (parsed := self._parse_event_datetime(doc_timestamp)) is not None
                and parsed >= cutoff
            )
        }

    @staticmethod
    def _elasticsearch_total_hits(result):
        hit_info = result.get("hits", {}).get("total", 0)
        return int(hit_info.get("value", 0)) if isinstance(hit_info, dict) else int(hit_info or 0)

    def _drain_elasticsearch_scroll(self, result):
        hits = list(result.get("hits", {}).get("hits", []))
        total_hits = self._elasticsearch_total_hits(result)
        scroll_id = result.get("_scroll_id")
        try:
            while scroll_id and len(hits) < total_hits:
                page = self.es_client.scroll(
                    scroll_id=scroll_id,
                    scroll=self.es_scroll_keepalive,
                )
                page_hits = list(page.get("hits", {}).get("hits", []))
                if not page_hits:
                    break
                hits.extend(page_hits)
                scroll_id = page.get("_scroll_id") or scroll_id
        finally:
            if scroll_id:
                try:
                    self.es_client.clear_scroll(scroll_id=scroll_id)
                except Exception as exc:
                    logger.debug(f"Elasticsearch scroll cleanup failed: {exc}")
        if len(hits) < total_hits:
            logger.warning(
                f"Elasticsearch scroll returned {len(hits)} of {total_hits} matching documents"
            )
        copied = dict(result)
        copied_hits = dict(result.get("hits", {}))
        copied_hits["hits"] = hits
        copied["hits"] = copied_hits
        return copied

    def parse_nginx_log(self, log_line):
        """Nginx JSON 로그 파싱"""
        try:
            if isinstance(log_line, str):
                log = json.loads(log_line)
            elif isinstance(log_line, dict):
                log = dict(log_line)
            else:
                return None

            # Filebeat 설정/버전에 따라 access JSON이 message 또는 json 하위에 들어올 수 있다.
            nested_json = log.get("json")
            if isinstance(nested_json, dict):
                merged = dict(nested_json)
                merged.update(log)
                log = merged

            message = log.get("message")
            if isinstance(message, str) and message.strip().startswith("{") and "request_uri" not in log:
                try:
                    parsed_message = json.loads(message)
                    if isinstance(parsed_message, dict):
                        merged = dict(parsed_message)
                        merged.update(log)
                        log = merged
                except Exception:
                    pass

            def _to_int(value, default=0):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default

            def _to_float(value, default=0.0):
                try:
                    if value in (None, "-"):
                        return default
                    return float(value)
                except (TypeError, ValueError):
                    return default

            # Query string 파싱
            query_string = log.get("query_string") or ""
            params = {}
            if query_string and query_string != "-":
                try:
                    for key, value in parse_qsl(query_string, keep_blank_values=True):
                        params[key] = value
                except Exception:
                    for param in query_string.split("&"):
                        if "=" in param:
                            key, value = param.split("=", 1)
                            params[key] = value

            # Use the address observed by the Edge. Query parameters are
            # provenance only and must never override logical-client identity.
            real_ip = log.get("client_ip", "-")
            # Gateway-normalized address (legacy/local fallback)
            if real_ip == "-":
                real_ip = log.get("client_real_ip", "-")
            # Explicit real_ip field from trusted ingestion
            if real_ip == "-":
                real_ip = log.get("real_ip", "-")
            # Socket address fallback
            if real_ip == "-":
                real_ip = log.get("remote_addr", "-")
            # Forwarded address is the final fallback only.
            if real_ip == "-":
                x_forwarded = log.get("x_forwarded_for", "-")
                if x_forwarded != "-":
                    real_ip = x_forwarded.split(",")[0].strip()
            
            # Docker gateway IP 필터링
            if real_ip == "172.18.0.1":
                real_ip = "device_unknown"

            playback_session_id = (
                log.get("token_playback_id")
                or params.get("playback_id")
                or params.get("pid")
                or log.get("token_session_id")
                or log.get("session_id", "-")
            )
            owner_auth_session_id = (
                log.get("token_owner_auth_session_id")
                or params.get("sid")
                or log.get("token_session_id")
                or "-"
            )
            user_id = (
                log.get("token_owner_account_id")
                or log.get("token_user_id")
                or log.get("user_id")
                or "-"
            )
            username = log.get("username", "-")
            owner_device_id = (
                log.get("token_owner_device_id")
                or log.get("token_device_id")
                or "-"
            )
            
            # account_id = user_id가 있으면 사용, 없으면 username 사용
            if user_id and user_id != "-":
                account_id = f"user_{user_id}"
            elif username and username != "-":
                account_id = f"guest_{username}"
            else:
                account_id = "-"
                
            content_path = log.get("request_uri") or log.get("url.original") or log.get("uri") or "-"
            
            # Referer 또는 Host에서 도메인 추출
            referer = log.get("http_referer", "-")
            host = log.get("http_host", "-")
            domain = self._extract_domain(referer) or self._extract_domain(host)

            # Content title과 filename
            title = self._extract_title(content_path)
            filename = self._extract_filename(content_path)
            
            # 리소스 타입 분류
            resource_type, resource_category = self.classify_resource_type(content_path)
            
            # 네트워크 패턴 정보
            connection_id = log.get("connection", "-")
            connection_requests = _to_int(log.get("connection_requests", 0), 0)
            http_range = log.get("http_range", "-")
            http_connection = log.get("http_connection", "-")
            http_accept = log.get("http_accept", "-")
            http_accept_language = log.get("http_accept_language", "-")
            keep_alive = http_connection.lower() == "keep-alive" if http_connection != "-" else False
            
            # 디버그 로그
            if filename:
                logger.debug(f"Extracted filename: '{filename}' from path: '{content_path}'")

            raw_status = log.get("status")
            if raw_status is None:
                raw_status = log.get("http.response.status_code")

            raw_size = log.get("bytes_sent")
            if raw_size is None:
                raw_size = log.get("http.response.body.bytes")

            if log.get("request_time_sec") not in (None, "-"):
                response_time_ms = _to_float(log.get("request_time_sec"), 0.0) * 1000.0
            else:
                raw_response_time = log.get("response_time_ms")
                if raw_response_time is None:
                    raw_response_time = log.get("upstream_response_time")
                response_time_ms = _to_float(raw_response_time, 0.0)

            request_method = log.get("request_method") or log.get("http.request.method") or "GET"

            timestamp_value = self._epoch_to_iso_timestamp(log.get("event_time_epoch"))
            if not timestamp_value:
                timestamp_value = log.get("@timestamp") or log.get("timestamp") or log.get("event.created")
            if not self._normalize_iso_timestamp(timestamp_value):
                timestamp_value = "1970-01-01T00:00:00Z"

            cdn_token_id = str(log.get("cdn_token_id") or "").strip()
            if cdn_token_id == "-":
                cdn_token_id = ""

            return {
                "timestamp": timestamp_value,
                "event_source": log.get("event_source", "edge-nginx"),
                "account_id": account_id,
                "user_id": user_id,
                "username": username,
                "client_ip": real_ip,
                "client_region": log.get("client_region", "UNKNOWN"),
                "edge_server": log.get("edge_server", "-"),
                "domain": domain,
                "cdn_token_id": cdn_token_id,
                "token_jti": log.get("token_jti", "-"),
                "playback_session_id": playback_session_id if playback_session_id else "-",
                "owner_auth_session_id": owner_auth_session_id,
                "owner_device_id": owner_device_id,
                "observed_device_id": log.get("observed_device_id", "-"),
                "token_content_id": log.get("token_content_id", "-"),
                "token_issued_at": log.get("token_issued_at", "-"),
                "token_expires": log.get("token_expires", "-"),
                "token_ttl_sec": _to_float(log.get("token_ttl_sec", 0.0), 0.0),
                "token_ttl_remaining_sec": _to_float(log.get("token_ttl_remaining_sec", 0.0), 0.0),
                "token_valid": log.get("token_valid", "-"),
                "token_edge_match": log.get("token_edge_match", "-"),
                "referer": referer,
                "query_params": params,
                "content_path": content_path,
                "content_title": title,
                "content_filename": filename,
                "method": request_method,
                "status": _to_int(raw_status, 0),
                "size": _to_int(raw_size, 0),
                "response_time_ms": response_time_ms,
                "cache_status": log.get("cache_status", "-"),
                "request_log_id": log.get("request_id", "-"),
                "user_agent": log.get("http_user_agent", "-"),
                "resource_type": resource_type,
                "resource_category": resource_category,
                "connection_id": connection_id,
                "connection_requests": connection_requests,
                "http_range": http_range,
                "keep_alive": keep_alive,
                "http_accept": http_accept,
                "http_accept_language": http_accept_language,
            }
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None

    def _read_new_logs_from_file(self):
        """로컬 access.log tail"""
        logs = []
        try:
            if not os.path.exists(self.log_file):
                return logs

            current_size = os.path.getsize(self.log_file)
            read_position = self.last_position
            if current_size < read_position:
                read_position = 0

            with open(self.log_file, "r") as f:
                f.seek(read_position)
                for line in f:
                    parsed = self.parse_nginx_log(line.strip())
                    if parsed:
                        logs.append(parsed)
                self.pending_file_position = f.tell()

            logger.info(f"Read {len(logs)} new log entries from file")
            return logs
        except Exception as e:
            logger.error(f"File read error: {e}")
            return logs

    def _read_new_logs_from_elasticsearch(self):
        """Elasticsearch 인덱스에서 신규 로그 poll"""
        logs = []
        if not self.es_client:
            return logs

        try:
            configured_patterns = [p.strip() for p in self.elasticsearch_index.split(",") if p.strip()]
            resolved_indices = []
            for pattern in configured_patterns:
                try:
                    index_rows = self.es_client.cat.indices(
                        index=pattern,
                        format="json",
                        expand_wildcards="all",
                    )
                    for row in index_rows:
                        idx_name = row.get("index")
                        if idx_name and idx_name not in resolved_indices:
                            resolved_indices.append(idx_name)
                except Exception as e:
                    logger.debug(f"Index resolve skipped for pattern '{pattern}': {e}")

            search_target = ",".join(resolved_indices) if resolved_indices else self.elasticsearch_index
            logger.info(
                f"ES search target resolved: patterns={configured_patterns}, resolved_count={len(resolved_indices)}"
            )
            query_start = self._elasticsearch_query_start()

            result = self.es_client.search(
                index=search_target,
                size=self.es_poll_size,
                track_total_hits=True,
                allow_no_indices=True,
                ignore_unavailable=True,
                expand_wildcards="all",
                scroll=self.es_scroll_keepalive,
                sort=[
                    {"@timestamp": {"order": "asc", "unmapped_type": "date", "missing": "_last"}},
                    {"_doc": {"order": "asc"}},
                ],
                query={
                    "range": {
                        "@timestamp": {
                            "gte": query_start,
                        }
                    }
                },
            )
            result = self._drain_elasticsearch_scroll(result)

            total_hits = self._elasticsearch_total_hits(result)

            count_value = 0
            if total_hits == 0:
                logger.warning(
                    "No Elasticsearch hits found for "
                    f"index='{search_target}' and @timestamp>={query_start}"
                )

                try:
                    count_check = self.es_client.count(
                        index=search_target,
                        allow_no_indices=True,
                        ignore_unavailable=True,
                        expand_wildcards="all",
                        query={"match_all": {}},
                    )
                    count_value = count_check.get("count", 0)
                    logger.warning(
                        f"ES self-check count(match_all) on '{search_target}': {count_value}"
                    )
                except Exception as e:
                    logger.warning(f"ES self-check count failed: {e}")

                # 초기 부팅 시 @timestamp 매핑/필드 이슈가 있으면 match_all로 1회 백필 시도
                if self.es_last_timestamp == self.es_default_start_timestamp:
                    bootstrap = self.es_client.search(
                        index=search_target,
                        size=self.es_poll_size,
                        track_total_hits=True,
                        allow_no_indices=True,
                        ignore_unavailable=True,
                        expand_wildcards="all",
                        scroll=self.es_scroll_keepalive,
                        sort=[
                            {"@timestamp": {"order": "asc", "unmapped_type": "date", "missing": "_last"}},
                            {"_doc": {"order": "asc"}},
                        ],
                        query={"match_all": {}},
                    )
                    bootstrap = self._drain_elasticsearch_scroll(bootstrap)

                    bootstrap_total_hits = self._elasticsearch_total_hits(bootstrap)

                    if bootstrap_total_hits > 0:
                        logger.warning(
                            "Primary @timestamp range returned 0, "
                            f"but bootstrap match_all found {bootstrap_total_hits} hits"
                        )
                        result = bootstrap
                        total_hits = bootstrap_total_hits
                    else:
                        logger.warning("Bootstrap match_all also returned 0 hits")

                # count는 있는데 range/match_all이 모두 0이면 _id 정렬 강제 백필
                if total_hits == 0 and count_value > 0:
                    id_fallback = self.es_client.search(
                        index=search_target,
                        size=self.es_poll_size,
                        track_total_hits=True,
                        allow_no_indices=True,
                        ignore_unavailable=True,
                        expand_wildcards="all",
                        scroll=self.es_scroll_keepalive,
                        sort=[{"_doc": {"order": "asc"}}],
                        query={"match_all": {}},
                    )
                    id_fallback = self._drain_elasticsearch_scroll(id_fallback)

                    id_total_hits = self._elasticsearch_total_hits(id_fallback)

                    if id_total_hits > 0:
                        logger.warning(
                            "Range/bootstrap returned 0 but doc-sort fallback found "
                            f"{id_total_hits} hits"
                        )
                        result = id_fallback
                        total_hits = id_total_hits

            processed_docs = []
            newly_seen_documents = {}
            known_document_ids = set(self.es_seen_documents)
            sample_new_source = None
            for hit in result.get("hits", {}).get("hits", []):
                source = hit.get("_source", {})
                doc_id = hit.get("_id")
                doc_index = hit.get("_index", "")
                document_key = f"{doc_index}:{doc_id}" if doc_index else str(doc_id)
                doc_ts = self._normalize_iso_timestamp(
                    source.get("@timestamp") or source.get("timestamp") or source.get("event.created")
                )
                if not doc_ts:
                    sort_values = hit.get("sort", [])
                    if sort_values:
                        doc_ts = self._normalize_iso_timestamp(sort_values[0])
                if not doc_ts:
                    doc_ts = datetime.utcnow().isoformat() + "Z"

                if document_key in known_document_ids:
                    continue
                known_document_ids.add(document_key)
                newly_seen_documents[document_key] = doc_ts
                if sample_new_source is None:
                    sample_new_source = source

                parsed = self.parse_nginx_log(source)
                if parsed:
                    processed_docs.append((doc_ts, document_key, parsed))

            if newly_seen_documents:
                seen_documents = dict(self.es_seen_documents)
                seen_documents.update(newly_seen_documents)
                latest_ts = self._latest_iso_timestamp(
                    [self.es_last_timestamp, *newly_seen_documents.values()],
                    self.es_last_timestamp,
                )
                self.pending_es_cursor = (
                    latest_ts,
                    self._prune_es_seen_documents(seen_documents, latest_ts),
                )

            if not processed_docs:
                if newly_seen_documents:
                    sample_source = sample_new_source if isinstance(sample_new_source, dict) else {}
                    sample_keys = list(sample_source.keys())[:20]
                    sample_message = sample_source.get("message")
                    logger.warning(
                        f"Elasticsearch returned {len(newly_seen_documents)} new hits "
                        "but no parsable documents were produced"
                    )
                    if sample_keys:
                        logger.warning(f"Sample _source keys: {sample_keys}")
                    if isinstance(sample_message, str):
                        logger.warning(f"Sample message head: {sample_message[:200]}")
                logger.info("Read 0 new log entries from Elasticsearch")
                return logs

            for _, _, parsed in processed_docs:
                logs.append(parsed)

            logger.info(f"Read {len(logs)} new log entries from Elasticsearch")
            return logs
        except Exception as e:
            logger.error(f"Elasticsearch read error: {e}")
            return logs

    def read_new_logs(self):
        """신규 로그 읽기 (file 또는 elasticsearch)"""
        if self.log_source in ("elasticsearch", "es"):
            return self._read_new_logs_from_elasticsearch()
        return self._read_new_logs_from_file()

    def build_knowledge_graph(self, logs):
        """Build the final ViewingSession-centered raw knowledge graph."""
        with self.driver.session(database=self.neo4j_database) as session:
            stats = defaultdict(int)

            ordered_logs = sorted(logs, key=lambda item: item.get("timestamp", ""))
            for log in ordered_logs:
                timestamp = log["timestamp"]
                account_id = log.get("account_id", "-")
                username = log.get("username", "-")
                user_id = log.get("user_id", "-")
                client_ip = log.get("client_ip", "-")
                app_session_id = log.get("playback_session_id", "-")
                owner_auth_session_id = log.get("owner_auth_session_id", "-")
                owner_device_id = log.get("owner_device_id", "-")
                content_path = log.get("content_path", "-")
                request_path = self._strip_query(content_path)
                request_kind = self._classify_request_kind(content_path, log.get("method"))
                if not request_kind:
                    continue
                if (
                    request_kind in {"browse_content", "playback_start"}
                    and log.get("event_source") != "ott-api"
                ):
                    # The Edge proxy log and the API telemetry describe the same
                    # API call. Only the API event carries the owner/content/token
                    # identity needed by the graph.
                    continue

                query_params = log.get("query_params") if isinstance(log.get("query_params"), dict) else {}
                referer = log.get("referer", "-")
                request_content_id = self._resolve_request_content_id(
                    request_kind,
                    request_path,
                    query_params,
                    referer,
                    log.get("token_content_id", "-"),
                )

                token_issued_at = log.get("token_issued_at", "-") or "-"
                token_expires = log.get("token_expires", "-") or "-"
                token_ttl_sec = log.get("token_ttl_sec", 0.0) or 0.0
                token_ttl_remaining_sec = log.get("token_ttl_remaining_sec", 0.0) or 0.0

                edge_id = log.get("edge_server") or "edge-unknown"
                edge_region = log.get("client_region", "UNKNOWN")
                if not edge_region or edge_region == "-" or edge_region == "UNKNOWN":
                    edge_region = self._infer_region_from_edge(edge_id)

                request_log_id = log.get("request_log_id", "-")
                request_seed = request_log_id if request_log_id and request_log_id != "-" else f"{edge_id}|{timestamp}|{log.get('connection_id', '-')}|{client_ip}|{content_path}"
                request_id = "req_" + hashlib.sha256(request_seed.encode("utf-8")).hexdigest()[:24]
                cdn_token_id = log.get("cdn_token_id", "")
                observed_device_id = str(log.get("observed_device_id") or "").strip()
                if not observed_device_id or observed_device_id == "-":
                    observed_device_id = self._device_id_from_user_agent(log.get("user_agent", "-"))
                content_type = self._infer_content_type(request_content_id, request_path)
                viewing_session_key = self._build_viewing_session_key(
                    account_id,
                    app_session_id,
                    cdn_token_id,
                    client_ip,
                    observed_device_id,
                    request_content_id,
                    request_id,
                )
                viewing_session_id = self._resolve_viewing_session_id(
                    session,
                    viewing_session_key,
                    timestamp,
                    request_id,
                    idle_timeout_sec=(
                        getattr(self, "live_viewing_session_idle_timeout_sec", 45)
                        if content_type == "live"
                        else getattr(self, "viewing_session_idle_timeout_sec", 120)
                    ),
                    start_new_playback=request_kind == "playback_start",
                )
                is_browse = request_kind == "browse_content"
                is_playback_start = request_kind == "playback_start"
                is_manifest = request_kind == "hls_manifest"
                is_segment = request_kind == "hls_segment"
                device_type = self._classify_device(log.get("user_agent", "-"))
                referrer_domain, referrer_category, referrer_legitimate = self._normalize_referrer(referer)

                session.run(
                    """
                    MERGE (r:Request {request_id: $request_id})
                    ON CREATE SET
                        r.first_seen = datetime($timestamp),
                        r.graph_ingested_at = datetime($graph_ingested_at)
                    SET
                        r.timestamp = datetime($timestamp),
                        r.method = $method,
                        r.path = $request_path,
                        r.kind = $request_kind,
                        r.target_content_id = CASE WHEN $content_id = '' THEN null ELSE $content_id END,
                        r.cdn_token_id = CASE WHEN $cdn_token_id = '' THEN null ELSE $cdn_token_id END,
                        r.app_session_id = CASE WHEN $app_session_id = '-' THEN null ELSE $app_session_id END,
                        r.edge_id = $edge_id,
                        r.client_ip = CASE WHEN $client_ip = '-' THEN null ELSE $client_ip END,
                        r.referrer_domain = $referrer_domain,
                        r.referer = $referer,
                        r.status = $status,
                        r.bytes_sent = $bytes_sent,
                        r.response_time_ms = $response_time_ms,
                        r.cache_status = $cache_status,
                        r.range_header = $range_header,
                        r.keep_alive = $keep_alive,
                        r.user_agent = $user_agent,
                        r.observed_device_id = $observed_device_id,
                        r.token_issued_at = CASE WHEN $token_issued_at = '-' THEN r.token_issued_at ELSE $token_issued_at END,
                        r.token_expires = CASE WHEN $token_expires = '-' THEN r.token_expires ELSE $token_expires END,
                        r.token_ttl_sec = $token_ttl_sec,
                        r.token_ttl_remaining_sec = $token_ttl_remaining_sec
                    """,
                    request_id=request_id,
                    timestamp=timestamp,
                    graph_ingested_at=datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z"),
                    method=log.get("method", "GET"),
                    request_path=request_path,
                    request_kind=request_kind,
                    content_id=request_content_id,
                    cdn_token_id=cdn_token_id,
                    app_session_id=app_session_id,
                    edge_id=edge_id,
                    client_ip=client_ip,
                    referrer_domain=referrer_domain,
                    referer=referer,
                    status=log.get("status", 0),
                    bytes_sent=log.get("size", 0),
                    response_time_ms=log.get("response_time_ms", 0.0),
                    cache_status=log.get("cache_status", "-"),
                    range_header=log.get("http_range", "-"),
                    keep_alive=log.get("keep_alive", False),
                    user_agent=log.get("user_agent", "-"),
                    observed_device_id=observed_device_id,
                    token_issued_at=token_issued_at,
                    token_expires=token_expires,
                    token_ttl_sec=token_ttl_sec,
                    token_ttl_remaining_sec=token_ttl_remaining_sec,
                )
                stats["Request"] += 1

                if request_content_id:
                    session.run(
                        """
                        MERGE (c:Content {content_id: $content_id})
                        ON CREATE SET
                            c.title = $content_id,
                            c.type = $content_type,
                            c.first_accessed = datetime($timestamp),
                            c.request_count = 0,
                            c.total_bytes = 0
                        SET
                            c.type = CASE
                                WHEN c.type IS NULL OR c.type IN ['CATALOG', 'HLS_STREAM'] THEN $content_type
                                ELSE c.type
                            END,
                            c.last_accessed = CASE
                                WHEN c.last_accessed IS NULL OR datetime($timestamp) > c.last_accessed
                                THEN datetime($timestamp)
                                ELSE c.last_accessed
                            END
                        """,
                        content_id=request_content_id,
                        content_type=content_type,
                        timestamp=timestamp,
                        bytes_sent=log.get("size", 0),
                    )
                    stats["Content"] += 1

                session.run(
                    """
                    MERGE (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                    ON CREATE SET
                        vs.start_time = datetime($timestamp),
                        vs.session_key = $session_key,
                        vs.request_count = 0,
                        vs.total_manifest_requests = 0,
                        vs.total_segment_requests = 0,
                        vs.total_playback_start_requests = 0,
                        vs.total_browse_requests = 0,
                        vs.total_bytes = 0,
                        vs.has_browse = false,
                        vs.has_playback_start = false,
                        vs.playback_without_browse = false
                    SET
                        vs.last_time = CASE
                            WHEN vs.last_time IS NULL OR datetime($timestamp) > vs.last_time THEN datetime($timestamp)
                            ELSE vs.last_time
                        END,
                        vs.end_time = CASE
                            WHEN vs.end_time IS NULL OR datetime($timestamp) > vs.end_time THEN datetime($timestamp)
                            ELSE vs.end_time
                        END,
                        vs.app_session_id = CASE WHEN $app_session_id = '-' THEN vs.app_session_id ELSE $app_session_id END,
                        vs.account_id = CASE WHEN $account_id = '-' THEN vs.account_id ELSE $account_id END,
                        vs.content_id = CASE WHEN $content_id = '' THEN vs.content_id ELSE $content_id END,
                        vs.content_type = CASE WHEN $content_id = '' THEN vs.content_type ELSE $content_type END,
                        vs.is_live = CASE WHEN $content_type = 'live' THEN true ELSE coalesce(vs.is_live, false) END,
                        vs.token_issued_at = CASE WHEN $token_issued_at = '-' THEN vs.token_issued_at ELSE $token_issued_at END,
                        vs.token_expires = CASE WHEN $token_expires = '-' THEN vs.token_expires ELSE $token_expires END,
                        vs.token_ttl_sec = CASE WHEN $token_ttl_sec = 0 THEN coalesce(vs.token_ttl_sec, 0) ELSE $token_ttl_sec END,
                        vs.observed_device_id = $observed_device_id,
                        vs.has_browse = coalesce(vs.has_browse, false) OR $is_browse,
                        vs.has_playback_start = coalesce(vs.has_playback_start, false) OR $is_playback_start,
                        vs.playback_without_browse = CASE
                            WHEN $is_playback_start AND NOT coalesce(vs.has_browse, false) THEN true
                            ELSE coalesce(vs.playback_without_browse, false)
                        END
                    """,
                    viewing_session_id=viewing_session_id,
                    session_key=viewing_session_key,
                    timestamp=timestamp,
                    app_session_id=app_session_id,
                    account_id=account_id,
                    content_id=request_content_id,
                    content_type=content_type,
                    token_issued_at=token_issued_at,
                    token_expires=token_expires,
                    token_ttl_sec=token_ttl_sec,
                    observed_device_id=observed_device_id,
                    manifest_inc=1 if is_manifest else 0,
                    segment_inc=1 if is_segment else 0,
                    playback_inc=1 if is_playback_start else 0,
                    browse_inc=1 if is_browse else 0,
                    bytes_sent=log.get("size", 0),
                    is_browse=is_browse,
                    is_playback_start=is_playback_start,
                )
                stats["ViewingSession"] += 1

                session.run(
                    """
                    MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                    MATCH (r:Request {request_id: $request_id})
                    MERGE (vs)-[link:MAKES_REQUEST]->(r)
                    ON CREATE SET
                        vs.request_count = coalesce(vs.request_count, 0) + 1,
                        vs.total_manifest_requests = coalesce(vs.total_manifest_requests, 0) + $manifest_inc,
                        vs.total_segment_requests = coalesce(vs.total_segment_requests, 0) + $segment_inc,
                        vs.total_playback_start_requests = coalesce(vs.total_playback_start_requests, 0) + $playback_inc,
                        vs.total_browse_requests = coalesce(vs.total_browse_requests, 0) + $browse_inc,
                        vs.total_bytes = coalesce(vs.total_bytes, 0) + $bytes_sent
                    """,
                    viewing_session_id=viewing_session_id,
                    request_id=request_id,
                    manifest_inc=1 if is_manifest else 0,
                    segment_inc=1 if is_segment else 0,
                    playback_inc=1 if is_playback_start else 0,
                    browse_inc=1 if is_browse else 0,
                    bytes_sent=log.get("size", 0),
                )

                if account_id and account_id != "-":
                    session.run(
                        """
                        MERGE (a:Account {account_id: $account_id})
                        ON CREATE SET
                            a.tier = 'basic',
                            a.status = 'normal',
                            a.username = $username,
                            a.user_id = $user_id,
                            a.created_at = datetime($timestamp)
                        SET
                            a.last_seen = datetime($timestamp),
                            a.username = CASE WHEN $username = '-' THEN a.username ELSE $username END,
                            a.user_id = CASE WHEN $user_id = '-' THEN a.user_id ELSE $user_id END
                        WITH a
                        MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                        MERGE (a)-[:HAS_VIEWING_SESSION]->(vs)
                        """,
                        account_id=account_id,
                        username=username,
                        user_id=user_id,
                        timestamp=timestamp,
                        viewing_session_id=viewing_session_id,
                    )
                    stats["Account"] += 1

                if cdn_token_id:
                    session.run(
                        """
                        MERGE (tok:CdnToken {cdn_token_id: $cdn_token_id})
                        ON CREATE SET
                            tok.first_seen = datetime($timestamp),
                            tok.token_jti = CASE WHEN $token_jti = '-' THEN null ELSE $token_jti END
                        SET
                            tok.last_seen = datetime($timestamp),
                            tok.app_session_id = CASE WHEN $app_session_id = '-' THEN tok.app_session_id ELSE $app_session_id END,
                            tok.owner_auth_session_id = CASE WHEN $owner_auth_session_id = '-' THEN tok.owner_auth_session_id ELSE $owner_auth_session_id END,
                            tok.owner_account_id = CASE WHEN $account_id = '-' THEN tok.owner_account_id ELSE $account_id END,
                            tok.owner_device_id = CASE WHEN $owner_device_id = '-' THEN tok.owner_device_id ELSE $owner_device_id END,
                            tok.content_id = CASE WHEN $content_id = '' THEN tok.content_id ELSE $content_id END,
                            tok.token_issued_at = CASE WHEN $token_issued_at = '-' THEN tok.token_issued_at ELSE $token_issued_at END,
                            tok.token_expires = CASE WHEN $token_expires = '-' THEN tok.token_expires ELSE $token_expires END,
                            tok.token_ttl_sec = CASE WHEN $token_ttl_sec = 0 THEN coalesce(tok.token_ttl_sec, 0) ELSE $token_ttl_sec END,
                            tok.valid = $token_valid
                        WITH tok
                        MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                        MERGE (vs)-[:USES_CDN_TOKEN]->(tok)
                        """,
                        cdn_token_id=cdn_token_id,
                        token_jti=log.get("token_jti", "-") or "-",
                        timestamp=timestamp,
                        app_session_id=app_session_id,
                        owner_auth_session_id=owner_auth_session_id,
                        account_id=account_id,
                        owner_device_id=owner_device_id,
                        content_id=request_content_id,
                        token_issued_at=token_issued_at,
                        token_expires=token_expires,
                        token_ttl_sec=token_ttl_sec,
                        token_valid=str(log.get("token_valid", "-")).lower() == "true",
                        viewing_session_id=viewing_session_id,
                    )
                    stats["CdnToken"] += 1

                if request_content_id:
                    session.run(
                        """
                        MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                        MATCH (r:Request {request_id: $request_id})
                        MATCH (c:Content {content_id: $content_id})
                        MERGE (vs)-[:TARGETS_CONTENT]->(c)
                        MERGE (r)-[link:TARGETS_CONTENT]->(c)
                        ON CREATE SET
                            c.request_count = coalesce(c.request_count, 0) + 1,
                            c.total_bytes = coalesce(c.total_bytes, 0) + $bytes_sent
                        """,
                        viewing_session_id=viewing_session_id,
                        request_id=request_id,
                        content_id=request_content_id,
                        bytes_sent=log.get("size", 0),
                    )

                if client_ip and client_ip != "-":
                    session.run(
                        """
                        MERGE (ip:ClientIP {ip_address: $ip_address})
                        ON CREATE SET ip.first_seen = datetime($timestamp)
                        SET ip.last_seen = datetime($timestamp)
                        WITH ip
                        MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                        MERGE (vs)-[:FROM_IP]->(ip)
                        """,
                        ip_address=client_ip,
                        timestamp=timestamp,
                        viewing_session_id=viewing_session_id,
                    )
                    stats["ClientIP"] += 1

                session.run(
                    """
                    MERGE (d:Device {device_id: $observed_device_id})
                    ON CREATE SET
                        d.first_seen = datetime($timestamp),
                        d.user_agent = $user_agent,
                        d.device_type = $device_type
                    SET d.last_seen = datetime($timestamp)
                    WITH d
                    MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                    MERGE (vs)-[:ON_DEVICE]->(d)
                    """,
                    observed_device_id=observed_device_id,
                    timestamp=timestamp,
                    user_agent=log.get("user_agent", "-"),
                    device_type=device_type,
                    viewing_session_id=viewing_session_id,
                )
                stats["Device"] += 1

                session.run(
                    """
                    MERGE (e:Edge {edge_id: $edge_id})
                    ON CREATE SET
                        e.region = $edge_region,
                        e.first_seen = datetime($timestamp)
                    SET
                        e.region = $edge_region,
                        e.last_seen = datetime($timestamp)
                    WITH e
                    MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                    MERGE (vs)-[:SERVED_BY]->(e)
                    """,
                    edge_id=edge_id,
                    edge_region=edge_region,
                    timestamp=timestamp,
                    viewing_session_id=viewing_session_id,
                )
                stats["Edge"] += 1

                session.run(
                    """
                    MERGE (ref:Referrer {domain: $domain})
                    ON CREATE SET ref.first_seen = datetime($timestamp)
                    SET
                        ref.last_seen = datetime($timestamp),
                        ref.category = $category,
                        ref.is_legitimate = $is_legitimate
                    WITH ref
                    MATCH (vs:ViewingSession {viewing_session_id: $viewing_session_id})
                    MERGE (vs)-[:REFERRED_BY]->(ref)
                    """,
                    domain=referrer_domain,
                    timestamp=timestamp,
                    category=referrer_category,
                    is_legitimate=referrer_legitimate,
                    viewing_session_id=viewing_session_id,
                )
                stats["Referrer"] += 1

                if is_segment and request_path.startswith("/hls/"):
                    path_parts = [part for part in request_path[len("/hls/") :].split("/") if part]
                    if len(path_parts) >= 2:
                        content_id = unquote(path_parts[0]).strip()
                        segment_file = unquote(path_parts[-1]).strip()
                        known_resolutions = {"1080p", "720p", "480p", "360p", "240p"}
                        segment_resolution = ""
                        if len(path_parts) >= 3 and path_parts[-2] in known_resolutions:
                            segment_resolution = path_parts[-2]

                        segment_number = None
                        segment_digits = "".join(ch for ch in segment_file if ch.isdigit())
                        if segment_digits:
                            try:
                                segment_number = int(segment_digits)
                            except ValueError:
                                segment_number = None

                        if segment_resolution:
                            segment_id = f"{content_id}_{segment_resolution}_{segment_file}"
                        else:
                            segment_id = f"{content_id}_{segment_file}"

                        session.run(
                            """
                            MERGE (seg:Segment {segment_id: $segment_id})
                            ON CREATE SET
                                seg.filename = $segment_file,
                                seg.number = $segment_number,
                                seg.resolution = CASE WHEN $segment_resolution = '' THEN null ELSE $segment_resolution END,
                                seg.size_bytes = $bytes_sent,
                                seg.path = $request_path,
                                seg.first_accessed = datetime($timestamp)
                            SET
                                seg.last_accessed = datetime($timestamp),
                                seg.size_bytes = CASE
                                    WHEN coalesce(seg.size_bytes, 0) < $bytes_sent THEN $bytes_sent
                                    ELSE seg.size_bytes
                                END
                            WITH seg
                            MATCH (r:Request {request_id: $request_id})
                            MERGE (r)-[:FOR_SEGMENT]->(seg)
                            WITH seg
                            MATCH (c:Content {content_id: $content_id})
                            MERGE (seg)-[:BELONGS_TO]->(c)
                            """,
                            segment_id=segment_id,
                            segment_file=segment_file,
                            segment_number=segment_number,
                            segment_resolution=segment_resolution,
                            bytes_sent=log.get("size", 0),
                            request_path=request_path,
                            timestamp=timestamp,
                            request_id=request_id,
                            content_id=content_id,
                        )
                        stats["Segment"] += 1
                    else:
                        logger.debug(f"Skip segment parse: unexpected hls path={request_path}")

            return stats

    def audit_token_fanout(self):
        """
        Optional operational audit over the graph.

        This threshold query is not the paper's H-OBD, ML-OBD, or GNN-OBD
        inference result. It is disabled by default so it cannot be mistaken
        for the evaluated detection pipeline or contaminate runtime results.
        """
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                """
                MATCH (tok:CdnToken)<-[:USES_CDN_TOKEN]-(vs:ViewingSession)
                OPTIONAL MATCH (a:Account)-[:HAS_VIEWING_SESSION]->(vs)
                OPTIONAL MATCH (vs)-[:TARGETS_CONTENT]->(c:Content)
                OPTIONAL MATCH (vs)-[:FROM_IP]->(ip:ClientIP)
                OPTIONAL MATCH (vs)-[:REFERRED_BY]->(ref:Referrer)
                WITH tok,
                     collect(DISTINCT a.account_id) as accounts,
                     collect(DISTINCT c.content_id) as contents,
                     count(DISTINCT ip) as ip_count,
                     count(DISTINCT ref) as referrer_count,
                     collect(DISTINCT ip.ip_address) as ips,
                     collect(DISTINCT ref.domain) as referrers
                WHERE ip_count > 2 OR referrer_count > 1
                RETURN tok.cdn_token_id as token,
                       accounts,
                       contents,
                       ip_count,
                       referrer_count,
                       ips,
                       referrers
                ORDER BY ip_count DESC, referrer_count DESC
                """
            )

            suspicious = []
            for record in result:
                suspicious.append({
                    "token": record["token"],
                    "accounts": record["accounts"],
                    "contents": record["contents"],
                    "ip_count": record["ip_count"],
                    "referrer_count": record["referrer_count"],
                    "ips": record["ips"],
                    "referrers": record["referrers"],
                })

            return suspicious

    def get_statistics(self):
        """그래프 통계"""
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                """
                MATCH (n)
                RETURN labels(n)[0] as NodeType, count(*) as Count
                ORDER BY Count DESC
                """
            )

            stats = {}
            for record in result:
                stats[record["NodeType"]] = record["Count"]

            result = session.run("MATCH ()-[r]->() RETURN count(r) as rel_count")
            stats["Relationships"] = result.single()["rel_count"]

            return stats

    def run(self):
        """메인 루프"""
        logger.info("=" * 60)
        logger.info("Graph Pipeline started (ViewingSession-centered)")
        logger.info("=" * 60)

        cycle = 0
        while True:
            try:
                cycle += 1
                logger.info(f"\n{'=' * 60}")
                logger.info(f"Cycle #{cycle}")
                logger.info(f"{'=' * 60}")

                logs = self.read_new_logs()
                if not logs:
                    self._commit_checkpoint()
                    logger.info("No new logs. Waiting...")
                    time.sleep(30)
                    continue

                logger.info("Building knowledge graph...")
                node_stats = self.build_knowledge_graph(logs)
                self._commit_checkpoint()
                logger.info(f"Processed nodes: {dict(node_stats)}")

                stats = self.get_statistics()
                logger.info("\nKnowledge Graph Statistics:")
                for node_type, count in stats.items():
                    logger.info(f"  {node_type}: {count}")

                if self.enable_operational_audit:
                    suspicious = self.audit_token_fanout()
                    if suspicious:
                        logger.warning("\nToken fan-out audit found suspicious relationships")
                        for item in suspicious:
                            logger.warning(f"  Token: {item['token']}")
                            logger.warning(f"  Accounts: {item['accounts']}")
                            logger.warning(f"  Contents: {item['contents']}")
                            logger.warning(
                                f"  IPs: {item['ip_count']}, "
                                f"Referrers: {item['referrer_count']}"
                            )
                            logger.warning(f"  IP List: {item['ips']}")
                            logger.warning(f"  Referrer List: {item['referrers']}")
                    else:
                        logger.info("\nToken fan-out audit found no suspicious relationships")

                logger.info("\nWaiting 30 seconds...")
                time.sleep(30)

            except KeyboardInterrupt:
                logger.info("\nShutting down...")
                break
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                time.sleep(10)

        self.driver.close()


if __name__ == "__main__":
    pipeline = GraphPipeline()
    pipeline.run()
