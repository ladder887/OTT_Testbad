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
scenario, label, run, client/host, raw ID는 metadata column에만 남고 모델 feature
allowlist에는 들어가지 않는다.

최소 fitting 경로만 확인하려면 다음을 실행한다.

```bash
python3 -m venv .venv-training
.venv-training/bin/pip install -r \
  03_experiments/04_data_tools/requirements-training-smoke.txt
.venv-training/bin/python \
  03_experiments/04_data_tools/train_session_smoke.py
```

이 결과는 stratified smoke check다. journal 평가에 필요한 host/content/time/account split을
대체하지 않으며 논문 수치로 사용하지 않는다.
