"""
Graph Builder - Neo4j 지식그래프 구축
CDN 로그에서 IP, Token, Referer, Content 관계를 지식그래프로 구축
"""

import logging
from neo4j import GraphDatabase
from typing import List, Dict
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class GraphBuilder:
    """Neo4j 지식그래프 구축 및 업데이트"""
    
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info(f"Connected to Neo4j: {uri}")
    
    def close(self):
        """연결 종료"""
        self.driver.close()
    
    def build_from_logs(self, logs: List[Dict]):
        """
        Elasticsearch 로그로부터 지식그래프 구축
        
        Args:
            logs: Elasticsearch에서 가져온 로그 리스트
        """
        logger.info(f"Building knowledge graph from {len(logs)} log entries")
        
        with self.driver.session() as session:
            for log in logs:
                self._process_log_entry(session, log)
        
        logger.info("Knowledge graph built successfully")
    
    def _process_log_entry(self, session, log: Dict):
        """
        단일 로그 엔트리 처리
        
        로그 구조:
        {
            "timestamp": "2025-12-02T10:30:45+09:00",
            "remote_addr": "192.168.1.100",
            "request_uri": "/hls/content1/index.m3u8",
            "http_referer": "http://localhost:3000",
            "http_x_session_token": "session_abc123",
            "http_x_content_id": "content_1",
            "body_bytes_sent": "1024000",
            "status": "200"
        }
        """
        # 로그 파싱
        ip_address = log.get('remote_addr', 'unknown')
        session_token = log.get('http_x_session_token', None)
        referer = log.get('http_referer', None)
        content_id = log.get('http_x_content_id', None)
        request_uri = log.get('request_uri', '')
        bytes_sent = int(log.get('body_bytes_sent', 0))
        timestamp = log.get('timestamp', datetime.utcnow().isoformat())
        status = log.get('status', '200')
        
        # URI에서 콘텐츠 ID 추출 (content_id가 없는 경우)
        if not content_id and '/hls/' in request_uri:
            parts = request_uri.split('/hls/')
            if len(parts) > 1:
                content_id = parts[1].split('/')[0]
        
        # Referer에서 도메인만 추출
        referer_domain = self._extract_domain(referer) if referer else 'direct'
        
        # 1. IP 노드 생성/업데이트
        session.run("""
            MERGE (ip:IP {address: $ip_address})
            ON CREATE SET 
                ip.first_seen = datetime($timestamp),
                ip.country = 'unknown',
                ip.asn_type = 'unknown',
                ip.request_count = 0,
                ip.total_bytes = 0
            ON MATCH SET
                ip.last_seen = datetime($timestamp),
                ip.request_count = ip.request_count + 1,
                ip.total_bytes = ip.total_bytes + $bytes_sent
        """, 
            ip_address=ip_address,
            timestamp=timestamp,
            bytes_sent=bytes_sent
        )
        
        # 2. Token 노드 및 관계 생성 (토큰이 있는 경우)
        if session_token:
            session.run("""
                MERGE (token:Token {value: $token_value})
                ON CREATE SET 
                    token.created_at = datetime($timestamp),
                    token.request_count = 0,
                    token.is_valid = true
                ON MATCH SET
                    token.request_count = token.request_count + 1,
                    token.last_used = datetime($timestamp)
                
                MERGE (ip:IP {address: $ip_address})
                MERGE (ip)-[r:USED_TOKEN]->(token)
                ON CREATE SET 
                    r.first_used = datetime($timestamp),
                    r.request_count = 1
                ON MATCH SET 
                    r.last_used = datetime($timestamp),
                    r.request_count = r.request_count + 1
            """,
                token_value=session_token,
                ip_address=ip_address,
                timestamp=timestamp
            )
        
        # 3. Referer 노드 및 관계 생성
        is_whitelist = referer_domain in ['localhost:3000', 'localhost:8081', 'direct']
        suspicious_keywords = ['illegal', 'free-stream', 'crack', 'torrent', 'pirate', '무료']
        risk_score = 0.8 if any(kw in referer_domain.lower() for kw in suspicious_keywords) else 0.1
        
        session.run("""
            MERGE (referer:Referer {domain: $referer_domain})
            ON CREATE SET 
                referer.first_seen = datetime($timestamp),
                referer.is_whitelist = $is_whitelist,
                referer.risk_score = $risk_score,
                referer.request_count = 0,
                referer.unique_ips = 0
            ON MATCH SET
                referer.last_seen = datetime($timestamp),
                referer.request_count = referer.request_count + 1
            
            MERGE (ip:IP {address: $ip_address})
            MERGE (ip)-[r:FROM_REFERER]->(referer)
            ON CREATE SET 
                r.first_seen = datetime($timestamp),
                r.request_count = 1
            ON MATCH SET 
                r.last_seen = datetime($timestamp),
                r.request_count = r.request_count + 1
        """,
            referer_domain=referer_domain,
            ip_address=ip_address,
            timestamp=timestamp,
            is_whitelist=is_whitelist,
            risk_score=risk_score
        )
        
        # 4. Content 노드 및 관계 생성 (content_id가 있는 경우)
        if content_id:
            session.run("""
                MERGE (content:Content {id: $content_id})
                ON CREATE SET 
                    content.type = 'video',
                    content.request_count = 0
                ON MATCH SET
                    content.request_count = content.request_count + 1,
                    content.last_accessed = datetime($timestamp)
                
                MERGE (ip:IP {address: $ip_address})
                MERGE (ip)-[r:ACCESSED]->(content)
                ON CREATE SET 
                    r.timestamp = datetime($timestamp),
                    r.bytes_sent = $bytes_sent,
                    r.request_count = 1
                ON MATCH SET 
                    r.last_access = datetime($timestamp),
                    r.bytes_sent = r.bytes_sent + $bytes_sent,
                    r.request_count = r.request_count + 1
            """,
                content_id=content_id,
                ip_address=ip_address,
                timestamp=timestamp,
                bytes_sent=bytes_sent
            )
    
    def _extract_domain(self, url: str) -> str:
        """URL에서 도메인만 추출"""
        try:
            if not url.startswith(('http://', 'https://')):
                url = 'http://' + url
            parsed = urlparse(url)
            return parsed.netloc or 'direct'
        except:
            return 'unknown'
    
    def calculate_correlations(self):
        """
        IP 간 상관관계 계산
        동일한 토큰이나 referer를 공유하는 IP들 간의 관계 생성
        """
        logger.info("Calculating IP correlations...")
        
        with self.driver.session() as session:
            # 동일 토큰을 사용하는 IP 쌍 찾기
            session.run("""
                MATCH (ip1:IP)-[:USED_TOKEN]->(token:Token)<-[:USED_TOKEN]-(ip2:IP)
                WHERE ip1.address < ip2.address
                WITH ip1, ip2, COUNT(DISTINCT token) as shared_tokens
                WHERE shared_tokens > 0
                MERGE (ip1)-[r:CORRELATED_WITH]-(ip2)
                SET r.shared_tokens = shared_tokens,
                    r.correlation_score = toFloat(shared_tokens) / 10.0,
                    r.updated_at = datetime()
            """)
            
            # 동일 referer를 공유하는 IP 쌍 찾기
            session.run("""
                MATCH (ip1:IP)-[:FROM_REFERER]->(ref:Referer)<-[:FROM_REFERER]-(ip2:IP)
                WHERE ip1.address < ip2.address AND ref.is_whitelist = false
                WITH ip1, ip2, COUNT(DISTINCT ref) as shared_referers
                WHERE shared_referers > 0
                MERGE (ip1)-[r:CORRELATED_WITH]-(ip2)
                ON CREATE SET r.shared_referers = shared_referers
                ON MATCH SET r.shared_referers = shared_referers
                SET r.correlation_score = coalesce(r.correlation_score, 0.0) + toFloat(shared_referers) / 5.0,
                    r.updated_at = datetime()
            """)
        
        logger.info("IP correlations calculated")
    
    def get_graph_stats(self) -> Dict:
        """그래프 통계 조회"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (ip:IP) WITH COUNT(ip) as ip_count
                MATCH (token:Token) WITH ip_count, COUNT(token) as token_count
                MATCH (referer:Referer) WITH ip_count, token_count, COUNT(referer) as referer_count
                MATCH (content:Content) WITH ip_count, token_count, referer_count, COUNT(content) as content_count
                MATCH ()-[r]->() WITH ip_count, token_count, referer_count, content_count, COUNT(r) as relationship_count
                RETURN ip_count, token_count, referer_count, content_count, relationship_count
            """)
            
            record = result.single()
            if record:
                return {
                    'ips': record['ip_count'],
                    'tokens': record['token_count'],
                    'referers': record['referer_count'],
                    'contents': record['content_count'],
                    'relationships': record['relationship_count']
                }
            return {}
    
    def upsert_sessions(self, sessions: List[Dict]):
        """
        기존 호환성을 위한 메서드
        세션 데이터를 그래프에 추가/업데이트
        """
        logger.info(f"Upserting {len(sessions)} sessions (legacy method)")
        # 구현 생략 - build_from_logs 사용 권장
        pass

