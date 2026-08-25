# OTT Testbed

CDN-backed OTT 서비스에서 token, account, device, IP, content, Edge, `ViewingSession` 관계를 수집하고 분석하기 위한 실증 플랫폼이다.

연구 문서, 학습 작업 폴더, 실험 결과와 과거 자료는 로컬에만 보관하며 Git 배포에서 제외한다.

## 현재 서버 역할

| hostname | 구성 | 주소 | 역할 |
|---|---|---:|---|
| `ott-origin` | Origin/API | `192.168.0.101` | 콘텐츠, account, token, playback API |
| `ott-edge-1~4` | Edge KR/JP/SG/US | `192.168.0.111~114` | token 검증, HLS cache, access log |
| `ott-storage` | Analysis Storage | `192.168.0.120` | Elasticsearch, Kibana, Neo4j |
| `ott-processing` | Graph Processing | `192.168.0.130` | log normalization, sessionization, graph 적재 |
| `ott-user1~10` | Client Pi 01~10 | `192.168.0.131~140` | logical client container host |
| `ott-control` | Management Control | `192.168.0.150` | Ansible/Tailscale 제어 노드 |
| - | logical clients 001~100 | `192.168.0.151~250` | 고유 source IP를 가진 실험 client |

기존 `Detection Server`는 실제 model inference server가 아니므로 **Graph Processing Server**로 이름을 바꿨다. 현재 Graph Pipeline은 로그를 읽어 Neo4j graph를 생성하며, online model inference는 아직 구현되지 않았다.

## 디렉터리

```text
01_platform/       실행 서비스
02_deployment/     서버와 Edge 배포 설정
03_experiments/    TNSM 확장 수집·배치·검증 도구
```

로컬 전용 디렉터리는 `04_training/`, `05_documents/`, `06_outputs/`, `99_archive/`다.
이 디렉터리는 clone한 장비에는 생성되지 않는다.

## 로컬 통합 실행

예제 파일을 복사한 뒤 비어 있는 비밀값을 모두 입력한다. 값이 비어 있으면 Compose가
실행을 거부한다. `.env.lab`은 Git에서 제외된다.

```powershell
Copy-Item .env.lab.example .env.lab
```

```powershell
docker compose --env-file .env.lab -f docker-compose.lab.yml up -d --build
```

```powershell
docker compose --env-file .env.lab -f docker-compose.lab.yml down
```

데이터 volume까지 초기화할 때만 다음을 사용한다.

```powershell
docker compose --env-file .env.lab -f docker-compose.lab.yml down -v
```

| 서비스 | URL |
|---|---|
| 사용자 UI | http://localhost:5173 |
| 운영 콘솔 | http://localhost:5174 |
| API | http://localhost:3001/health |
| Edge Gateway | http://localhost:8081/health |
| Origin | http://localhost:8080/health |
| Kibana | http://localhost:5601 |
| Neo4j Browser | http://localhost:7474 |

## 확장 실험 준비 순서

1. 새 token/log/session schema를 Origin/API, Edge 4대, Graph Processing에 재배포한다.
2. VOD 하나를 재생한 뒤 `validate_telemetry_contract.py`를 통과시킨다.
3. `generate_logical_client_inventory.py`로 배치 파일을 생성하고 한 Raspberry Pi에서 10개
   `ipvlan` logical clients의 고유 source IP를 검증한다.
4. N1/N6/A1/A5 runner를 구현한 뒤 smoke run으로 Elasticsearch와 Neo4j 적재를 대조한다.
5. replay idempotency와 수집 완전성 validator를 구현하고 20/40/60/80/100-client ramp
   test를 통과한 뒤 main collection을 시작한다.

현재 logical client image는 배치와 통신을 확인하는 probe runtime이다. 정상/공격 시청
runner는 아직 구현되지 않았으므로 현재 image만으로 main collection을 시작하지 않는다.

## 원격 장비 관리

실제 VOD/LIVE media는 Origin Raspberry Pi에만 두고 Git 배포에서 제외한다. 모든
장비는 2026-08-07 기준 새 OS로 초기화됐으며, Origin media는 플랫폼 배포 후 다시
업로드한다. 현재 Origin에는 VOD `video_01~video_15`가 있고 LIVE는 아직 없다. 먼저
`live_01~live_03` rolling 검증을 완료하며, main 실험 목표는 controlled LIVE 4개와
실제 입력 검증 channel 1개다. 모든 실험 콘텐츠는 1080p/720p rendition을 사용한다.

고정 IP 이후 설치 명령과 Ansible 파일별 역할은
[원격 설치 도구 README](02_deployment/09_remote-management/README.md)에 있다.
