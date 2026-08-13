// ================================================================
// CDN 리칭 탐지를 위한 지식그래프 스키마
// ================================================================

// ----------------------------------------------------------------
// 1. 제약조건 및 인덱스 생성
// ----------------------------------------------------------------

// IP 노드
CREATE CONSTRAINT ip_address_unique IF NOT EXISTS
FOR (ip:IP) REQUIRE ip.address IS UNIQUE;

CREATE INDEX ip_asn_type IF NOT EXISTS
FOR (ip:IP) ON (ip.asn_type);

CREATE INDEX ip_country IF NOT EXISTS
FOR (ip:IP) ON (ip.country);

// 세션 토큰 노드
CREATE CONSTRAINT token_value_unique IF NOT EXISTS
FOR (token:Token) REQUIRE token.value IS UNIQUE;

CREATE INDEX token_issued_to IF NOT EXISTS
FOR (token:Token) ON (token.issued_to);

// Referer 도메인 노드
CREATE CONSTRAINT referer_domain_unique IF NOT EXISTS
FOR (referer:Referer) REQUIRE referer.domain IS UNIQUE;

CREATE INDEX referer_whitelist IF NOT EXISTS
FOR (referer:Referer) ON (referer.is_whitelist);

// 콘텐츠 노드
CREATE CONSTRAINT content_id_unique IF NOT EXISTS
FOR (content:Content) REQUIRE content.id IS UNIQUE;

// 사용자 노드
CREATE CONSTRAINT user_id_unique IF NOT EXISTS
FOR (user:User) REQUIRE user.id IS UNIQUE;

// 세션 노드
CREATE CONSTRAINT session_id_unique IF NOT EXISTS
FOR (session:Session) REQUIRE session.id IS UNIQUE;

CREATE INDEX session_created_at IF NOT EXISTS
FOR (session:Session) ON (session.created_at);

// ----------------------------------------------------------------
// 2. 노드 라벨 정의
// ----------------------------------------------------------------

// IP 노드: 클라이언트 IP 주소
// 속성:
//   - address: IP 주소 (필수, 유니크)
//   - asn_type: ASN 타입 (residential, datacenter, mobile, vpn, hosting)
//   - asn: AS 번호
//   - asn_org: AS 조직명
//   - country: 국가 코드 (KR, US, JP 등)
//   - city: 도시
//   - risk_score: 위험 점수 (0.0 ~ 1.0)
//   - is_proxy: 프록시 여부
//   - is_vpn: VPN 여부
//   - first_seen: 첫 관찰 시간
//   - last_seen: 마지막 관찰 시간
//   - request_count: 총 요청 수
//   - total_bytes: 총 전송 바이트

// Token 노드: 세션 토큰
// 속성:
//   - value: 토큰 값 (필수, 유니크)
//   - issued_to: 발급 대상 사용자 ID
//   - created_at: 생성 시간
//   - expires_at: 만료 시간
//   - is_valid: 유효 여부
//   - ip_count: 사용된 IP 수
//   - request_count: 총 요청 수

// Referer 노드: HTTP Referer 도메인
// 속성:
//   - domain: 도메인 (필수, 유니크)
//   - is_whitelist: 화이트리스트 여부
//   - risk_score: 위험 점수
//   - first_seen: 첫 관찰 시간
//   - request_count: 총 요청 수
//   - unique_ips: 고유 IP 수
//   - unique_tokens: 고유 토큰 수

// Content 노드: 스트리밍 콘텐츠
// 속성:
//   - id: 콘텐츠 ID (필수, 유니크)
//   - title: 제목
//   - type: 타입 (movie, series, episode)
//   - duration: 재생 시간 (초)
//   - file_size: 파일 크기 (바이트)
//   - request_count: 총 요청 수

// User 노드: 사용자
// 속성:
//   - id: 사용자 ID (필수, 유니크)
//   - email: 이메일
//   - username: 사용자명
//   - subscription_plan: 구독 플랜
//   - created_at: 가입 시간
//   - session_count: 세션 수

// Session 노드: 세션
// 속성:
//   - id: 세션 ID (필수, 유니크)
//   - user_id: 사용자 ID
//   - created_at: 생성 시간
//   - expires_at: 만료 시간
//   - is_active: 활성 여부
//   - request_count: 요청 수
//   - total_bytes: 전송 바이트

// ----------------------------------------------------------------
// 3. 관계 정의
// ----------------------------------------------------------------

// (IP)-[:ACCESSED]->(Content)
// 속성:
//   - timestamp: 접속 시간
//   - bytes_sent: 전송 바이트
//   - request_count: 요청 수
//   - last_access: 마지막 접속 시간

// (IP)-[:USED_TOKEN]->(Token)
// 속성:
//   - first_used: 첫 사용 시간
//   - last_used: 마지막 사용 시간
//   - request_count: 요청 수

// (IP)-[:FROM_REFERER]->(Referer)
// 속성:
//   - first_seen: 첫 관찰 시간
//   - last_seen: 마지막 관찰 시간
//   - request_count: 요청 수

