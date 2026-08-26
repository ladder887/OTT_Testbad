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

1. `check_collection_gate.py`로 물리 장비 18대의 NTP, 시각 차이, 부하, disk, container와 endpoint를 검사한다.
2. `generate_collection_matrix.py`로 split/client/content/scenario/variant와 예상 session
   기여도를 고정하고 정상/공격별 content/Edge/profile/host 분포를 검사한다.
3. `run_collection_matrix.py`를 `--execute` 없이 실행해 예약 중복과 matrix 구조를 확인한다.
4. calibration batch를 실행하고 각 run의 Elasticsearch/Neo4j 자동 검증을 통과시킨다.
5. 20/40/60/80/100-client ramp에서 실제 동시성과 ingest/graph lag를 측정한다.
6. 수집 데이터를 초기화한 뒤 `run_collection_campaign.py`로 main matrix를 batch별 gate와
   재개 상태를 보존하며 실행하고 dataset/split 감사를 통과시킨다.

N1~N7과 A1/A2/A3/A6/A7 runner, 100개 `ipvlan` client, run validator, cache/graph replay,
F0~F4 exporter와 hard-negative pilot은 구현·검증됐다. 2026-08-27 기준 혼합 batch와
20/40/60/80/100-client runtime ramp도 모두 통과했다. 첫 비축소 calibration의 21개
run은 기술 검증을 통과했지만, 50개 session에서 content와 network profile의 정상/공격
분포 차이가 허용 기준을 넘어 학습 데이터로는 기각했다. 이후 matrix schema v2로 수집한
`balanced02`는 5개 batch, 42개 run, 95개 session(정상 45/공격 50)의 실행·ES·Neo4j
검증과 dataset/split 감사를 모두 통과했다. content/Edge/network profile/physical host
TV는 각각 `0.118/0.047/0.082/0.056`이다. 다음 단계는 이 parameter를 검토·동결하고
독립 group/content/future split을 가진 main collection matrix를 확정하는 것이다.

## 원격 장비 관리

실제 VOD/LIVE media는 Origin Raspberry Pi에만 두고 Git 배포에서 제외한다. 모든
장비는 2026-08-07 기준 새 OS로 초기화됐으며, Origin media는 플랫폼 배포 후 다시
업로드한다. 현재 Origin에는 VOD `video_01~video_15`와 rolling LIVE
`live_01~live_03`이 있고 모두 1080p/720p rendition을 사용한다. LIVE 3개만으로 가능한
`1/1/1` content split은 일반화 근거가 약하므로 논문에서는 controlled limitation으로
기록하고, 강한 LIVE content-holdout 주장이 필요하면 독립 원본 channel을 추가한다.

고정 IP 이후 설치 명령과 Ansible 파일별 역할은
[원격 설치 도구 README](02_deployment/09_remote-management/README.md)에 있다.
