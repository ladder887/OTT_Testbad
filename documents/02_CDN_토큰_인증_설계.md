# 02. CDN 토큰 및 인증 설계

> 운영 참고 (2026-04-04): 본 문서는 인증 설계 기준 문서다. 실제 엔드포인트/배포 검증은 `10`, `11`, `12`, `13` 문서를 우선한다.

> 2026-04-04 추가: 라이브 스트리밍(`live_*`)도 동일한 토큰 검증 체계를 사용한다.
> 콘텐츠 유형(vod/live)과 해상도(1080p/720p)는 플랫폼 메타데이터로 관리하며,
> 실제 검증 단위는 기존과 동일하게 `hls_path` + edge 일치성이다.

---

## 1. 인증 체계 전체 흐름

```
┌────────┐     ①로그인      ┌──────────┐
│ Client ├──────────────────→│ API/Auth │
│        │←──────────────────│ Server   │
│        │  ②JWT 발급       │          │
│        │                   │          │
│        │  ③재생 요청(JWT)  │          │
│        ├──────────────────→│          │
│        │←──────────────────│          │
│        │  ④CDN Signed URL  │          │
│        │                   └──────────┘
│        │
│        │  ⑤세그먼트 요청    ┌──────────┐     ⑥캐시 미스 시    ┌──────────┐
│        │  (CDN Token)      │ CDN Edge │────────────────────→│ Origin   │
│        ├──────────────────→│ Server   │←────────────────────│ Server   │
│        │←──────────────────│          │  ⑦세그먼트 반환     │          │
│        │  ⑧세그먼트 반환   └──────────┘                    └──────────┘
└────────┘
```

---

## 2. JWT Access Token (사용자 인증용)

### 2.1 구조

```json
{
  "header": {
    "alg": "HS256",
    "typ": "JWT"
  },
  "payload": {
    "sub": "user_kr_01",
    "email": "user_kr_01@test.com",
    "plan": "premium",
    "max_streams": 2,
    "iat": 1721000000,
    "exp": 1721003600,
    "jti": "jwt_unique_id_001"
  },
  "signature": "HMAC-SHA256(header.payload, JWT_SECRET)"
}
```

### 2.2 발급 조건

| 항목 | 값 |
|------|-----|
| 발급 시점 | 로그인 성공 시 |
| 유효 기간 | 1시간 |
| 갱신 방식 | Refresh Token (7일) |
| 저장 위치 | 클라이언트 메모리 (브라우저) 또는 Secure Cookie |

### 2.3 API 엔드포인트

```
POST /api/auth/login
  Body: { "email": "...", "password": "..." }
  Response: { "access_token": "eyJ...", "refresh_token": "..." }

POST /api/auth/refresh
  Body: { "refresh_token": "..." }
  Response: { "access_token": "eyJ..." }

POST /api/auth/logout
  Header: Authorization: Bearer {access_token}
  Response: { "message": "logged out" }
  → Redis에 토큰 블랙리스트 등록
```

---

## 3. CDN Token (콘텐츠 접근용)

CDN Token은 JWT와 별개로, CDN Edge 서버에서 세그먼트 요청을 검증하기 위한 토큰이다.
실제 상용 CDN(AWS CloudFront, Akamai)의 Signed URL 방식을 참고.

### 3.1 구조

```
CDN Token 페이로드 (URL 파라미터에 Base64로 인코딩):
{
  "uid": "user_kr_01",        // 사용자 ID
  "sid": "sess_a1b2c3",       // 세션 ID
  "cid": "movie_001",         // 콘텐츠 ID
  "edge": "edge-kr",          // 허용 Edge (Primary)
  "exp": 1721001800,          // 만료 시간 (현재 + 30분)
  "iat": 1721000000,          // 발급 시간
  "ip_bind": false,           // IP 바인딩 여부 (선택적)
  "client_ip": "192.168.0.11"  // ip_bind=true일 때만 검증
}
```

### 3.2 서명 방식

```
signature = HMAC-SHA256(
  key = CDN_SHARED_SECRET,
  message = "uid=user_kr_01&sid=sess_a1b2c3&cid=movie_001&edge=edge-kr&exp=1721001800"
)

최종 URL:
https://edge-kr.cdn.local/content/movie_001/manifest.mpd
  ?token=eyJ1aWQiOiJ1c2VyX2...  (Base64 인코딩된 페이로드)
  &sig=a3f8b2c1d4e5...          (HMAC 서명 hex)
```

