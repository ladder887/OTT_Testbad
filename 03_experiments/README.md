# TNSM 확장 실험

이 디렉터리는 석사논문 당시 collector를 복원한 공간이 아니다. 100 logical clients 기반 확장 실험을 새 기준으로 구현한다.

| 디렉터리 | 역할 |
|---|---|
| `01_client_runtime/` | logical client container image와 연결 점검 agent |
| `02_scenarios/` | 정상/공격 scenario 구현과 상태 |
| `03_orchestration/` | 100 clients inventory와 Pi별 Compose 생성 |
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
- N1~N7/A1~A7 traffic runner: 재구현 필요
- A1/A6/A7 cross-container coordinator: 미구현
- run manifest와 ES/Neo4j 대조 validator: schema만 정의, 실행 코드 미구현

미구현 항목을 완료하기 전에는 main collection을 시작하지 않는다.
