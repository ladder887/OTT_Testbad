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

과거 collector는 `99_archive/`에서 참고할 수 있지만 그대로 복사하지 않는다.

`--smoke`는 segment 수, pause, duration을 줄여 통신과 결합만 검사한다. smoke manifest의
`timing_scaled=true`인 데이터는 정식 dataset에 섞지 않는다.
