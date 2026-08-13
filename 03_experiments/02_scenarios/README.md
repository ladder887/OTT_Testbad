# Scenario 구현 상태

정의와 parameter 정본은 다음 문서를 사용한다.

- 정상: `05_documents/01_research/02_정상_시나리오.md`
- 공격: `05_documents/01_research/03_공격_시나리오.md`
- 전체 gate: `05_documents/01_research/01_TNSM_확장_실험_마스터플랜.md`

| 항목 | 상태 | main collection 전 조건 |
|---|---|---|
| N1~N7 runner | 미구현 | 실제 player 흐름과 run manifest 기록 |
| A1 | 미구현 | 2개 이상 container가 동일 VOD token 공유 |
| A2/A3/A4 | 미구현 | 하나의 harvesting family 아래 실행 variant로 구현 |
| A5 | 미구현 | 3~8 token 발급, token당 0~2 segments |
| A6 | 미구현 | 4 containers, 2+ accounts, 2+ tokens, 보완적 segment 범위 |
| A7 | 미구현 | rolling LIVE playlist와 live-edge lag 검증 |
| camouflage matrix | 미구현 | UA/referrer/browse/jitter가 label과 일대일 대응하지 않음 |

과거 collector는 `99_archive/`에서 참고할 수 있지만 그대로 복사하지 않는다.
