"""
Leeching Detector - 리칭 패턴 탐지
"""

import logging
from neo4j import GraphDatabase
from typing import Dict, List

logger = logging.getLogger(__name__)


class LeechingDetector:
    """지식그래프 기반 리칭 탐지"""
    
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info("Leeching detector initialized")
    
    def close(self):
        """연결 종료"""
        self.driver.close()
    
    def detect_suspicious_patterns(self) -> Dict[str, List[str]]:
        """다양한 패턴으로 의심스러운 엔티티 탐지"""
        suspicious = {
            'ips': [],
            'tokens': [],
            'referers': []
        }
        
        with self.driver.session() as session:
            # 1. 동일 토큰의 다중 IP 사용
            multi_ip_sessions = self._detect_multi_ip_sessions(session)
            suspicious['tokens'].extend(multi_ip_sessions)
            
            # 2. 과도한 트래픽
            high_traffic_sessions = self._detect_high_traffic(session)
            suspicious['tokens'].extend(high_traffic_sessions)
            
            # 3. 의심스러운 Referer
            suspicious_referers = self._detect_suspicious_referers(session)
            suspicious['referers'].extend(suspicious_referers)
            
            # 4. 비정상적인 IP 패턴
            suspicious_ips = self._detect_suspicious_ips(session)
            suspicious['ips'].extend(suspicious_ips)
        
        # 중복 제거
        suspicious['ips'] = list(set(suspicious['ips']))
        suspicious['tokens'] = list(set(suspicious['tokens']))
        suspicious['referers'] = list(set(suspicious['referers']))
        
        # 의심 세션을 Neo4j에 마킹
        self._mark_suspicious_sessions(suspicious['tokens'])
        
        return suspicious
    
    def _detect_multi_ip_sessions(self, session, threshold: int = 3) -> List[str]:
        """동일 토큰에서 너무 많은 IP 사용 (리칭의 전형적 패턴)"""
        result = session.run("""
            MATCH (s:Session)-[:USED_IP]->(ip:IPAddress)
            WITH s, COUNT(DISTINCT ip) AS ip_count
            WHERE ip_count >= $threshold
            RETURN s.token AS token, ip_count
            ORDER BY ip_count DESC
        """, threshold=threshold)
        
        tokens = []
        for record in result:
            tokens.append(record['token'])
            logger.info(f"Multi-IP session detected: {record['token']} ({record['ip_count']} IPs)")
        
        return tokens
    
    def _detect_high_traffic(self, session, gb_threshold: float = 5.0) -> List[str]:
        """과도한 트래픽 (GB 단위)"""
        result = session.run("""
            MATCH (s:Session)
            WHERE s.total_bytes > $threshold
            RETURN s.token AS token, s.total_bytes AS bytes
            ORDER BY s.total_bytes DESC
        """, threshold=int(gb_threshold * 1024 * 1024 * 1024))
        
        tokens = []
        for record in result:
            tokens.append(record['token'])
            gb = record['bytes'] / (1024 * 1024 * 1024)
            logger.info(f"High traffic session detected: {record['token']} ({gb:.2f} GB)")
        
        return tokens
    
    def _detect_suspicious_referers(self, session) -> List[str]:
        """의심스러운 Referer에서 온 세션들"""
        result = session.run("""
            MATCH (s:Session)-[:CAME_FROM]->(r:Referer)
            WHERE r.suspicious = true
            RETURN DISTINCT r.url AS referer
        """)
        
        referers = []
        for record in result:
            referers.append(record['referer'])
            logger.info(f"Suspicious referer detected: {record['referer']}")
        
        return referers
    
    def _detect_suspicious_ips(self, session, session_threshold: int = 20) -> List[str]:
        """하나의 IP에서 너무 많은 세션 (공유 계정 의심)"""
        result = session.run("""
            MATCH (s:Session)-[:USED_IP]->(ip:IPAddress)
            WITH ip, COUNT(DISTINCT s) AS session_count
            WHERE session_count >= $threshold
            RETURN ip.address AS address, session_count
            ORDER BY session_count DESC
        """, threshold=session_threshold)
        
        ips = []
        for record in result:
            ips.append(record['address'])
            logger.info(f"Suspicious IP detected: {record['address']} ({record['session_count']} sessions)")
        
        return ips
    
    def _mark_suspicious_sessions(self, tokens: List[str]):
        """의심스러운 세션을 그래프에 마킹"""
        if not tokens:
            return
        
        with self.driver.session() as session:
            session.run("""
                UNWIND $tokens AS token
                MATCH (s:Session {token: token})
                SET s.suspicious = true,
                    s.flagged_at = datetime()
            """, tokens=tokens)
        
        logger.info(f"Marked {len(tokens)} sessions as suspicious")
