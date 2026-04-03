# notion-webhook-meeting-summary

노션 회의 페이지 버튼 클릭 → STT 받아쓰기 텍스트 추출 → Claude 요약 → 메일 발송

## 흐름

```
노션 버튼 클릭
  → POST /webhook/meeting-summary (Railway)
    → Notion API로 페이지 블록 조회
    → 받아쓰기 섹션 텍스트 추출
    → Claude claude-sonnet-4-6 요약 생성
    → Brevo API로 HTML 이메일 발송
```

---

## Railway 배포

### 1. 저장소 연결

Railway 대시보드 → **New Project → Deploy from GitHub Repo** → 이 폴더가 포함된 레포 선택

> 서브디렉토리 배포 시: **Settings → Root Directory** 를
> `개발코드/노션/notion_webhook_meeting_summary` 로 지정

### 2. 시작 명령

Railway가 자동 감지하거나 **Settings → Start Command** 에 입력:

```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

### 3. 환경변수 설정

Railway 대시보드 → **Variables** 탭에서 아래 변수 추가:

| 변수명 | 설명 |
|---|---|
| `NOTION_API_KEY` | Notion Integration 토큰 |
| `ANTHROPIC_API_KEY` | Claude API 키 |
| `BREVO_API_KEY` | Brevo SMTP API 키 |
| `GMAIL_USER` | 발신자 이메일 주소 (Brevo 발신자로 등록된 주소) |
| `RECIPIENT_EMAILS` | 수신자 주소 (콤마 구분, 여러 명 가능) |
| `WEBHOOK_SECRET` | 노션 버튼 설정과 동일한 비밀 토큰 |

### 4. 배포 URL 확인

배포 완료 후 Railway가 발급한 URL 확인:
예) `https://your-app.railway.app`

헬스체크: `GET https://your-app.railway.app/health`

---

## 노션 버튼 웹훅 연결

### 노션 Automation 설정

1. 회의 데이터베이스 페이지 → 우상단 **⚡ Automation** 클릭
2. **+ New automation** 생성
3. **Trigger**: `Button clicked` (페이지 내 버튼)
   - 버튼 이름 예: `📋 회의 요약 시작`
4. **Action**: `Send webhook`
   - **URL**: `https://your-app.railway.app/webhook/meeting-summary`
   - **Method**: `POST`
   - **Headers**:
     ```
     X-Webhook-Secret: your-strong-random-secret-here
     Content-Type: application/json
     ```
   - **Body** (Notion이 자동으로 현재 페이지 정보를 `data` 필드에 포함):
     노션 자동화 웹훅은 아래 구조로 payload를 전송합니다.

### 노션이 전송하는 Payload 구조

```json
{
  "source": {
    "type": "automation",
    "automation_id": "uuid"
  },
  "data": {
    "object": "page",
    "id": "페이지-uuid",
    "url": "https://notion.so/...",
    "properties": {
      "회의명": { "title": [{ "plain_text": "2024 Q1 전략 회의" }] },
      "회의 날짜": { "date": { "start": "2024-01-15" } }
    }
  }
}
```

> **Secret을 헤더 대신 payload에 넣을 경우**:
> Body 에 `"secret": "your-strong-random-secret-here"` 필드를 추가해도 동작합니다.

### 받아쓰기 섹션 구성

회의 페이지 내에 **`받아쓰기`** 제목 블록(H1~H3) 아래에 STT 텍스트가 위치해야 합니다:

```
## 받아쓰기          ← 이 제목 블록을 기준으로 텍스트 추출
안녕하세요. 오늘 회의는...
논의 사항 1...
결정된 사항은...
```

> 받아쓰기 섹션이 없으면 페이지 전체 텍스트를 요약 대상으로 사용합니다.

---

## Notion Integration 권한

사용하는 Notion Integration에 아래 권한이 필요합니다:

- **Read content** (페이지 블록 읽기)
- 대상 데이터베이스/페이지에 Integration이 연결되어 있어야 함
  → 페이지 우상단 `...` → **Add connections** → Integration 선택

---

## 로컬 테스트

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일 편집 후 실행

# 서버 실행
uvicorn main:app --reload --port 8000

# 웹훅 테스트 (curl)
curl -X POST http://localhost:8000/webhook/meeting-summary \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-strong-random-secret-here" \
  -d '{
    "data": {
      "id": "노션-페이지-UUID",
      "url": "https://notion.so/...",
      "object": "page"
    }
  }'
```

응답 예시:
```json
{"status": "accepted", "title": "2024 Q1 전략 회의"}
```

> 요약·메일 발송은 백그라운드에서 처리되므로 202 응답 후 비동기로 실행됩니다.
