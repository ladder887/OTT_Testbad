# Kibana Dashboard Configuration

이 디렉토리에는 Kibana 대시보드 설정 파일을 저장합니다.

## 수동 대시보드 생성 단계

1. **Kibana 접속**
   - http://localhost:5601

2. **인덱스 패턴 생성**
   - Management → Stack Management → Index Patterns
   - `access-gateway-nginx-*` 패턴 생성 (timestamp 필드 선택)
   - `origin-nginx-*` 패턴 생성 (timestamp 필드 선택)

3. **대시보드 생성**
   - Analytics → Dashboard → Create dashboard

## 추천 시각화 패널

### 1. 전체 요청 수 (타임라인)
- Type: Line chart
- Y-axis: Count
- X-axis: timestamp
- Split series: service

### 2. 상태 코드 분포
- Type: Pie chart
- Slice by: status.keyword
- Size: Count

### 3. Top IP 주소
- Type: Data table
- Rows: remote_addr.keyword
- Metrics: Count
- Top 10

### 4. Top Referer
- Type: Data table
- Rows: http_referer.keyword
- Metrics: Count
- Top 10

### 5. 차단된 요청 (403)
- Type: Metric
- Filter: status is 403
- Metrics: Count

### 6. 평균 응답 시간
- Type: Metric
- Metrics: Average of request_time

### 7. Top User-Agent
- Type: Tag cloud
- Tags: http_user_agent.keyword
- Size: Count

### 8. 세션 토큰별 요청 패턴
- Type: Data table
- Rows: http_x_session_token.keyword
- Metrics: Count, Unique count of remote_addr
- Filter: http_x_session_token exists

## 대시보드 JSON Export

대시보드 생성 후:
1. Dashboard → Export
2. JSON 파일을 이 디렉토리에 저장
3. 다른 환경에서 Import로 재사용 가능

## 샘플 쿼리

### 의심스러운 활동 탐지

```json
{
  "query": {
    "bool": {
      "should": [
        {
          "term": { "status": 403 }
        },
        {
          "range": {
            "body_bytes_sent": { "gte": 10000000 }
          }
        },
        {
          "regexp": {
            "http_referer.keyword": ".*illegal.*|.*free.*|.*crack.*"
          }
        }
      ],
      "minimum_should_match": 1
    }
  }
}
```

### 동일 토큰의 다중 IP 접속

```json
{
  "size": 0,
  "aggs": {
    "tokens": {
      "terms": {
        "field": "http_x_session_token.keyword",
        "size": 100
      },
      "aggs": {
        "unique_ips": {
          "cardinality": {
            "field": "remote_addr.keyword"
          }
        },
        "ip_list": {
          "terms": {
            "field": "remote_addr.keyword",
            "size": 10
          }
        }
      }
    }
  }
}
```
