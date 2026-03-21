# Claw-Trader Roadmap

## 목표
- **수익 극대화**: 전략 고도화 + 백테스트 기반 검증
- **자동화 완성**: 사람 개입 없이 완전 무인 운용

---

## 현재 상태 (Phase 19 완료 + 7차 코드리뷰, 2026-03-21) — 228 tests passing

| 영역 | 상태 |
|------|------|
| KR 자동매매 파이프라인 | ✅ 완전 자동 |
| AI 신호 생성 (Claude + Qwen) | ✅ 운영 중 |
| 동적 워치리스트 (12종목) | ✅ 운영 중 |
| 뉴스 → AI 통합 | ✅ 운영 중 |
| 신호 품질 필터 (ret_15m / volume surge / 뉴스+신뢰도 가중) | ✅ Phase 17 완료 |
| Trailing Stop + 비대칭 R:R (stop 1.5%, take 3%) | ✅ Phase 15 완료 |
| Time Limit 연장 (수익 중 최대 2×) | ✅ Phase 15 완료 |
| Partial Consensus + Regime Filter (3방향) | ✅ Phase 15 완료 |
| per-signal 동적 stop/take_pct | ✅ 버그 수정 완료 |
| 포지션/PnL 자동 기록 | ✅ 운영 중 |
| TOCTOU 방지 (포지션 1회 읽기) | ✅ 버그 수정 완료 |
| 성과 통계 자동화 (TG 일일 리포트) | ✅ Phase 16 완료 |
| 백테스트 프레임워크 | ✅ Phase 16+ 완료 |
| 완전 무인 자동화 (supervisord + 자동조정) | ✅ Phase 18 완료 |
| 하락장 대응 (인버스 ETF + 헤지) | ✅ Phase 19 완료 |
| 7차 코드리뷰 (이슈 0건) | ✅ 완료 |

---

## ✅ Phase 0~19 + 코드리뷰 — 완료

| Phase | 내용 |
|-------|------|
| 0~9 | 인프라, 거래소 연결, 리스크 엔진, AI 레이어 구축 |
| 10~11 | Dual AI consensus, 신호 품질 필터, execution funnel |
| 12~13 | 자동 매도, Fill Detection, PnL 기록 |
| 14 | 뉴스 통합, 동적 워치리스트 |
| 15 | Trailing stop, 비대칭 R:R, Partial consensus, Regime filter, per-signal stop/take_pct |
| 16 | PerformanceReporter, TG 일일 리포트, 백테스트 프레임워크 |
| 17 | ret_15m 필터, 거래량 서지, 뉴스/신뢰도 가중 size_cash |
| 18 | supervisord, daily cap 자동 리셋, TG 파라미터, streak 자본 자동 조정 |
| 19 | 인버스 ETF, Regime 3방향 전환, hedge_runner |
| 코드리뷰 | 7차 리뷰 — TOCTOU, per-signal 버그, Fill 멱등성, limit_price None 처리 등 수정 |

---

## 운영 검증 필요 항목 (코드 완료, 실운용 미검증)

| 항목 | 완료 조건 | 비고 |
|------|---------|------|
| 완전 무인 운용 | 5거래일 연속 개입 없이 정상 운용 | supervisord 전환 후 |
| supervisord 자동 복구 | 크래시 → 60초 내 재시작 | crash-notifier TG 알림 포함 |
| 인버스 ETF 수익 | 하락장 3거래일 이상 수익 발생 | bearish regime 발동 시 |
| 헤지 효과 | 포트폴리오 낙폭 30% 이상 감소 | KOSPI -1% 급락 시 |
| 백테스트 방향성 일치 | 백테스트 추천 파라미터 → 실PnL 개선 | 5거래일 데이터 축적 후 |
| 자동 파라미터 튜닝 | win_rate/drawdown 기반 자동 조정 효과 | 5거래일 성과 누적 후 |

---

## 운영 전환 가이드

### supervisord로 전환
```bash
# 기존 백그라운드 프로세스 종료 후
supervisord -c config/supervisord.conf
supervisorctl status        # 전체 프로세스 상태
supervisorctl tail -f runner   # 로그 실시간 확인
```

### 핵심 config (config/phase10_kr_micro.env)
```bash
EXIT_STOP_LOSS_PCT=0.015
EXIT_TAKE_PROFIT_PCT=0.03
EXIT_TRAIL_STOP_PCT=0.015
EXIT_TIME_LIMIT_MAX_SEC=7200
RISK_KR_MAX_CONCURRENT=3
UNIVERSE_SELECT_COUNT=12
INVERSE_ETF_KR=114800,251340
INVERSE_ETF_ENABLED=true
HEDGE_SYMBOL_KR=114800
HEDGE_TRIGGER_RET=-0.01
HEDGE_SIZE_CASH=100000
```

### 백테스트 수동 실행
```bash
PYTHONPATH=src venv/bin/python -m scripts.backtest_runner --now
```

### TG 커맨드 목록
- `/claw status` — 시스템 상태
- `/claw pnl` — 포지션/PnL 조회
- `/claw report` — 당일 성과 리포트
- `/claw backtest` — 파라미터 스윕 즉시 실행
- `/claw set stop_pct 0.015` — 런타임 파라미터 변경 (KR/US 공통)
- `/claw news` — 최근 뉴스 요약
- `/claw help` — 도움말

---

## 향후 개선 아이디어 (우선순위 미정)

- **백테스트 ↔ 실PnL 방향성 검증**: 5거래일 이상 데이터 축적 후 일치율 측정 (목표 70%+)
- **/claw set 시장 구분**: `/claw set KR stop_pct 0.015` 형태로 KR/US 독립 설정
- **US 시장 고도화**: US도 동일 수준의 품질 필터 적용 (현재 KR 중심)
- **Sharpe ratio 추가**: PerformanceReporter에 일별 수익률 변동성 기반 Sharpe 계산
- **웹 대시보드**: Redis 데이터 시각화 (성과, 포지션, 백테스트 결과)
