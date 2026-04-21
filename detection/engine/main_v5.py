"""
Detection Engine V5 - Account 기반 지식그래프 (지식그래프 구성.md v2)

노드:
- Account: 가입자 계정
- ClientIP: 클라이언트 IP 주소
- Domain: 요청/Referer 도메인
- Token: 세션 토큰
- Content: CDN 콘텐츠 (title + 파일명만)
- Edge: CDN 엣지 서버
- Request: 개별 요청 로그 (중심 노드)

관계:
- (Account)-[:OWNS_TOKEN]->(Token)
- (ClientIP)-[:MADE_REQUEST]->(Request)
- (Request)-[:TO_DOMAIN]->(Domain)
- (Request)-[:TARGETS_CONTENT]->(Content)
- (Request)-[:USING_TOKEN]->(Token)
- (Request)-[:ON_EDGE]->(Edge)
"""
import os
import time
import json
import logging
import hashlib
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse, parse_qsl, unquote
from elasticsearch import Elasticsearch
from neo4j import GraphDatabase

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class DetectionEngineV5:
    def __init__(self):
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "ott_detection_2025")
        self.neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
        self.driver = GraphDatabase.driver(self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password))

        self.log_source = os.getenv("LOG_SOURCE", "file").lower()
        self.log_file = "/var/log/scrubber/access.log"
        self.last_position = 0

        self.elasticsearch_url = os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
        self.elasticsearch_index = os.getenv(
            "ELASTICSEARCH_INDEX",
            "scrubber-nginx-*,filebeat-*,.ds-scrubber-nginx-*,.ds-filebeat-*",
        )
        self.es_poll_size = int(os.getenv("ES_POLL_SIZE", "500"))
        self.es_default_start_timestamp = os.getenv("ES_START_TIMESTAMP", "1970-01-01T00:00:00Z")
        self.es_last_timestamp = self.es_default_start_timestamp
        self.es_seen_ids_at_last_timestamp = set()
        self.es_client = None

        if self.log_source in ("elasticsearch", "es"):
            self.es_client = Elasticsearch(self.elasticsearch_url, verify_certs=False)
        elif self.log_source != "file":
            logger.warning(f"Unknown LOG_SOURCE={self.log_source}, fallback to file mode")
            self.log_source = "file"

        logger.info(
            "Detection Engine V5 Initialized "
            f"(Account-based, source={self.log_source}, neo4j_db={self.neo4j_database})"
        )
        self._create_indexes()

    def _create_indexes(self):
        """Neo4j 인덱스 생성"""
        with self.driver.session(database=self.neo4j_database) as session:
            indexes = [
                "CREATE INDEX account_id IF NOT EXISTS FOR (a:Account) ON (a.account_id)",
                "CREATE INDEX client_ip IF NOT EXISTS FOR (c:ClientIP) ON (c.ip)",
                "CREATE INDEX domain_host IF NOT EXISTS FOR (d:Domain) ON (d.host)",
                "CREATE INDEX token_value IF NOT EXISTS FOR (t:Token) ON (t.value)",
                "CREATE INDEX content_filename IF NOT EXISTS FOR (c:Content) ON (c.filename)",
                "CREATE INDEX segment_segment_id IF NOT EXISTS FOR (s:Segment) ON (s.segment_id)",
                "CREATE INDEX edge_id IF NOT EXISTS FOR (e:Edge) ON (e.id)",
                "CREATE INDEX request_id IF NOT EXISTS FOR (r:Request) ON (r.id)",
                "CREATE INDEX request_kind IF NOT EXISTS FOR (r:Request) ON (r.kind)",
                "CREATE INDEX request_target_content IF NOT EXISTS FOR (r:Request) ON (r.target_content_id)",
                "CREATE INDEX session_id IF NOT EXISTS FOR (s:Session) ON (s.id)",
                "CREATE INDEX resource_type IF NOT EXISTS FOR (rt:ResourceType) ON (rt.type)",
                "CREATE INDEX content_content_id IF NOT EXISTS FOR (c:Content) ON (c.content_id)",
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
    def _normalize_iso_timestamp(value):
        """Elasticsearch range cursor로 쓸 수 있는 ISO8601 문자열만 반환"""
        if isinstance(value, str):
            candidate = value.strip()
            if len(candidate) >= 19 and candidate[4] == "-" and candidate[10] == "T":
                return candidate
        return None

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

            # IP 주소 우선순위 (X-Real-IP 최우선)
            # 1. URL 파라미터의 real_ip
            real_ip = params.get("real_ip", "-")
            # 2. Nginx 변수 real_ip (Scrubber Gateway에서 전달)
            if real_ip == "-":
                real_ip = log.get("real_ip", "-")
            # 3. client_real_ip
            if real_ip == "-":
                real_ip = log.get("client_real_ip", "-")
            # 4. edge access_log의 표준 필드(client_ip)
            if real_ip == "-":
                real_ip = log.get("client_ip", "-")
            # 5. remote_addr (fallback)
            if real_ip == "-":
                real_ip = log.get("remote_addr", "-")
            # 6. X-Forwarded-For 헤더 (최후 fallback)
            if real_ip == "-":
                x_forwarded = log.get("x_forwarded_for", "-")
                if x_forwarded != "-":
                    real_ip = x_forwarded.split(",")[0].strip()
            
            # Docker gateway IP 필터링
            if real_ip == "172.18.0.1":
                real_ip = "device_unknown"

            session_token = params.get("token", log.get("session_token", "-"))
            # user_id를 account_id로 사용 (고유 식별자)
            user_id = params.get("user_id", log.get("user_id", "-"))
            username = params.get("user", log.get("username", "-"))
            run_id = params.get("run_id", log.get("token_run_id", "-"))
            scenario_id = params.get("scenario_id", log.get("token_scenario_id", "-"))
            dataset_label = params.get("dataset_label", log.get("token_dataset_label", "-"))
            label = params.get("label", log.get("token_label", dataset_label))
            
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

            request_method = log.get("request_method") or log.get("http.request.method") or "GET"

            timestamp_value = log.get("@timestamp") or log.get("timestamp") or log.get("event.created")
            if not self._normalize_iso_timestamp(timestamp_value):
                timestamp_value = "1970-01-01T00:00:00Z"

            return {
                "timestamp": timestamp_value,
                "account_id": account_id,
                "user_id": user_id,
                "username": username,
                "client_ip": real_ip,
                "client_region": log.get("client_region", "UNKNOWN"),
                "edge_server": log.get("edge_server", "-"),
                "domain": domain,
                "session_token": session_token,
                "run_id": run_id,
                "scenario_id": scenario_id,
                "label": label,
                "dataset_label": dataset_label,
                "referer": referer,
                "query_params": params,
                "content_path": content_path,
                "content_title": title,
                "content_filename": filename,
                "method": request_method,
                "status": _to_int(raw_status, 0),
                "size": _to_int(raw_size, 0),
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
            if current_size < self.last_position:
                self.last_position = 0

            with open(self.log_file, "r") as f:
                f.seek(self.last_position)
                for line in f:
                    parsed = self.parse_nginx_log(line.strip())
                    if parsed:
                        logs.append(parsed)
                self.last_position = f.tell()

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

            result = self.es_client.search(
                index=search_target,
                size=self.es_poll_size,
                track_total_hits=True,
                allow_no_indices=True,
                ignore_unavailable=True,
                expand_wildcards="all",
                sort=[
                    {"@timestamp": {"order": "asc", "unmapped_type": "date", "missing": "_last"}},
                    {"_doc": {"order": "asc"}},
                ],
                query={
                    "range": {
                        "@timestamp": {
                            "gte": self.es_last_timestamp,
                        }
                    }
                },
            )

            hit_info = result.get("hits", {}).get("total", 0)
            if isinstance(hit_info, dict):
                total_hits = hit_info.get("value", 0)
            else:
                total_hits = hit_info

            count_value = 0
            if total_hits == 0:
                logger.warning(
                    "No Elasticsearch hits found for "
                    f"index='{search_target}' and @timestamp>={self.es_last_timestamp}"
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
                        sort=[
                            {"@timestamp": {"order": "asc", "unmapped_type": "date", "missing": "_last"}},
                            {"_doc": {"order": "asc"}},
                        ],
                        query={"match_all": {}},
                    )

                    bootstrap_hit_info = bootstrap.get("hits", {}).get("total", 0)
                    if isinstance(bootstrap_hit_info, dict):
                        bootstrap_total_hits = bootstrap_hit_info.get("value", 0)
                    else:
                        bootstrap_total_hits = bootstrap_hit_info

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
                        sort=[{"_doc": {"order": "asc"}}],
                        query={"match_all": {}},
                    )

                    id_hit_info = id_fallback.get("hits", {}).get("total", 0)
                    if isinstance(id_hit_info, dict):
                        id_total_hits = id_hit_info.get("value", 0)
                    else:
                        id_total_hits = id_hit_info

                    if id_total_hits > 0:
                        logger.warning(
                            "Range/bootstrap returned 0 but doc-sort fallback found "
                            f"{id_total_hits} hits"
                        )
                        result = id_fallback
                        total_hits = id_total_hits

            processed_docs = []
            for hit in result.get("hits", {}).get("hits", []):
                source = hit.get("_source", {})
                doc_id = hit.get("_id")
                doc_ts = self._normalize_iso_timestamp(
                    source.get("@timestamp") or source.get("timestamp") or source.get("event.created")
                )
                if not doc_ts:
                    sort_values = hit.get("sort", [])
                    if sort_values:
                        doc_ts = self._normalize_iso_timestamp(sort_values[0])
                if not doc_ts:
                    doc_ts = datetime.utcnow().isoformat() + "Z"

                if doc_ts == self.es_last_timestamp and doc_id in self.es_seen_ids_at_last_timestamp:
                    continue

                parsed = self.parse_nginx_log(source)
                if parsed:
                    processed_docs.append((doc_ts, doc_id, parsed))

            if not processed_docs:
                if total_hits > 0:
                    sample_hit = (result.get("hits", {}).get("hits", []) or [{}])[0]
                    sample_source = sample_hit.get("_source", {}) if isinstance(sample_hit, dict) else {}
                    sample_keys = list(sample_source.keys())[:20] if isinstance(sample_source, dict) else []
                    sample_message = sample_source.get("message") if isinstance(sample_source, dict) else None
                    logger.warning(
                        f"Elasticsearch returned {total_hits} hits but no parsable documents were produced"
                    )
                    if sample_keys:
                        logger.warning(f"Sample _source keys: {sample_keys}")
                    if isinstance(sample_message, str):
                        logger.warning(f"Sample message head: {sample_message[:200]}")
                logger.info("Read 0 new log entries from Elasticsearch")
                return logs

            for _, _, parsed in processed_docs:
                logs.append(parsed)

            latest_ts = processed_docs[-1][0]
            latest_ids = {doc_id for doc_ts, doc_id, _ in processed_docs if doc_ts == latest_ts}

            if latest_ts == self.es_last_timestamp:
                self.es_seen_ids_at_last_timestamp |= latest_ids
            else:
                self.es_last_timestamp = latest_ts
                self.es_seen_ids_at_last_timestamp = latest_ids

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
        """
        지식그래프 생성 (Account 포함 Request 중심)
        """
        with self.driver.session(database=self.neo4j_database) as session:
            stats = defaultdict(int)

            for log in logs:
                if log["status"] < 200 or log["status"] >= 400:
                    continue
                # /hls/ 필터 제거 - 모든 요청 수집 (html, css, js, video 등)
                # if "/hls/" not in log["content_path"]:
                #     continue

                timestamp = log["timestamp"]
                account_id = log["account_id"]
                username = log.get("username", "-")
                user_id = log.get("user_id", "-")
                client_ip = log["client_ip"]
                domain = log["domain"]
                token = log["session_token"]
                content_path = log["content_path"]
                request_path = self._strip_query(content_path)
                request_kind = self._classify_request_kind(content_path, log.get("method"))
                if not request_kind:
                    continue
                query_params = log.get("query_params") if isinstance(log.get("query_params"), dict) else {}
                referer = log.get("referer", "-")
                request_content_id = self._extract_content_id_from_request(
                    request_kind,
                    request_path,
                    query_params,
                    referer,
                )
                content_title = log["content_title"]
                content_filename = log["content_filename"]
                resource_type = log["resource_type"]
                resource_category = log["resource_category"]
                connection_id = log["connection_id"]
                connection_requests = log["connection_requests"]
                http_range = log["http_range"]
                keep_alive = log["keep_alive"]
                run_id = log.get("run_id", "-")
                scenario_id = log.get("scenario_id", "-")
                label = log.get("label", log.get("token_label", "normal"))
                dataset_label = log.get("dataset_label", "-")
                edge_id = log.get("edge_server") or "edge-unknown"
                edge_region = log.get("client_region", "UNKNOWN")
                if not edge_region or edge_region == "-" or edge_region == "UNKNOWN":
                    edge_region = self._infer_region_from_edge(edge_id)
                
                # Request ID 생성 (connection_id 포함하여 고유성 보장)
                req_hash = hashlib.md5(
                    f"{timestamp}{connection_id}{client_ip}{content_path}".encode()
                ).hexdigest()[:12]
                request_id = f"req_{req_hash}"

                # 1. Account 노드
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
                        ON MATCH SET
                            a.last_seen = datetime($timestamp),
                            a.username = $username,
                            a.user_id = $user_id
                        """,
                        account_id=account_id,
                        username=username,
                        user_id=user_id,
                        timestamp=timestamp,
                    )
                    stats["Account"] += 1

                # 2. Token 노드
                if token and token != "-":
                    session.run(
                        """
                        MERGE (t:Token {value: $token})
                        ON CREATE SET
                            t.issued_at = datetime($timestamp),
                            t.state = 'active'
                        ON MATCH SET
                            t.last_used = datetime($timestamp)
                        """,
                        token=token,
                        timestamp=timestamp,
                    )
                    stats["Token"] += 1

                    # Account -> OWNS_TOKEN -> Token
                    if account_id and account_id != "-":
                        session.run(
                            """
                            MATCH (a:Account {account_id: $account_id})
                            MATCH (t:Token {value: $token})
                            MERGE (a)-[:OWNS_TOKEN]->(t)
                            """,
                            account_id=account_id,
                            token=token,
                        )

                # 3. Request 노드 (중심 노드)
                session.run(
                    """
                    MERGE (r:Request {id: $req_id})
                    ON CREATE SET
                        r.timestamp = datetime($timestamp),
                        r.method = $method,
                        r.path = $request_path,
                        r.kind = $request_kind,
                        r.target_content_id = CASE
                            WHEN $request_content_id = '' THEN null
                            ELSE $request_content_id
                        END,
                        r.status = $status,
                        r.size = $size,
                        r.user_agent = $user_agent,
                        r.http_range = $http_range,
                        r.keep_alive = $keep_alive,
                        r.label = $label,
                        r.run_id = $run_id,
                        r.scenario_id = $scenario_id,
                        r.dataset_label = $dataset_label
                    ON MATCH SET
                        r.path = $request_path,
                        r.kind = $request_kind,
                        r.target_content_id = CASE
                            WHEN $request_content_id = '' THEN r.target_content_id
                            ELSE $request_content_id
                        END,
                        r.label = CASE WHEN $label = '-' THEN coalesce(r.label, 'normal') ELSE $label END,
                        r.run_id = CASE WHEN $run_id = '-' THEN coalesce(r.run_id, '-') ELSE $run_id END,
                        r.scenario_id = CASE WHEN $scenario_id = '-' THEN coalesce(r.scenario_id, '-') ELSE $scenario_id END,
                        r.dataset_label = CASE WHEN $dataset_label = '-' THEN coalesce(r.dataset_label, '-') ELSE $dataset_label END
                    """,
                    req_id=request_id,
                    timestamp=timestamp,
                    method=log["method"],
                    request_path=request_path,
                    request_kind=request_kind,
                    request_content_id=request_content_id,
                    status=log["status"],
                    size=log["size"],
                    user_agent=log["user_agent"],
                    http_range=http_range,
                    keep_alive=keep_alive,
                    label=label,
                    run_id=run_id,
                    scenario_id=scenario_id,
                    dataset_label=dataset_label,
                )
                stats["Request"] += 1

                # 3-1. API 이벤트 타깃 콘텐츠 연결 (HLS는 아래 Segment/Manifest 분기에서 처리)
                if request_content_id and request_kind in ("playback_start", "browse_content"):
                    session.run(
                        """
                        MERGE (c:Content {content_id: $content_id})
                        ON CREATE SET
                            c.title = $content_id,
                            c.type = 'CATALOG',
                            c.first_accessed = datetime($timestamp),
                            c.view_count = 0
                        ON MATCH SET
                            c.last_accessed = datetime($timestamp)
                        """,
                        content_id=request_content_id,
                        timestamp=timestamp,
                    )
                    stats["Content"] += 1

                    session.run(
                        """
                        MATCH (r:Request {id: $req_id})
                        MATCH (c:Content {content_id: $content_id})
                        MERGE (r)-[:TARGETS_CONTENT]->(c)
                        """,
                        req_id=request_id,
                        content_id=request_content_id,
                    )

                # 4. ClientIP 노드
                if client_ip and client_ip != "-":
                    session.run(
                        """
                        MERGE (ip:ClientIP {ip: $ip})
                        ON CREATE SET ip.first_seen = datetime($timestamp)
                        ON MATCH SET ip.last_seen = datetime($timestamp)
                        """,
                        ip=client_ip,
                        timestamp=timestamp,
                    )
                    stats["ClientIP"] += 1

                    # ClientIP -> MADE_REQUEST -> Request
                    session.run(
                        """
                        MATCH (ip:ClientIP {ip: $ip})
                        MATCH (r:Request {id: $req_id})
                        MERGE (ip)-[:MADE_REQUEST]->(r)
                        """,
                        ip=client_ip,
                        req_id=request_id,
                    )

                # 5. Domain 노드
                if domain and domain != "-":
                    session.run(
                        """
                        MERGE (d:Domain {host: $host})
                        ON CREATE SET
                            d.category = 'unknown',
                            d.first_seen = datetime($timestamp)
                        ON MATCH SET
                            d.last_seen = datetime($timestamp)
                        """,
                        host=domain,
                        timestamp=timestamp,
                    )
                    stats["Domain"] += 1

                    # Request -> TO_DOMAIN -> Domain
                    session.run(
                        """
                        MATCH (r:Request {id: $req_id})
                        MATCH (d:Domain {host: $host})
                        MERGE (r)-[:TO_DOMAIN]->(d)
                        """,
                        req_id=request_id,
                        host=domain,
                    )

                # 6. Content/Segment 노드 (문서 스키마 기준)
                if request_kind == "hls_segment" and request_path.startswith("/hls/"):
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
                            MERGE (c:Content {content_id: $content_id})
                            ON CREATE SET
                                c.title = $content_id,
                                c.type = 'HLS_STREAM',
                                c.first_accessed = datetime($timestamp),
                                c.view_count = 1,
                                c.total_bytes = $size
                            ON MATCH SET
                                c.last_accessed = datetime($timestamp),
                                c.view_count = coalesce(c.view_count, 0) + 1,
                                c.total_bytes = coalesce(c.total_bytes, 0) + $size
                            """,
                            content_id=content_id,
                            timestamp=timestamp,
                            size=log["size"],
                        )
                        stats["Content"] += 1

                        session.run(
                            """
                            MERGE (seg:Segment {segment_id: $segment_id})
                            ON CREATE SET
                                seg.id = $segment_id,
                                seg.filename = $segment_file,
                                seg.number = $segment_number,
                                seg.resolution = CASE
                                    WHEN $segment_resolution = '' THEN null
                                    ELSE $segment_resolution
                                END,
                                seg.size_bytes = $size,
                                seg.path = $request_path,
                                seg.first_accessed = datetime($timestamp)
                            ON MATCH SET
                                seg.id = coalesce(seg.id, $segment_id),
                                seg.filename = $segment_file,
                                seg.number = CASE
                                    WHEN $segment_number IS NULL THEN seg.number
                                    ELSE $segment_number
                                END,
                                seg.resolution = CASE
                                    WHEN $segment_resolution = '' THEN seg.resolution
                                    ELSE $segment_resolution
                                END,
                                seg.size_bytes = CASE
                                    WHEN coalesce(seg.size_bytes, 0) < $size THEN $size
                                    ELSE seg.size_bytes
                                END,
                                seg.last_accessed = datetime($timestamp)
                            WITH seg
                            MATCH (c:Content {content_id: $content_id})
                            MERGE (seg)-[:BELONGS_TO]->(c)
                            """,
                            segment_id=segment_id,
                            segment_file=segment_file,
                            segment_number=segment_number,
                            segment_resolution=segment_resolution,
                            size=log["size"],
                            request_path=request_path,
                            timestamp=timestamp,
                            content_id=content_id,
                        )
                        stats["Segment"] += 1

                        session.run(
                            """
                            MATCH (r:Request {id: $req_id})
                            MATCH (seg:Segment {segment_id: $segment_id})
                            MERGE (r)-[:FOR_SEGMENT]->(seg)
                            """,
                            req_id=request_id,
                            segment_id=segment_id,
                        )

                        session.run(
                            """
                            MATCH (r:Request {id: $req_id})
                            MATCH (c:Content {content_id: $content_id})
                            MERGE (r)-[:TARGETS_CONTENT]->(c)
                            """,
                            req_id=request_id,
                            content_id=content_id,
                        )
                    else:
                        logger.debug(f"Skip segment parse: unexpected hls path={request_path}")

                elif request_kind == "hls_manifest" and request_path.startswith("/hls/"):
                    path_parts = [part for part in request_path[len("/hls/") :].split("/") if part]
                    if path_parts:
                        content_id = unquote(path_parts[0]).strip()
                        if content_id:
                            session.run(
                                """
                                MERGE (c:Content {content_id: $content_id})
                                ON CREATE SET
                                    c.title = $content_id,
                                    c.type = 'HLS_STREAM',
                                    c.first_accessed = datetime($timestamp),
                                    c.view_count = 1,
                                    c.total_bytes = $size
                                ON MATCH SET
                                    c.last_accessed = datetime($timestamp),
                                    c.view_count = coalesce(c.view_count, 0) + 1,
                                    c.total_bytes = coalesce(c.total_bytes, 0) + $size
                                """,
                                content_id=content_id,
                                timestamp=timestamp,
                                size=log["size"],
                            )
                            stats["Content"] += 1

                            session.run(
                                """
                                MATCH (r:Request {id: $req_id})
                                MATCH (c:Content {content_id: $content_id})
                                MERGE (r)-[:TARGETS_CONTENT]->(c)
                                """,
                                req_id=request_id,
                                content_id=content_id,
                            )

                # 7. Request -> USING_TOKEN -> Token
                if token and token != "-":
                    session.run(
                        """
                        MATCH (r:Request {id: $req_id})
                        MATCH (t:Token {value: $token})
                        MERGE (r)-[:USING_TOKEN]->(t)
                        """,
                        req_id=request_id,
                        token=token,
                    )

                # 8. Edge 노드
                session.run(
                    """
                    MERGE (e:Edge {id: $edge_id})
                    ON CREATE SET
                        e.region = $edge_region,
                        e.first_seen = datetime($timestamp)
                    ON MATCH SET
                        e.last_seen = datetime($timestamp)
                    """,
                    edge_id=edge_id,
                    edge_region=edge_region,
                    timestamp=timestamp,
                )
                stats["Edge"] += 1

                # Request -> ON_EDGE -> Edge
                session.run(
                    """
                    MATCH (r:Request {id: $req_id})
                    MATCH (e:Edge {id: $edge_id})
                    MERGE (r)-[:ON_EDGE]->(e)
                    """,
                    req_id=request_id,
                    edge_id=edge_id,
                )

                # 9. Session 노드 (TCP 연결 기반)
                if connection_id and connection_id != "-":
                    session.run(
                        """
                        MERGE (s:Session {id: $conn_id})
                        ON CREATE SET
                            s.start_time = datetime($timestamp),
                            s.request_count = 1,
                            s.keep_alive = $keep_alive
                        ON MATCH SET
                            s.request_count = s.request_count + 1,
                            s.last_time = datetime($timestamp)
                        """,
                        conn_id=connection_id,
                        timestamp=timestamp,
                        keep_alive=keep_alive,
                    )
                    stats["Session"] += 1

                    # Session -> MAKES_REQUEST -> Request
                    session.run(
                        """
                        MATCH (s:Session {id: $conn_id})
                        MATCH (r:Request {id: $req_id})
                        MERGE (s)-[:MAKES_REQUEST]->(r)
                        """,
                        conn_id=connection_id,
                        req_id=request_id,
                    )

                    # Token -> USED_IN_SESSION -> Session
                    if token and token != "-":
                        session.run(
                            """
                            MATCH (t:Token {value: $token})
                            MATCH (s:Session {id: $conn_id})
                            MERGE (t)-[:USED_IN_SESSION]->(s)
                            """,
                            token=token,
                            conn_id=connection_id,
                        )

                # 10. ResourceType 노드 (제거됨 - 비디오만 수집하므로 불필요)
                # if resource_type and resource_type != "other":
                #     session.run(
                #         """
                #         MERGE (rt:ResourceType {type: $res_type})
                #         ON CREATE SET rt.category = $res_category
                #         """,
                #         res_type=resource_type,
                #         res_category=resource_category,
                #     )
                #     stats["ResourceType"] += 1
                #
                #     # Request -> IS_TYPE -> ResourceType
                #     session.run(
                #         """
                #         MATCH (r:Request {id: $req_id})
                #         MATCH (rt:ResourceType {type: $res_type})
                #         MERGE (r)-[:IS_TYPE]->(rt)
                #         """,
                #         req_id=request_id,
                #         res_type=resource_type,
                #     )

            return stats

    def detect_leeching(self):
        """
        리칭 패턴 탐지 (Account 기반):
        - 동일 Account/Token의 콘텐츠를 여러 ClientIP/Domain이 사용
        """
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                """
                MATCH (a:Account)-[:OWNS_TOKEN]->(t:Token)<-[:USING_TOKEN]-(r:Request)
                MATCH (ip:ClientIP)-[:MADE_REQUEST]->(r)
                MATCH (r)-[:TO_DOMAIN]->(d:Domain)
                 OPTIONAL MATCH (r)-[:TARGETS_CONTENT]->(c:Content)
                 OPTIONAL MATCH (r)-[:FOR_SEGMENT]->(seg:Segment)-[:BELONGS_TO]->(sc:Content)
                 WITH a, t,
                     coalesce(c.content_id, sc.content_id, r.target_content_id) as content_id,
                     count(DISTINCT ip) as ip_count, 
                     count(DISTINCT d) as domain_count,
                     collect(DISTINCT ip.ip) as ips,
                     collect(DISTINCT d.host) as domains
                 WHERE content_id IS NOT NULL AND content_id <> ''
                   AND (ip_count > 2 OR domain_count > 1)
                RETURN a.account_id as account,
                       t.value as token, 
                       content_id as content_id,
                       ip_count, 
                       domain_count,
                       ips, 
                       domains
                ORDER BY ip_count DESC, domain_count DESC
                """
            )

            suspicious = []
            for record in result:
                suspicious.append({
                    "account": record["account"],
                    "token": record["token"][:20] + "...",
                    "content_id": record["content_id"],
                    "ip_count": record["ip_count"],
                    "domain_count": record["domain_count"],
                    "ips": record["ips"],
                    "domains": record["domains"],
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
        logger.info("Detection Engine V5 Started (Account-based)")
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
                    logger.info("No new logs. Waiting...")
                    time.sleep(30)
                    continue

                logger.info("Building knowledge graph...")
                node_stats = self.build_knowledge_graph(logs)
                logger.info(f"Processed nodes: {dict(node_stats)}")

                stats = self.get_statistics()
                logger.info("\nKnowledge Graph Statistics:")
                for node_type, count in stats.items():
                    logger.info(f"  {node_type}: {count}")

                suspicious = self.detect_leeching()
                if suspicious:
                    logger.warning("\n🚨  CDN Leeching Detected!")
                    for item in suspicious:
                        logger.warning(f"  Account: {item['account']}, Token: {item['token']}")
                        logger.warning(f"  Content ID: {item['content_id']}")
                        logger.warning(f"  IPs: {item['ip_count']}, Domains: {item['domain_count']}")
                        logger.warning(f"  IP List: {item['ips']}")
                        logger.warning(f"  Domain List: {item['domains']}")
                else:
                    logger.info("\n✓ No leeching detected")

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
    engine = DetectionEngineV5()
    engine.run()
