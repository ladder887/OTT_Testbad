# 정상 시나리오 수집 도구
# 정상 시나리오 수집 도구

> 갱신일: 2026-04-19  
> 목적: Raspberry Pi 1대 = 사용자 1명 기준으로 정상 로그를 수집하고, 한 사용자 안에서 여러 정상 패턴이 섞이도록 만든다.

이 디렉토리는 정상 수집 전용이다. 공격 수집은 같은 구조를 복제해서 별도 디렉토리나 별도 프로필 묶음으로 분리하는 편이 맞다.

---

## 1. 수집 모델

이 프로젝트에서 정상 수집은 다음 원칙으로 운영한다.

1. Pi 1대는 사용자 1명에 대응한다.
2. 같은 사용자는 N1~N7의 여러 정상 패턴을 수행한다.
3. 시나리오 순서는 고정 1개가 아니라, 계정별 수집 계획 안에서 섞이거나 랜덤화할 수 있다.
4. 공격 시나리오는 정상 수집 이후 별도 수집군으로 분리한다.

즉, 지금처럼 `Pi = 시나리오 1개`로 고정하는 방식보다는 `Pi = 사용자 1명, 사용자마다 여러 정상 패턴`이 더 맞다.

---

## 2. 언제 무엇을 돌리나

### 정상 수집

- 목적: 정상 사용자 행위의 베이스라인 확보
- 구성: Pi 10대, 사용자 10명
- 패턴: 각 사용자마다 여러 정상 패턴을 반복 또는 랜덤 실행

### 공격 수집

- 목적: 공격 행위의 별도 데이터 확보
- 구성: 별도 Pi 10대 또는 별도 실행군
- 패턴: 공격 계정/공격 패턴만 사용

### 동시간대 운영

정상과 공격은 같은 시간대에 꼭 돌릴 필요가 없다.

- 운영 단순화 우선: 정상 10대 수집 완료 후 공격 10대 수집
- 실증 동시성 검증 필요: 정상/공격을 동시간대 병행

기본 권장 시작은 순차 수집이다. 즉 정상 데이터를 먼저 충분히 쌓고, 이후 계정/프로필 전환으로 공격 데이터를 쌓는다.

---

## 3. 파일 구성

- [collect_normal_scenarios.py](collect_normal_scenarios.py): 실행 스크립트
- [profiles.json](profiles.json): 계정과 Pi 프로필 정의
- [pyproject.toml](pyproject.toml): uv 환경 설정

스크립트는 표준 라이브러리만 사용하므로 추가 pip 의존성은 없다.

---

## 4. 실행 준비

### 4.1 공통 조건

1. API/Web 서버가 먼저 올라와 있어야 한다.
2. Edge와 Detection이 먼저 올라와 있어야 한다.
3. 각 Pi는 자기 계정으로 로그인해야 한다.
4. 정상 수집용 계정은 `user1` ~ `user10`을 사용한다.

### 4.2 uv 환경

Raspberry Pi에서는 uv 환경으로 실행한다.

```bash
cd ~/OTT_Testbad/scripts/normal_scenario_collection
uv sync
uv run python3 collect_normal_scenarios.py --profile pi-01
```

필요하면 `nohup` 또는 `screen`으로 백그라운드 실행한다.

---

## 5. 계정 정책

정상 수집에는 admin 계정을 쓰지 않는다. admin은 운영/관리용이다.

- `user1` ~ `user10`: 정상 수집 전용
- `admin`: 관리용, 정상 수집 제외

현재 `profiles.json`은 Pi별 사용자 매핑을 정의한다. 한 Pi는 한 사용자에 고정하되, 그 사용자 안에서 여러 정상 패턴을 수행하는 구조로 확장할 수 있다.

---

## 6. 권장 운영 방식

권장하는 운영 방식은 다음과 같다.

1. 정상 수집군 10대는 정상 패턴만 돌린다.
2. 각 사용자별로 N1~N7 중 여러 패턴을 섞는다.
3. 프로필의 run_order를 random으로 두어 시나리오 순서를 랜덤화한다.
4. 정상 수집이 끝나면 공격 전용 계정/프로필로 전환한다.
5. 공격 수집군은 동일 10대를 재사용하거나 별도 10대를 쓸 수 있다.

### 순차 수집 템플릿

1. Phase-A 정상: Pi 10대 전체 실행, label=normal
2. 검증: watch_history, edge log, ES, Neo4j 적재 확인
3. Phase-B 공격: 계정/프로필 교체, label=attack_* 실행
4. 최종 검증: normal과 attack 라벨 분리 확인

이렇게 하면 정상 데이터와 공격 데이터를 시간대상 겹치게 운영할 수도 있고, 분리해서 운영할 수도 있다.

---

## 7. 스크립트 동작 개요

스크립트는 각 세션마다 다음 순서로 동작한다.

1. 로그인
2. browse/search
3. playback/start
4. HLS manifest 및 segment 요청
5. 시나리오별 행동 수행
6. watch-history 저장
7. Edge probe 요청

이때 `label`, `run_id`, `scenario_id`, `dataset_label`이 끝까지 전달되어야 한다.

### 콘텐츠 선택 규칙

현재 기본 설정은 `content_selection: random_from_list`다.

1. 세션 시작 시 `/api/content/list`에서 콘텐츠 목록을 조회한다.
2. `pattern.type`이 `live`면 `live` 타입에서, 그 외에는 `vod` 타입에서 랜덤 선택한다.
3. 같은 타입 후보가 여러 개면 직전 선택 콘텐츠는 우선적으로 피해서 고른다.
4. 후보가 없을 때만 세션의 `content_id`(고정값)로 fallback한다.

