# ⚙ Order Service State Machine

## States

NEW → SUBMITTED → PARTIAL → FILLED  
NEW → REJECTED  
SUBMITTED → FAILED → RETRY  
STOP_TRIGGERED → EXIT_PENDING → EXITED  
EXIT_PENDING → FAILED → RETRY  
RETRY_EXCEEDED → EMERGENCY_MARKET

---

## Core Rules

- signal_id 기반 idempotency
- 중복 주문 금지
- 부분 체결 시 잔량 관리
- STOP 실패 시:
  1) 지정가 재시도
  2) 시간 초과 시 긴급 시장가

---

## Emergency Market

조건:
- 손절 확정
- 지정가 실패
- 슬리피지 악화

조치:
- 즉시 알림
- 종목락
