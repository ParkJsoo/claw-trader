# ⚙ Execution Spec — claw-trader v1

## 🌍 Markets
- KR + US 동시 운용

---

## 💰 Capital Rules
- 실전 노출: 30%
- 포지션 상한: 25%
- 동시 포지션: 2

---

## 📈 Strategy
- 뉴스 이벤트 + 모멘텀 단타
- Anchored VWAP (혼합 앵커)

---

## 🔻 Stop Loss
- 구조 기반
- Anchored VWAP 이탈
- 지지선 붕괴

완충:
- 10초 + 5틱

실행:
- 지정가 손절
- 재시도
- 긴급 시장가(최후수단)

---

## 🎯 Take Profit
혼합:
- R 기반
- 구조 기반

---

## 🚨 Emergency Market Policy
발동 조건:
- 손절 확정
- 지정가 실패
- 슬리피지 악화
- 리스크 제한 근접

발동 후:
- 종목락 60분
- 경보

---

## 🧠 AI Autonomy
허용:
- 손절폭 조정
- 진입 민감도
- 사이징

금지:
- 리스크 제한 변경

---

## 🔔 Alerts
즉시:
- 손절/익절
- 리스크 개입
- 긴급 시장가
- AI 변경

요약:
- 시그널/체결/PnL
