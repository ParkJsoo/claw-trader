# Claw-Trader Roadmap

## 목표
- **수익 극대화**: 전략 고도화 + 백테스트 기반 검증
- **자동화 완성**: 사람 개입 없이 완전 무인 운용

---

## 현재 상태 (Phase 15 완료 기준, 2026-03-21)

| 영역 | 상태 |
|------|------|
| KR 자동매매 파이프라인 | ✅ 완전 자동 |
| AI 신호 생성 (Claude + Qwen) | ✅ 운영 중 |
| 동적 워치리스트 (12종목) | ✅ 운영 중 |
| 뉴스 → AI 통합 | ✅ 운영 중 |
| Trailing Stop + 비대칭 R:R | ✅ Phase 15 완료 |
| Partial Consensus + Regime Filter | ✅ Phase 15 완료 |
| 포지션/PnL 자동 기록 | ✅ 운영 중 |
| 백테스트 | ❌ 없음 |
| 성과 통계 자동화 | ❌ 없음 |
| 하락장 대응 (숏/인버스) | ❌ 없음 |
| 완전 무인 자동화 | ⚠️ 재기동/파라미터 변경은 수동 |

---

## ✅ Phase 0~15 — 완료

Phase 0~9: 인프라, 거래소 연결, 리스크 엔진, AI 레이어 구축
Phase 10~11: Dual AI consensus, 신호 품질 필터
Phase 12~13: 자동 매도, Fill Detection, PnL 기록
Phase 14: 뉴스 통합, 동적 워치리스트
Phase 15: Trailing stop, 비대칭 R:R, Partial consensus, Regime filter

---

## 🔥 Phase 16 — 성과 측정 기반 구축

> **Why:** 지금은 파라미터를 바꾸면 실전에서만 결과를 알 수 있다.
> 개선하려면 측정이 먼저다.

### 목표
- 전략 파라미터 변경의 효과를 데이터로 검증
- 매일 자동으로 성과 요약 수신

### 구현 항목
- **백테스트 프레임워크**: mark_hist Redis 데이터 기반 전략 시뮬레이션
  - stop_pct / take_pct / trail_pct 파라미터 스윕
  - 최근 N일 실제 mark 데이터로 가상 체결 시뮬레이션
- **성과 통계 자동 계산**
  - win rate, avg R:R, profit factor, Sharpe ratio, max drawdown
  - 일별/주별 집계 → Redis 저장
- **TG 일일 리포트**: 장 마감 후 자동 성과 요약 발송
  - 체결 건수, 수익/손실, win rate, 베스트/워스트 종목

### 완료 조건
- 백테스트 결과와 실제 PnL의 방향성 일치율 70% 이상
- TG 리포트 하루도 누락 없이 장 마감 후 30분 내 수신

---

## 🔥 Phase 17 — 신호 품질 강화

> **Why:** 현재 신호는 5분 모멘텀 하나에 의존한다.
> 노이즈가 많고 false positive가 높다.

### 목표
- 진입 신호의 정밀도 향상 (false positive 감소)
- 고확률 구간에서만 진입

### 구현 항목
- **Volume surge 필터**: 거래량이 20일 평균 대비 1.5배 이상일 때만 진입
- **멀티타임프레임 확인**: 5분 신호가 15분 추세 방향과 일치할 때만 통과
- **뉴스 가중 size_cash**: 뉴스 스코어 높을수록 size_cash 최대 1.5배
  - full consensus + high 뉴스 → 100% size
  - full consensus + no 뉴스 → 80% size
  - partial consensus + medium 뉴스 → 50% size (현재와 동일)
- **AI confidence 반영**: Claude confidence 0.8+ 종목 우선 순위 부여

### 완료 조건
- 백테스트 기준 win rate Phase 16 대비 5%p 이상 향상
- strategy_pass 대비 실제 체결율 개선 (현재 reject 비율 추적 후 목표 설정)

---

## 🔥 Phase 18 — 자동화 완성

> **Why:** 재기동, 파라미터 변경, 일일 cap 리셋에 사람이 개입해야 한다.
> 완전 무인이 되려면 자가 복구와 자동 조정이 필요하다.

### 목표
- 사람이 개입하지 않아도 시스템이 스스로 동작하고 회복

### 구현 항목
- **자가 복구 (supervisord)**: 프로세스 크래시 시 자동 재시작
  - 모든 runner를 supervisord로 관리
  - 재시작 횟수 임계치 초과 시 TG 알림
- **Daily cap 자동 리셋**: 장 시작 전 자동으로 strategy daily_count 초기화
- **파라미터 자동 튜닝**: 최근 5거래일 성과 기반 stop/take pct 자동 조정
  - win rate < 40% → stop_pct 축소, take_pct 확대
  - max drawdown 과다 → size_cash_pct 하향
- **자본 자동 조정**: 연속 수익/손실에 따른 size_cash 비율 자동 조정
  - 3일 연속 수익 → size_cash_pct +5% (상한 50%)
  - 3일 연속 손실 → size_cash_pct -10% (하한 10%)
- **TG 원격 파라미터 변경**: `/claw set stop_pct 0.015` 형태로 런타임 변경

### 완료 조건
- 5거래일 연속 사람 개입 없이 정상 운용
- 크래시 → 자동 복구 시간 60초 이내

---

## 🔥 Phase 19 — 하락장 대응

> **Why:** 현재 하락장에서는 Regime Filter가 신호를 막아 완전히 쉰다.
> 하락장에서도 수익 기회를 잡아야 한다.

### 목표
- 상승/하락장 모두에서 수익 창출
- 포지션 보유 중 급락 시 손실 최소화

### 구현 항목
- **인버스 ETF 편입**: KODEX 200선물인버스(114800), KODEX 코스닥150인버스(251340) universe 추가
- **Regime 기반 자동 전환**
  - 상승장 (regime bullish) → LONG 종목 신호 활성
  - 하락장 (regime bearish) → 인버스 ETF 신호 활성, LONG 억제
  - 횡보장 → 양쪽 비율 조정
- **헤지 로직**: LONG 포지션 보유 중 KOSPI -1% 이상 급락 시 인버스 부분 매수

### 완료 조건
- 하락장 3거래일 이상에서 인버스 ETF 수익 발생
- 헤지 발동 후 전체 포트폴리오 낙폭 30% 이상 감소

---

## 우선순위 요약

```
Phase 16 (측정) → Phase 17 (품질) → Phase 18 (자동화) → Phase 19 (하락장)
```

측정 없이 품질 개선은 맹목적이고,
자동화 없이 규모 확장은 위험하다.
순서대로 진행.
