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

현재 자동 수집 도구는 미구현이다. model inference가 추가되기 전에는 이 값을 `detection latency`라고 부르지 않는다.
