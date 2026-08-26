# TNSM 확장 실험

이 디렉터리는 석사논문 당시 collector를 복원한 공간이 아니다. 100 logical clients 기반 확장 실험을 새 기준으로 구현한다.

| 디렉터리 | 역할 |
|---|---|
| `01_client_runtime/` | logical client container image와 연결 점검 agent |
| `02_scenarios/` | 정상/공격 scenario 구현과 상태 |
| `03_orchestration/` | inventory, 시나리오, split 예약, 혼합 batch 실행 |
| `04_data_tools/` | run manifest, export, provenance |
| `05_validation/` | IP/식별자/로그/graph 정합성 검증 |
| `06_runtime_metrics/` | throughput, lag, CPU, memory, I/O 측정 |

## 시작

```powershell
python 03_experiments/03_orchestration/generate_logical_client_inventory.py
```

```powershell
python 03_experiments/05_validation/validate_inventory.py `
  03_experiments/07_generated/logical_clients.json
```

생성 결과는 `03_experiments/07_generated/`에 저장되며 Git에 포함하지 않는다.

## 현재 구현 상태

- 100 logical clients와 10개 physical hosts의 deterministic inventory 생성: 구현됨
- Pi별 Docker Compose와 `ipvlan` 고정 IP 생성: 구현됨
- inventory 정합성 검사: 구현됨
- logical client image의 설정 출력/Edge health probe: 구현됨
- 로그인, browse, playback 발급, signed HLS VOD/LIVE 소비 agent: 구현됨
- N1~N7 traffic runner: 구현됨
- A1/A2/A3/A6/A7 traffic runner: 구현됨
- A1/A6/A7 cross-container coordinator: 구현됨
- A4: 3개 rendition 구축 전까지 실행 차단
- A5: 주 ViewingSession 분류에서 제외
- run manifest token binding schema/기록 도구: 구현됨
- manifest와 ES/Neo4j request/session 대조 validator: 구현됨
- ViewingSession F0~F4 dataset export, leakage audit와 Logistic/RF smoke: 구현됨
- train/validation/test client·host·content 사전 예약 matrix: schema v2 구현됨
- 정상/공격별 예상 session 기준 content/Edge/profile/host 균형 gate: 구현됨
- 동시 batch logical-client lock과 run별 ES/Neo4j 자동 검증: 구현됨
- 수집 직전 NTP/시각 차이/부하/container/endpoint gate: 구현됨
- 최종 CSV split provenance 누수와 metadata 분포(TV 0.15) 감사: 구현됨

개별 시나리오, hard-negative smoke, 혼합 batch와 20/40/60/80/100-client runtime
ramp는 통과했다. runtime ramp에서는 모든 단계가 목표 동시성, HTTP 무실패,
Elasticsearch 수집 시각 보존, token-graph 반영과 backlog 회복 조건을 만족했다. 첫
비축소 calibration 21개 run도 실행 검증은 통과했지만, export한 50개 session은 content
TV 0.465와 network profile TV 0.369로 학습에서 제외했다. matrix v2로 균형화한
`balanced02` calibration은 5개 batch, 42개 run, 95개 session(정상 45/공격 50)을
생성했고 모든 run/ES/Neo4j 검증과 dataset/split 감사를 통과했다. content/Edge/network
profile/physical host TV는 `0.118/0.047/0.082/0.056`이며 class 전용 범주는 없다.
`segment_duplicate_ratio`와 `status_4xx_count`는 이번 수집에서 상수였다. parameter,
feature 후보와 독립 group/content/future split을 동결한 `main_v1`은 2,550 runs와 계획
5,483 segment-bearing sessions다. 첫 train batch는 20/20 run validation과 계획/실제
60/60 session export를 통과했고 남은 train campaign을 실행 중이다. smoke/pilot metric과
partial train metric은 논문 분류 성능 결과에 사용하지 않는다.
