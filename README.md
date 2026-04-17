# Claw-Trader

현금 전용 완전 자동매매 엔진. 한국(KIS) / 미국(IBKR) / 코인(Upbit) 지원.

---

## 특징

- **AI 신호 생성** — Claude 기반 momentum breakout 신호 (runtime: `claude_only`)
- **동적 워치리스트** — KR: Universe → 상위 12종목 자동 선정 / COIN: 거래대금 상위 30개 + 급등 종목 추가 (10분 갱신)
- **뉴스 인텔리전스** — DART/Google News/Yahoo Finance RSS 수집 → AI 프롬프트 자동 주입
- **신호 품질 필터** — ret_5m / range_5m / volume surge(×1.5) / live mark_hist 실시간 검증
- **KR Exit Profile** — HWM 기반 trailing + scalp R:R (기본 `1.5% / 3.0% / 1.5%`)
- **COIN Exit Profile** — Big mover ride (`3.0% / 15.0% / 4.0%`, +5% 이후 tight trail)
- **Time Limit** — KR 15분 기본 / COIN 1시간 기본, 수익 중 연장
- **Regime Filter** — 3방향(bearish/neutral/bullish), bearish 시 모든 LONG 억제
- **자동 파라미터 튜닝** — 5거래일 성과 기반 stop/size 자동 조정 (KST 15:40 후)
- **streak 자본 조정** — 3연속 수익 → size +5%, 3연속 손실 → size −10%
- **Fill Detection + PnL** — 잔고 diff 기반 체결 감지 → realized/unrealized PnL 자동 기록
- **TG 일일 리포트** — KR 15:40, COIN 자정 기준 전일 리포트 자동 발송
- **백테스트 프레임워크** — stop/take/trail_pct 그리드 스윕, 매일 KST 16:10 자동 실행
- **3중 시장** — KR (KIS API) / US (IBKR TWS API) / COIN (Upbit API) 동시 운영
- **현금 전용 리스크 모델** — 마진/레버리지/파생 거래 없음
- **Telegram 제어판** — 원격 일시정지/재개/파라미터 변경

---

## 아키텍처

```
Watchlist Selector (KR: Universe → 상위 12종목 / COIN: 거래대금 상위 30개 + 급등 추가, 10분 갱신)
       ↓
News Runner (DART / Google RSS / Yahoo Finance)
       ↓
News Classifier → Redis (news:{symbol} / news:macro)
       ↓
AI Dual Eval Runner (Claude) ← ret_5m prefilter / 뉴스 컨텍스트 주입
       ↓
Consensus Signal Runner (live mark_hist ret_5m 재검증 + volume surge + Regime Filter → Signal → queue)
       ↓
Strategy Engine (dedupe / cooldown / daily_cap)
       ↓
Risk Engine (5-rule gatekeeper)
       ↓
Executor (idempotency / per-signal stop/take_pct / audit log)
       ↓
KIS / IBKR / Upbit Order API
       ↓
Order Watcher (TTL 기반 미체결 취소)
       ↓
Position Exit Runner (KR primary / COIN fallback)
       ↓
Upbit WS Exit Monitor (COIN primary real-time exit)
       ↓
Position Engine (Fill Detection → Portfolio / PnL / streak 조정)
       ↓
Daily Report Runner (08:55 KR daily_cap 리셋 / 00:00 COIN 리셋 / 15:40 TG 리포트 + 자동 튜닝)
```

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| 상태 저장소 | Redis 7 (Docker) |
| KR 거래소 | KIS OpenAPI |
| US 거래소 | IBKR TWS API (ib_insync) |
| COIN 거래소 | Upbit API |
| AI (주) | Anthropic Claude (momentum catalyst filter) |
| 뉴스 수집 | DART OpenAPI + Google News RSS + Yahoo Finance RSS |
| 알림 | Telegram Bot API |
| 프로세스 관리 | supervisord |
| 테스트 | pytest + fakeredis |

---

## 실행 방법

### 권장: supervisord (완전 무인 자동화)

```bash
# 1. 환경변수 설정
cp .env.example .env
# .env 파일에 실제 값 입력

# 2. 의존성 설치
python -m venv venv
source venv/bin/activate
pip install -r src/requirements.txt

# 3. Redis 실행 (Docker, persistence 포함)
docker run -d --name claw-redis --restart always \
  -p 127.0.0.1:6379:6379 \
  -v claw-redis-data:/data \
  -v $(pwd)/config/redis.conf:/usr/local/etc/redis/redis.conf \
  redis:7-alpine redis-server /usr/local/etc/redis/redis.conf

# 4. 환경변수 로드
set -a && source .env && source config/phase10_kr_micro.env && set +a

# 5. supervisord 기동 (runtime truth)
supervisord -c config/supervisord.conf
supervisorctl status   # 상태 확인
supervisorctl tail -f runner   # 로그 실시간 확인
```

