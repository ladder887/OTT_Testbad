# Data Tools

모든 scenario 실행은 `run_manifest.schema.json` 형식의 manifest를 먼저 만들고 상태를 갱신해야 한다.

manifest는 다음 대조의 기준이다.

1. client가 의도한 logical clients와 parameter
2. Edge/Elasticsearch에서 관측된 request
3. Neo4j에서 생성된 `ViewingSession`과 관계
4. dataset export에 포함된 provenance

`run_id`, `scenario_id`, `logical_client_id`, seed는 학습 입력이 아니다.

## HLS inventory

Origin의 실제 media를 검사한다.

```bash
python 03_experiments/04_data_tools/inventory_hls.py \
  --root 01_platform/01_origin/hls \
  --output 06_outputs/01_data_quality/hls_inventory.json
```

검사 항목:

- content별 `master.m3u8`
- rendition/media playlist
- segment 수와 첫/마지막 파일 존재
- target duration
- media sequence
- `ENDLIST` 여부와 LIVE/VOD 구분

media가 없거나 참조 segment가 빠지면 exit code 1을 반환한다.
