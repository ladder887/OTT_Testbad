"""
Manual Graph Builder - Elasticsearch 로그를 Neo4j 그래프로 변환
Detection Engine이 작동하지 않을 때 수동으로 실행하는 스크립트
"""

import json
from datetime import datetime, timedelta
from elasticsearch import Elasticsearch
from neo4j import GraphDatabase

# 설정
ES_URL = "http://localhost:9200"
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "ott_detection_2025"

def fetch_recent_logs(hours=1):
    """최근 N시간 로그 가져오기"""
    es = Elasticsearch([ES_URL])
    
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=hours)
    
    query = {
        "query": {
            "range": {
                "@timestamp": {
                    "gte": start_time.isoformat(),
                    "lte": end_time.isoformat()
                }
            }
        },
        "size": 1000,
        "sort": [{"@timestamp": "desc"}]
    }
    
    try:
        response = es.search(index="scrubber-nginx-*", **query)
        logs = [hit['_source'] for hit in response['hits']['hits']]
        print(f"✅ Fetched {len(logs)} logs")
        return logs
    except Exception as e:
        print(f"❌ Error fetching logs: {e}")
        return []

def build_graph(logs):
    """로그에서 그래프 생성"""
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    with driver.session() as session:
        # 기존 데이터 삭제 (옵션)
        # session.run("MATCH (n) DETACH DELETE n")
        
        node_count = 0
        rel_count = 0
        
        for log in logs:
            remote_addr = log.get('remote_addr', 'unknown')
            session_token = log.get('http_x_session_token', '')
            referer = log.get('http_referer', '')
            request_uri = log.get('request_uri', '')
            timestamp = log.get('@timestamp', log.get('timestamp', ''))
            
            # Content ID 추출
            content_id = None
            if '/hls/' in request_uri:
                parts = request_uri.split('/hls/')
                if len(parts) > 1:
                    content_id = parts[1].split('.')[0]
            
            # IP 노드 생성
            session.run("""
                MERGE (ip:IPAddress {address: $address})
                ON CREATE SET ip.first_seen = datetime($timestamp),
                              ip.country = 'KR'
                ON MATCH SET ip.last_seen = datetime($timestamp)
            """, address=remote_addr, timestamp=timestamp)
            node_count += 1
            
            # Token이 있으면 Token 노드 및 관계 생성
            if session_token:
                session.run("""
                    MERGE (token:Token {value: $value})
                    ON CREATE SET token.created_at = datetime($timestamp)
                """, value=session_token, timestamp=timestamp)
                node_count += 1
                
                session.run("""
                    MATCH (ip:IPAddress {address: $address})
                    MATCH (token:Token {value: $token})
                    MERGE (ip)-[r:USED_TOKEN]->(token)
                    ON CREATE SET r.first_used = datetime($timestamp)
                    ON MATCH SET r.last_used = datetime($timestamp)
                """, address=remote_addr, token=session_token, timestamp=timestamp)
                rel_count += 1
            
            # Referer가 있으면 Referer 노드 및 관계 생성
            if referer:
                domain = referer.split('//')[-1].split('/')[0]
                session.run("""
                    MERGE (ref:Referer {domain: $domain})
                    ON CREATE SET ref.is_whitelist = true
                """, domain=domain)
                node_count += 1
                
                session.run("""
                    MATCH (ip:IPAddress {address: $address})
                    MATCH (ref:Referer {domain: $domain})
                    MERGE (ip)-[:FROM_REFERER]->(ref)
                """, address=remote_addr, domain=domain)
                rel_count += 1
            
            # Content가 있으면 Content 노드 및 관계 생성
            if content_id and session_token:
                session.run("""
                    MERGE (content:Content {id: $content_id})
                    ON CREATE SET content.title = $content_id
                """, content_id=content_id)
                node_count += 1
                
                session.run("""
                    MATCH (token:Token {value: $token})
                    MATCH (content:Content {id: $content_id})
                    MERGE (token)-[r:ACCESSED]->(content)
                    ON CREATE SET r.first_access = datetime($timestamp), r.access_count = 1
                    ON MATCH SET r.last_access = datetime($timestamp), r.access_count = r.access_count + 1
                """, token=session_token, content_id=content_id, timestamp=timestamp)
                rel_count += 1
        
        # 통계 조회
        result = session.run("MATCH (n) RETURN count(n) as total")
        total_nodes = result.single()['total']
        
        result = session.run("MATCH ()-[r]->() RETURN count(r) as total")
        total_rels = result.single()['total']
        
        print(f"✅ Graph built successfully!")
        print(f"   - Total nodes: {total_nodes}")
        print(f"   - Total relationships: {total_rels}")
    
    driver.close()

if __name__ == "__main__":
    print("=" * 60)
    print("Manual Graph Builder")
    print("=" * 60)
    
    # 1. 로그 수집
    print("\n1️⃣  Fetching logs from Elasticsearch...")
    logs = fetch_recent_logs(hours=24)  # 최근 24시간
    
    if not logs:
        print("❌ No logs found. Exiting.")
        exit(1)
    
    # 2. 그래프 생성
    print("\n2️⃣  Building knowledge graph in Neo4j...")
    build_graph(logs)
    
    print("\n" + "=" * 60)
    print("✅ Done! Visit http://localhost:7474 to view the graph")
    print("   Try this query: MATCH p=()-[]->() RETURN p LIMIT 50")
    print("=" * 60)