// (IP)-[:CORRELATED_WITH]->(IP)
// 속성:
//   - correlation_score: 상관관계 점수 (0.0 ~ 1.0)
//   - shared_tokens: 공유 토큰 수
//   - shared_referers: 공유 referer 수
//   - time_overlap: 시간 겹침 정도

// (Token)-[:ISSUED_TO]->(User)
// 속성:
//   - issued_at: 발급 시간

// (Token)-[:USED_FROM]->(IP)
// 속성:
//   - first_used: 첫 사용 시간
//   - last_used: 마지막 사용 시간
//   - request_count: 요청 수

// (Session)-[:BELONGS_TO]->(User)
// 속성:
//   - created_at: 생성 시간

// (Session)-[:HAS_TOKEN]->(Token)
// 속성:
//   - created_at: 생성 시간

// (Session)-[:FROM_IP]->(IP)
// 속성:
//   - first_seen: 첫 관찰 시간
//   - last_seen: 마지막 관찰 시간

// (Referer)-[:ACCESSED]->(Content)
// 속성:
//   - request_count: 요청 수
//   - unique_ips: 고유 IP 수

// ----------------------------------------------------------------
// 4. 샘플 데이터 (테스트용)
// ----------------------------------------------------------------

// 정상 사용자 패턴
MERGE (user1:User {id: 1, email: 'user1@test.com', username: 'testuser1', subscription_plan: 'standard'})
MERGE (ip1:IP {address: '192.168.1.100', asn_type: 'residential', country: 'KR'})
MERGE (token1:Token {value: 'session_valid_token_1', issued_to: 1})
MERGE (referer1:Referer {domain: 'localhost:3000', is_whitelist: true})
MERGE (content1:Content {id: 'content_1', title: '샘플 영상 1', type: 'video'})

MERGE (user1)-[:HAS_SESSION]->(token1)
MERGE (ip1)-[:USED_TOKEN]->(token1)
MERGE (ip1)-[:FROM_REFERER]->(referer1)
MERGE (ip1)-[:ACCESSED {timestamp: datetime(), bytes_sent: 1024000}]->(content1)

// 의심스러운 패턴 (다중 IP가 동일 토큰 사용)
MERGE (ip2:IP {address: '203.0.113.10', asn_type: 'hosting', country: 'US'})
MERGE (ip3:IP {address: '203.0.113.11', asn_type: 'hosting', country: 'US'})
MERGE (ip4:IP {address: '203.0.113.12', asn_type: 'hosting', country: 'US'})
MERGE (referer2:Referer {domain: 'suspicious-site.com', is_whitelist: false, risk_score: 0.8})

MERGE (ip2)-[:USED_TOKEN]->(token1)
MERGE (ip3)-[:USED_TOKEN]->(token1)
MERGE (ip4)-[:USED_TOKEN]->(token1)
MERGE (ip2)-[:FROM_REFERER]->(referer2)
MERGE (ip3)-[:FROM_REFERER]->(referer2)
MERGE (ip4)-[:FROM_REFERER]->(referer2)

// IP 간 상관관계
MERGE (ip2)-[:CORRELATED_WITH {correlation_score: 0.9, shared_tokens: 1}]->(ip3)
MERGE (ip3)-[:CORRELATED_WITH {correlation_score: 0.9, shared_tokens: 1}]->(ip4)

// ----------------------------------------------------------------
// 5. 유용한 쿼리 예제
// ----------------------------------------------------------------

// 예제 1: 동일 토큰을 사용하는 IP 수가 많은 경우 (리칭 의심)
// MATCH (token:Token)<-[:USED_TOKEN]-(ip:IP)
// WITH token, COUNT(DISTINCT ip) as ip_count
// WHERE ip_count > 5
// RETURN token.value, ip_count
// ORDER BY ip_count DESC;

// 예제 2: 의심스러운 Referer에서 온 요청
// MATCH (referer:Referer {is_whitelist: false})<-[:FROM_REFERER]-(ip:IP)-[:ACCESSED]->(content:Content)
// RETURN referer.domain, COUNT(DISTINCT ip) as unique_ips, COUNT(*) as request_count
// ORDER BY request_count DESC;

// 예제 3: 특정 IP의 행동 패턴 분석
// MATCH (ip:IP {address: '203.0.113.10'})-[r]->(target)
// RETURN labels(target) as target_type, type(r) as relationship, COUNT(*) as count;

// 예제 4: 토큰 당 ASN 타입 분포 (정상 vs 의심)
// MATCH (token:Token)<-[:USED_TOKEN]-(ip:IP)
// WITH token, ip.asn_type as asn_type, COUNT(*) as count
// RETURN token.value, COLLECT({asn_type: asn_type, count: count}) as asn_distribution;

// 예제 5: IP 상관관계 그래프
// MATCH path = (ip1:IP)-[:CORRELATED_WITH*1..2]->(ip2:IP)
// WHERE ip1.asn_type = 'hosting'
// RETURN path;