세션 단위로 고정 재생으로 되돌리려면 아래처럼 설정한다.

- `content_selection: fixed`
- `content_id: movie_001`

랜덤 후보를 제한하려면 아래 키를 세션에 넣는다.

- `content_pool: ["movie_001", "movie_003"]`
- `exclude_content_ids: ["movie_004"]`

### 시간 랜덤화 규칙

현재 스크립트는 "고정값"과 "범위 랜덤값"을 둘 다 지원한다.

1. 고정키만 있으면 고정 시간으로 동작한다.
2. `*_min_*`, `*_max_*` 키가 있으면 그 범위에서 랜덤으로 뽑는다.

예시:

- 고정: `pause_sec: 7200`
- 랜덤: `pause_min_sec: 600`, `pause_max_sec: 10800`

지원 키 예시:

- 시청 시간: `watch_duration_sec` 또는 `watch_duration_min_sec`/`watch_duration_max_sec`
- 대기 시간: `pause_sec` 또는 `pause_min_sec`/`pause_max_sec`
- seek 대기: `seek_pause_sec` 또는 `seek_pause_min_sec`/`seek_pause_max_sec`
- 라이브 polling: `poll_interval_sec` 또는 `poll_interval_min_sec`/`poll_interval_max_sec`
- ABR 전환: `switch_delay_sec` 또는 `switch_delay_min_sec`/`switch_delay_max_sec`

---

## 8. 주의 사항

1. 정상 수집과 공격 수집은 같은 라벨로 섞지 않는 편이 좋다.
2. 한 사용자에게 항상 같은 시나리오만 주면 데이터가 편향된다.
3. 랜덤화를 쓸 때도 최소 실행 횟수와 패턴 분포는 기록해야 한다.
4. 나중에 공격 수집을 붙일 때는 별도 `profiles.attack.json` 같은 분리를 추천한다.

---

## 9. 결론

현재 단계에서는 정상 수집이 우선이다. 따라서 지금은 Pi 10대로 정상 로그가 잘 쌓이는지 확인하고, 그 다음에 공격 수집군을 별도로 추가하는 방식이 가장 안전하다.

원하면 다음 단계로는 이 디렉토리를 `normal` / `attack` 두 세트로 나눠서, 프로필과 실행 스크립트를 각각 분리해 줄 수 있다.

- Edge는 살아 있지만 filebeat가 내려가 있거나 offset이 꼬였을 수 있다.
- [documents/15_데이터_초기화_및_수집_운영_가이드.md](../../documents/15_데이터_초기화_및_수집_운영_가이드.md) 기준으로 filebeat 상태를 점검한다.

### 10.3 watch-history가 안 쌓인다

- access token이 만료됐거나, /api/user/watch-history 호출이 실패했을 수 있다.
- 스크립트 마지막에 출력되는 에러를 확인한다.

### 10.4 같은 Pi에서 너무 많은 시도를 했다

- login limiter가 걸릴 수 있다.
- 프로필을 반복 실행하기 전에 다른 계정 또는 다른 Pi로 분산한다.

---

## 11. 새 시나리오를 추가하는 방법

새로운 정상 시나리오를 추가하려면 다음 순서로 한다.

1. `profiles.json`에서 새 Pi 프로필 또는 새 run을 추가한다.
2. `pattern.type`을 `sequential`, `seek`, `abr`, `pause_resume`, `live`, `mixed` 중 하나로 고른다.
3. `browse_queries`, `history`, `repeat`를 조정한다.
4. 새 시나리오가 어떤 로그 패턴을 만드는지 README에 한 줄 적는다.
5. 실행 후 Kibana와 Neo4j에서 라벨이 분리되는지 확인한다.

---

## 12. 운영 팁

1. 정상 데이터는 한 번에 많이 넣는 것보다, 시나리오별로 나눠서 넣는 편이 좋다.
2. 같은 content_id만 계속 쓰면 feature가 너무 단조로워진다.
3. N1, N2, N3, N4, N5, N6, N7을 최소 한 번씩은 확보하는 편이 좋다.
4. live 데이터는 VOD보다 훨씬 덜 안정적이므로 별도 세션으로 모으는 게 좋다.
5. 수집 전에 [documents/15_데이터_초기화_및_수집_운영_가이드.md](../../documents/15_데이터_초기화_및_수집_운영_가이드.md)로 초기 상태를 맞추면 이후 라벨 분리 검증이 쉽다.

---

## 13. 추천 실행 순서

1. 서버3/서버1/서버4/Edge를 모두 기동한다.
2. 필요하면 데이터 초기화를 수행한다.
3. Pi-01, Pi-02, Pi-03 순서로 VOD baseline을 먼저 채운다.
4. Pi-04, Pi-05로 pause/resume 계열을 채운다.
5. Pi-06으로 동시 시청 정상 패턴을 채운다.
6. Pi-07, Pi-10으로 live 패턴을 채운다.
7. Pi-08, Pi-09로 추가 반복 샘플을 채운다.
8. Kibana, Neo4j, PostgreSQL에서 라벨과 시나리오가 분리되는지 확인한다.

이 순서를 따르면 정상 데이터가 먼저 안정적으로 쌓이고, 이후 공격 시나리오 수집 때 기준선이 명확해진다.
