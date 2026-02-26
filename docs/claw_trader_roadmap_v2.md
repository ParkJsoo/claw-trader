# 🚀 Claw-Trader Roadmap v2 (현실 반영 버전)

---

## ✅ PHASE 0 — 보안 환경 격리 ✔ 완료
- 전용 macOS 계정
- API 키 격리
- 개발 환경 분리

**상태:** ✅ DONE

---

## ✅ PHASE 1 — Core Trading Infrastructure ✔ 완료
- Redis 상태 저장소
- Telegram Control Plane
- Pause / Resume / PIN
- Executor
- Idempotency
- Reject / Audit 로그

**상태:** ✅ DONE

---

## ✅ PHASE 2 — Exchange Connectivity ✔ 완료
- 🇰🇷 KIS Client
- 🇺🇸 IBKR Client
- Healthcheck
- Order Smoke

**상태:** ✅ DONE

---

## ✅ PHASE 3 — Order Lifecycle Control ✔ 완료 (v1)
- Order Watcher
- TTL 기반 자동 취소
- 주문 상태 Redis 기록

**상태:** ✅ DONE

---

# 🎯 이제부터 핵심 영역

---

## 🔥 PHASE 4 — Portfolio / Position Engine ⭐⭐⭐⭐⭐
> 자동매매 실전 필수 레이어

### 목표
- 보유 포지션 추적
- 평균 매입가 계산
- 실현 / 미실현 PnL
- 중복 진입 방지
- 전략 / 리스크 엔진 데이터 제공

### 구현 항목
- position:{market}:{symbol}
- pnl:{market}
- trade history
- fill 이벤트 반영

**우선순위:** 🚨 최우선

---

## ✅ PHASE 5 — Risk Engine v1 ✔ 완료

### 구현 완료
- RiskEngine 단일 게이트키퍼 (executor/risk.py)
- 5개 규칙: PAUSED / DUPLICATE_POSITION / MAX_CONCURRENT / KILLSWITCH_REALIZED / ALLOCATION_CAP
- 킬스위치: realized PnL 기반, SET NX 원자적 발동
- RiskConfig: KR(-500,000원) / US(-$500) 기본값, 시장별 분리
- Executor: is_paused() / risk_check_cash_only() 제거 → RiskEngine 일원화

**상태:** ✅ DONE

---

## ✅ PHASE 6 — Strategy Engine (Rule-Based v1) ✔ 완료 (Filter Layer)

### 구현 완료
- StrategyEngine 신호 품질 필터 (strategy/engine.py)
- 3개 규칙 (비용 기준): DUP_SIGNAL → COOLDOWN → DAILY_CAP
- StrategyConfig: KR/US 시장별 설정, 쿨다운 5분, 일일 캡 20, dedupe 7d
- runner.py: strategy.check(signal) → Executor 앞단 배치
- 관측성 카운터: pass_count / reject_count by reason (일별 Hash)
- 역할 분리: StrategyEngine(신호 품질) ↔ RiskEngine(계좌 안전) ↔ Executor(주문 실행)

**상태:** ✅ DONE (Filter Layer)

---

## ✅ PHASE 7 — Market Data Service v1 ✔ 완료

### 구현 완료
- KisFeed (market_data/kis_feed.py) — KIS REST 현재가 폴링
- IbkrFeed (market_data/ibkr_feed.py) — IBKR ib_insync 스냅샷 (client_id=12 별도)
- MarketDataUpdater (market_data/updater.py) — position_index 기반 활성 심볼만 폴링
- market_data_runner.py — 독립 프로세스 (MD_POLL_INTERVAL, 기본 3초)
- mark:{market}:{symbol} 갱신 → recalc_unrealized 자동 호출 (기존 portfolio 연동)

### 설계 원칙
- 보유 포지션 없으면 no-op (불필요한 API 호출 없음)
- 개별 심볼 실패 무시 → 나머지 심볼 처리 계속
- 기존 mark:{market}:{symbol} 키 재사용 (portfolio 코드 변경 없음)
- unrealized PnL이 fill 이후 실시간 가격으로 지속 갱신

**상태:** ✅ DONE (v1 — 현재가/unrealized PnL 갱신)

---

## ✅ PHASE 8 — AI Layer v1 (Shadow Mode) ✔ 완료

### 구현 완료
- DataGuard (guards/data_guard.py) — md:last_update 기반 stale 감지 (warn-only, hard_block 토글)
- AIAdvisor (ai/advisor.py) — shadow mode, Strategy 통과 후 추천 기록 (실행 영향 0)
- runner.py 연결 — DataGuard → Strategy → AI Advisory → Executor

### AI Advisory 설계
- 모델: claude-haiku-4-5-20251001 (AI_MODEL env로 교체 가능)
- 실패 격리: try/except 완전 흡수, ERROR decision 반환
- 저장: ai:advice:*/ai:advice_index:*/ai:advice_stats:* (TTL 30d)
- 파싱 내구성: {…} substring 추출 + recommend validate + confidence clamp + reason[:100]
- 모델 추적: model/provider 필드 저장 (haiku→sonnet 업그레이드 성과 비교 가능)

### Phase 8 v2 (미구현 — 추후)
- AI 주도 신호 생성
- 뉴스/이벤트 해석
- shadow 데이터 충분히 검증 후 전환

**상태:** ✅ DONE (v1 — Shadow Mode)

---

## 🧠 PHASE 9 — OpenClaw / 로컬 LLM 통합
- Ollama
- 로컬 모델
- 프롬프트 엔진
- 전략 진화 실험

---

## 🚀 PHASE 10 — Adaptive / Autonomous Mode
- AI 전략 최적화
- 리스크 동적 조정
- 메타 전략 진화

---

# 📊 v2 로드맵 핵심 변화

❌ 이전: AI → 전략 → 주문  
✅ 현재: 엔진 → 포지션 → 리스크 → 전략 → 데이터 → AI

---

# 🎯 설계 철학

- 돈이 걸린 시스템
- Fail-safe 우선
- Risk Engine 우선
- 상태 머신 안정성 우선
- AI는 가속기, 기반 아님

---

# 🔥 현재 위치

⭐⭐⭐⭐⭐ PHASE 8 완료 (AI Advisory Shadow Mode v1)
➡ 다음 단계: **Shadow 데이터 검증 후 PHASE 8 v2 또는 PHASE 9 (OpenClaw/LLM)**
