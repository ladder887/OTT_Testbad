# DEPRECATED - deploy/server2

이 폴더는 과거 임시 통합형(Edge 4개 + Detection + Filebeat 단일 호스트) 배포 유산이다.

현재 운영 기준(2026-03-29 이후):

- Edge는 `deploy/edge-kr`, `deploy/edge-jp`, `deploy/edge-sg`, `deploy/edge-us`를 각 Edge Pi에서 1개씩 실행
- Detection은 `deploy/server4/docker-compose.yml`로 별도 서버에서 실행
- 중앙 저장/관측은 `deploy/server3/docker-compose.yml` 사용

실제 배포는 `documents/11_라즈베리_및_서버_분산배포_매뉴얼.md`를 따른다.
