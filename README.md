# OTT Testbed (실증랩 통합 실행)

## 빠른 시작

```powershell
docker compose -f docker-compose.lab.yml up -d --build
```

중지:

```powershell
docker compose -f docker-compose.lab.yml down
```

볼륨 포함 초기화:

```powershell
docker compose -f docker-compose.lab.yml down -v
```

## 주요 접속 URL

- 사용자 웹 UI: http://localhost:5173
- 운영 콘솔: http://localhost:5174
- API: http://localhost:3001/health
- Edge Gateway: http://localhost:8081/health
- Origin: http://localhost:8080/health
- Kibana: http://localhost:5601
- Neo4j Browser: http://localhost:7474

## 시드 계정

- admin@ott.com / admin123!
- user1@test.com / testpass123
- user2@test.com / testpass123

## 문서

- 설계 문서: `documents/01~09`
- 현재 구현 기준: `documents/10_실제_구축_현황_및_실행가이드.md`
