# Logical Client Orchestration

`generate_logical_client_inventory.py`는 10대의 Client Raspberry Pi에 배치할
100개 logical client의 정본 inventory와 Pi별 Compose를 생성한다.

## 생성 파일

- `03_experiments/07_generated/logical_clients.csv`
- `03_experiments/07_generated/logical_clients.json`
- `03_experiments/07_generated/pi01/docker-compose.yml`
- `03_experiments/07_generated/pi02/docker-compose.yml`
- `...`
- `03_experiments/07_generated/pi10/docker-compose.yml`

생성 결과는 Git에 저장하지 않는다. 같은 generator와 revision으로 각 Pi에서 다시
만든다.

## 고정 배치

- Client Pi management IP: `192.168.0.131~140`
- logical client source IP: `192.168.0.151~250`
- Pi 한 대당 logical client container: 10개
- Edge: `edge-kr`, `edge-jp`, `edge-sg`, `edge-us`에 round-robin 배정
- network profile: `P0~P4`에 round-robin 배정

## 생성 및 검증

```bash
python3 03_experiments/03_orchestration/generate_logical_client_inventory.py
python3 03_experiments/05_validation/validate_inventory.py \
  03_experiments/07_generated/logical_clients.json
```

Pi의 실험용 물리 interface가 `eth0`이 아니면 Compose 실행 전에 지정한다.

```bash
export CLIENT_PARENT_INTERFACE=enp1s0
```

`SOURCE_IP` 환경변수는 설정값일 뿐이다. 실제 source IP 분리는 `ipvlan` network가
담당하며, 성공 여부는 각 Edge access log의 `client_ip`로 검증한다.

## Scenario 실행

`ott-control`에서 실행한다. runner는 inventory를 매번 다시 생성해 generator와 client
배포의 ID/IP/device 값이 어긋나지 않게 한다.

```bash
cd ~/OTT_Testbad
python3 03_experiments/03_orchestration/run_scenario.py \
  --scenario N1 \
  --variant catalog_preview \
  --smoke \
  --seed 2026082601 \
  --cache-state cold
```

지원 범위는 `N1~N7`, `A1`, `A2`, `A3`, `A6`, `A7`이다. 실행 후 출력되는
`manifest_path`를 `validate_run_collection.py --manifest`에 그대로 넣는다.

- N6는 2~4개 container가 같은 account로 각자 token을 발급한다.
- N6 `flash_crowd`는 2~5개 독립 account/token이 같은 VOD를 정상 속도로 동시에 본다.
- N7 `popular_channel`은 2~5개 독립 account/token이 같은 LIVE channel을 동시에 본다.
- A1/A7은 owner가 발급한 token URL을 실제 2~5개 consumer container에 전달한다.
- A3는 한 container 안에서 2~4 worker thread가 실제 동시에 요청한다.
- A6는 네 container와 네 account/token을 순서대로 교대한다.
- A4는 현재 2개 rendition이라 거부한다.
- A5는 주 ViewingSession task가 아니므로 거부한다.

`--variant`를 생략하면 기준 variant를 사용한다. `--variant auto`는 seed 기반으로
재현 가능하게 선택하지만, 정식 수집에서는 variant별 표본 수가 달라지지 않도록 값을
직접 지정한다. 전체 목록은 `03_experiments/02_scenarios/README.md`에 있다.

`--cache-state`는 `cold`, `warmup`, `warm`, `mixed`, `unspecified` 중 하나를
manifest에 기록한다. 이 옵션 자체가 Edge cache를 변경하지 않는다. cold run 전에는
`playbooks/09_clear_edge_cache.yml`로 대상 Edge cache를 비우고, warm run 전에는 같은
resource를 요청하는 별도 warm-up run을 수행한다.

## 혼합 수집 matrix

본 수집은 `run_scenario.py`를 여러 terminal에서 직접 실행하지 않는다. 다음 도구가
split별 client/content를 먼저 고정하고, 한 batch 안에서 정상과 공격 run을 섞는다.

```bash
python3 03_experiments/03_orchestration/generate_collection_matrix.py \
  --phase calibration \
  --splits train \
  --repetitions 1 \
  --target-clients 20 \
  --smoke \
  --dataset-prefix tnsm_100lc_20260826_mixed_smoke
```

이 명령은 traffic을 만들지 않는다. 생성된 JSON에는 run마다 scenario/variant/seed,
허용 content, 예약 logical clients, 시작 offset이 들어간다. 기본 split은 다음과 같다.

| split | physical hosts | VOD | LIVE |
|---|---|---|---|
| train | `pi01~pi06` | `video_01~09` | `live_01` |
| validation | `pi07~pi08` | `video_10~12` | `live_02` |
| test | `pi09~pi10` | `video_13~15` | `live_03` |

현재 LIVE 3개로는 `1/1/1`만 가능하므로 LIVE content 일반화의 근거는 제한적이다.
공격의 낮은 강도 variant는 main matrix의 test에만 예약한다. calibration matrix에는
parameter 점검을 위해 낮은 강도와 높은 강도를 모두 넣되 그 데이터는 학습에 쓰지 않는다.

생성 후 먼저 구조만 검사한다. `--execute`가 없으므로 traffic은 발생하지 않는다.

```bash
python3 03_experiments/03_orchestration/run_collection_matrix.py \
  --matrix 06_outputs/00_collection_plans/MATRIX.json
```

선택한 batch를 실제 실행할 때만 `--execute`를 붙인다.

```bash
python3 03_experiments/03_orchestration/run_collection_matrix.py \
  --matrix 06_outputs/00_collection_plans/MATRIX.json \
  --batch-id train_b001 \
  --execute
```

runner는 각 logical client에 lock을 만들고 중복 사용을 거부한다. 각 scenario가 끝나면
`validate_run_collection.py`를 자동 실행하며, 하나라도 실패하면 execution report의
`passed`가 false가 된다. `planned_client_count`는 예약 수다. 실제 동시 활성 수는
runtime metric으로 별도 측정해야 한다. 실제 실행에는 최근 15분 이내 생성된
`passed: true` collection gate report가 필요하며, 기본적으로 가장 최근 report를 찾는다.
각 execution report는 batch ID와 실행 시각을 파일명에 넣어 이전 결과를 덮어쓰지 않는다.

matrix runner는 batch의 split에 맞춰 Origin에서 `live_01`, `live_02`, `live_03` 중 필요한
channel 하나만 활성화한다. 시작한 channel의 1080p와 720p playlist가 실제로 진행되는지
확인한 뒤 traffic을 시작한다. LIVE run이 없는 batch는 encoder를 모두 정지한다. 이를
수동으로 우회하는 `--skip-live-management`는 장애 조사 외에는 사용하지 않는다.
