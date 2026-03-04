# Claw-Trader × OpenClaw 운영 컨트롤 플레인 설계서 (v1.2)

> **ChangeLog**
> - v1.2: **현재 운영 모드: AI-First / No-Trade** 선언 추가. 실주문은 뒤로 미루고, AI 평가/관찰 파이프라인 먼저 진행. `/claw ai status` 명령 추가. Live checklist 트랙 분기 명시.
> - v1.1: OpenClaw(openclaw.ai)는 로컬 에이전트 프레임워크. 커스텀 스킬 기반 구현으로 방식 교정.
> - v1.0: 초기 설계.

---

> **현재 운영 모드: AI-First / No-Trade**
> - `claw:pause:global=true` 유지 — 실주문 나가지 않음
> - OpenClaw의 목적은 AI 평가/관찰 자동화 및 운영 모니터링이며, pause 해제를 유도하지 않음
> - `pause off`는 Live Trading 트랙으로 전환할 때만 사용 (별도 조건 충족 + 2단 확인 필수)

---

## 0. 범위(Non-Goals)

### Goals
- 채팅(OpenClaw 내장 Telegram/WhatsApp 통합)에서 명령 → 로컬 머신에서 Claw-Trader 운영 작업 수행
- 실수로 실거래가 나가지 않도록 **pause 해제에 강력한 안전장치** 적용
- Live 전환 체크리스트 실행을 “원격 오퍼레이터” 관점에서 지원
- 상태 요약/프로세스 헬스/Redis 지표 확인을 1~2개의 명령으로 끝내기

### Non-Goals
- OpenClaw로 매매 판단/전략 엔진을 대체하지 않음
- OpenClaw가 watchlist/universe를 자동 변경하지 않음
- OpenClaw가 RiskEngine/Executor 로직을 대체하지 않음
- **현재 단계에서는 실주문(포지션 오픈/청산)을 수행하지 않음** — 실주문 전환은 별도 Live checklist 트랙 A에서만 다룸
- (선택) 뉴스/커뮤니티 수집/분류는 추후 확장으로 남기되, v1.2에서는 운영 컨트롤에 집중

---

## 1. 최종 역할 분리(아키텍처 결론)

### 트레이딩 코어 (Claw-Trader)
- Market Data Runner → mark_hist 축적
- Feature 계산
- **Claude API 직접 호출**로 판단/신호 생성 (단, `claw:pause:global=true`면 신호/주문 차단)
- RiskEngine
- Executor (주문 실행)
- Order Watcher (체결 반영 → position_index → PnL)

### 운영 컨트롤 플레인 (OpenClaw)
- OpenClaw는 로컬에서 실행되는 에이전트 프레임워크이며:
  - Telegram/WhatsApp 통합 내장
  - Shell 명령 실행 가능
  - 커스텀 스킬 확장 가능
- 역할: “상태 요약/헬스체크/재기동/pause 토글/체크리스트 실행 보조/긴급정지”
- 핵심 원칙: OpenClaw는 *결정*이 아니라 *운영*을 자동화한다.

---

## 2. 절대 안전원칙

### 2.1 실거래 차단 1차 장치
- Redis: `claw:pause:global=true` 유지 시
  - signal_generator_runner는 AI 호출 없이 sleep
  - 주문 실행 불가

### 2.2 실거래 차단 2차 장치(필수)
- OpenClaw 스킬에서 `pause off` 실행 시 **PIN 또는 2단 확인**을 강제한다.
- 추천: **2단 확인 + TTL** (운영 실수 방지에 강함)

#### 옵션 A: PIN 방식
- `/claw pause off 1234`
- PIN은 Secret로 관리(로그 마스킹)

#### 옵션 B: 2단 확인 방식 (권장)
1) `/claw pause off` → “확인 코드 ABCD 발급(예: 60초 유효)”
2) `/claw confirm ABCD` → pause 해제 실행  
- 확인코드는 Redis에 `ops:confirm:{code}` 형태로 TTL 저장