### 수동 기동 (개발/디버깅용)

```bash
set -a && source .env && source config/phase10_kr_micro.env && set +a

PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m app.runner >> logs/runner.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.market_data_runner >> logs/market_data.log 2>&1 &
PYTHONUNBUFFERED=1 WATCHER_TTL_CANCEL_SEC=60 PYTHONPATH=src venv/bin/python -m scripts.order_watcher >> logs/order_watcher.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.ai_dual_eval_runner >> logs/ai_dual_eval.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.consensus_signal_runner >> logs/consensus_signal.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.openclaw_bot >> logs/openclaw_bot.log 2>&1 &
PYTHONPATH=src venv/bin/python -m app.news_runner >> logs/news_runner.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.position_exit_runner >> logs/position_exit.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.position_engine >> logs/position_engine.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.watchlist_selector_runner >> logs/watchlist_selector.log 2>&1 &
PYTHONUNBUFFERED=1 PYTHONPATH=src venv/bin/python -m scripts.daily_report_runner >> logs/daily_report.log 2>&1 &
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

`config/supervisord.conf`가 실제 운영 source of truth이고, `config/phase10_kr_micro.env`는 로컬 기본값입니다.

`config/phase10_kr_micro.env` 기본 설정:

```bash
EXIT_STOP_LOSS_PCT=0.015       # KR -1.5% 손절
EXIT_TAKE_PROFIT_PCT=0.030     # KR +3.0% 익절
EXIT_TRAIL_STOP_PCT=0.015      # KR trailing stop
EXIT_TIME_LIMIT_SEC=900        # KR 15분 보유 제한
KR_TRAIL_ONLY_TRIGGER_PCT=0.020
COIN_EXIT_STOP_LOSS_PCT=0.030
COIN_EXIT_TAKE_PROFIT_PCT=0.150
COIN_EXIT_TRAIL_STOP_PCT=0.040
COIN_EARLY_EXIT_SEC=600
COIN_EARLY_EXIT_PCT=0.010
MB_MIN_SURGE_5M_KR=0.020
RISK_KR_MAX_CONCURRENT=3
UNIVERSE_SELECT_COUNT=12
```

---

## Telegram 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/claw status` | 시스템 상태 |
| `/claw pnl` | 포지션/PnL 조회 |
| `/claw report` | 당일 성과 리포트 |
| `/claw backtest` | 파라미터 스윕 즉시 실행 |
| `/claw set stop_pct 0.015` | 런타임 파라미터 변경 |
| `/claw news` | 최근 뉴스 요약 |
| `/claw help` | 도움말 |

허용 파라미터: `stop_pct` (0.005~0.05), `take_pct` (0.01~0.10), `trail_pct` (0.005~0.05), `size_cash_pct` (0.05~0.50), `max_concurrent` (1~5)

---

## 운영 안전장치

- `claw:pause:global` — 전역 일시정지 (Redis 키, 자정 KST TTL 자동 만료)
- `claw:killswitch:{market}` — 시장별 킬스위치 (일일 손실 한도 초과 시 자동 발동)
- Risk Engine 5-rule 게이트키퍼 (PAUSED / DUPLICATE_POSITION / MAX_CONCURRENT / KILLSWITCH / ALLOCATION_CAP)
- Strategy Engine 3-rule 필터 (dedupe / cooldown / daily_cap)
- AI 호출 하드 캡 — 일일 cap 초과 시 자동 pause + TG 알림
- 비정상 자동 감지 — MD_STALE / AI_ERROR_SPIKE → auto-pause + TG 알림
- Exit 중복 방지 — `claw:exit_lock:{market}:{symbol}` SET NX TTL 60s
- supervisord crash-notifier — 프로세스 FATAL 시 TG 알림

---

## 테스트

```bash
pip install pytest fakeredis
PYTHONPATH=src pytest tests/ -v
```

228개 테스트, 주요 커버리지:
- `RiskEngine` 5개 규칙
- `StrategyEngine` 3개 규칙 + Lua 원자적 daily_cap
- `consensus_signal_runner` 합의 / Partial Consensus / Regime Filter / 품질 필터
- `position_exit_runner` trailing stop / time_limit 연장 / per-signal stop/take_pct
- `position_engine` TOCTOU 방지 / streak 조정 / Fill 멱등성
- `watchlist_selector_runner` KR/US 동적 워치리스트 / 인버스 ETF 포함
- `hedge_runner` 헤지 트리거 / 재발동 방지
- AI 응답 파서 / 보안 필터
- `backtester` 그리드 스윕 / trailing stop 재현

---

## 라이선스

Private
