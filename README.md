<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

---

# TradingAgents: 멀티 에이전트 LLM 데이 트레이딩 시스템

> TradingAgents 기반 Discord 봇으로, **실시간 스코어링(09:30) → 상위 AI 분석 → 자동 매수 → 장 마감 전 자동 매도(15:20)**를 수행하는 완전 자동 데이 트레이딩 시스템입니다.

---

## 목차

- [시스템 개요](#시스템-개요)
- [아키텍처](#아키텍처)
  - [에이전트 팀 구조](#에이전트-팀-구조)
  - [Discord 봇 흐름](#discord-봇-흐름)
  - [한국투자증권 API 연동](#한국투자증권-api-연동)
- [설치](#설치)
- [환경 설정 (.env)](#환경-설정-env)
- [사용법](#사용법)
  - [Discord 봇 명령어](#discord-봇-명령어)
  - [자동 스케줄](#자동-스케줄)
  - [Python 직접 사용](#python-직접-사용)
  - [CLI 사용](#cli-사용)
- [지원 LLM 모델](#지원-llm-모델)
- [파일 구조](#파일-구조)
- [안전장치](#안전장치)
- [Citation](#citation)

---

## 시스템 개요

이 시스템은 **5개의 전문 AI 에이전트 팀**이 협업하여 주식을 분석하고, Discord를 통해 사용자와 상호작용하며, 한국투자증권 API로 실제 매매까지 연결하는 엔드투엔드 자동 투자 플랫폼입니다.

### 전체 흐름 (데이 트레이딩)

```
🌅 매일 아침 (09:30 KST) ─ 스코어링 + AI 분석 + 자동 매수
┌─────────────────────────────────────────────────┐
│  1. 실시간 KIS 순위 API 4종 멀티시그널 스코어링  │
│     ├─ 거래량 순위 (+10점)                        │
│     ├─ 체결강도 순위 (≥120: +25점)                │
│     ├─ 등락률 순위 (0~3%: +20점)                  │
│     └─ 대량체결 매수 순위 (+15점)                  │
│  2. 상위 5개만 순차 AI 분석 (~25분, BUY만 수집)     │
│     ├─ 애널리스트 4명 (시장/소셜/뉴스/펀더멘털)    │
│     ├─ 리서치팀 토론 (강세 vs 약세)                │
│     ├─ 트레이더 투자계획 수립                      │
│     ├─ 리스크 관리팀 (공격/보수/중립)              │
│     └─ 포트폴리오 매니저 최종 결정                 │
│  3. BUY 종목 통장 전액 ÷ 종목수 균등분배 → 자동 매수 │
└─────────────────────────────────────────────────┘
        ↓  (장중 30분 간격 손절/익절 감시)
📅 매일 오후 (15:20 KST) ─ 자동 매도
┌─────────────────────────────────────────────────┐
│  4. 보유 전종목 전량 시장가 매도                   │
│  5. 실패 시 60초 후 1회 자동 재시도                │
│  6. 일일 손익 요약 + 누적 승률 Discord 보고        │
└─────────────────────────────────────────────────┘
```

> **핵심**: 스코어링(무료, ~5초)으로 먼저 걸러내고, AI(유료, ~5분/종목)는 상위 5개만 분석.
> 일일 토큰 비용 ~$2.5 (월 ~$50)

---

## 아키텍처

### 에이전트 팀 구조

시스템은 실제 트레이딩 회사의 조직 구조를 모방합니다:

#### 1단계: 애널리스트팀 (4명이 동시에 분석)

| 에이전트 | 역할 | 데이터 소스 |
|----------|------|-------------|
| 📊 **시장 애널리스트** | 기술적 지표 분석 (MACD, RSI, 볼린저 밴드, 이동평균선) | yfinance |
| 💬 **소셜 미디어 애널리스트** | SNS 감성 분석, 투자자 심리 평가 | yfinance 뉴스 |
| 📰 **뉴스 애널리스트** | 글로벌 뉴스, 내부자 거래, 거시경제 이벤트 분석 | yfinance 뉴스 |
| 📈 **펀더멘털 애널리스트** | 재무제표, 대차대조표, 현금흐름표, 손익계산서 분석 | yfinance |

#### 2단계: 리서치팀 (토론)

| 에이전트 | 역할 |
|----------|------|
| 🟢 **강세 리서처** | 매수 근거를 제시하고 옹호 |
| 🔴 **약세 리서처** | 리스크와 매도 근거를 제시 |
| ⚖️ **리서치 매니저** | 양측 토론을 심판하고 최종 리서치 결론 도출 |

- `max_debate_rounds` 설정에 따라 여러 라운드의 토론 진행

#### 3단계: 트레이딩팀

| 에이전트 | 역할 |
|----------|------|
| 🏦 **트레이더** | 애널리스트+리서치 결과를 종합하여 구체적 투자 계획 수립 |

#### 4단계: 리스크 관리팀 (3자 토론)

| 에이전트 | 역할 |
|----------|------|
| 🔥 **공격적 리스크 매니저** | 높은 수익을 위한 공격적 관점 |
| 🛡️ **보수적 리스크 매니저** | 자본 보전 중심의 보수적 관점 |
| ⚖️ **중립적 리스크 매니저** | 균형 잡힌 리스크-수익 분석 |

#### 5단계: 최종 결정

| 에이전트 | 역할 |
|----------|------|
| 💼 **포트폴리오 매니저** | 모든 보고서를 검토하고 **BUY / SELL / HOLD** 최종 결정 |

### Discord 봇 흐름

```
Discord 명령어 입력
    ↓
[채널 권한 확인] → 미허용 채널이면 차단
    ↓
[분석 잠금 확인] → 이미 분석 중이면 대기 메시지
    ↓
[분석 실행] → run_in_executor (비동기 래핑)
    ↓
[결과 Embed 전송]
  ├─ 🟢 BUY → 초록색 Embed + 매수 확인 버튼
  ├─ 🔴 SELL → 빨간색 Embed
  └─ 🟡 HOLD → 주황색 Embed
    ↓
[전체 보고서 .md 파일 첨부]
    ↓
[매수 버튼 클릭 시] → KIS API 시장가 매수 → 체결 결과 전송
```

### 한국투자증권 API 연동

`kis_client.py`가 한국투자증권 REST API를 래핑합니다:

| 기능 | API | 설명 |
|------|-----|------|
| **토큰 발급** | `POST /oauth2/tokenP` | OAuth2 access token 자동 발급/갱신 |
| **잔고 조회** | `GET /trading/inquire-balance` | 보유종목, 평가손익, 예수금 조회 |
| **현재가 조회** | `GET /quotations/inquire-price` | 종목 실시간 현재가 |
| **매수** | `POST /trading/order-cash` | 시장가/지정가 매수 주문 |
| **매도** | `POST /trading/order-cash` | 시장가/지정가 매도 주문 |
| **시가총액 순위** | `GET /ranking/market-cap` | 코스피 시가총액 상위 종목 조회 |
| **거래량 순위** | `GET /quotations/volume-rank` | 거래량 상위 종목 (스코어링) |
| **체결강도 순위** | `GET /ranking/volume-power` | 매수/매도 체결강도 비율 |
| **등락률 순위** | `GET /ranking/fluctuation` | 등락률 상위 종목 |
| **대량체결 순위** | `GET /ranking/bulk-trans-num` | 기관/외국인 대량 매수 |
| **전량 매도** | `sell_all_holdings()` | 보유 전종목 일괄 시장가 매도 |

- **멀티시그널 스코어링**: 5개 순위 API를 종합하여 종목별 점수 산정
- **모의투자/실전 전환**: `KIS_VIRTUAL=true/false`로 제어
- 모의투자와 실전은 URL이 다름 (자동 처리)

---

## 설치

### 1. 레포 클론
```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

### 2. 가상환경 생성 & 활성화
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 3. 의존성 설치
```bash
pip install -r requirements.txt
```

### 4. 환경변수 설정
```bash
cp .env.example .env
# .env 파일을 열어서 API 키 입력
```

---

## 환경 설정 (.env)

```env
# ─── LLM 제공자 (사용하는 것만 설정) ───────────────────
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...
ANTHROPIC_API_KEY=sk-ant-...
XAI_API_KEY=xai-...
OPENROUTER_API_KEY=sk-or-...

# ─── Discord 봇 ────────────────────────────────────────
DISCORD_BOT_TOKEN=MTIz...           # 필수
DISCORD_CHANNEL_IDS=123456,789012   # 봇 동작 채널 (비우면 전체)

# ─── LLM 모델 설정 (선택) ──────────────────────────────
DEEP_THINK_LLM=gemini-3-flash-preview   # 깊은 추론용
QUICK_THINK_LLM=gemini-3-flash-preview  # 빠른 작업용
MAX_DEBATE_ROUNDS=1                      # 리서치 토론 라운드

# ─── 한국투자증권 API ──────────────────────────────────
KIS_APP_KEY=PSxxx...                # 앱키 (36자리)
KIS_APP_SECRET=xxx...               # 시크릿키 (180자리)
KIS_ACCOUNT_NO=12345678-01          # 계좌번호
KIS_VIRTUAL=true                    # true=모의투자, false=실전
KIS_MAX_ORDER_AMOUNT=1000000        # 수동(/분석,/대형주,/매수) 1회 매수 예산 상한

# ─── 미국(US) 거래 설정 ───────────────────────────────
ENABLE_US_TRADING=false             # 미국 자동주문 활성화
US_MAX_ORDER_AMOUNT=5000            # 수동(/분석,/매수) 미국 매수 예산 상한 (USD)
US_EXCHANGE_SEARCH_ORDER=NASD,NYSE,AMEX
US_WATCHLIST=AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,AVGO,QQQ,SPY

# ─── 데이 트레이딩 설정 ──────────────────────────────
DAY_TRADE_PICKS=5                   # 매일 매수할 종목 수 (기본 5)
AUTO_BUY_TIME=09:30                 # 자동 매수 시각 KST (기본 09:30)
AUTO_SELL_TIME=15:20                # 자동 매도 시각 KST (기본 15:20)
REPORTS_DIR=reports                 # 분석 보고서 저장 경로 (도커: /app/reports)
AUTO_REPORT_UPLOAD=true             # 자동매매 분석 보고서 디스코드 업로드 여부

# ─── 미국 데이 트레이딩 설정 (뉴욕시간 ET) ─────────────
US_DAY_TRADE_PICKS=5                # 미국 자동매수 종목 수
US_AUTO_BUY_TIME=09:35              # 미국 자동 매수 시각 ET
US_AUTO_SELL_TIME=15:50             # 미국 자동 매도 시각 ET

# ─── 손절/익절 설정 ──────────────────────────────
STOP_LOSS_PCT=-5.0                  # 손절 라인 (%, 기본 -5%)
TAKE_PROFIT_PCT=10.0                # 익절 라인 (%, 기본 10%)
MONITOR_INTERVAL_MIN=30             # 모니터링 간격 (분, 기본 30분)
```

### 한국투자증권 API 키 발급 방법
1. [한국투자증권 홈페이지](https://www.koreainvestment.com/)에 로그인
2. **API 포탈** → **API 신청** → 앱키 발급
3. 모의투자용과 실전용 앱키가 **별도**이므로, 테스트 시 모의투자 앱키를 먼저 발급

### Discord 봇 토큰 발급 방법
1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application
2. **Bot** 탭 → Token 복사
3. **OAuth2** → URL Generator → `bot` + `applications.commands` 스코프 선택
4. 권한: Send Messages, Embed Links, Attach Files, Use Slash Commands
5. 생성된 초대 URL로 서버에 봇 초대

### Discord 채널 ID 확인 방법
1. Discord 설정 → 고급 → **개발자 모드** ON
2. 채널 우클릭 → **채널 ID 복사**

---

## 사용법

### Discord 봇 실행

```bash
python bot.py
```

성공 시 콘솔 출력:
```
✅ TradingBot#1234 로그인 완료!
   서버 수: 1
  동기화된 슬래시 명령 수: 9
  슬래시 명령: /분석, /대형주, /잔고, /매수, /매도, /상태, /봇정보, /스코어링, /수익
   KIS: ✅ 설정됨
   모드: 🧪 모의투자
   데이 트레이딩: 매수 09:30 / 매도 15:20 KST
   매수 종목 수: 5개 | 예산: 통장 전액
   손절: -5.0% | 익절: 10.0%
   모니터링: 30분 간격
   허용 채널: {123456789012345678}
```

### Discord 봇 명령어

#### 분석 명령

| 명령 | 설명 | 예시 |
|------|------|------|
| `/분석 <티커>` | 단일 종목 AI 분석 | `/분석 AAPL` |
| `/분석 <티커> <날짜>` | 특정 날짜 기준 분석 | `/분석 005930 2026-02-13` |

- 분석 완료 시 **색상 코딩된 Embed** (BUY=🟢, SELL=🔴, HOLD=🟡) 표시
- **전체 보고서**는 `.md` 파일로 첨부
- 보고서는 디스크에도 저장됨 (`REPORTS_DIR`, 기본 `reports/`)
- 티커는 **시장 자동판단**: `005930`(KR), `AAPL`(US)
- **BUY** → 매수 확인 버튼 표시 (KIS 설정 시)
- **SELL + 해당 종목 보유 중** → 매도 확인 버튼 표시
- **HOLD / SELL(미보유)** → Embed만 표시

#### 대형주 TOP5 분석

| 명령 | 설명 |
|------|------|
| `/대형주` | 코스피 시가총액 TOP5 조회 → 전체 AI 분석 |
| `/대형주 <날짜>` | 특정 날짜 지정 |

**실행 과정:**
1. KIS 공식 API로 코스피 시가총액 상위 5개 종목 조회
2. TOP5 목록을 Embed로 표시 (종목명, 코드, 현재가, 시가총액)
3. 각 종목을 **순차적으로** AI 분석 (진행률 표시: `[1/5]`, `[2/5]`...)
4. 분석 완료 후 BUY 판정 종목에 **매수 확인 버튼** 노출 (정규장/거래일에만)
5. SELL 판정 + 보유 종목에 **매도 확인 버튼** 노출
6. `✅ 매수 확인` / `🔴 매도 확인` 클릭 → KIS API 시장가 주문 실행
7. `⏭️ 건너뛰기` / `취소` 클릭 → 해당 종목 스킵

**예산 분배 규칙:**
- `/대형주` 수동 실행: `KIS_MAX_ORDER_AMOUNT` 상한을 BUY 종목 수로 균등 분할 (테스트 모드)
- 자동매수(09:30): 예수금 전액을 BUY 종목 수로 균등 분할 (실전 데이 트레이딩)

**장외/휴장 정책:**
- 장이 닫힌 시간(또는 휴장일)에는 `/대형주`의 BUY 버튼을 비활성화하고 추천 종목만 안내합니다.

#### 계좌 관리

| 명령 | 설명 |
|------|------|
| `/잔고` | 보유종목, 평가손익, 예수금 조회 |
| `/매수 <종목코드> [수량]` | 시장가 매수 (수량 생략 시 예산 상한 기준 자동 계산, 확인 버튼) |
| `/매도 <종목코드>` | 전량 시장가 매도 (확인 버튼) |
| `/매도 <종목코드> <수량>` | 지정 수량 매도 (확인 버튼) |
| `/스코어링 [시장]` | 현재 KR/US 자동매매 스코어링 룰 조회 |
| `/수익` | 누적 실현손익(통화분리), 승률, 종목별 수익 조회 |
| `/상태` | 오늘 자동매매 실행 상태 조회 |
| `/봇정보` | 스케줄/설정/계좌/실행이력 통합 조회 |

- 잔고 조회 시 각 종목의 **시장(KR/US)**, **평균매수가 → 현재가**, **손익금액**, **수익률** 표시
- `/수익`은 KRW/USD를 분리 집계하여 환산 왜곡을 방지
- 매도 시 **확인/취소 버튼**이 나타나며, 확인 클릭 시에만 실행

### 자동 스케줄 (데이 트레이딩)

`DISCORD_CHANNEL_IDS`가 설정되어 있으면, 매일 자동으로 **매수 → 감시 → 매도** 사이클을 실행합니다.

#### 아침 자동매수 (기본 09:30 KST)

**실행 순서:**
1. **실시간 멀티시그널 스코어링** — KIS 순위 API 4종(거래량·체결강도·등락률·대량체결) 조회 (~5초)
2. **상위 5개 후보 순차 AI 분석** — BUY 판정 종목만 수집 (~25분)
3. **자동 매수** — 통장 전액 ÷ BUY 종목 수 균등분배 → 시장가 매수

> 스코어링(무료)으로 먼저 걸러내고, AI(유료)는 상위 5개만 분석 → 일일 토큰 비용 ~$2.5

#### 장중 손절/익절 모니터링 (30분 간격)

| 조건 | 동작 |
|------|------|
| 수익률 ≤ `-5%` (손절 라인) | 🚨 자동 시장가 매도 + Discord 알림 |
| 수익률 ≥ `+10%` (익절 라인) | 🎉 자동 시장가 매도 + Discord 알림 |

- 임계값은 `.env`의 `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`로 설정
- **확인 없이 자동 매도** → 손실 확대/이익 환수 방지

#### 오후 전량매도 (기본 15:20 KST)

1. 보유 전종목 전량 시장가 매도 (`sell_all_holdings()`)
2. 실패 종목 60초 후 1회 자동 재시도
3. 종목별 손익 + 일일 합산 + 누적 승률 Discord 보고
4. 모든 거래 SQLite DB에 자동 기록 (`/수익`으로 조회)

#### 미국 자동 스케줄 (ENABLE_US_TRADING=true)

1. **미국 자동매수**: `US_AUTO_BUY_TIME` (기본 09:35 ET)
2. **미국 자동매도**: `US_AUTO_SELL_TIME` (기본 15:50 ET)
3. 미국 자동매수 후보: **KIS 해외 랭킹 API 우선**, 실패 시 `US_WATCHLIST` 기반 yfinance fallback
4. 상태키 분리: `us_morning_buy`, `us_afternoon_sell`

### Python 직접 사용

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-3-flash-preview"
config["quick_think_llm"] = "gemini-3-flash-preview"

ta = TradingAgentsGraph(debug=True, config=config)

# 분석 실행 → (전체상태, BUY/SELL/HOLD) 반환
final_state, decision = ta.propagate("005930", "2026-02-13")
print(decision)  # "BUY", "SELL", or "HOLD"

# (선택) 실제 결과로 에이전트 학습
# ta.reflect_and_remember(1000)  # 수익률 입력
```

### CLI 사용

```bash
python -m cli.main
```

터미널에서 대화형으로 종목, 날짜, LLM 모델 등을 선택하고 분석을 실행합니다.

---

## 지원 LLM 모델

| 제공자 | 모델 |
|--------|------|
| **OpenAI** | `gpt-5.2`, `gpt-5.1`, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano`, `o4-mini`, `o3`, `o3-mini`, `o1`, `gpt-4o`, `gpt-4o-mini` |
| **Google Gemini** | `gemini-3-pro-preview`, `gemini-3-flash-preview`, `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.0-flash`, `gemini-2.0-flash-lite` |
| **Anthropic Claude** | `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5`, `claude-opus-4-1-20250805`, `claude-sonnet-4-20250514`, `claude-3-7-sonnet-20250219`, `claude-3-5-sonnet-20241022`, `claude-3-5-haiku-20241022` |
| **xAI Grok** | `grok-4-1-fast`, `grok-4-1-fast-reasoning`, `grok-4`, `grok-4-0709` |
| **Ollama** | 모든 로컬 모델 (제한 없음) |
| **OpenRouter** | 모든 라우팅 모델 (제한 없음) |

---

## 파일 구조

```
TradingAgents/
├── bot.py                      # Discord 봇 (메인 엔트리포인트)
├── kis_client.py               # 한국투자증권 API 클라이언트 (매매 + 시총 순위)
├── trade_history.py            # 매매 이력 DB (SQLite) — 수익 추적
├── main.py                     # Python 직접 실행용 예시
├── .env                        # 환경변수 (비공개)
├── .env.example                # 환경변수 템플릿
├── requirements.txt            # Python 패키지 의존성
│
├── data/                       # SQLite DB 저장 (자동 생성)
│   └── trade_history.db        # 매매 이력 + 실현손익 기록
│
├── tradingagents/              # 핵심 프레임워크
│   ├── default_config.py       # 기본 설정값
│   ├── graph/                  # LangGraph 기반 에이전트 그래프
│   │   ├── trading_graph.py    # 메인 그래프 (TradingAgentsGraph)
│   │   ├── propagation.py      # 상태 초기화 & 전파
│   │   ├── signal_processing.py # BUY/SELL/HOLD 신호 추출
│   │   ├── reflection.py       # 학습 & 메모리 반영
│   │   └── setup.py            # 그래프 노드 연결
│   ├── agents/                 # 에이전트 정의
│   │   ├── analysts/           # 애널리스트 4명
│   │   ├── researchers/        # 강세/약세 리서처
│   │   ├── managers/           # 리서치/리스크 매니저
│   │   ├── trader/             # 트레이더
│   │   └── risk_mgmt/          # 리스크 관리팀
│   ├── dataflows/              # 데이터 수집 (yfinance, Alpha Vantage)
│   └── llm_clients/            # LLM 제공자별 클라이언트
│
├── cli/                        # 터미널 CLI 인터페이스
├── reports/                    # 생성된 분석 보고서
├── results/                    # 종목별 분석 결과
└── eval_results/               # 평가 로그
```

---

## 안전장치

| 항목 | 설명 |
|------|------|
| **데이 트레이딩 자동매수** | 매일 09:30 KST, 스코어링 → 상위 5개 AI분석 → BUY 종목 통장 전액 균등 매수 |
| **데이 트레이딩 자동매도** | 매일 15:20 KST, 보유 전종목 전량 시장가 매도 (1회 재시도) |
| **손절/익절 자동매도** | 30분 간격 감시, 임계값 도달 시 즉시 자동 매도 |
| **수동 매수 확인 버튼** | `/분석`, `/대형주`, `/매수`는 버튼 확인 후에만 매수 실행 (장외/휴장 시 BUY 버튼 비활성화) |
| **수동 예산 상한** | 수동 매수는 `KIS_MAX_ORDER_AMOUNT`/`US_MAX_ORDER_AMOUNT` 상한 내에서 주문 |
| **매매 이력 기록** | 모든 매수/매도를 SQLite DB에 자동 저장 (`/수익`으로 조회) |
| **재시작 중복 방지** | `daily_state` 테이블로 아침매수/오후매도/손절·익절 실행 여부를 일자별 기록해 중복 주문 방지 |
| **모의투자 모드** | `KIS_VIRTUAL=true`로 가상계좌에서 안전하게 테스트 |
| **채널 제한** | `DISCORD_CHANNEL_IDS`로 특정 채널에서만 봇 동작 |
| **동시 실행 방지** | 한 번에 하나의 분석만 실행 (asyncio Lock) |
| **버튼 타임아웃** | 매수 버튼 5분, 매도 버튼 2분 후 자동 만료 |

> ⚠️ **면책 조항**: 이 시스템은 연구 및 교육 목적으로 설계되었습니다. 실제 투자 결과는 LLM 모델, 시장 상황, 데이터 품질 등에 따라 달라질 수 있습니다. [투자 조언이 아닙니다.](https://tauric.ai/disclaimer/)

---

## Citation

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
