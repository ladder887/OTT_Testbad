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
- (Request)-[:FOR_CONTENT]->(Content)
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
from urllib.parse import urlparse
from neo4j import GraphDatabase

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class DetectionEngineV5:
    def __init__(self):
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "ott_detection_2025")
        self.driver = GraphDatabase.driver(self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password))

        self.log_file = "/var/log/scrubber/access.log"
        self.last_position = 0

        logger.info("Detection Engine V5 Initialized (Account-based)")
        self._create_indexes()

    def _create_indexes(self):
        """Neo4j 인덱스 생성"""
        with self.driver.session() as session:
            indexes = [
                "CREATE INDEX account_id IF NOT EXISTS FOR (a:Account) ON (a.account_id)",
                "CREATE INDEX client_ip IF NOT EXISTS FOR (c:ClientIP) ON (c.ip)",
                "CREATE INDEX domain_host IF NOT EXISTS FOR (d:Domain) ON (d.host)",
                "CREATE INDEX token_value IF NOT EXISTS FOR (t:Token) ON (t.value)",
                "CREATE INDEX content_filename IF NOT EXISTS FOR (c:Content) ON (c.filename)",
                "CREATE INDEX edge_id IF NOT EXISTS FOR (e:Edge) ON (e.id)",
                "CREATE INDEX request_id IF NOT EXISTS FOR (r:Request) ON (r.id)",
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
    def _extract_title(path):
        """경로에서 타이틀 추출 (예: /hls/funny/episode1.m3u8 -> funny)"""
        if not path or path == "-":
            return "unknown"
        parts = path.strip("/").split("/")
        # /hls/타이틀/파일명 구조 가정
        if len(parts) >= 2:
            return parts[1]  # hls 다음 경로를 타이틀로
        return "unknown"

    def parse_nginx_log(self, log_line):
        """Nginx JSON 로그 파싱"""
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

            # IP 주소 우선순위
            real_ip = params.get("real_ip", "-")
            if real_ip == "-":
                real_ip = log.get("client_real_ip", "-")
            if real_ip == "-":
                real_ip = log.get("real_ip", "-")
            if real_ip == "-":
                x_forwarded = log.get("x_forwarded_for", "-")
                if x_forwarded != "-":
                    real_ip = x_forwarded.split(",")[0].strip()
                else:
                    real_ip = log.get("remote_addr", "-")

            session_token = params.get("token", log.get("session_token", "-"))
            # user_id를 account_id로 사용 (고유 식별자)
            user_id = params.get("user_id", log.get("user_id", "-"))
            username = params.get("user", log.get("username", "-"))
            
            # account_id = user_id가 있으면 사용, 없으면 username 사용
            if user_id and user_id != "-":
                account_id = f"user_{user_id}"
            elif username and username != "-":
                account_id = f"guest_{username}"
            else:
                account_id = "-"
                
            content_path = log.get("request_uri", "-")
            
            # Referer 또는 Host에서 도메인 추출
            referer = log.get("http_referer", "-")
            host = log.get("http_host", "-")
            domain = self._extract_domain(referer) or self._extract_domain(host)

            # Content title과 filename
            title = self._extract_title(content_path)
            filename = self._extract_filename(content_path)
            
            # 디버그 로그
            if filename:
                logger.debug(f"Extracted filename: '{filename}' from path: '{content_path}'")

            return {
                "timestamp": log.get("@timestamp") or log.get("timestamp"),
                "account_id": account_id,
                "user_id": user_id,
                "username": username,
                "client_ip": real_ip,
                "domain": domain,
                "session_token": session_token,
                "content_path": content_path,
                "content_title": title,
                "content_filename": filename,
                "method": log.get("request_method", "GET"),
                "status": int(log.get("status", 0) or 0),
                "size": int(log.get("bytes_sent", 0) or 0),
                "user_agent": log.get("http_user_agent", "-"),
            }
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None

    def read_new_logs(self):
        """신규 로그 읽기"""
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

            logger.info(f"Read {len(logs)} new log entries")
            return logs
        except Exception as e:
            logger.error(f"Read error: {e}")
            return logs

    def build_knowledge_graph(self, logs):
        """
        지식그래프 생성 (Account 포함 Request 중심)
        """
        with self.driver.session() as session:
            stats = defaultdict(int)
            edge_id = "edge-scrubber-1"

            for log in logs:
                if log["status"] < 200 or log["status"] >= 400:
                    continue
                if "/hls/" not in log["content_path"]:
                    continue

                timestamp = log["timestamp"]
                account_id = log["account_id"]
                username = log.get("username", "-")
                user_id = log.get("user_id", "-")
                client_ip = log["client_ip"]
                domain = log["domain"]
                token = log["session_token"]
                content_path = log["content_path"]
                content_title = log["content_title"]
                content_filename = log["content_filename"]
                
                # Request ID 생성
                req_hash = hashlib.md5(
                    f"{timestamp}{client_ip}{content_path}".encode()
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
                        r.status = $status,
                        r.size = $size,
                        r.user_agent = $user_agent
                    """,
                    req_id=request_id,
                    timestamp=timestamp,
                    method=log["method"],
                    status=log["status"],
                    size=log["size"],
                    user_agent=log["user_agent"],
                )
                stats["Request"] += 1

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

                # 6. Content 노드 (filename 기준으로 중복 제거)
                if content_filename and content_filename != "-":
                    session.run(
                        """
                        MERGE (c:Content {filename: $filename})
                        ON CREATE SET
                            c.title = $title,
                            c.path = $full_path,
                            c.type = CASE
                                WHEN $filename CONTAINS '.m3u8' THEN 'HLS'
                                WHEN $filename CONTAINS '.mp4' THEN 'MP4'
                                WHEN $filename CONTAINS '.ts' THEN 'HLS_SEGMENT'
                                ELSE 'unknown'
                            END,
                            c.first_accessed = datetime($timestamp),
                            c.view_count = 0,
                            c.total_bytes = 0
                        ON MATCH SET
                            c.last_accessed = datetime($timestamp),
                            c.view_count = coalesce(c.view_count, 0) + 1,
                            c.total_bytes = coalesce(c.total_bytes, 0) + $size
                        """,
                        filename=content_filename,
                        title=content_title,
                        full_path=content_path,
                        timestamp=timestamp,
                        size=log["size"],
                    )
                    stats["Content"] += 1

                    # Request -> FOR_CONTENT -> Content
                    session.run(
                        """
                        MATCH (r:Request {id: $req_id})
                        MATCH (c:Content {filename: $filename})
                        MERGE (r)-[:FOR_CONTENT]->(c)
                        """,
                        req_id=request_id,
                        filename=content_filename,
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
                        e.region = 'local'
                    """,
                    edge_id=edge_id,
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

            return stats

    def detect_leeching(self):
        """
        리칭 패턴 탐지 (Account 기반):
        - 동일 Account/Token의 콘텐츠를 여러 ClientIP/Domain이 사용
        """
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (a:Account)-[:OWNS_TOKEN]->(t:Token)<-[:USING_TOKEN]-(r:Request)
                MATCH (ip:ClientIP)-[:MADE_REQUEST]->(r)
                MATCH (r)-[:TO_DOMAIN]->(d:Domain)
                MATCH (r)-[:FOR_CONTENT]->(c:Content)
                WITH a, t, c, 
                     count(DISTINCT ip) as ip_count, 
                     count(DISTINCT d) as domain_count,
                     collect(DISTINCT ip.ip) as ips,
                     collect(DISTINCT d.host) as domains
                WHERE ip_count > 2 OR domain_count > 1
                RETURN a.account_id as account,
                       t.value as token, 
                       c.title as content_title,
                       c.path as content_file,
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
                    "content_title": record["content_title"],
                    "content_file": record["content_file"],
                    "ip_count": record["ip_count"],
                    "domain_count": record["domain_count"],
                    "ips": record["ips"],
                    "domains": record["domains"],
                })

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
                        logger.warning(f"  Content: {item['content_title']} / {item['content_file']}")
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
