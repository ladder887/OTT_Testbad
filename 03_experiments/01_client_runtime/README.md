# Logical Client Runtime

이 image는 100 logical clients의 source IP를 유지한 채 실제 OTT 요청을 실행한다.

- 계정 로그인과 cookie/session 유지
- 선택적인 content browse
- playback token과 signed master playlist 발급
- VOD master/media playlist 해석과 segment 요청
- seek, rendition 전환, pause, 병렬 요청
- rolling LIVE playlist polling과 live-edge 추적
- 공유 token 소비

HTTP에는 scenario, run, label, logical/physical client ID를 보내지 않는다. 각 요청에는
opaque `X-Device-ID`만 보내며 run provenance는 control node의 manifest에만 저장한다.

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
docker compose -f 03_experiments/07_generated/pi01/docker-compose.yml exec -T lc001 \
  python /app/client_agent.py show-config
```

Edge 통신 확인:

```bash
docker compose -f 03_experiments/07_generated/pi01/docker-compose.yml exec -T lc001 \
  python /app/client_agent.py probe
```

`configured_source_ip`는 배치 설정값이다. 실제 source IP가 맞는지는 Edge access log의 `client_ip`와 대조해야 한다.

`run-spec`은 사람이 직접 작성하는 명령이 아니다. `run_scenario.py`가 label 없는 실행
spec을 base64url로 전달한다. token relay도 coordinator가 owner와 consumer를 나누어 실행한다.
