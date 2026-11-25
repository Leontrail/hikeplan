# 설산 추천 리포트

기상청 API허브의 **적설 관측(500m 격자)**과 **산악예보(정상 기준)**를 받아
설산 24곳의 산행 추천 순위를 만들고, GitHub Pages 웹페이지로 보여줍니다.

👉 결과 보기: https://leontrail.github.io/hikeplan/

## 실행 시각
- 매일 **05:17, 08:17, 11:17, 14:17, 17:17, 20:17, 23:17 (KST)**
- 기상청 예보 발표(02·05·08·11·14·17·20·23시) 후 17분: 발표분이 API에 올라오는 데 10분쯤 걸려서, 07분에 돌리면 3시간 전 발표분을 쓰게 됨
- 실행은 **cron-job.org**가 GitHub API로 깨움 (GitHub 자체 예약은 자주 늦거나 빠져서 사용 안 함)
- 수동 실행: Actions → 설산 추천 리포트 → Run workflow

## 순위 기준
- **목표일**: 토요일 (토요일 예보가 아직 없으면 예보가 있는 마지막 날)
- **점수** = 목표일 09시 예상 적설
  − 녹을 위험(영상 시간·비, 쌓인 눈 이하로 제한)
  + 0.3 × 최근 7일 신적설
  − 1.5 × 산행 중(08~15시) 눈 오는 시간
  − 0.05 × 산행 시간 강수확률(%)
- 즉, **전날까지 눈이 충분히 쌓이고 산행 당일엔 눈·비가 안 오는 산**이 위로 올라옵니다.

## 웹페이지 카드에 표시되는 것
- 현재 적설, 7일 신적설, 목표일 09시 예상 적설
- 산행 기온, 강수확률, 산행 중 눈 여부, 녹을 위험(영상 시간·비)
- 🥾 겨울 추천 등산 코스
- 기상청 산악예보 링크 (지점이 있는 산), 곰배령은 숲나들e 예약 링크
- 상단: 예보 발표 시각, 적설 분석 시각, 페이지 생성 시각, 다음 업데이트 예정 시각

## 파일
- `snow_report.py` : 조회·점수 계산·웹페이지 생성 스크립트
- `top20_snow_mountains.csv` : 대상 산 목록 24곳 (산악예보 지점번호, 좌표, 추천 코스, 추가 링크)
- `.github/workflows/snow.yml` : 실행 설정 (수동/외부 호출 실행)

## 설정값
- Secrets: `KMA_AUTH_KEY` (기상청 API허브 인증키, 필수)
- Pages: Source = GitHub Actions
- cron-job.org: `POST https://api.github.com/repos/leontrail/hikeplan/actions/workflows/snow.yml/dispatches`
  - 헤더: `Authorization: Bearer <토큰>` 외 Accept, X-GitHub-Api-Version, Content-Type
  - 본문: `{"ref":"main"}`
  - 토큰: Fine-grained, snow-report 저장소만, Actions 읽기·쓰기 — **2027-05-30 만료, 그 전에 재발급**

## 실행 시각을 바꿀 때
1. cron-job.org에서 작업 시각 수정
2. `snow_report.py`의 `--schedule` 기본값(웹페이지 "다음 업데이트" 표시용)도 같은 시각으로 수정
