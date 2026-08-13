// Neo4j 지식그래프 샘플 데이터 생성 스크립트
// Neo4j Browser에서 실행하거나 cypher-shell로 실행

// 1. 기존 데이터 삭제 (선택사항)
// MATCH (n) DETACH DELETE n;

// 2. 샘플 노드 및 관계 생성

// 정상 사용자 패턴
CREATE (ip1:IPAddress {address: '192.168.1.100', country: 'KR', asn_type: 'ISP', first_seen: datetime()})
CREATE (token1:Token {value: 'user1_session_abc123', issued_to: 'user1', created_at: datetime()})
CREATE (content1:Content {id: 'video1', title: 'AlphaGo Video', content_type: 'video'})
CREATE (content2:Content {id: 'video2', title: 'Starcraft Video', content_type: 'video'})
CREATE (referer1:Referer {domain: 'localhost:3000', is_whitelist: true})

CREATE (ip1)-[:USED_TOKEN {first_used: datetime(), last_used: datetime()}]->(token1)
CREATE (token1)-[:ACCESSED {timestamp: datetime(), duration: 180, access_count: 1}]->(content1)
CREATE (ip1)-[:FROM_REFERER]->(referer1);

// 의심스러운 패턴 - 같은 토큰을 여러 IP에서 사용
CREATE (ip2:IPAddress {address: '203.248.45.123', country: 'KR', asn_type: 'ISP', first_seen: datetime()})
CREATE (ip3:IPAddress {address: '58.234.123.45', country: 'KR', asn_type: 'Mobile', first_seen: datetime()})
CREATE (token2:Token {value: 'shared_token_xyz789', issued_to: 'user2', created_at: datetime()})

CREATE (ip2)-[:USED_TOKEN {first_used: datetime(), last_used: datetime()}]->(token2)
CREATE (ip3)-[:USED_TOKEN {first_used: datetime(), last_used: datetime()}]->(token2)
CREATE (token2)-[:ACCESSED {timestamp: datetime(), duration: 240, access_count: 5}]->(content1)
CREATE (token2)-[:ACCESSED {timestamp: datetime(), duration: 150, access_count: 3}]->(content2)
CREATE (ip2)-[:FROM_REFERER]->(referer1)
CREATE (ip3)-[:FROM_REFERER]->(referer1);

// 외부 링크 접근 (리칭 의심)
CREATE (referer2:Referer {domain: 'unknown-site.com', is_whitelist: false})
CREATE (ip4:IPAddress {address: '123.45.67.89', country: 'KR', asn_type: 'ISP', first_seen: datetime()})
CREATE (token3:Token {value: 'leaked_token_def456', issued_to: 'user3', created_at: datetime()})

CREATE (ip4)-[:USED_TOKEN {first_used: datetime(), last_used: datetime()}]->(token3)
CREATE (ip4)-[:FROM_REFERER]->(referer2)
CREATE (token3)-[:ACCESSED {timestamp: datetime(), duration: 300, access_count: 10}]->(content1);

// 통계 조회
MATCH (n) RETURN labels(n)[0] as NodeType, count(*) as Count
ORDER BY Count DESC;