### 2.3 Kill Switch (필수)
- `/claw kill-switch`는 어떤 경우에도 즉시:
  - `claw:pause:global=true`
  - Telegram 알림
- **Emergency Flatten(포지션 청산)** 정책은 “필수”로 문서에 명시하되, 구현은 Phase 9에서 신중히 설계(청산 실패 fallback 포함).

---

## 3. OpenClaw v1.2 명령(스킬) 최소 세트

> 아래 7개는 “운영 컨트롤 플레인” 최소 구성이다.
> 구현 우선순위 권장(AI-First 모드 기준): 3.1 → 3.5(ai status) → 3.3 → 3.2 → 3.4 → 3.7 → 3.6

### 3.1 `/claw status` — 상태 요약 (1순위)
**출력해야 할 핵심 지표**
- `claw:pause:global` (true/false)
- `md:last_update:KR`, `md:last_update:US`의 age (초)
- `gen:runner:lock` 존재 여부 + TTL
- `mark_hist:KR:<대표심볼>` 길이, `mark_hist:US:<대표심볼>` 길이
- `md:error:*` 오늘 델타(가능하면 KR/US 분리)
- `ai:call_count:{market}:{YYYYMMDD}` (오늘 누적)
- 현재 watchlist(KR/US)

**출력 형식**
- 표 형태 + 임계 초과 시 🔴/🟡/🟢 표시

---

### 3.2 `/claw ps` — 프로세스 헬스
**확인 대상**
- `app.runner`
- `app.market_data_runner`
- `app.signal_generator_runner`
- `scripts.order_watcher`
- (옵션) `caffeinate`

**출력**
- 실행/중지, 시작 시각/업타임

---

### 3.3 `/claw pause on|off` — pause 토글
- `/claw pause on`: 즉시 `claw:pause:global=true`
- `/claw pause off`: **반드시 안전장치(PIN 또는 2단 확인)**
- 해제 후 `/claw status` 자동 출력

---

### 3.4 `/claw restart <service|all>` — 재기동
**지원 예시**
- `/claw restart market_data_runner`
- `/claw restart signal_generator_runner`
- `/claw restart order_watcher`
- `/claw restart all`

**핵심 안전장치**
- `restart all`은 **포지션 존재 시 차단**(guard 필수)
- 재기동 후 md_age/lock TTL 자동 확인

---

### 3.5 `/claw ai status` — AI 평가 상태 요약 (AI-First 모드용)

> 주문과 무관하게 "AI가 제대로 돌고 있는가"를 확인하는 명령.

**출력 항목**
- `ai:call_count:{KR/US}:{YYYYMMDD}` — 오늘 누적 AI 호출 수
- 최근 `ai:decision:*` 또는 `ai:gen_stats:*` 키 존재 여부
- feature 0.0 감지 여부(이상 징후)
- `claw:pause:global` 상태 재확인

---

### 3.6 `/claw live checklist` — 라이브 전환 체크리스트 보조

> **트랙 분기**: 본 체크리스트는 두 가지 트랙으로 나뉜다.
> - **트랙 A (Live Trading)**: STEP 4(pause off) 진행 → 실주문 가능 상태 진입
> - **트랙 B (AI-First / No-Trade)**: STEP 4 진행하지 않음 — pause=true 유지, AI 평가만 수행
>
> **현재 모드는 트랙 B. STEP 4는 별도 의사결정 후에만 진행.**

- `docs/live_transition_checklist.md`를 단계별로 안내/추적
- 진행도 Redis 기록:
  - `ops:live:step`
  - `ops:live:step_ts:{n}`

---

### 3.7 `/claw kill-switch` — 긴급정지
- 즉시 `claw:pause:global=true`
- Telegram 알림 전송
- Emergency Flatten은 Phase 9에서 별도 설계/구현

---

