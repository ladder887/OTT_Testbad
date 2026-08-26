# Data Tools

모든 scenario 실행은 `run_manifest.schema.json` 형식의 manifest를 먼저 만들고 상태를 갱신해야 한다.

manifest는 다음 대조의 기준이다.

1. client가 의도한 logical clients와 parameter
2. Edge/Elasticsearch에서 관측된 request
3. Neo4j에서 생성된 `ViewingSession`과 관계
4. dataset export에 포함된 provenance

`run_id`, `scenario_id`, `logical_client_id`, physical host, network profile,
label, seed는 서비스 토큰·URL·Edge 로그·raw graph에 기록하지 않는다.

`POST /api/playback/start` 응답의 `token_binding`을 manifest의
`token_bindings`에 추가한다. 이후 dataset builder가 `cdn_token_id`로 raw
graph와 manifest를 결합한다. 이 결합은 split 생성과 정답 부여 단계에서만
수행하며 `run_id`, scenario, label은 모델 입력 특징으로 사용하지 않는다.

A1/A7처럼 하나의 토큰을 여러 client가 소비하는 경우에는
`owner_logical_client_id`는 발급 client 하나를 기록하고,
`consumer_logical_client_ids`에 실제 요청을 보낼 모든 client를 기록한다.

실행기가 playback 응답을 받은 직후 다음 도구로 binding을 원자적으로
추가할 수 있다.

```bash
python 03_experiments/04_data_tools/record_token_binding.py \
  --manifest RUN_MANIFEST.json \
  --token-jti TOKEN_JTI \
  --cdn-token-id CDN_TOKEN_ID \
  --playback-id PLAYBACK_ID \
  --content-id video_01 \
  --owner lc001 \
  --consumer lc001 \
  --issued-at 2026-08-19T00:00:00Z
```

token relay는 `--consumer lc002 --consumer lc003`처럼 옵션을 반복한다.
`cdn_token_id`가 `token_jti`에서 계산한 값과 다르거나 run에 없는 client를 지정하면
기록을 거부한다. manifest 파일은 중앙 orchestrator 하나만 순차적으로 갱신한다.
여러 client container가 같은 manifest를 동시에 직접 수정하지 않는다.

## HLS inventory

Origin의 실제 media를 검사한다.

```bash
python 03_experiments/04_data_tools/inventory_hls.py \
  --root 01_platform/01_origin/hls \
  --output 06_outputs/01_data_quality/hls_inventory.json
```

검사 항목:

- content별 `master.m3u8`
- rendition/media playlist
- segment 수와 첫/마지막 파일 존재
- target duration
- media sequence
- `ENDLIST` 여부와 LIVE/VOD 구분

media가 없거나 참조 segment가 빠지면 exit code 1을 반환한다.

## ViewingSession dataset export

완료된 run manifest의 `cdn_token_id`와 Neo4j `CdnToken`을 결합한다. HLS segment가 하나도
없는 token owner용 control-plane session은 주 ViewingSession dataset에서 제외한다.

```bash
python3 03_experiments/04_data_tools/export_session_dataset.py \
  --manifests 06_outputs/01_run_manifests
```

결과는 `06_outputs/02_datasets/session_features.csv`와 SHA-256/schema metadata다.
scenario, variant, label, run, matrix/split, client/host, account/device/content/Edge/network
profile 같은 provenance는 metadata column에만 남고 모델 feature allowlist에는 들어가지
않는다.

현재 exporter는 다음 feature group을 구분해 metadata에 기록한다.

- `F0_F1`: 요청 수, segment timing, token fan-out의 기본 특징
- `F2_relation`: 최근 10분 account/content session, device, IP, token 관계 특징
- `F3_behavior`: timing 분산, burst/concurrency, segment 순서·중복, cache/응답시간 특징
- `F4_lifecycle`: token 발급 이후 사용 시간과 남은 TTL

10분 특징은 export 대상 manifest에 결합된 session cohort 안에서 각 session 종료 시점까지의
trailing window로 계산한다. 최종 dataset은 manifest 집합과 함께 버전과 hash를 고정한다.

최소 fitting 경로만 확인하려면 다음을 실행한다.

```bash
python3 -m venv .venv-training
.venv-training/bin/pip install -r \
  03_experiments/04_data_tools/requirements-training-smoke.txt
.venv-training/bin/python \
  03_experiments/04_data_tools/train_session_smoke.py \
  --feature-set f0-f1
```

`--feature-set all`은 exporter metadata에 선언된 F0~F4 전체 열의 숫자 변환과 fitting만
검사한다. 어느 경우도 journal metric이 아니며, main 평가는 group/time/content/host split
training pipeline에서 별도로 수행한다.

이 결과는 stratified smoke check다. journal 평가에 필요한 host/content/time/account split을
대체하지 않으며 논문 수치로 사용하지 않는다.

main dataset을 export한 뒤에는 예약 split이 실제 CSV에서도 유지됐는지 검사한다.

```bash
python3 03_experiments/05_validation/audit_dataset_splits.py \
  --dataset 06_outputs/02_datasets/session_features.csv \
  --mode main
```

같은 run/token/account/device/client IP/physical host/content가 split을 넘거나, smoke row가
main에 섞이거나, 지정 host/content/공격 variant 정책을 위반하면 실패한다. test가
train/validation보다 완전히 늦게 수집되지 않았으면 future-time 성립 실패를 경고한다.
