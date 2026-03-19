# Claw-Trader

현금 전용 완전 자동매매 엔진. 한국(KIS) 및 미국(IBKR) 주식 시장 지원.

---

## 특징

- **AI 신호 생성** — Claude + Qwen(Ollama) 듀얼 합의 기반 모멘텀 신호
- **동적 워치리스트** — Universe 종목 → 뉴스+모멘텀 스코어링 → 상위 N종목 자동 선정 (6시간 주기)
- **뉴스 인텔리전스** — DART/Google News/Yahoo Finance RSS 수집 → AI 프롬프트 자동 주입
- **자동 매도** — stop_loss / take_profit / time_limit 조건 충족 시 자동 SELL 주문
- **Fill Detection + PnL** — 잔고 diff 기반 체결 감지 → realized/unrealized PnL 자동 기록
- **이중 시장** — KR (KIS API) / US (IBKR TWS API) 동시 운영
- **현금 전용 리스크 모델** — 마진/레버리지/파생 거래 없음
- **Telegram 제어판** — 원격 일시정지/재개, PIN 인증

---

## 아키텍처

```
Watchlist Selector (Universe → 뉴스+모멘텀 → dynamic:watchlist)
       ↓
News Runner (DART / Google RSS / Yahoo Finance)
       ↓
News Classifier → Redis (news:symbol / news:macro)
       ↓
AI Dual Eval Runner (Claude + Qwen) ← 뉴스 컨텍스트 주입
       ↓
Consensus Signal Runner (합의 → Signal → queue)
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
Position Exit Runner (stop_loss / take_profit / time_limit)
       ↓
Position Engine (Fill Detection → Portfolio / PnL)
```

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| 상태 저장소 | Redis 7 (Docker) |
| KR 거래소 | KIS OpenAPI |
| US 거래소 | IBKR TWS API (ib_insync) |
| AI (주) | Anthropic Claude |
| AI (보조) | Qwen 2.5 (Ollama) |
| 뉴스 수집 | DART OpenAPI + Google News RSS + Yahoo Finance RSS |
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

# 4. Ollama 설치 및 Qwen 모델 다운로드
ollama pull qwen2.5:7b && ollama serve

# 5. 환경변수 로드 (반드시 set -a 사용)
set -a && source .env && source config/phase10_kr_micro.env && set +a

# 6. 프로세스 기동 (순서 중요, 프로젝트 루트에서 실행)
PYTHONPATH=src venv/bin/python -m app.runner >> logs/runner.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.market_data_runner >> logs/market_data.log 2>&1 &
PYTHONUNBUFFERED=1 WATCHER_TTL_CANCEL_SEC=60 PYTHONPATH=src venv/bin/python -m scripts.order_watcher >> logs/order_watcher.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.signal_generator_runner >> logs/signal_generator.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.ai_eval_runner >> logs/ai_eval.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.ai_dual_eval_runner >> logs/ai_dual_eval.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.consensus_signal_runner >> logs/consensus_signal.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.openclaw_bot >> logs/openclaw_bot.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.news_runner >> logs/news_runner.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.position_exit_runner >> logs/position_exit.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.position_engine >> logs/position_engine.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.watchlist_selector_runner >> logs/watchlist_selector.log 2>&1 &
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
- `GEN_WATCHLIST_KR` / `GEN_WATCHLIST_US` — 기본 워치리스트 (동적 선정 없을 때 fallback)
- `GEN_UNIVERSE_KR` / `GEN_UNIVERSE_US` — 동적 워치리스트 Universe 종목 풀

---

## 운영 안전장치

- `claw:pause:global` — 전역 일시정지 (Redis 키, 자정 KST TTL 자동 만료)
- `claw:killswitch:{market}` — 시장별 킬스위치 (일일 손실 한도 초과 시 자동 발동)
- Risk Engine 5-rule 게이트키퍼 (PAUSED / DUPLICATE_POSITION / MAX_CONCURRENT / KILLSWITCH / ALLOCATION_CAP)
- Strategy Engine 3-rule 필터 (dedupe / cooldown / daily_cap)
- AI 호출 하드 캡 — 일일 cap 초과 시 자동 pause + TG 알림
- 비정상 자동 감지 — MD_STALE / AI_ERROR_SPIKE → auto-pause + TG 알림
- Exit 중복 방지 — `claw:exit_lock:{market}:{symbol}` SET NX TTL 60s

---

## 테스트

```bash
pip install pytest fakeredis
PYTHONPATH=src pytest tests/ -v
```

주요 커버리지 (141개):
- `RiskEngine` 5개 규칙
- `StrategyEngine` 3개 규칙 + Lua 원자적 daily_cap
- `consensus_signal_runner` 호가 정규화 / 합의 / cooldown / dedup
- `position_exit_runner` Fill Detection / exit 조건 / 중복 방지
- `watchlist_selector_runner` KR/US 동적 워치리스트 선정
- AI 응답 파서 / 보안 필터

---

## 라이선스

Private
