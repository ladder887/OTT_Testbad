# Validation

현재 validator와 regression test는 다음 조건을 확인한다.

- logical clients 100개
- physical hosts 10개, host당 clients 10개
- source IP `.151~.250`의 누락/중복 없음
- account, email, device ID 중복 없음
- 4개 Edge와 P0~P4 profile 분포
- query의 `real_ip`이 Edge에서 관측한 `client_ip`을 덮어쓰지 않음
- token/URL/API telemetry에 run, scenario, label이 포함되지 않음
- `token_jti`와 `cdn_token_id`의 안정적인 연결
- 같은 token을 사용해도 관측 IP가 다른 소비자는 서로 다른 ViewingSession으로 분리
- browse와 playback이 같은 계정·소비자·content 세션으로 결합
- Edge proxy API log와 `ott-api` event의 graph 중복 적재 방지
- VOD 120초, LIVE 45초 idle timeout 구분
- Nginx 초 단위 응답시간을 millisecond로 정규화
- 4xx/5xx HLS 요청도 raw graph에 보존
- run manifest token binding의 중복 방지와 원자적 저장

추가 구현이 필요한 validator:

- Edge log의 실제 `client_ip` 대조
- run manifest의 예상/관측 request 수와 token binding 대조
- Elasticsearch duplicate/missing event 검사
- Neo4j replay idempotency 검사
- split leakage 검사

## 배포 후 telemetry 검사

Origin/API, Edge, Graph Processing을 새 코드로 재배포한 뒤 콘텐츠 하나를 30초 이상
재생하고 다음 명령을 실행한다.

```bash
python3 03_experiments/05_validation/validate_telemetry_contract.py \
  --elasticsearch http://192.168.0.120:9200
```

`errors`가 빈 배열이고 `joined_token_ids`가 1 이상이어야 한다. 최근 30분 안에 이전
버전 로그가 섞여 있으면 금지 field 검사에 실패하므로 pilot 전 test index를 비우고
다시 실행한다.

재생 traffic이 없으면 `no HLS Edge event`와 `no token_issued API event`가 출력되는 것이
정상이다. 이 경우 validator를 통과한 것이 아니므로 반드시 새 코드 배포 후 실제 재생을
만들고 다시 검사한다.
