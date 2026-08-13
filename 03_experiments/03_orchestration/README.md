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
