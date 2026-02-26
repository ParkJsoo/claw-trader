# 🚀 Claw Trader Roadmap

## 🎯 목표
AI 기반 완전 자동 단타 트레이딩 시스템  
KR + US 동시 운용  
뉴스 이벤트 + 모멘텀 전략  
Anchored VWAP 중심 구조

---

## ✅ Phase 0 — 보안 환경 격리
- macOS 새 사용자 계정
- OpenClaw 전용

---

## ✅ Phase 1 — OpenClaw 설치
- OpenClaw CLI 설치
- Workspace 설정

---

## ✅ Phase 2 — 로컬 LLM
- Ollama
- Qwen 2.5 Instruct 14B

---

## ✅ Phase 3 — 클라우드 모델
- Claude Sonnet 4.5
- 중요 이벤트 검증

---

## ✅ Phase 4 — 모델 역할 분리
- 로컬: 요약/태깅
- 클라우드: 영향도/판단 강화

---

## ✅ Phase 5 — 뉴스/커뮤니티 수집

---

## ✅ Phase 6 — Intel Service
- 이벤트 추출/점수화
- validated_events 발행

---

## ✅ Phase 7 — Strategy Service
- 뉴스 + 모멘텀 신호 생성
- Signal(JSON) 발행

---

## ✅ Phase 8 — Market Data Service
- WebSocket
- mid-quote
- Anchored VWAP
- 1s/5s/1m 캔들

---

## ✅ Phase 9 — Order Service
- 주문 상태머신
- 구조 손절
- 지정가 손절
- 긴급 시장가(최후수단)

---

## ✅ Phase 10 — Risk Engine
- 하드 가드레일
- 하루 손실 제한
- 누적 손실 제한

---

## ✅ Phase 11 — Telegram Alerts
- 실시간 위험 알림
- 요약 알림

---

## ✅ Phase 12 — Ultra-Fast Validation (3시간)

---

## ✅ Phase 13 — 제한적 실전

---

## ✅ Phase 14 — 안정화 & 고도화
