# Kibana 대시보드 설정 가이드

## 1. 인덱스 패턴 생성

1. http://localhost:5601 접속
2. 왼쪽 메뉴 → **Management** → **Stack Management**
3. **Kibana** → **Data Views** 클릭
4. **Create data view** 버튼 클릭
5. 설정:
   - Name: `Scrubber Nginx Logs`
   - Index pattern: `scrubber-nginx-*`
   - Timestamp field: `@timestamp`
6. **Save data view to Kibana** 클릭

## 2. Discover에서 로그 확인

1. 왼쪽 메뉴 → **Discover** 클릭
2. 상단 데이터 뷰에서 `Scrubber Nginx Logs` 선택
3. 시간 범위를 Last 24 hours로 설정
4. 확인할 필드:
   - `username`: 사용자명
   - `session_token`: 세션 토큰
   - `content_id`: 컨텐츠 ID
   - `client_ip`: 클라이언트 IP
   - `request_uri`: 요청 URI
   - `bytes_sent`: 전송 바이트
   - `status`: 응답 상태

## 3. 유용한 필터

### 비디오 요청만 보기
```
request_uri: *.mp4*
```

### 특정 사용자 로그
```
username: "testuser"
```

### 대용량 전송 (10MB 이상)
```
bytes_sent >= 10485760
```

### 에러 응답
```
status >= 400
```

## 4. 시각화 생성 예시

### 4.1 사용자별 시청 통계
1. **Visualize Library** → **Create visualization**
2. **Pie** 선택
3. 설정:
   - Slice by: `username.keyword`
   - Size by: Count

### 4.2 컨텐츠별 인기도
1. **Bar vertical** 선택
2. 설정:
   - Horizontal axis: `content_id.keyword`
   - Vertical axis: Count

### 4.3 시간대별 트래픽
1. **Area** 선택
2. 설정:
   - Horizontal axis: `@timestamp` (Date Histogram, 1 hour)
   - Vertical axis: Sum of `bytes_sent`

### 4.4 IP별 요청 수
1. **Data table** 선택
2. 설정:
   - Rows: `client_ip.keyword`
   - Metrics: Count

## 5. 대시보드 생성

1. **Dashboard** → **Create dashboard**
2. **Add from library** → 위에서 만든 시각화들 추가
3. 레이아웃 조정
4. **Save** → 이름: `OTT CDN Monitoring`

## 6. 실시간 모니터링

1. Discover 또는 Dashboard에서 우상단 시계 아이콘 클릭
2. **Refresh every** → 10 seconds 선택
3. 실시간으로 로그 업데이트 확인

## 7. 리칭 탐지를 위한 쿼리

### 동일 토큰을 여러 IP에서 사용
Discover에서 검색:
```
session_token: * AND NOT session_token: "-"
```
그 후 `session_token.keyword`로 그룹화하여 집계

### 비정상적으로 많은 요청
```
connection_requests > 100
```

### 외부 Referer
```
http_referer: * AND NOT http_referer: "http://localhost:3000*"
```

## 8. 알람 설정 (선택)

1. **Stack Management** → **Rules and Connectors**
2. **Create rule**
3. 조건 설정:
   - Index: `scrubber-nginx-*`
   - Threshold: bytes_sent > 100MB in 5 minutes
4. 액션: 로그 기록 또는 웹훅 호출