## 4. 구현 방식(중요) — “OpenClaw 커스텀 스킬” 기반

### 4.1 원칙
- OpenClaw 스킬은 내부적으로 **Shell 명령을 안전하게 실행**해 상태를 수집/제어한다.
- 직접 `pkill -f` 같은 패턴 매칭 kill은 **오동작 위험**이 있어 지양한다.

### 4.2 Redis 조회
- Docker 사용 중이면:
  - `docker exec claw-redis redis-cli GET claw:pause:global`
  - `docker exec claw-redis redis-cli TTL gen:runner:lock`
  - `docker exec claw-redis redis-cli LLEN mark_hist:KR:005930`
- (또는) 로컬 redis-cli 직접 접근(구성에 따라 선택)

### 4.3 프로세스 확인
- `ps aux | grep ...` / `pgrep -fl ...` 등으로 상태 확인
- “정확한 식별자”를 사용해 오탐을 줄인다(예: 실행 인자 포함)

### 4.4 재기동(중간 해법 권장: 스크립트 기반)
> supervisor가 없더라도 안전하게 재기동하려면 **스크립트 기반 재기동**이 좋다.

- `scripts/restart_market_data_runner.sh`
- `scripts/restart_signal_generator_runner.sh`
- `scripts/restart_order_watcher.sh`
- `scripts/restart_all.sh` (단, 포지션 없을 때만)

OpenClaw는 스킬에서 위 스크립트만 호출한다:
- `bash scripts/restart_market_data_runner.sh`

**장점**
- pkill 패턴 오탐을 피함
- supervisor 없이도 재현성 있는 재기동 가능

### 4.5 supervisor 도입(권장, 필수 아님)
- 도입 시 재기동이 가장 안정적:
  - `supervisorctl restart market_data_runner`
- v1.2에서는 “권장”으로 두고, Phase 9 킥오프에서 채택 여부 확정

---

## 5. 운영 이벤트 로그(필수)

원격 운영 인터페이스에서는 audit이 필수다.

- `ops:event:{timestamp}` → JSON:
  - user/chat_id
  - command
  - args
  - result(ok/fail)
  - metadata(예: md_age, lock_ttl 등)
- 실패도 반드시 기록

---

## 6. restart all / kill-switch 정책(필수 가드)

### 6.1 restart all guard
- open position 존재 시:
  - `restart all` 거부
  - 안내: “Restart blocked: open positions exist.”

### 6.2 kill-switch 정책
- 즉시 pause=true
- (Phase 9) Emergency Flatten 설계:
  - position_index 순회
  - 시장가 청산
  - 체결 확인/타임아웃
  - 실패 시 fallback 정책

---

## 7. 구현 우선순위(권장)

> AI-First 모드 기준 — 실주문 관련 명령은 나중에 검증

1) `/claw status`
2) `/claw ai status` (AI-First 모드에서 핵심 확인 명령)
3) `/claw pause on/off` (2단 확인+TTL)
4) audit 로그(항상 기록)
5) `/claw ps`
6) `/claw kill-switch`
7) `/claw restart ...` (스크립트 기반 중간 해법 → 필요 시 supervisor)

---

## 8. 완료 기준(Definition of Done)

- `/claw status`만으로 5초 내 운영 판단 가능
- `pause off`는 안전장치 없이는 실행 불가
- 모든 명령은 audit 로그에 남음
- `restart all`은 포지션 있으면 차단
- kill-switch는 즉시 pause=true + Telegram 알림

---

## 부록: `/claw status` 예시 출력

- PAUSE: true ✅
- KR md_age: 19s ✅ (threshold 180s)
- US md_age: 22s ✅ (threshold 180s)
- lock TTL: 80s ✅
- mark_hist:KR:005930 len=134 ✅
- mark_hist:US:AAPL len=28 ✅
- md_error_delta_today: KR=0, US=0 ✅
- ai_call_count_today: KR=0, US=0 ✅