### 3.3 Edge 서버에서의 검증 로직

```lua
-- OpenResty Lua 기반 토큰 검증 (access_by_lua_block)
local token_b64 = ngx.var.arg_token
local sig = ngx.var.arg_sig

if not token_b64 or not sig then
    ngx.exit(403)  -- 토큰 없음
end

-- 1) Base64 디코딩
local token_json = ngx.decode_base64(token_b64)
local token = cjson.decode(token_json)

-- 2) 만료 시간 검증
if os.time() > token.exp then
    ngx.exit(403)  -- 토큰 만료
end

-- 3) 서명 검증
local expected_sig = ngx.hmac_sha256(CDN_SECRET, canonical_string(token))
if sig ~= expected_sig then
    ngx.exit(403)  -- 서명 불일치
end

-- 4) Edge 서버 검증
if token.edge ~= CURRENT_EDGE_ID then
    ngx.exit(403)  -- 다른 Edge에서 사용 시도
end

-- 5) IP 바인딩 검증 (활성화된 경우)
if token.ip_bind and token.client_ip ~= ngx.var.remote_addr then
    ngx.exit(403)  -- IP 불일치
end

-- 6) 콘텐츠 ID 검증
local requested_content = extract_content_id(ngx.var.uri)
if requested_content ~= token.cid then
    ngx.exit(403)  -- 다른 콘텐츠 접근 시도
end

-- 검증 통과 → 로그에 토큰 정보 기록
ngx.var.token_uid = token.uid
ngx.var.token_sid = token.sid
ngx.var.token_cid = token.cid
ngx.var.token_valid = "true"
```

### 3.4 토큰 생명주기

```
[로그인] → JWT 발급 (1시간)
    │
    ├─ [재생 요청] → CDN Token 발급 (30분)
    │      │
    │      ├─ 세그먼트 요청 (토큰 유효)
    │      ├─ 세그먼트 요청 (토큰 유효)
    │      └─ ...
    │
    ├─ [30분 경과] → CDN Token 만료 → 재생 요청으로 새 토큰 발급
    │
    └─ [1시간 경과] → JWT 만료 → Refresh Token으로 갱신
```

---

## 4. 재생 요청 API

```
POST /api/playback/start
  Header: Authorization: Bearer {JWT}
  Body: { "content_id": "movie_001" }
  
  서버 처리:
    1. JWT 검증
    2. 구독 플랜 확인 (콘텐츠 접근 권한)
    3. 동시 스트림 수 확인 (max_streams 초과 여부)
    4. 세션 ID 생성 → Redis에 저장
    5. 사용자 지역(요청 IP 기반) → Primary Edge 결정
    6. CDN Token 생성 + 서명
    7. Signed URL 반환

  Response:
  {
    "session_id": "sess_a1b2c3",
    "manifest_url": "http://192.168.0.111/hls/movie_001/master.m3u8?token=...&sig=...",
    "token_expires": "2025-07-15T14:30:00Z",
    "edge": "edge-kr"
  }
```

---

## 5. 토큰 관련 보안 설정 (실험 변수)

논문 실험에서 토큰 보안 수준을 변경하며 공격 탐지율 변화를 측정할 수 있음.

| 설정 | 수준 1 (약한 보안) | 수준 2 (중간) | 수준 3 (강한 보안) |
|------|----------------|------------|----------------|
| 토큰 유효시간 | 2시간 | 30분 | 5분 |
| IP 바인딩 | 비활성 | 비활성 | 활성 |
| Edge 바인딩 | 비활성 | 활성 | 활성 |
| 동시 세션 제한 | 4개 | 2개 | 1개 |
| Referer 검증 | 비활성 | 활성 | 활성 |

수준 1에서 가장 많은 공격이 성공하고, 수준 3에서는 대부분 차단됨.
→ 탐지 모델은 수준 1~2 환경에서 평가해야 의미가 있음
(수준 3이면 토큰 자체가 공격을 막으므로 탐지가 필요 없어짐)
