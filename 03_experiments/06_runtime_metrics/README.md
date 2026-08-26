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

20/40/60/80/100 부하 단계의 시계열 sampler와 achieved concurrency 계산은 아직
구현 대상이다. collection matrix의 `planned_client_count`만으로 실제 동시성을 주장하지 않는다.
