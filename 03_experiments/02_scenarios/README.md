# Scenario 구현 상태

정의와 parameter 정본은 다음 문서를 사용한다.

- 정상: `05_documents/01_research/02_정상_시나리오.md`
- 공격: `05_documents/01_research/03_공격_시나리오.md`
- 전체 gate: `05_documents/01_research/01_TNSM_확장_실험_마스터플랜.md`

| 항목 | 상태 | main collection 전 조건 |
|---|---|---|
| N1~N7 runner | 구현 | 정식 수집 전 각 scenario smoke 검증 |
| A1 | 구현 | 실제 owner 1개와 consumer 2~5개가 동일 VOD token 공유 |
| A2/A3 | 구현 | A2는 2~5 content serial 수집, A3는 실제 worker 병렬 실행 |
| A4 | 실행 차단 | 3 rendition을 구축한 뒤에만 구현/활성화 |
| A5 | 주 실험 제외 | 별도 Account-TimeWindow control-plane task 여부 결정 |
| A6 | 구현 | 4 containers, 4개 실제 accounts/tokens, 보완 segment 범위 |
| A7 | 구현 | 동일 LIVE token을 2~5 consumers가 사용하고 rolling 여부 검증 |
| camouflage matrix | 구현 | UA/referrer/browse 조건을 normal/attack 양쪽에 중첩 배정 |
| 정상 hard negative | 구현 | N1 catalog preview, N6 flash crowd, N7 popular LIVE를 공격 대조군으로 포함 |
| 공격 강도 variant | 구현 | A1/A7 fan-out, A2 속도, A3 병렬도를 `--variant`로 명시 |

과거 collector는 `99_archive/`에서 참고할 수 있지만 그대로 복사하지 않는다.

A1 VOD consumers의 시작 위치는 영상 길이에 고정된 절대 segment 번호가 아니라 재생목록
앞 절반의 상대 위치로 분산한다. 따라서 짧은 VOD에서도 fan-out 의미를 유지하면서
playlist 범위를 벗어나지 않는다.

`--smoke`는 segment 수, pause, duration을 줄여 통신과 결합만 검사한다. smoke manifest의
`timing_scaled=true`인 데이터는 정식 dataset에 섞지 않는다.

## 실행 variant

| scenario | 지정 가능한 variant |
|---|---|
| N1 | `preview`, `standard`, `long`, `catalog_preview` |
| N6 | `household`, `flash_crowd` |
| N7 | `single`, `popular_channel` |
| A1/A7 | `low_fanout`, `high_fanout` |
| A2 | `fast`, `stealth` |
| A3 | `low_parallel`, `high_parallel` |
| A6 | `low_rate` |

`default`는 scenario의 기준 variant를 사용하고, `auto`는 같은 seed에서 항상 같은
variant를 선택한다. 정식 수집에서는 `auto`에 맡기지 않고 collection matrix가 각
variant를 명시한다. 선택 결과는 manifest의 `parameters.scenario_variant`에 저장된다.
