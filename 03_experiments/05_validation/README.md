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
- manifest consumer와 Edge `client_ip` 실제 대조
- opaque `X-Device-ID`의 Edge/API/Graph 보존
- manifest token과 Elasticsearch API/HLS event 결합
- manifest token과 Neo4j ViewingSession/IP/Device 결합
- raw Elasticsearch/Neo4j provenance leakage 차단

추가 구현이 필요한 journal gate는 대규모 duplicate/missing event 검사와 최종 split
leakage 검사다. Neo4j replay idempotency는 전용 rebuild/replay playbook으로 검사한다.

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

## Run 단위 검사

scenario가 출력한 manifest 하나를 검사한다.

```bash
python3 03_experiments/05_validation/validate_run_collection.py \
  --manifest 06_outputs/01_run_manifests/RUN_ID.json \
  --wait-sec 120
```

Graph Pipeline 반영을 최대 120초 기다린다. 각 예상 consumer IP에 segment request가
연결되고 `passed: true`가 된 run만 dataset에 넣는다. A1/A7은 실제 HLS source IP와
device가 각각 2개 이상인지 추가로 검사한다.

## 파일럿 묶음 감사

시나리오 구현과 수집 결합을 검사한다.

```bash
python3 03_experiments/05_validation/audit_pilot_collection.py \
  --manifests 06_outputs/01_run_manifests/pilot_YYYYMMDD \
  --mode scenario \
  --output 06_outputs/01_data_quality/pilot_scenario_audit.json
```

P0~P4가 실제 적용된 별도 network pilot은 `--mode network`로 검사한다. `main` mode는
모든 시나리오, 실제 network 적용, 4개 Edge와 최소 5개 host coverage를 함께 요구한다.

## Dataset label-proxy 감사

```bash
python3 03_experiments/05_validation/audit_session_dataset.py \
  --dataset 06_outputs/02_datasets/session_features.csv \
  --output 06_outputs/01_data_quality/session_dataset_audit.json
```

금지 field, 중복 sample/token, class-exclusive Edge/profile/content type, 상수 특징과
단일 특징 ROC-AUC를 보고한다. 높은 단일 특징 AUC는 자동 삭제 사유가 아니라 workload
generator artifact인지 실제 공격 신호인지 확인해야 한다는 경고다.

## Edge cold/warm cache 쌍 검사

대상 Edge cache를 비운 뒤 같은 scenario seed로 cold와 warm run을 순서대로 실행한다.
두 manifest가 같은 HLS resource를 요청했으며 cache 상태가 전부 `MISS`에서 `HIT`로
바뀌었는지 검사한다.

```bash
python3 03_experiments/05_validation/validate_edge_cache_pair.py \
  --cold-manifest 06_outputs/01_run_manifests/COLD_RUN.json \
  --warm-manifest 06_outputs/01_run_manifests/WARM_RUN.json \
  --output 06_outputs/01_data_quality/cache_pair.json
```
