"""
개선된 지식 그래프 빌더 - 상세 노드 및 관계 생성
"""
import os
from neo4j import GraphDatabase
import logging

logger = logging.getLogger(__name__)

class GraphBuilderV2:
    def __init__(self):
        neo4j_uri = os.getenv('NEO4J_URI', 'bolt://neo4j:7687')
        neo4j_user = os.getenv('NEO4J_USER', 'neo4j')
        neo4j_password = os.getenv('NEO4J_PASSWORD', 'ott_detection_2025')
        
        self.driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        logger.info(f"Connected to Neo4j at {neo4j_uri}")
        
        # 인덱스 생성
        self._create_indexes()
    
    def _create_indexes(self):
        """성능 향상을 위한 인덱스 생성"""
        with self.driver.session() as session:
            indexes = [
                "CREATE INDEX ip_address IF NOT EXISTS FOR (ip:IPAddress) ON (ip.address)",
                "CREATE INDEX user_username IF NOT EXISTS FOR (u:User) ON (u.username)",
                "CREATE INDEX token_value IF NOT EXISTS FOR (t:Token) ON (t.value)",
                "CREATE INDEX content_id IF NOT EXISTS FOR (c:Content) ON (c.id)",
                "CREATE INDEX referer_url IF NOT EXISTS FOR (r:Referer) ON (r.url)"
            ]
            for index_query in indexes:
                try:
                    session.run(index_query)
                except Exception as e:
                    logger.debug(f"Index creation (already exists or failed): {e}")
    
    def build_graph(self, log_data: list) -> dict:
        """
        로그 데이터로부터 지식 그래프 생성 (대폭 개선)
        
        노드 타입:
        - User: 사용자 계정 (username)
        - Token: 세션 토큰 (session token)
        - IPAddress: 클라이언트 IP 주소
        - Content: 비디오 콘텐츠 (video1, video2, etc.)
        - Referer: 요청 출처 도메인
        
        관계 타입:
        - (User)-[HAS_TOKEN]->(Token): 사용자가 생성한 토큰
        - (User)-[WATCHES]->(Content): 사용자가 시청한 콘텐츠
        - (Token)-[USED_BY]->(IPAddress): 토큰을 사용한 IP
        - (Token)-[ACCESSES]->(Content): 토큰으로 접근한 콘텐츠
        - (IPAddress)-[VIEWS]->(Content): IP에서 시청한 콘텐츠
        - (Referer)-[REFERS]->(IPAddress): Referer에서 유입된 IP
        """
        try:
            processed_count = 0
            skipped_count = 0
            
            with self.driver.session() as session:
                for log in log_data:
                    # 유효성 검사
                    if not log.get('is_valid', True):
                        skipped_count += 1
                        continue
                    
                    # 노드 생성
                    self.create_nodes(session, log)
                    # 관계 생성
                    self.create_relationships(session, log)
                    processed_count += 1
            
            # 통계 수집
            stats = self.get_graph_statistics()
            
            return {
                'success': True,
                'message': 'Knowledge graph built successfully',
                'processed': processed_count,
                'skipped': skipped_count,
                'statistics': stats
            }
            
        except Exception as e:
            logger.error(f"Failed to build graph: {e}")
            return {
                'success': False,
                'message': f'Failed to build graph: {str(e)}'
            }
    
    def create_nodes(self, session, log: dict):
        """
        노드 생성 (개선된 버전)
        - MERGE로 중복 방지
        - ON CREATE/ON MATCH로 속성 업데이트
        """
        timestamp = log['timestamp']
        
        # 1. User 노드
        if log.get('username') and log['username'] not in ['-', '', None]:
            session.run("""
                MERGE (u:User {username: $username})
                ON CREATE SET 
                    u.first_seen = $timestamp,
                    u.last_seen = $timestamp,
                    u.request_count = 1,
                    u.total_bytes = $bytes
                ON MATCH SET 
                    u.last_seen = $timestamp,
                    u.request_count = u.request_count + 1,
                    u.total_bytes = COALESCE(u.total_bytes, 0) + $bytes
            """, username=log['username'], timestamp=timestamp, bytes=log['bytes'])
        
        # 2. Token 노드
        if log.get('token') and log['token'] not in ['-', '', None]:
            session.run("""
                MERGE (t:Token {value: $token})
                ON CREATE SET 
                    t.first_seen = $timestamp,
                    t.last_seen = $timestamp,
                    t.usage_count = 1,
                    t.user = $username
                ON MATCH SET 
                    t.last_seen = $timestamp,
                    t.usage_count = t.usage_count + 1
            """, token=log['token'], timestamp=timestamp, username=log.get('username', 'unknown'))
        
        # 3. IPAddress/Device 노드 (client_id 기반)
        if log.get('ip') and log['ip'] not in ['-', 'unknown', '', None, '172.18.0.1']:
            # device_ 접두사가 있으면 Device 노드, 아니면 IPAddress 노드
            if log['ip'].startswith('device_'):
                node_label = 'Device'
                node_prop = 'device_id'
            else:
                node_label = 'IPAddress'
                node_prop = 'address'
            
            session.run(f"""
                MERGE (ip:{node_label} {{{node_prop}: $ip}})
                ON CREATE SET 
                    ip.first_seen = $timestamp,
                    ip.last_seen = $timestamp,
                    ip.request_count = 1,
                    ip.total_bytes = $bytes,
                    ip.user_agent = $user_agent,
                    ip.client_id = $client_id
                ON MATCH SET 
                    ip.last_seen = $timestamp,
                    ip.request_count = ip.request_count + 1,
                    ip.total_bytes = COALESCE(ip.total_bytes, 0) + $bytes
            """, ip=log['ip'], timestamp=timestamp, bytes=log['bytes'], 
                 user_agent=log['user_agent'], client_id=log.get('client_id', '-'))
        
        # 4. Content 노드 (2-Level: Video + Segment)
        if log.get('content_id') and log['content_id'] not in ['-', '', None]:
            uri = log.get('request_uri', '')
            
            # HLS 세그먼트 요청인지 확인 (master0.ts, master1.ts 등)
            if '/hls/' in uri and '.ts' in uri:
                # URI에서 video_id와 segment 파일명 추출
                # 예: /hls/cat1/master0.ts -> video_id=cat1, segment=master0.ts
                path_parts = uri.split('/hls/')[-1].split('?')[0].split('/')
                if len(path_parts) >= 2:
                    video_id = path_parts[0]  # cat1
                    segment_file = path_parts[1]  # master0.ts
                    segment_id = f"{video_id}_{segment_file}"
                    
                    # Video 노드 생성
                    session.run("""
                        MERGE (v:Content {id: $video_id})
                        ON CREATE SET 
                            v.type = 'VIDEO',
                            v.title = $video_id,
                            v.first_accessed = $timestamp,
                            v.last_accessed = $timestamp,
                            v.view_count = 0,
                            v.total_bytes = 0
                        ON MATCH SET 
                            v.last_accessed = $timestamp
                    """, video_id=video_id, timestamp=timestamp)
                    
                    # Segment 노드 생성 및 Video와 연결
                    session.run("""
                        MERGE (s:Content {id: $segment_id})
                        ON CREATE SET 
                            s.type = 'HLS_SEGMENT',
                            s.filename = $segment_file,
                            s.first_accessed = $timestamp,
                            s.last_accessed = $timestamp,
                            s.access_count = 1,
                            s.total_bytes = $bytes,
                            s.uri = $uri
                        ON MATCH SET 
                            s.last_accessed = $timestamp,
                            s.access_count = s.access_count + 1,
                            s.total_bytes = COALESCE(s.total_bytes, 0) + $bytes
                        
                        WITH s
                        MATCH (v:Content {id: $video_id})
                        MERGE (s)-[:PART_OF]->(v)
                    """, segment_id=segment_id, segment_file=segment_file, video_id=video_id, 
                         timestamp=timestamp, bytes=log['bytes'], uri=uri)
                else:
                    # fallback: 일반 Content 노드
                    session.run("""
                        MERGE (c:Content {id: $content_id})
                        ON CREATE SET 
                            c.first_accessed = $timestamp,
                            c.last_accessed = $timestamp,
                            c.access_count = 1,
                            c.total_bytes = $bytes,
                            c.uri = $uri,
                            c.title = $content_id
                        ON MATCH SET 
                            c.last_accessed = $timestamp,
                            c.access_count = c.access_count + 1,
                            c.total_bytes = COALESCE(c.total_bytes, 0) + $bytes
                    """, content_id=log['content_id'], timestamp=timestamp, bytes=log['bytes'], uri=uri)
            elif '/hls/' in uri and '.m3u8' in uri:
                # m3u8 플레이리스트 요청 - Video 노드만 생성
                path_parts = uri.split('/hls/')[-1].split('?')[0].split('/')
                if len(path_parts) >= 1:
                    video_id = path_parts[0]  # cat1
                    
                    session.run("""
                        MERGE (v:Content {id: $video_id})
                        ON CREATE SET 
                            v.type = 'VIDEO',
                            v.title = $video_id,
                            v.first_accessed = $timestamp,
                            v.last_accessed = $timestamp,
                            v.view_count = 1,
                            v.total_bytes = $bytes
                        ON MATCH SET 
                            v.last_accessed = $timestamp,
                            v.view_count = v.view_count + 1,
                            v.total_bytes = COALESCE(v.total_bytes, 0) + $bytes
                    """, video_id=video_id, timestamp=timestamp, bytes=log['bytes'])
                else:
                    # fallback
                    session.run("""
                        MERGE (c:Content {id: $content_id})
                        ON CREATE SET 
                            c.first_accessed = $timestamp,
                            c.last_accessed = $timestamp,
                            c.access_count = 1,
                            c.total_bytes = $bytes,
                            c.uri = $uri,
                            c.title = $content_id
                        ON MATCH SET 
                            c.last_accessed = $timestamp,
                            c.access_count = c.access_count + 1,
                            c.total_bytes = COALESCE(c.total_bytes, 0) + $bytes
                    """, content_id=log['content_id'], timestamp=timestamp, bytes=log['bytes'], uri=uri)
            else:
                # 일반 Content 노드 (HLS가 아닌 경우)
                session.run("""
                    MERGE (c:Content {id: $content_id})
                    ON CREATE SET 
                        c.first_accessed = $timestamp,
                        c.last_accessed = $timestamp,
                        c.access_count = 1,
                        c.total_bytes = $bytes,
                        c.uri = $uri,
                        c.title = $content_id
                    ON MATCH SET 
                        c.last_accessed = $timestamp,
                        c.access_count = c.access_count + 1,
                        c.total_bytes = COALESCE(c.total_bytes, 0) + $bytes
                """, content_id=log['content_id'], timestamp=timestamp, bytes=log['bytes'], uri=uri)
        
        # 5. Referer 노드
        if log.get('referer_domain') and log['referer_domain'] not in ['-', 'direct', '', None]:
            session.run("""
                MERGE (r:Referer {domain: $domain})
                ON CREATE SET 
                    r.first_seen = $timestamp,
                    r.last_seen = $timestamp,
                    r.count = 1,
                    r.full_url = $referer
                ON MATCH SET 
                    r.last_seen = $timestamp,
                    r.count = r.count + 1
            """, domain=log['referer_domain'], timestamp=timestamp, referer=log['referer'])
    
    def create_relationships(self, session, log: dict):
        """
        관계 생성 (개선된 버전)
        - 모든 가능한 관계 매핑
        - 속성에 통계 정보 추가
        """
        timestamp = log['timestamp']
        
        # 1. User -> Token
        if log.get('username') and log['username'] not in ['-', '', None] and \
           log.get('token') and log['token'] not in ['-', '', None]:
            session.run("""
                MATCH (u:User {username: $username})
                MATCH (t:Token {value: $token})
                MERGE (u)-[r:HAS_TOKEN]->(t)
                ON CREATE SET 
                    r.created = $timestamp,
                    r.last_used = $timestamp,
                    r.use_count = 1
                ON MATCH SET 
                    r.last_used = $timestamp,
                    r.use_count = r.use_count + 1
            """, username=log['username'], token=log['token'], timestamp=timestamp)
        
        # 2. Token -> IPAddress/Device (역방향: Token을 어떤 디바이스가 사용했는지)
        if log.get('token') and log['token'] not in ['-', '', None] and \
           log.get('ip') and log['ip'] not in ['-', 'unknown', '', None, '172.18.0.1']:
            # device_ 접두사 확인
            if log['ip'].startswith('device_'):
                node_match = "MATCH (ip:Device {device_id: $ip})"
            else:
                node_match = "MATCH (ip:IPAddress {address: $ip})"
            
            session.run(f"""
                MATCH (t:Token {{value: $token}})
                {node_match}
                MERGE (t)-[r:USED_BY]->(ip)
                ON CREATE SET 
                    r.first_seen = $timestamp,
                    r.last_seen = $timestamp,
                    r.count = 1,
                    r.total_bytes = $bytes
                ON MATCH SET 
                    r.last_seen = $timestamp,
                    r.count = r.count + 1,
                    r.total_bytes = COALESCE(r.total_bytes, 0) + $bytes
            """, token=log['token'], ip=log['ip'], timestamp=timestamp, bytes=log['bytes'])
        
        # 3. Token -> Content (Segment 또는 Video)
        if log.get('token') and log['token'] not in ['-', '', None]:
            uri = log.get('request_uri', '')
            
            # HLS 세그먼트 요청인 경우 Segment 노드와 연결
            if '/hls/' in uri and '.ts' in uri:
                path_parts = uri.split('/hls/')[-1].split('?')[0].split('/')
                if len(path_parts) >= 2:
                    video_id = path_parts[0]
                    segment_file = path_parts[1]
                    segment_id = f"{video_id}_{segment_file}"
                    
                    session.run("""
                        MATCH (t:Token {value: $token})
                        MATCH (s:Content {id: $segment_id})
                        MERGE (t)-[r:ACCESSES]->(s)
                        ON CREATE SET 
                            r.first_access = $timestamp,
                            r.last_access = $timestamp,
                            r.access_count = 1,
                            r.total_bytes = $bytes
                        ON MATCH SET 
                            r.last_access = $timestamp,
                            r.access_count = r.access_count + 1,
                            r.total_bytes = COALESCE(r.total_bytes, 0) + $bytes
                    """, token=log['token'], segment_id=segment_id, timestamp=timestamp, bytes=log['bytes'])
            # m3u8 플레이리스트 요청인 경우 Video 노드와 연결
            elif '/hls/' in uri and '.m3u8' in uri:
                path_parts = uri.split('/hls/')[-1].split('?')[0].split('/')
                if len(path_parts) >= 1:
                    video_id = path_parts[0]
                    
                    session.run("""
                        MATCH (t:Token {value: $token})
                        MATCH (v:Content {id: $video_id})
                        MERGE (t)-[r:ACCESSES]->(v)
                        ON CREATE SET 
                            r.first_access = $timestamp,
                            r.last_access = $timestamp,
                            r.access_count = 1,
                            r.total_bytes = $bytes
                        ON MATCH SET 
                            r.last_access = $timestamp,
                            r.access_count = r.access_count + 1,
                            r.total_bytes = COALESCE(r.total_bytes, 0) + $bytes
                    """, token=log['token'], video_id=video_id, timestamp=timestamp, bytes=log['bytes'])
            # content_id가 있는 경우 기존 로직
            elif log.get('content_id') and log['content_id'] not in ['-', '', None]:
                session.run("""
                    MATCH (t:Token {value: $token})
                    MATCH (c:Content {id: $content_id})
                    MERGE (t)-[r:ACCESSES]->(c)
                    ON CREATE SET 
                        r.first_access = $timestamp,
                        r.last_access = $timestamp,
                        r.access_count = 1,
                        r.total_bytes = $bytes
                    ON MATCH SET 
                        r.last_access = $timestamp,
                        r.access_count = r.access_count + 1,
                        r.total_bytes = COALESCE(r.total_bytes, 0) + $bytes
                """, token=log['token'], content_id=log['content_id'], timestamp=timestamp, bytes=log['bytes'])
        
        # 4. User -> Content (직접 시청 기록)
        if log.get('username') and log['username'] not in ['-', '', None] and \
           log.get('content_id') and log['content_id'] not in ['-', '', None]:
            session.run("""
                MATCH (u:User {username: $username})
                MATCH (c:Content {id: $content_id})
                MERGE (u)-[r:WATCHES]->(c)
                ON CREATE SET 
                    r.first_watch = $timestamp,
                    r.last_watch = $timestamp,
                    r.watch_count = 1,
                    r.total_bytes = $bytes
                ON MATCH SET 
                    r.last_watch = $timestamp,
                    r.watch_count = r.watch_count + 1,
                    r.total_bytes = COALESCE(r.total_bytes, 0) + $bytes
            """, username=log['username'], content_id=log['content_id'], timestamp=timestamp, bytes=log['bytes'])
        
        # 5. IPAddress/Device -> Content
        if log.get('ip') and log['ip'] not in ['-', 'unknown', '', None, '172.18.0.1'] and \
           log.get('content_id') and log['content_id'] not in ['-', '', None]:
            # device_ 접두사 확인
            if log['ip'].startswith('device_'):
                node_match = "MATCH (ip:Device {device_id: $ip})"
            else:
                node_match = "MATCH (ip:IPAddress {address: $ip})"
            
            session.run(f"""
                {node_match}
                MATCH (c:Content {{id: $content_id}})
                MERGE (ip)-[r:VIEWS]->(c)
                ON CREATE SET 
                    r.first_view = $timestamp,
                    r.last_view = $timestamp,
                    r.view_count = 1,
                    r.total_bytes = $bytes
        # 6. Referer -> IPAddress/Device
        if log.get('referer_domain') and log['referer_domain'] not in ['-', 'direct', '', None] and \
           log.get('ip') and log['ip'] not in ['-', 'unknown', '', None, '172.18.0.1']:
            # device_ 접두사 확인
            if log['ip'].startswith('device_'):
                node_match = "MATCH (ip:Device {device_id: $ip})"
            else:
                node_match = "MATCH (ip:IPAddress {address: $ip})"
            
            session.run(f"""
                MATCH (r:Referer {{domain: $referer_domain}})
                {node_match}
                MERGE (r)-[rel:REFERS]->(ip)
                ON CREATE SET 
                    rel.first_seen = $timestamp,
                    rel.last_seen = $timestamp,
                    rel.count = 1
                ON MATCH SET 
                    rel.last_seen = $timestamp,
                    rel.count = rel.count + 1
            """, referer_domain=log['referer_domain'], ip=log['ip'], timestamp=timestamp)
                    rel.last_seen = $timestamp,
                    rel.count = 1
                ON MATCH SET 
                    rel.last_seen = $timestamp,
                    rel.count = rel.count + 1
            """, referer_domain=log['referer_domain'], ip=log['ip'], timestamp=timestamp)
    
    def get_graph_statistics(self) -> dict:
        """그래프 통계 수집"""
        with self.driver.session() as session:
            # 노드 수
            result = session.run("""
                MATCH (n)
                RETURN labels(n)[0] as NodeType, count(*) as Count
                ORDER BY Count DESC
            """)
            nodes = {row['NodeType']: row['Count'] for row in result}
            
            # 관계 수
            result = session.run("""
                MATCH ()-[r]->()
                RETURN type(r) as RelationType, count(*) as Count
                ORDER BY Count DESC
            """)
            relationships = {row['RelationType']: row['Count'] for row in result}
            
            # 리칭 의심 패턴 탐지 (Device 포함)
            result = session.run("""
                MATCH (t:Token)-[:USED_BY]->(n)
                WHERE n:IPAddress OR n:Device
                WITH t, count(DISTINCT n) as device_count
                WHERE device_count > 1
                RETURN count(*) as suspicious_tokens
            """)
            suspicious_tokens = result.single()['suspicious_tokens']
            
            return {
                'nodes': nodes,
                'relationships': relationships,
                'total_nodes': sum(nodes.values()),
                'total_relationships': sum(relationships.values()),
                'suspicious_tokens': suspicious_tokens
            }
    
    def close(self):
        """Neo4j 연결 종료"""
        self.driver.close()
