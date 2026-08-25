# Kibana 8.11 확인 가이드

Kibana는 Elasticsearch에 저장된 데이터를 조회하고 시각화하는 UI다. Kibana에서
data view나 dashboard를 만들지 않아도 Edge Filebeat, API telemetry, Graph Pipeline은
계속 동작한다.

| 항목 | 데이터 수집에 필요한가 | 용도 |
|---|---|---|
| Elasticsearch index/data stream | 필요 | 실제 원본 로그 저장 |
| Kibana data view | 불필요 | Discover에서 원본 로그를 보기 위한 조회 설정 |
| Kibana dashboard | 불필요 | 운영 상태를 반복해서 볼 때 사용하는 시각화 |

## 1. 저장 데이터 확인

Kibana 설정 전에 브라우저 또는 `ott-control`에서 다음 주소를 확인한다.

```bash
curl -fsS 'http://192.168.0.120:9200/_cat/indices?v&s=index'
curl -fsS 'http://192.168.0.120:9200/_cat/data_stream?v'
```

현재 플랫폼이 사용하는 대상은 두 종류다.

| 이름 | 생성 주체 | 저장 내용 |
|---|---|---|
| `access-gateway-nginx-*` | 네 대의 Edge에 있는 Filebeat | HLS/API 요청, source IP, 응답, cache, 운영 token ID |
| `ott-api-events-*` | Origin의 Access API | 탐색과 playback token 발급 이벤트 |

각 대상의 문서 수는 다음 명령으로 확인한다.

```bash
curl -fsS 'http://192.168.0.120:9200/access-gateway-nginx-*/_count?pretty'
curl -fsS 'http://192.168.0.120:9200/ott-api-events-*/_count?pretty'
```

`count`가 0이면 Web에서 콘텐츠를 탐색하고 한 개를 재생한 뒤 다시 확인한다. 대상이
없다는 404가 나오면 해당 로그가 아직 한 건도 저장되지 않았거나 Filebeat/API와
Elasticsearch 연결에 문제가 있는 것이다.

## 2. Edge 요청 data view 생성

1. `http://192.168.0.120:5601`을 연다.
2. 왼쪽 메뉴에서 **Management → Stack Management**로 이동한다.
3. **Kibana → Data Views**를 선택한다.
4. **Create data view**를 누른다.
5. 다음 값을 입력한다.

| 화면 항목 | 입력값 |
|---|---|
| Name | `Edge Access Logs` |
| Index pattern | `access-gateway-nginx-*` |
| Timestamp field | `@timestamp` |

6. **Save data view to Kibana**를 누른다.

오른쪽에 날짜가 붙은 `access-gateway-nginx-YYYY.MM.DD`가 보이면 pattern이 실제
데이터를 찾은 것이다. 화면에서 `Data stream`으로 표시되어도 정상이다.

## 3. API 이벤트 data view 생성

Data Views 화면에서 다시 **Create data view**를 누르고 다음 값을 입력한다.

| 화면 항목 | 입력값 |
|---|---|
| Name | `OTT API Events` |
| Index pattern | `ott-api-events-*` |
| Timestamp field | `@timestamp` |

**Save data view to Kibana**를 누른다. 두 index를 하나의 `*` pattern으로 합치지 않는다.
필드 구조와 발생 시점이 다르므로 두 data view로 나누는 편이 확인하기 쉽다.

## 4. Discover에서 원본 로그 확인

### 4.1 Edge 요청 로그

1. 왼쪽 메뉴의 **Analytics → Discover**를 연다.
2. 상단 data view에서 `Edge Access Logs`를 선택한다.
3. 오른쪽 위 시간 범위를 `Last 24 hours`로 바꾸고 **Refresh**를 누른다.
4. 왼쪽 필드 목록에서 다음 필드를 표에 추가한다.

```text
client_ip
edge_server
request_method
request_uri
status
bytes_sent
request_time_sec
cache_status
cdn_token_id
token_playback_id
token_owner_account_id
token_owner_device_id
http_user_agent
```

logical client probe만 보려면 KQL 검색창에 다음을 입력한다.

```text
http_user_agent : OTT-TNSM-Probe*
```

특정 logical client의 정식 playback 요청은 inventory에 기록된 source IP로 찾는다.

```text
client_ip : "192.168.0.151"
```

`run_id`, scenario, label, logical client ID, physical host, network profile은
학습 원본 로그에 저장하지 않는다. 해당 값은 별도 run manifest에서
`cdn_token_id`로 결합한다.

오류 응답과 cache hit 확인 예시는 다음과 같다.

```text
status : "4*"
cache_status : "HIT"
```

### 4.2 API 이벤트

1. 상단 data view를 `OTT API Events`로 바꾼다.
2. 시간 범위를 `Last 24 hours`로 둔다.
3. 다음 필드를 표에 추가한다.

```text
event_kind
client_ip
status
cdn_token_id
token_owner_account_id
token_playback_id
token_content_id
token_ttl_sec
```

주요 이벤트를 찾는 KQL 예시는 다음과 같다.

```text
event_kind : "browse_content"
event_kind : "token_issued"
```

## 5. Dashboard는 지금 만들지 않아도 된다

플랫폼 구축과 데이터 수집 확인에는 Discover만으로 충분하다. Dashboard 유무는
Elasticsearch 원본 데이터, Graph Pipeline, Neo4j, 학습 dataset에 영향을 주지 않는다.

정상/공격 scenario 실행기가 완성된 뒤 반복 모니터링이 필요하면 다음 패널만 만든다.

| 패널 | data view | 설정 |
|---|---|---|
| 시간별 요청 수 | `Edge Access Logs` | X=`@timestamp`, Y=Count |
| Edge별 요청 수 | `Edge Access Logs` | Top values=`edge_server.keyword` |
| 상태 코드 분포 | `Edge Access Logs` | Top values=`status` |
| cache 상태 | `Edge Access Logs` | Top values=`cache_status.keyword` |
| source IP별 요청 수 | `Edge Access Logs` | Top values=`client_ip.keyword` |
| API 이벤트 종류 | `OTT API Events` | Top values=`event_kind.keyword` |

dashboard 이름은 `OTT Experiment Monitoring`으로 저장한다. 이 dashboard는 운영 확인용일
뿐 논문 결과 계산에는 사용하지 않는다. 논문 수치는 Elasticsearch 원본을 고정된
script로 추출해 계산해야 한다.

## 6. data view를 잘못 만들어도 원본 데이터는 지워지지 않는다

Kibana에서 data view나 dashboard를 삭제해도 Elasticsearch index/data stream은 삭제되지
않는다. 잘못 만든 data view는 삭제하고 위 두 개를 다시 만들면 된다.
