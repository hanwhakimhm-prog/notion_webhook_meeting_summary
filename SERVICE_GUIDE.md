# notion_webhook_meeting_summary — 서비스 가이드

> 마지막 업데이트: 2026-04-08

---

## 한 줄 요약

노션 회의록 페이지에서 버튼 클릭 → STT(받아쓰기) 텍스트 자동 추출 → Claude AI 요약 → 노션 페이지에 요약 삽입 + 참석자에게 이메일 발송

---

## 목차

1. [전체 아키텍처](#전체-아키텍처)
2. [처리 플로우](#처리-플로우)
3. [파일 구조](#파일-구조)
4. [각 파일 역할 상세](#각-파일-역할-상세)
5. [멀티 워크스페이스 구조](#멀티-워크스페이스-구조)
6. [환경변수](#환경변수)
7. [외부 서비스 연동](#외부-서비스-연동)
8. [배포 서버 (Railway)](#배포-서버-railway)
9. [Notion 설정 방법](#notion-설정-방법)
10. [주요 로직 메모](#주요-로직-메모)
11. [트러블슈팅 기록](#트러블슈팅-기록)

---

## 전체 아키텍처

```
[Notion 회의록 페이지]
        │
        │ 버튼 클릭 (자동화)
        ▼
[Railway 서버: FastAPI]  ←── POST /webhook/meeting-summary
        │
        ├─ 1. 웹훅 시크릿 인증
        ├─ 2. 워크스페이스 자동 감지 (멀티 워크스페이스 지원)
        ├─ 3. Notion API → 페이지 메타 + STT 텍스트 추출
        ├─ 4. Anthropic API (Claude) → 회의 요약 생성
        ├─ 5. Notion API → 요약 결과를 페이지 하단에 삽입
        └─ 6. Brevo API → 참석자 이메일 발송
```

---

## 처리 플로우

```
Notion 버튼 클릭
    │
    ▼
POST /webhook/meeting-summary
    │
    ├── [인증] X-Webhook-Secret 헤더 or payload.secret 검증
    │         → 불일치: 401 반환
    │
    ├── [page_id 추출] payload.data.id or payload.page_id
    │         → 없으면: 400 반환
    │
    ├── [워크스페이스 감지] 등록된 클라이언트 순서대로 pages.retrieve() 시도
    │         → 전부 실패: 500 반환
    │         → 성공한 클라이언트 사용
    │
    ├── [메타 추출] 회의명, 날짜, URL 파싱
    │
    └── [202 Accepted 즉시 반환] ← 노션에는 여기서 응답
              │
              ▼ (Background Task 비동기)
    ┌─────────────────────────────────┐
    │  STT 텍스트 추출                 │
    │  (우선순위)                      │
    │  1순위: transcription 블록      │
    │  2순위: '받아쓰기' 섹션 이하     │
    │  3순위: 페이지 전체 텍스트       │
    │                                  │
    │  Claude API 요약 생성            │
    │  → [SECTION:key] 태그 파싱      │
    │                                  │
    │  참석자 이메일 조회              │
    │  (연락처 DB 매칭)                │
    │  → 없으면 RECIPIENT_EMAILS 사용  │
    │                                  │
    │  Notion 페이지에 요약 삽입       │
    │  (기존 요약 있으면 먼저 삭제)    │
    │                                  │
    │  Brevo API 이메일 발송           │
    └─────────────────────────────────┘
```

---

## 파일 구조

```
notion_webhook_meeting_summary/
├── main.py              # FastAPI 서버, 웹훅 엔드포인트, 워크스페이스 감지
├── summarizer.py        # Claude API 호출 및 요약 파싱
├── notion_writer.py     # 노션 페이지에 요약 블록 삽입
├── notion_contacts.py   # 연락처 DB 조회 → 참석자 이메일 매핑
├── mailer.py            # Brevo API로 HTML 이메일 발송
├── models.py            # Pydantic 웹훅 payload 모델
├── requirements.txt     # 의존성 패키지
├── .env                 # 실제 환경변수 (gitignore)
├── .env.example         # 환경변수 템플릿
└── SERVICE_GUIDE.md     # 이 파일
```

---

## 각 파일 역할 상세

### main.py

- FastAPI 앱 초기화 (`/docs`, `/redoc`, `/openapi.json` 비활성화)
- 시작 시 `_build_workspace_clients()` 실행 → 환경변수로 워크스페이스 클라이언트 맵 구성
- **엔드포인트**
  - `POST /webhook/meeting-summary` — 핵심 웹훅
  - `GET /health` — 헬스체크
- **워크스페이스 자동 감지**: 등록된 Notion 클라이언트를 순서대로 시도해 page_id 조회 성공한 클라이언트를 사용 (Notion 자동화 payload에 workspace_id가 없기 때문)
- 백그라운드 처리로 202 즉시 응답 → 노션 버튼 타임아웃 방지

### summarizer.py

- `summarize_with_claude(meeting_title, memo, ...)` — Claude `claude-sonnet-4-6` 모델 호출
- 입력: memo(STT), transcript(선택), attached_text(선택), agenda_text(선택)
- 출력 포맷: `[SECTION:개요]`, `[SECTION:내용]`, `[SECTION:결정]`, `[SECTION:액션]`, `[SECTION:한줄요약]`
- `parse_summary(text)` — 정규식으로 SECTION 태그 분리 → `(one_liner, sections_dict)` 반환

### notion_writer.py

- `append_summary_to_page(notion, page_id, ...)` — 페이지 하단에 요약 블록 추가
- 삽입 전 `_delete_existing_summary()` 실행 → `🤖 AI 요약 |` 마커로 기존 요약 찾아 삭제 (중복 방지)
- 삽입 구조:
  ```
  [divider]
  [callout] 🤖 AI 요약 | 날짜 / 한줄요약
  [heading_2] 📌 회의 개요
  [table] 2열
  [heading_2] 🗣️ 회의 내용
  [table] 2열 (헤더 있음)
  [heading_2] ✅ 결정 사항
  [table] 2열 (헤더 있음)
  [heading_2] 📋 액션 아이템
  [table] 3열 (담당자|할 일|기한)
  [divider]
  ```
- Notion API 100블록 제한 → 90개씩 분할 append

### notion_contacts.py

- `get_attendee_emails(notion, page)` — 회의록 `참석자` 컬럼(people 타입) → Notion user ID 추출
- 연락처 DB (`CONTACTS_DB_ID = 4e634a54f4814299a5d5c7d2e37653e8`) 전체 조회
- DB 컬럼: 이름(title), 노션 계정(people), 이메일(email)
- user ID → email 매핑 반환 / 없으면 빈 리스트 → `RECIPIENT_EMAILS` fallback

### mailer.py

- `send_email_notification(to_email, meeting_title, ...)` — Brevo SMTP API REST 호출
- HTML 이메일: 회의명, 날짜, 한줄요약(노란 박스), 각 섹션 테이블, "Notion에서 보기" 버튼
- 수신자 여러 명: 콤마 또는 줄바꿈 구분 자동 파싱
- 발신자명: `MeetingOS`

### models.py

- `NotionWebhookPayload` — `source`, `data`, `secret`, `page_id` 필드 / `extra = "allow"`로 미정의 필드 허용

---

## 멀티 워크스페이스 구조

### 개요

Notion 자동화 웹훅 payload에는 `workspace_id`가 포함되지 않음.
따라서 **page_id로 각 워크스페이스 클라이언트를 순서대로 시도**해 성공한 클라이언트를 사용하는 자동 감지 방식 채택.

### 환경변수 쌍 구조

```
WORKSPACE_ID_1=c6d35e77-...   NOTION_TOKEN_WS_1=ntn_xxx   ← 워크스페이스 A
WORKSPACE_ID_2=15f056cc-...   NOTION_TOKEN_WS_2=ntn_yyy   ← 워크스페이스 B
(필요 시 _3, _4 ... 추가 가능)
```

### 현재 등록된 워크스페이스

| 번호 | Workspace ID | 설명 |
|------|-------------|------|
| 1 | `c6d35e77-c23d-8142-8910-000346605b2d` | 기존 워크스페이스 (data agent_hm_api integration) |
| 2 | `15f056cc-7000-8152-809b-0003524ac364` | 신규 워크스페이스 (데이터전략부문_김흥무 integration) |

### Notion Integration 연결 필수 조건

각 워크스페이스의 Integration이 해당 데이터베이스에 연결되어 있어야 함.
- 데이터베이스 우측 상단 `...` → Connections → Integration 추가

---

## 환경변수

| 변수명 | 필수 | 설명 |
|--------|------|------|
| `WORKSPACE_ID_1` | ✅ | 워크스페이스 A의 ID |
| `NOTION_TOKEN_WS_1` | ✅ | 워크스페이스 A의 Notion Integration Token |
| `WORKSPACE_ID_2` | 선택 | 워크스페이스 B의 ID |
| `NOTION_TOKEN_WS_2` | 선택 | 워크스페이스 B의 Notion Integration Token |
| `ANTHROPIC_API_KEY` | ✅ | Claude API 키 |
| `BREVO_API_KEY` | ✅ | Brevo 이메일 API 키 |
| `GMAIL_USER` | ✅ | 발신자 이메일 주소 (Brevo에 등록된 주소) |
| `RECIPIENT_EMAILS` | ✅ | 참석자 매칭 실패 시 fallback 수신자 (콤마 구분) |
| `WEBHOOK_SECRET` | ✅ | 웹훅 인증 토큰 (노션 버튼 설정과 동일하게) |

---

## 외부 서비스 연동

| 서비스 | 용도 | 인증 방식 |
|--------|------|-----------|
| **Notion API** | 페이지 읽기/쓰기, 연락처 DB 조회 | Integration Token (`ntn_...`) |
| **Anthropic (Claude)** | 회의 요약 생성 | API Key (`sk-ant-...`) |
| **Brevo** | 이메일 발송 | API Key (`xkeysib-...`) |
| **Railway** | 서버 호스팅, 환경변수 관리, 자동 배포 | GitHub 연동 |

---

## 배포 서버 (Railway)

- **플랫폼**: Railway
- **트리거**: GitHub `main` 브랜치 push → 자동 배포
- **포트**: `8080`
- **프로세스**: `uvicorn main:app --host 0.0.0.0 --port 8080`
- **환경변수 관리**: Railway 대시보드 → Variables 탭
- **레포지토리**: `hanwhakimhm-prog/notion_webhook_meeting_summary`

### Railway 배포 후 체크리스트

- [ ] Variables 탭에 모든 환경변수 등록
- [ ] Logs 탭에서 `워크스페이스 등록: WORKSPACE_ID_N=...` 확인
- [ ] `/health` 엔드포인트 200 응답 확인

---

## Notion 설정 방법

### 1. Integration 생성

1. [https://www.notion.so/profile/integrations](https://www.notion.so/profile/integrations) 접속
2. "새 Integration 만들기" → 이름 입력 → 워크스페이스 선택
3. Integration Token (`ntn_...`) 복사 → Railway `NOTION_TOKEN_WS_N`에 등록

### 2. Integration을 데이터베이스에 연결

1. 노션 회의록 데이터베이스 열기
2. 우측 상단 `...` → Connections
3. 생성한 Integration 검색 후 추가
4. 연락처 DB에도 동일하게 추가 (참석자 이메일 조회용)

### 3. 자동화 버튼 설정

1. 데이터베이스 상단 `자동화` 클릭
2. 트리거: "버튼 클릭 시"
3. 액션: "웹훅 전송"
   - URL: `https://<railway-domain>/webhook/meeting-summary`
   - Method: `POST`
   - Headers: `X-Webhook-Secret: <WEBHOOK_SECRET 값>`

### 4. 회의록 페이지 필수 컬럼

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| 회의명 (또는 이름/Name) | title | 회의 제목 |
| 회의 날짜 (또는 날짜/Date) | date | 회의 일자 |
| 참석자 | people | Notion 유저 태그 |

### 5. 연락처 DB 구조

- DB ID: `4e634a54f4814299a5d5c7d2e37653e8` (notion_contacts.py에 하드코딩)
- 컬럼: 이름(title), 노션 계정(people), 이메일(email)

---

## 주요 로직 메모

### STT 텍스트 추출 우선순위

```
1순위: block.type == "transcription" (노션 AI 받아쓰기 블록)
       → 재귀적으로 하위 블록 텍스트 모두 수집

2순위: heading 블록 텍스트에 "받아쓰기" 포함 시
       → 해당 섹션 이후 다음 heading 전까지의 텍스트 수집

3순위: 페이지 전체 텍스트 (fallback)
```

### Claude 프롬프트 출력 형식

```
[SECTION:개요]    회의명, 날짜, 목적, 배경, 참석자 (파이프 구분 2열 테이블)
[SECTION:내용]    주제 | 내용 (파이프 구분 2열 테이블)
[SECTION:결정]    번호 | 결정사항 (파이프 구분 2열 테이블)
[SECTION:액션]    담당자 | 할 일 | 기한 (파이프 구분 3열 테이블)
[SECTION:한줄요약] 전체 회의 한 문장 요약
```

### 기존 요약 중복 방지

Notion 페이지에 요약 삽입 전, `🤖 AI 요약 |` 문자열이 포함된 callout 블록을 찾아 그 이후 블록을 모두 삭제 후 재삽입.

---

## 트러블슈팅 기록

### 2026-04-08: 멀티 워크스페이스 도입 시 400 에러

**증상**: 노션 버튼 클릭 시 400 Bad Request
**원인**: Notion 자동화 웹훅 payload에 `workspace_id` 필드가 없음 (`source`, `data`만 포함)
**해결**: `workspace_id` 기반 분기 대신, `page_id`로 등록된 클라이언트를 순서대로 시도하는 자동 감지 방식으로 전환

### Integration 연결 누락으로 500 에러

**증상**: `Could not find page with ID: ... Make sure the relevant pages and databases are shared with your integration`
**원인**: 신규 워크스페이스 Integration이 해당 데이터베이스에 연결되지 않음
**해결**: Notion 데이터베이스 → Connections → Integration 추가
