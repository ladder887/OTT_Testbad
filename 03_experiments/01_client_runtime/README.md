# Logical Client Runtime

이 image는 100 logical clients 배치와 source IP 통신을 먼저 검증하기 위한 최소 runtime이다. 정상/공격 traffic은 아직 실행하지 않는다.

## Image build

Raspberry Pi에서:

```bash
docker build -t ott-logical-client:tnsm 03_experiments/01_client_runtime
```

Pi별 생성 Compose 실행:

```bash
docker compose -f 03_experiments/07_generated/pi01/docker-compose.yml up -d
```

설정 확인:

```bash
docker compose -f 03_experiments/07_generated/pi01/docker-compose.yml run --rm lc001 show-config
```

Edge 통신 확인:

```bash
docker compose -f 03_experiments/07_generated/pi01/docker-compose.yml run --rm lc001 probe
```

`configured_source_ip`는 배치 설정값이다. 실제 source IP가 맞는지는 Edge access log의 `client_ip`와 대조해야 한다.
