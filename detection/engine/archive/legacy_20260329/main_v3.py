"""
Detection Engine V4 - Knowledge Graph Design
- ClientIP: 클라이언트 IP 주소
- Domain: 요청/Referer 도메인
- Token: 세션 토큰 (통합)
- Content: CDN 콘텐츠 (HLS, MP4 등)
- Edge: CDN 엣지 서버 (현재는 1개)
- Request: 개별 요청 로그 (중심 노드)

관계:
- (ClientIP)-[:MADE_REQUEST]->(Request)
- (Request)-[:TO_DOMAIN]->(Domain)
- (Request)-[:FOR_CONTENT]->(Content)
- (Request)-[:USING_TOKEN]->(Token)
- (Request)-[:ON_EDGE]->(Edge)
"""
import os
import time
import json
import logging
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from neo4j import GraphDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class DetectionEngineV3:
    def __init__(self):
        # Neo4j 연결
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "ott_detection_2025")
        self.driver = GraphDatabase.driver(self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password))

        # 로그 파일 경로 (Filebeat가 수집하는 동일 파일)
        self.log_file = "/var/log/scrubber/access.log"
        self.last_position = 0

        logger.info("Detection Engine V3 Initialized")
        self._create_indexes()

    @staticmethod
    def _first_valid(*values):
        """Return the first non-empty, non-dash value."""
        for val in values:
            if val and val != "-":
                return val
        return None

    @staticmethod
    def _content_id_from_path(request_uri):
        """Fallback extractor: /hls/video1.mp4 -> video1."""
        try:
            if not request_uri:
                return None
            path = urlparse(request_uri).path
            parts = path.split("/")
            last = parts[-1] if parts else ""
            if "." in last:
                return last.split(".")[0]
            return last or None
        except Exception:
            return None

    def _create_indexes(self):
        """Neo4j 인덱스 생성"""
        with self.driver.session() as session:
            indexes = [
                "CREATE INDEX user_id IF NOT EXISTS FOR (u:User) ON (u.user_id)",
                "CREATE INDEX user_username IF NOT EXISTS FOR (u:User) ON (u.username)",
                "CREATE INDEX session_token IF NOT EXISTS FOR (s:Session) ON (s.token)",
                "CREATE INDEX device_id IF NOT EXISTS FOR (d:Device) ON (d.device_id)",
                "CREATE INDEX content_id IF NOT EXISTS FOR (c:Content) ON (c.content_id)",
                "CREATE INDEX ip_address IF NOT EXISTS FOR (ip:IPAddress) ON (ip.address)",
            ]
            for idx in indexes:
                try:
                    session.run(idx)
                except Exception as e:
                    logger.debug(f"Index creation: {e}")

    def parse_nginx_log(self, log_line):
        """Nginx JSON 로그 파싱 (legacy 키 포함)"""
        try:
            log = json.loads(log_line)

            # Query string 파싱
            query_string = log.get("query_string", "")
            params = {}
            if query_string and query_string != "-":
                for param in query_string.split("&"):
                    if "=" in param:
                        key, value = param.split("=", 1)
                        params[key] = value

            username = self._first_valid(params.get("user"), log.get("username"), log.get("http_x_username"))
            session_token = self._first_valid(
                params.get("token"),
                log.get("session_token"),
                log.get("http_x_session_token")
            )
            client_id = self._first_valid(
                params.get("client_id"),
                log.get("client_id"),
                log.get("http_x_client_type")
            )
            content_id = self._first_valid(
                params.get("content_id"),
                log.get("content_id"),
                log.get("http_x_content_id"),
                self._content_id_from_path(log.get("request_uri"))
            )

            # 실제 클라이언트 IP 결정 (우선순위)
            real_ip = params.get("real_ip", "-")
            if real_ip == "-" or not real_ip:
                real_ip = log.get("client_real_ip", "-")
            if real_ip == "-" or not real_ip:
                real_ip = log.get("real_ip", "-")
            if real_ip == "-" or not real_ip:
                x_forwarded = log.get("x_forwarded_for", "-")
                if x_forwarded != "-" and x_forwarded:
                    real_ip = x_forwarded.split(",")[0].strip()
                else:
                    real_ip = log.get("remote_addr", "-")

            return {
                "timestamp": log.get("@timestamp") or log.get("timestamp"),
                "remote_addr": log.get("remote_addr", "-"),
                "real_ip": real_ip,
                "x_forwarded_for": log.get("x_forwarded_for", "-"),
                "request_uri": log.get("request_uri", "-"),
                "status": int(log.get("status", 0) or 0),
                "bytes_sent": int(log.get("bytes_sent", 0) or 0),
                "user_agent": log.get("http_user_agent", "-"),
                "referer": log.get("http_referer", "-"),
                "username": username,
                "session_token": session_token,
                "content_id": content_id,
                "client_id": client_id,
            }
        except Exception as e:
            logger.error(f"Error parsing log: {e}")
            return None

    def read_new_logs(self):
        """신규 로그 읽기"""
        logs = []
        try:
            if not os.path.exists(self.log_file):
                logger.warning(f"Log file not found: {self.log_file}")
                return logs

            current_size = os.path.getsize(self.log_file)
            if current_size < self.last_position:
                logger.info("Log file truncated. Resetting read position to 0.")
                self.last_position = 0

            with open(self.log_file, "r") as f:
                f.seek(self.last_position)
                for line in f:
                    parsed = self.parse_nginx_log(line.strip())
                    if parsed:
                        logs.append(parsed)
                self.last_position = f.tell()

            logger.info(f"Read {len(logs)} new log entries")
            return logs
        except Exception as e:
            logger.error(f"Error reading logs: {e}")
            return logs

    def build_knowledge_graph(self, logs):
        """
        지식그래프 생성 (V4 - Request 중심)
        노드: ClientIP, Domain, Token, Content, Edge, Request
        """
        with self.driver.session() as session:
            stats = defaultdict(int)
            edge_id = "edge-scrubber-1"  # 현재는 엣지 1개

            for log in logs:
                if log["status"] < 200 or log["status"] >= 400:
                    continue

                if "/hls/" not in log["request_uri"]:
                    continue

                timestamp = log["timestamp"]
                username = log["username"]
                session_token = log["session_token"]
                client_id = log["client_id"]
                content_id = log["content_id"]
                ip_addr = log["real_ip"]
                bytes_sent = log["bytes_sent"]

                # 1. User (계정 식별을 위한 user_id 추가)
                if username:
                    session.run(
                        """
                        MERGE (u:User {username: $username})
                        ON CREATE SET 
                            u.user_id = randomUUID(),
                            u.account_id = $username,
                            u.created_at = datetime($timestamp),
                            u.first_seen = datetime($timestamp),
                            u.last_seen = datetime($timestamp),
                            u.total_requests = 1,
                            u.total_bytes = $bytes
                        ON MATCH SET
                            u.last_seen = datetime($timestamp),
                            u.total_requests = u.total_requests + 1,
                            u.total_bytes = u.total_bytes + $bytes
                        """,
                        username=username,
                        timestamp=timestamp,
                        bytes=bytes_sent,
                    )
                    stats["User"] += 1

                # 2. Session (토큰 기반, owner_id로 계정 식별)
                if session_token:
                    session.run(
                        """
                        MERGE (s:Session {token: $token})
                        ON CREATE SET
                            s.created_at = datetime($timestamp),
                            s.first_used = datetime($timestamp),
                            s.last_used = datetime($timestamp),
                            s.use_count = 1,
                            s.owner = $username,
                            s.owner_id = $username
                        ON MATCH SET
                            s.last_used = datetime($timestamp),
                            s.use_count = s.use_count + 1
                        """,
                        token=session_token,
                        timestamp=timestamp,
                        username=username,
                    )
                    stats["Session"] += 1

                    if username:
                        session.run(
                            """
                            MATCH (u:User {username: $username})
                            MATCH (s:Session {token: $token})
                            MERGE (u)-[r:HAS_SESSION]->(s)
                            ON CREATE SET r.created_at = datetime($timestamp)
                            """,
                            username=username,
                            token=session_token,
                            timestamp=timestamp,
                        )

                # 3. Device (IP 기반 = 물리적 기기/네트워크 위치)
                if ip_addr:
                    device_id = f"device_{ip_addr.replace('.', '_').replace(':', '_')}"
                    session.run(
                        """
                        MERGE (d:Device {device_id: $device_id})
                        ON CREATE SET
                            d.ip_address = $ip_addr,
                            d.first_seen = datetime($timestamp),
                            d.last_seen = datetime($timestamp),
                            d.request_count = 1,
                            d.total_bytes = $bytes
                        ON MATCH SET
                            d.last_seen = datetime($timestamp),
                            d.request_count = d.request_count + 1,
                            d.total_bytes = d.total_bytes + $bytes
                        """,
                        device_id=device_id,
                        ip_addr=ip_addr,
                        timestamp=timestamp,
                        bytes=bytes_sent,
                    )
                    stats["Device"] += 1

                # 4. Browser (브라우저 지문 = 같은 기기의 다른 브라우저)
                if client_id:
                    browser_id = f"browser_{client_id}"
                    session.run(
                        """
                        MERGE (b:Browser {browser_id: $browser_id})
                        ON CREATE SET
                            b.fingerprint = $client_id,
                            b.first_seen = datetime($timestamp),
                            b.last_seen = datetime($timestamp),
                            b.request_count = 1,
                            b.total_bytes = $bytes,
                            b.user_agent = $user_agent
                        ON MATCH SET
                            b.last_seen = datetime($timestamp),
                            b.request_count = b.request_count + 1,
                            b.total_bytes = b.total_bytes + $bytes
                        """,
                        browser_id=browser_id,
                        client_id=client_id,
                        timestamp=timestamp,
                        bytes=bytes_sent,
                        user_agent=log["user_agent"],
                    )
                    stats["Browser"] += 1

                    # Device -> RUNS_BROWSER -> Browser (기기가 어떤 브라우저 실행)
                    if ip_addr:
                        device_id = f"device_{ip_addr.replace('.', '_').replace(':', '_')}"
                        session.run(
                            """
                            MATCH (d:Device {device_id: $device_id})
                            MATCH (b:Browser {browser_id: $browser_id})
                            MERGE (d)-[r:RUNS_BROWSER]->(b)
                            ON CREATE SET r.first_seen = datetime($timestamp)
                            ON MATCH SET r.last_seen = datetime($timestamp)
                            """,
                            device_id=device_id,
                            browser_id=browser_id,
                            timestamp=timestamp,
                        )
                    stats["Device"] += 1

                    # Session -> USED_ON -> Device (IP 기반)
                    if session_token and ip_addr:
                        device_id = f"device_{ip_addr.replace('.', '_').replace(':', '_')}"
                        session.run(
                            """
                            MATCH (s:Session {token: $token})
                            MATCH (d:Device {device_id: $device_id})
                            MERGE (s)-[r:USED_ON]->(d)
                            ON CREATE SET
                                r.first_used = datetime($timestamp),
                                r.last_used = datetime($timestamp),
                                r.use_count = 1
                            ON MATCH SET
                                r.last_used = datetime($timestamp),
                                r.use_count = r.use_count + 1
                            """,
                            token=session_token,
                            device_id=device_id,
                            timestamp=timestamp,
                        )
                    
                    # User -> HAS_DEVICE -> Device (IP 기반)
                    if username and ip_addr:
                        device_id = f"device_{ip_addr.replace('.', '_').replace(':', '_')}"
                        session.run(
                            """
                            MATCH (u:User {username: $username})
                            MATCH (d:Device {device_id: $device_id})
                            MERGE (u)-[r:HAS_DEVICE]->(d)
                            ON CREATE SET r.first_used = datetime($timestamp)
                            ON MATCH SET r.last_used = datetime($timestamp)
                            """,
                            username=username,
                            device_id=device_id,
                            timestamp=timestamp,
                        )

                # 5. Content
                if content_id:
                    session.run(
                        """
                        MERGE (c:Content {content_id: $content_id})
                        ON CREATE SET
                            c.first_accessed = datetime($timestamp),
                            c.last_accessed = datetime($timestamp),
                            c.view_count = 1,
                            c.total_bytes = $bytes,
                            c.uri = $uri
                        ON MATCH SET
                            c.last_accessed = datetime($timestamp),
                            c.view_count = c.view_count + 1,
                            c.total_bytes = c.total_bytes + $bytes
                        """,
                        content_id=content_id,
                        timestamp=timestamp,
                        bytes=bytes_sent,
                        uri=log["request_uri"],
                    )
                    stats["Content"] += 1

                    # Browser -> VIEWED -> Content (브라우저로 시청)
                    if client_id:
                        browser_id = f"browser_{client_id}"
                        session.run(
                            """
                            MATCH (b:Browser {browser_id: $browser_id})
                            MATCH (c:Content {content_id: $content_id})
                            MERGE (b)-[r:VIEWED]->(c)
                            ON CREATE SET
                                r.first_view = datetime($timestamp),
                                r.last_view = datetime($timestamp),
                                r.view_count = 1
                            ON MATCH SET
                                r.last_view = datetime($timestamp),
                                r.view_count = r.view_count + 1
                            """,
                            browser_id=browser_id,
                            content_id=content_id,
                            timestamp=timestamp,
                        )

            return stats

    def detect_account_sharing(self):
        """계정 공유 탐지"""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (s:Session)-[:USED_ON]->(d:Device)
                WITH s, count(DISTINCT d) as device_count, collect(DISTINCT d.device_id) as devices
                WHERE device_count > 1
                RETURN s.token as token, s.owner as username, device_count, devices
                ORDER BY device_count DESC
                """
            )

            suspicious = []
            for record in result:
                suspicious.append(
                    {
                        "username": record["username"],
                        "device_count": record["device_count"],
                        "devices": record["devices"],
                    }
                )

            return suspicious

    def get_statistics(self):
        """그래프 통계"""
        with self.driver.session() as session:
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

            result = session.run(
                """
                MATCH ()-[r]->()
                RETURN count(r) as rel_count
                """
            )
            stats["Relationships"] = result.single()["rel_count"]

            return stats

    def run(self):
        """메인 루프"""
        logger.info("=" * 60)
        logger.info("Detection Engine V3 Started")
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

                suspicious = self.detect_account_sharing()
                if suspicious:
                    logger.warning("\n🚨  Account Sharing Detected!")
                    for item in suspicious:
                        logger.warning(f"  User: {item['username']}, Devices: {item['device_count']}")
                        for device in item["devices"]:
                            logger.warning(f"    - {device}")
                else:
                    logger.info("\n✓ No suspicious activity detected")

                logger.info("\nWaiting 30 seconds until next cycle...")
                time.sleep(30)

            except KeyboardInterrupt:
                logger.info("\nShutting down...")
                break
            except Exception as e:
                logger.error(f"Error in cycle: {e}", exc_info=True)
                time.sleep(10)

        self.driver.close()


if __name__ == "__main__":
    engine = DetectionEngineV3()
    engine.run()
