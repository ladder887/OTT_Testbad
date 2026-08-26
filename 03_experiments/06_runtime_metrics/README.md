# Runtime Metrics

20/40/60/80/100 concurrent client 단계마다 다음을 수집한다.

- Client/Edge: request/s, MB/s, HTTP error rate
- Edge: cache hit ratio, upstream bytes
- Elasticsearch: ingest rate, ingest lag
- Graph Pipeline: events/s, sessionization rate, cursor lag
- Neo4j: transaction latency, upsert rate
- 전체: event timestamp부터 graph 반영까지 p50/p95/p99
- 각 장비: CPU, memory, disk I/O, network I/O
- Graph Pipeline 재시작 후 backlog 소진 시간

model inference가 추가되기 전에는 이 값을 `detection latency`라고 부르지 않는다.

## 수집 직전 gate

다음 명령은 Control, Origin, Edge 4대, Storage, Processing, Client Pi 10대 등 물리 장비
18대에서 시각과 자원 상태를 동시에 읽는다. NTP, controller 대비 시각 차이, CPU당 1분 load, memory, root disk,
실행/unhealthy container 수와 주요 HTTP endpoint를 검사하고 JSON을 남긴다.

```bash
python3 03_experiments/06_runtime_metrics/check_collection_gate.py
```

기본 중단 기준은 시각 차이 1.5초 초과, CPU당 load 1.25 초과, memory 90% 초과,
root disk 여유 10% 미만, NTP 미동기화, 필수 container 부족 또는 endpoint 실패다.
`passed: true`인 report가 없는 batch는 실행하지 않는다.

Origin의 활성 LIVE channel은 2개 rendition을 계속 생성한다. 각 `libx264` encoder는
`ultrafast`, filter thread 1개, encoder thread 1개로 제한한다. 시청 traffic이 없는
상태에서 gate를 실패할 정도로 encoder가 Origin을 포화시키면 응답시간과 cache 결과가
encoder 부하의 대리값이 되므로 수집하지 않는다.

세 channel을 모두 동시에 encoding하면 Pi 5의 4개 CPU가 포화되므로 상시 실행하지 않는다.
API 시작 시 기본 `live_01`만 복구하고, collection matrix가 split에 필요한 channel 하나로
전환한다. `playbooks/04_verify.yml`만 세 channel을 잠시 모두 켜 rolling을 검사한 뒤
`live_01` 하나로 되돌린다.

## 20/40/60/80/100-client ramp

다음 controller는 각 단계의 client를 10대 Pi, P0~P4, 4개 Edge에 균등하게 고른다.
각 client는 30~40개 VOD segment를 정상 재생 간격으로 요청한다. 이는 시스템 용량
측정용 정상 workload이며 학습 dataset에 넣지 않는다.

```bash
python3 03_experiments/06_runtime_metrics/run_concurrency_ramp.py \
  --levels 20,40,60,80,100
```

실행 전에 LIVE encoder를 모두 정지하고 각 단계마다 collection gate를 다시 실행한다.
5초 간격 sample에는 다음 값이 들어간다.

- client Pi에서 실제 실행 중인 `client_agent.py run-spec` process 수
- 18대의 CPU, memory, load, 유선 network, disk I/O, container 수
- Edge request/s, Mbit/s, 4xx, cache hit ratio와 Edge별 request 수
- Elasticsearch event ingest p50/p95/p99 lag
- Graph checkpoint lag, Request/ViewingSession upsert rate, Neo4j query latency
- `Request.timestamp`부터 `Request.graph_ingested_at`까지 p50/p95/p99 lag

기본 통과 조건은 목표의 90% 이상 실제 활성 client가 30초 이상 유지되고, client HTTP
failure가 없으며, Edge 4xx 비율이 1% 이하이고, 모든 token의 segment가 Neo4j에
반영되며, workload 종료 뒤 Graph cursor lag가 2초 이하로 회복되는 것이다. JSONL
시계열과 단계별 요약 JSON을 `06_outputs/03_runtime_metrics/`에 남긴다.

`planned_client_count`는 이 측정을 대신하지 않는다. 본 수집 전에는 smoke가 아닌 위
ramp의 5개 단계가 모두 통과해야 한다.
