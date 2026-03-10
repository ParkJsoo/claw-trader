# Claw-Trader

현금 전용 완전 자동매매 엔진. 한국(KIS) 및 미국(IBKR) 주식 시장 지원.

---

## 특징

- **AI 신호 생성** — Anthropic Claude 기반 모멘텀 피처 분석 후 매매 신호 생성
- **듀얼런 비교 엔진** — Claude vs Qwen(Ollama) 합의 기반 신호 품질 검증
- **이중 시장** — KR (KIS API) / US (IBKR TWS API) 동시 운영
- **현금 전용 리스크 모델** — 마진/레버리지/파생 거래 없음
- **완전 자동화** — 신호 생성 → 리스크 검증 → 주문 실행 → 상태 감시 파이프라인
- **뉴스 인텔리전스** — DART/Google News RSS 수집 → Qwen 분류/요약 → Redis 저장
- **Telegram 제어판** — 원격 일시정지/재개, 허용 사용자 PIN 인증

---

## 아키텍처

```
News Runner (DART / Google RSS)
       ↓
News Classifier (Qwen) → Redis (news:symbol / news:macro)

AI Signal Generator (Claude)    AI Eval Runner (Claude)
                    ↘           ↙
              signal:queue (Redis)
                       ↓
        Strategy Engine (dedupe / cooldown / daily_cap)
                       ↓
          Risk Engine (5-rule gatekeeper)
                       ↓
        Executor (idempotency / audit log)
                       ↓
           KIS / IBKR Order API
                       ↓
     Order Watcher (TTL 기반 미체결 취소)
                       ↓
       Portfolio Engine (포지션 / PnL)
```

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| 상태 저장소 | Redis 7 (Docker) |
| KR 거래소 | KIS OpenAPI |
| US 거래소 | IBKR TWS API (ib_insync) |
| AI (주) | Anthropic Claude (claude-haiku-4-5) |
| AI (보조) | Qwen 2.5:7b (Ollama) |
| 뉴스 수집 | DART OpenAPI + Google News RSS |
| 알림 | Telegram Bot API |
| 테스트 | pytest + fakeredis |

---

## 실행 방법

```bash
# 1. 환경변수 설정
cp .env.example .env
# .env 파일에 실제 값 입력

# 2. 의존성 설치
python -m venv venv
source venv/bin/activate
pip install -r src/requirements.txt

# 3. Redis 실행 (Docker)
docker run -d --name claw-redis -p 6379:6379 redis:7-alpine \
  redis-server --requirepass <password>

# 4. Ollama 설치 및 Qwen 모델 다운로드 (듀얼런/뉴스 분류용)
ollama pull qwen2.5:7b && ollama serve

# 5. 프로세스 기동 (순서 중요, 프로젝트 루트에서 실행)
PYTHONPATH=src venv/bin/python -m app.runner                    # 신호 처리 파이프라인
PYTHONPATH=src venv/bin/python -m app.market_data_runner        # 현재가 폴링
PYTHONPATH=src venv/bin/python -m scripts.order_watcher         # 주문 감시
PYTHONPATH=src venv/bin/python -m app.signal_generator_runner   # AI 신호 생성기
PYTHONPATH=src venv/bin/python -m app.ai_eval_runner            # AI 평가 러너
PYTHONPATH=src venv/bin/python -m app.ai_dual_eval_runner       # Claude vs Qwen 듀얼런
PYTHONPATH=src venv/bin/python -m app.consensus_signal_runner   # Phase 10: 듀얼 합의 → Signal → queue
PYTHONPATH=src venv/bin/python -m app.openclaw_bot              # Telegram 제어판
PYTHONPATH=src venv/bin/python -m app.news_runner               # 뉴스 수집/분류 (30분 폴링)
```

---

## 주요 환경변수

`.env.example` 참조. 필수 값:

- `ANTHROPIC_API_KEY` — AI 신호 생성 (Claude)
- `KIS_APP_KEY` / `KIS_APP_SECRET` / `KIS_ACCOUNT_NO` — 한국 거래소
- `IBKR_ACCOUNT_ID` — 미국 거래소 (IBKR Gateway 별도 실행 필요)
- `REDIS_URL` — Redis 연결 (예: `redis://:<password>@127.0.0.1:6379/0`)
- `TG_BOT_TOKEN` / `TG_ALLOWED_CHAT_ID` — Telegram 제어판
- `DART_API_KEY` — 뉴스 수집 (DART OpenAPI)
- `GEN_WATCHLIST_KR` / `GEN_WATCHLIST_US` — 종목 워치리스트

---

## 운영 안전장치

- `claw:pause:global` — 전역 일시정지 (Redis 키)
- `claw:killswitch:{market}` — 시장별 킬스위치 (일일 손실 한도 초과 시 자동 발동)
- `gen:runner:lock` / `eval:runner:lock` — 중복 실행 방지 프로세스 락
- Risk Engine 5-rule 게이트키퍼 (PAUSED / DUPLICATE_POSITION / MAX_CONCURRENT / KILLSWITCH / ALLOCATION_CAP)
- Strategy Engine 3-rule 필터 (dedupe / cooldown / daily_cap)
- AI 호출 하드 캡 — 일일 1000회 초과 시 자동 pause + TG 알림
- 비정상 자동 감지 — MD_STALE / AI_ERROR_SPIKE → auto-pause + TG 알림
- 프롬프트 인젝션 방어 — 뉴스 입력 sanitize + AI 출력 allowlist 필터

---

## 테스트

```bash
pip install pytest fakeredis
PYTHONPATH=src pytest tests/ -v
```

주요 테스트 커버리지:
- `RiskEngine` 5개 규칙 (pause / duplicate / concurrent / killswitch / allocation)
- `StrategyEngine` 3개 규칙 (dedupe / cooldown / daily_cap)
- `parse_decision_response` AI 응답 파서 (confidence clamp / direction 검증)
- 보안 필터 (sanitize_input / summary allowlist / 인젝션 패턴)
- `consensus_signal_runner` 호가 정규화 / happy path / reject / dedup (26개)

---

## 라이선스

Private
