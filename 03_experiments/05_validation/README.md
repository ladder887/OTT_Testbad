# Validation

현재 validator와 regression test는 다음 조건을 확인한다.

- logical clients 100개
- physical hosts 10개, host당 clients 10개
- source IP `.151~.250`의 누락/중복 없음
- account, email, device ID 중복 없음
- 4개 Edge와 P0~P4 profile 분포
- query의 `real_ip`이 Edge에서 관측한 `client_ip`을 덮어쓰지 않음
- API playback event의 CDN token hash, run, content가 보존됨

추가 구현이 필요한 validator:

- Edge log의 실제 `client_ip` 대조
- run manifest의 예상/관측 request 수 대조
- Elasticsearch duplicate/missing event 검사
- Neo4j replay idempotency 검사
- split leakage 검사
