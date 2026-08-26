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
  --smoke \
  --seed 2026082601 \
  --cache-state cold
```

지원 범위는 `N1~N7`, `A1`, `A2`, `A3`, `A6`, `A7`이다. 실행 후 출력되는
`manifest_path`를 `validate_run_collection.py --manifest`에 그대로 넣는다.

- N6는 2~4개 container가 같은 account로 각자 token을 발급한다.
- A1/A7은 owner가 발급한 token URL을 실제 2~5개 consumer container에 전달한다.
- A3는 한 container 안에서 2~4 worker thread가 실제 동시에 요청한다.
- A6는 네 container와 네 account/token을 순서대로 교대한다.
- A4는 현재 2개 rendition이라 거부한다.
- A5는 주 ViewingSession task가 아니므로 거부한다.

`--cache-state`는 `cold`, `warmup`, `warm`, `mixed`, `unspecified` 중 하나를
manifest에 기록한다. 이 옵션 자체가 Edge cache를 변경하지 않는다. cold run 전에는
`playbooks/09_clear_edge_cache.yml`로 대상 Edge cache를 비우고, warm run 전에는 같은
resource를 요청하는 별도 warm-up run을 수행한다.
