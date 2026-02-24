"""
TradingAgents Discord Bot
- 슬래시 명령: /분석, /대형주, /잔고, /매도, /상태, /봇정보, /수익
- 데이 트레이딩: 아침 자동매수 / 오후 자동매도 / 손절·익절 감시
- 한국투자증권 API 연동 매매
"""

import os
import asyncio
import datetime
import re
from pathlib import Path
from io import BytesIO
from zoneinfo import ZoneInfo

import discord
import yfinance as yf
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from kis_client import KISClient, format_krw, format_usd
from trade_history import (
    record_trade,
    record_pnl,
    get_total_pnl,
    get_total_pnl_by_currency,
    get_recent_pnl,
    get_ticker_summary,
    is_action_done, mark_action_done, get_daily_state,
)

load_dotenv()

# ─── Config ────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN이 .env에 설정되어 있지 않습니다.")

# 봇이 동작할 채널 ID (쉼표로 여러 개 지정 가능, 비워두면 모든 채널에서 동작)
# 예: DISCORD_CHANNEL_IDS=123456789012345678,987654321098765432
_channel_ids_raw = os.getenv("DISCORD_CHANNEL_IDS", "")
ALLOWED_CHANNEL_IDS: set[int] = {
    int(cid.strip()) for cid in _channel_ids_raw.split(",") if cid.strip()
}


def _is_allowed_channel(channel_id: int | None) -> bool:
    """채널 제한이 설정되어 있으면 허용된 채널인지 확인."""
    if channel_id is None:
        return False
    if not ALLOWED_CHANNEL_IDS:
        return True  # 설정 안 하면 모든 채널 허용
    return channel_id in ALLOWED_CHANNEL_IDS

# 손절/익절 임계값 (%)
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-5.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "10.0"))
MONITOR_INTERVAL_MIN = int(os.getenv("MONITOR_INTERVAL_MIN", "30"))

# 데이 트레이딩 설정
DAY_TRADE_PICKS = int(os.getenv("DAY_TRADE_PICKS", "5"))  # 매일 매수할 종목 수
AUTO_BUY_TIME = os.getenv("AUTO_BUY_TIME", "09:30")         # 자동 매수 시각 (HH:MM)
AUTO_SELL_TIME = os.getenv("AUTO_SELL_TIME", "15:20")        # 자동 매도 시각 (HH:MM)
_buy_h, _buy_m = (int(x) for x in AUTO_BUY_TIME.split(":"))
_sell_h, _sell_m = (int(x) for x in AUTO_SELL_TIME.split(":"))

# 미국 데이 트레이딩 설정
ENABLE_US_TRADING = os.getenv("ENABLE_US_TRADING", "false").lower() == "true"
US_DAY_TRADE_PICKS = int(os.getenv("US_DAY_TRADE_PICKS", "5"))
US_AUTO_BUY_TIME = os.getenv("US_AUTO_BUY_TIME", "09:35")
US_AUTO_SELL_TIME = os.getenv("US_AUTO_SELL_TIME", "15:50")
_us_buy_h, _us_buy_m = (int(x) for x in US_AUTO_BUY_TIME.split(":"))
_us_sell_h, _us_sell_m = (int(x) for x in US_AUTO_SELL_TIME.split(":"))

config = DEFAULT_CONFIG.copy()
config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", "gemini-3-flash-preview")
config["quick_think_llm"] = os.getenv("QUICK_THINK_LLM", "gemini-3-flash-preview")
config["max_debate_rounds"] = int(os.getenv("MAX_DEBATE_ROUNDS", "1"))
config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}


# ─── Bot Setup ─────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

_analysis_lock = asyncio.Lock()

# ─── KIS 클라이언트 초기화 ──────────────────────────────────
kis = KISClient()
KST = ZoneInfo("Asia/Seoul")
NY_TZ = ZoneInfo("America/New_York")
TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "reports"))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
AUTO_REPORT_UPLOAD = os.getenv("AUTO_REPORT_UPLOAD", "true").lower() == "true"
_analysis_symbol_cache: dict[str, str] = {}


def _log(level: str, event: str, message: str):
    now = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{level}] [{event}] {message}")


def _interaction_actor(interaction: discord.Interaction) -> str:
    user = interaction.user
    user_label = str(user) if user else "unknown"
    return f"user={user_label} channel={interaction.channel_id}"


def _latest_yf_close(symbol: str) -> float:
    """yfinance 심볼의 최근 종가 조회 (실패 시 0)."""
    try:
        hist = yf.Ticker(symbol).history(period="7d", interval="1d")
        if hist.empty or "Close" not in hist.columns:
            return 0.0
        close = hist["Close"].dropna()
        if close.empty:
            return 0.0
        return float(close.iloc[-1])
    except Exception:
        return 0.0


def _resolve_analysis_symbol(
    ticker: str,
    market: str | None = None,
    reference_price: float | None = None,
) -> str:
    """분석용 심볼 정규화.

    - US: 티커 그대로 사용
    - KR 6자리: .KS/.KQ 중 yfinance 데이터/가격 근접도로 자동 판별
    """
    t = (ticker or "").upper().strip()
    m = (market or kis.detect_market(t)).upper()
    if m != "KR":
        return t
    if t.endswith((".KS", ".KQ")):
        return t
    if not (t.isdigit() and len(t) == 6):
        return t

    if t in _analysis_symbol_cache:
        return _analysis_symbol_cache[t]

    ref = float(reference_price or 0)
    if ref <= 0 and kis.is_configured:
        try:
            ref = float(kis.get_price(t, market="KR"))
        except Exception:
            ref = 0.0

    candidates = [f"{t}.KS", f"{t}.KQ"]
    prices = {sym: _latest_yf_close(sym) for sym in candidates}
    available = {sym: px for sym, px in prices.items() if px > 0}

    if not available:
        resolved = f"{t}.KS"
    elif len(available) == 1:
        resolved = next(iter(available))
    elif ref > 0:
        resolved = min(
            available.keys(),
            key=lambda sym: abs(available[sym] - ref) / max(ref, 1.0),
        )
    else:
        resolved = max(available.keys(), key=lambda sym: available[sym])

    _analysis_symbol_cache[t] = resolved
    if resolved != f"{t}.KS":
        _log("INFO", "ANALYSIS_SYMBOL_RESOLVED", f"ticker={t} resolved={resolved}")
    return resolved


def _yf_ticker(ticker: str, reference_price: float | None = None) -> str:
    """TradingAgents에 전달할 yfinance 심볼 반환."""
    t = (ticker or "").upper().strip()
    market = kis.detect_market(t)
    if market == "KR":
        return _resolve_analysis_symbol(t, market="KR", reference_price=reference_price)
    return t


def _market_of_ticker(ticker: str) -> str:
    return kis.detect_market(ticker)


def _currency_of_market(market: str) -> str:
    return "USD" if market == "US" else "KRW"


def _format_money(amount: float, currency: str) -> str:
    if currency == "USD":
        return format_usd(amount)
    return f"{amount:,.0f}원"


def _save_report_markdown(
    report_text: str,
    *,
    market: str,
    ticker: str,
    trade_date: str,
    scope: str,
) -> Path:
    """분석 보고서를 reports 디렉터리에 저장."""
    safe_market = re.sub(r"[^A-Z0-9_-]", "_", (market or "NA").upper())
    safe_ticker = re.sub(r"[^A-Z0-9._-]", "_", (ticker or "UNKNOWN").upper())
    safe_scope = re.sub(r"[^A-Z0-9_-]", "_", (scope or "analysis").upper())
    stamp = datetime.datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    filename = f"{stamp}_{safe_scope}_{safe_market}_{safe_ticker}_{trade_date}.md"
    path = REPORTS_DIR / filename
    path.write_text(report_text, encoding="utf-8")
    _log("INFO", "REPORT_SAVED", f"path={path}")
    return path


def _prepare_report_attachment(
    report_text: str,
    *,
    market: str,
    ticker: str,
    trade_date: str,
    scope: str,
) -> tuple[discord.File, Path | None]:
    """디스코드 업로드용 파일 객체와 (가능하면) 로컬 저장 경로를 반환."""
    try:
        saved_path = _save_report_markdown(
            report_text,
            market=market,
            ticker=ticker,
            trade_date=trade_date,
            scope=scope,
        )
        return discord.File(str(saved_path), filename=saved_path.name), saved_path
    except Exception as e:
        _log(
            "ERROR",
            "REPORT_SAVE_FAIL",
            f"scope={scope} market={market} ticker={ticker} error={str(e)[:160]}",
        )
        fallback_name = (
            f"{scope}_{market}_{re.sub(r'[^A-Z0-9._-]', '_', ticker.upper())}_{trade_date}.md"
        )
        return discord.File(fp=BytesIO(report_text.encode("utf-8")), filename=fallback_name), None


def _parse_trade_date(date_text: str | None) -> str:
    """사용자 입력 날짜를 YYYY-MM-DD로 정규화."""
    if not date_text:
        return str(datetime.date.today())
    try:
        parsed = datetime.datetime.strptime(date_text.strip(), "%Y-%m-%d").date()
        return parsed.isoformat()
    except ValueError as exc:
        raise ValueError("날짜 형식이 올바르지 않습니다. YYYY-MM-DD 형식으로 입력하세요.") from exc


def _validate_ticker_format(ticker: str) -> str | None:
    """티커 문자열 형식 검증."""
    if not ticker:
        return "티커를 입력해주세요."
    if not TICKER_PATTERN.fullmatch(ticker):
        return "티커 형식이 올바르지 않습니다. 예: AAPL, BRK-B, 005930"
    return None


def _ticker_has_market_data(ticker: str) -> bool:
    """실제 종목 데이터가 존재하는지 확인."""
    market = _market_of_ticker(ticker)
    kr_price = 0.0

    # 한국 6자리 종목은 KIS 시세를 우선 확인
    if market == "KR" and kis.is_configured:
        try:
            kr_price = float(kis.get_price(ticker, market="KR"))
            if kr_price <= 0:
                return False
        except Exception as e:
            _log("WARN", "TICKER_VALIDATE_KIS_FAIL", f"ticker={ticker} error={str(e)[:160]}")

    # 글로벌 티커 포함 yfinance로 최종 확인
    try:
        yf_symbol = _yf_ticker(ticker, reference_price=kr_price if market == "KR" else None)
        hist = yf.Ticker(yf_symbol).history(period="1mo", interval="1d")
        if hist.empty or "Close" not in hist.columns:
            return False
        return not hist["Close"].dropna().empty
    except Exception:
        return False


async def _validate_analysis_ticker(ticker: str) -> tuple[bool, str]:
    """분석 요청 전에 티커 유효성 검증."""
    format_error = _validate_ticker_format(ticker)
    if format_error:
        return False, format_error

    loop = asyncio.get_running_loop()
    has_data = await loop.run_in_executor(None, _ticker_has_market_data, ticker)
    if not has_data:
        return (
            False,
            f"`{ticker}` 종목 데이터를 찾지 못했습니다. "
            "오타 여부와 거래소 접미사(예: 005930, AAPL, 7203.T)를 확인해주세요.",
        )
    return True, ""


def _is_market_day(market: str = "KR") -> bool:
    """시장 거래일 여부 확인."""
    market = market.upper()
    now = datetime.datetime.now(NY_TZ if market == "US" else KST).date()
    if market == "US":
        return kis.is_market_open(now, market="US")

    if now.weekday() >= 5:
        return False
    if kis.is_configured:
        return kis.is_market_open(now, market="KR")
    return True


def _is_market_open_now(market: str = "KR") -> bool:
    """시장 정규장 시간 여부 확인."""
    return kis.is_market_open_now(market=market.upper())


# ─── Helper: 보고서 생성 ──────────────────────────────────────
def _build_report_text(
    final_state: dict,
    ticker: str,
    *,
    market: str | None = None,
    analysis_symbol: str | None = None,
) -> str:
    """final_state에서 Markdown 보고서 텍스트 생성."""
    sections: list[str] = []

    analyst_parts = []
    if final_state.get("market_report"):
        analyst_parts.append(("📊 시장 애널리스트", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analyst_parts.append(("💬 소셜 미디어 애널리스트", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analyst_parts.append(("📰 뉴스 애널리스트", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analyst_parts.append(("📈 펀더멘털 애널리스트", final_state["fundamentals_report"]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. 애널리스트팀 보고서\n\n{content}")

    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research_parts = []
        if debate.get("bull_history"):
            research_parts.append(("🟢 강세 애널리스트", debate["bull_history"]))
        if debate.get("bear_history"):
            research_parts.append(("🔴 약세 애널리스트", debate["bear_history"]))
        if debate.get("judge_decision"):
            research_parts.append(("⚖️ 리서치 매니저", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. 리서치팀 판단\n\n{content}")

    if final_state.get("trader_investment_plan"):
        sections.append(
            f"## III. 트레이딩팀 계획\n\n### 🏦 트레이더\n{final_state['trader_investment_plan']}"
        )

    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_parts = []
        if risk.get("aggressive_history"):
            risk_parts.append(("🔥 공격적 애널리스트", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_parts.append(("🛡️ 보수적 애널리스트", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_parts.append(("⚖️ 중립적 애널리스트", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. 리스크 관리팀 결정\n\n{content}")

        if risk.get("judge_decision"):
            sections.append(
                f"## V. 포트폴리오 매니저 결정\n\n### 💼 포트폴리오 매니저\n{risk['judge_decision']}"
            )

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_lines = [f"# 📋 트레이딩 분석 보고서: {ticker}", "", f"생성일시: {now}"]
    if market:
        header_lines.append(f"시장: {market}")
    if analysis_symbol and analysis_symbol.upper() != ticker.upper():
        header_lines.append(f"분석 심볼: {analysis_symbol}")
    header = "\n".join(header_lines) + "\n\n"
    return header + "\n\n".join(sections)


def _extract_decision_summary(
    final_state: dict,
    decision: str,
    ticker: str,
    market: str | None = None,
) -> str:
    """Discord Embed에 넣을 요약 문자열 생성."""
    market = (market or _market_of_ticker(ticker)).upper()
    lines = [f"**시장:** {market}", f"**종목:** {ticker}", f"**최종 결정:** {decision}"]
    if final_state.get("investment_plan"):
        plan = final_state["investment_plan"]
        if len(plan) > 300:
            plan = plan[:300] + "…"
        lines.append(f"**투자 계획 요약:**\n{plan}")
    return "\n".join(lines)


async def _show_trade_button(
    channel: discord.abc.Messageable,
    ticker: str,
    decision: str,
    market: str | None = None,
):
    """개별 분석 결과에 따라 BUY/SELL 확인 버튼을 표시한다."""
    if not kis.is_configured:
        return

    market = (market or _market_of_ticker(ticker)).upper()
    currency = _currency_of_market(market)
    if market == "US" and not kis.enable_us_trading:
        await channel.send(
            "ℹ️ 미국 자동주문은 비활성화되어 있습니다. `.env`의 "
            "`ENABLE_US_TRADING=true` 설정 후 사용하세요."
        )
        return
    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
    loop = asyncio.get_running_loop()

    if decision.upper() == "BUY":
        if not _is_market_open_now(market):
            _log("INFO", "MANUAL_BUY_BLOCKED", f"market={market} ticker={ticker}")
            await channel.send(
                f"ℹ️ `{ticker}`({market}) BUY 신호이지만 현재 장외/휴장이라 "
                "수동 매수 버튼을 표시하지 않습니다."
            )
            return
        try:
            price = await loop.run_in_executor(None, kis.get_price, ticker, market)
            if price <= 0:
                return
            budget = kis.us_max_order_amount if market == "US" else kis.max_order_amount
            qty = int(budget // price)
            if qty <= 0:
                await channel.send(
                    f"⚠️ {ticker} — 예산({_format_money(budget, currency)}) 대비 "
                    f"현재가({_format_money(price, currency)})가 높아 매수 불가"
                )
                return
            view = BuyConfirmView(
                ticker=ticker,
                name=ticker,
                qty=qty,
                price=price,
                market=market,
                currency=currency,
            )
            embed = discord.Embed(
                title=f"🛒 {ticker} 매수 확인",
                description=(
                    f"**시장:** {market}\n"
                    f"**종목:** `{ticker}`\n"
                    f"**현재가:** {_format_money(price, currency)}\n"
                    f"**매수 수량:** {qty}주\n"
                    f"**예상 금액:** {_format_money(qty * price, currency)}\n\n"
                    f"매수하시겠습니까?"
                ),
                color=0x00FF00,
            )
            embed.set_footer(text=f"{mode_label} | {currency}")
            await channel.send(embed=embed, view=view)
        except Exception:
            pass

    elif decision.upper() == "SELL":
        try:
            balance_data = await loop.run_in_executor(None, kis.get_balance, market)
            holding = next(
                (
                    h for h in balance_data["holdings"]
                    if h["ticker"] == ticker and h.get("market", market) == market
                ),
                None,
            )
            if not holding or holding["qty"] <= 0:
                return
            view = SellConfirmView(
                ticker=ticker,
                name=holding["name"],
                qty=holding["qty"],
                avg_price=holding["avg_price"],
                market=market,
                currency=currency,
                exchange=holding.get("exchange", ""),
            )
            embed = discord.Embed(
                title=f"🔴 {holding['name']} 매도 확인",
                description=(
                    f"**시장:** {market}\n"
                    f"**종목:** {holding['name']} (`{ticker}`)\n"
                    f"**보유:** {holding['qty']}주 "
                    f"(평균 {_format_money(holding['avg_price'], currency)})\n"
                    f"**현재가:** {_format_money(holding['current_price'], currency)}\n"
                    f"**손익:** {_format_money(holding['pnl'], currency)} "
                    f"({holding['pnl_rate']:+.2f}%)\n\n"
                    f"AI가 SELL을 권고합니다. 전량 매도하시겠습니까?"
                ),
                color=0xFF0000,
            )
            embed.set_footer(text=f"{mode_label} | {currency}")
            await channel.send(embed=embed, view=view)
        except Exception:
            pass


# ─── Helper: 멀티시그널 스코어링 ────────────────────────────
async def _compute_stock_scores(count: int = 10) -> list[dict]:
    """
    KIS 순위 API 5종을 조합해 종목별 점수를 매기고 상위 후보 반환.

    스코어링 기준:
      - 거래량 top30 진입: +10
      - 체결강도 ≥120: +25 / 100≤x<120: +15
      - 등락률 0%<x≤3%: +20 / 3%<x≤7%: +10
      - 대량체결 매수상위: +15
      - 시가총액 top30: +5 (대형주 보너스)

    필터: 등락률 >10% 또는 <-3% → 제외

    Returns:
        [{"ticker", "name", "price", "score", "signals": [str]}, ...] 점수 내림차순
    """
    loop = asyncio.get_running_loop()

    # 랭킹 API는 VTS에서 5xx가 자주 발생해 직렬 호출로 부하를 낮춘다.
    volume_list = await loop.run_in_executor(None, kis.get_volume_rank, 30)
    power_list = await loop.run_in_executor(None, kis.get_volume_power, 30)
    fluct_list = await loop.run_in_executor(None, kis.get_fluctuation_rank, 30)
    bulk_list = await loop.run_in_executor(None, kis.get_bulk_trans, 30)
    cap_list = await loop.run_in_executor(None, kis.get_top_market_cap, 30)

    # 인덱스 매핑: ticker → 데이터
    volume_map = {s["ticker"]: s for s in volume_list}
    power_map = {s["ticker"]: s for s in power_list}
    fluct_map = {s["ticker"]: s for s in fluct_list}
    bulk_map = {s["ticker"]: s for s in bulk_list}
    cap_map = {s["ticker"]: s for s in cap_list}

    # 모든 후보 종목 수집
    all_tickers: set[str] = set()
    for m in (volume_map, power_map, fluct_map, bulk_map, cap_map):
        all_tickers.update(m.keys())

    scored: list[dict] = []
    for ticker in all_tickers:
        score = 0
        signals: list[str] = []
        name = ""
        price = 0
        prdy_ctrt = 0.0

        # 등락률 확인 (여러 소스에서 가져오기)
        for m in (fluct_map, volume_map, power_map, bulk_map, cap_map):
            if ticker in m:
                prdy_ctrt = m[ticker].get("prdy_ctrt", 0.0)
                name = name or m[ticker].get("name", "")
                price = price or m[ticker].get("price", 0)
                break

        # 필터: 상한가 근접 or 급락 제외
        if prdy_ctrt > 10.0 or prdy_ctrt < -3.0:
            continue

        # 1) 거래량 시그널
        if ticker in volume_map:
            score += 10
            signals.append(f"거래량 {volume_map[ticker]['rank']}위")

        # 2) 체결강도 시그널
        if ticker in power_map:
            rltv = power_map[ticker].get("tday_rltv", 0)
            if rltv >= 120:
                score += 25
                signals.append(f"체결강도 {rltv:.0f}(강한매수)")
            elif rltv >= 100:
                score += 15
                signals.append(f"체결강도 {rltv:.0f}")

        # 3) 등락률 시그널
        if ticker in fluct_map:
            if 0 < prdy_ctrt <= 3:
                score += 20
                signals.append(f"등락률 +{prdy_ctrt:.1f}%(적절)")
            elif 3 < prdy_ctrt <= 7:
                score += 10
                signals.append(f"등락률 +{prdy_ctrt:.1f}%")

        # 4) 대량체결 매수상위
        if ticker in bulk_map:
            score += 15
            signals.append(f"대량매수 {bulk_map[ticker]['rank']}위")

        # 5) 시가총액 대형주 보너스
        if ticker in cap_map:
            score += 5
            signals.append(f"시총 {cap_map[ticker]['rank']}위")

        if score > 0:
            scored.append({
                "ticker": ticker,
                "name": name,
                "price": price,
                "prdy_ctrt": prdy_ctrt,
                "score": score,
                "signals": signals,
            })

    # 점수 내림차순 정렬
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:count]


def _compute_us_scores_from_yfinance(watchlist: list[str], count: int = 10) -> list[dict]:
    """yfinance 워치리스트 기반 미국 후보 점수 계산."""
    scored: list[dict] = []
    for ticker in watchlist:
        try:
            hist = yf.Ticker(ticker).history(period="7d", interval="1d")
            if hist.empty or "Close" not in hist.columns:
                continue
            closes = hist["Close"].dropna()
            vols = hist["Volume"].dropna() if "Volume" in hist.columns else None
            if len(closes) < 2:
                continue

            price = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            if prev <= 0:
                continue

            pct = (price - prev) / prev * 100
            vol = int(vols.iloc[-1]) if vols is not None and len(vols) > 0 else 0

            score = 0
            signals: list[str] = []
            if 0 < pct <= 5:
                score += 25
                signals.append(f"등락률 +{pct:.2f}%")
            elif pct > 5:
                score += 10
                signals.append(f"등락률 +{pct:.2f}% (과열주의)")
            elif pct < -3:
                continue

            if vol >= 5_000_000:
                score += 20
                signals.append(f"거래량 {vol:,}")
            elif vol >= 1_000_000:
                score += 10
                signals.append(f"거래량 {vol:,}")

            if score <= 0:
                continue

            scored.append(
                {
                    "market": "US",
                    "currency": "USD",
                    "exchange": kis._us_exchange_cache.get(ticker, ""),
                    "ticker": ticker,
                    "name": ticker,
                    "price": price,
                    "prdy_ctrt": pct,
                    "score": score,
                    "signals": signals,
                }
            )
        except Exception:
            continue

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:count]


async def _compute_us_stock_scores(count: int = 10) -> list[dict]:
    """미국 후보 스코어링 (KIS 해외 랭킹 우선, yfinance 폴백)."""
    loop = asyncio.get_running_loop()

    # 1) KIS 해외 랭킹 우선
    kis_candidates = await loop.run_in_executor(None, kis.get_us_volume_rank, max(30, count * 2))
    if kis_candidates:
        scored: list[dict] = []
        for item in kis_candidates:
            pct = float(item.get("prdy_ctrt", 0))
            vol = int(item.get("acml_vol", 0))
            score = 0
            signals: list[str] = []

            if 0 < pct <= 5:
                score += 25
                signals.append(f"등락률 +{pct:.2f}%")
            elif pct > 5:
                score += 10
                signals.append(f"등락률 +{pct:.2f}% (과열주의)")
            elif pct < -3:
                continue

            if vol >= 5_000_000:
                score += 20
                signals.append(f"거래량 {vol:,}")
            elif vol >= 1_000_000:
                score += 10
                signals.append(f"거래량 {vol:,}")

            if score <= 0:
                continue

            scored.append(
                {
                    "market": "US",
                    "currency": "USD",
                    "exchange": item.get("exchange", ""),
                    "ticker": item["ticker"],
                    "name": item.get("name", item["ticker"]),
                    "price": float(item.get("price", 0)),
                    "prdy_ctrt": pct,
                    "score": score,
                    "signals": signals,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        if scored:
            return scored[:count]

    # 2) yfinance fallback
    return await loop.run_in_executor(None, _compute_us_scores_from_yfinance, kis.us_watchlist, count)


# ─── Helper: TOP5 분석 실행 ───────────────────────────────────
async def _run_top5_analysis(channel: discord.abc.Messageable, trade_date: str):
    """대형주 TOP5를 조회하고 각각 AI 분석 실행."""
    status = await channel.send("📊 **시가총액 TOP5** 조회 중…")
    loop = asyncio.get_running_loop()
    top5 = await loop.run_in_executor(None, kis.get_top_market_cap, 5)

    if not top5:
        await status.edit(content="❌ 시가총액 데이터를 가져올 수 없습니다. (휴장일?)")
        return

    # TOP5 목록 Embed
    desc_lines = []
    for s in top5:
        cap_str = format_krw(s["market_cap"])
        desc_lines.append(
            f"**{s['rank']}.** {s['name']} (`{s['ticker']}`) "
            f"— {s['price']:,}원 | 시총 {cap_str}"
        )
    list_embed = discord.Embed(
        title="🏆 코스피 시가총액 TOP 5",
        description="\n".join(desc_lines),
        color=0x0066FF,
        timestamp=datetime.datetime.now(),
    )
    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
    list_embed.set_footer(text=f"TradingAgents | {mode_label}")
    await status.edit(content=None, embed=list_embed)

    # 각 종목 AI 분석
    buy_targets = []
    sell_targets = []
    for i, stock_info in enumerate(top5):
        ticker = stock_info["ticker"]
        name = stock_info["name"]
        progress = await channel.send(
            f"🔍 [{i+1}/5] **{name}** (`{ticker}`) 분석 중… (약 2~5분)"
        )
        try:
            ta = TradingAgentsGraph(debug=False, config=config)
            analysis_symbol = _yf_ticker(ticker, reference_price=stock_info["price"])
            final_state, decision = await loop.run_in_executor(
                None, ta.propagate, analysis_symbol, trade_date
            )

            color_map = {"BUY": 0x00FF00, "SELL": 0xFF0000, "HOLD": 0xFFAA00}
            summary = _extract_decision_summary(final_state, decision, ticker)
            emoji = "🟢" if decision == "BUY" else "🔴" if decision == "SELL" else "🟡"
            embed = discord.Embed(
                title=f"{emoji} {name} ({ticker}) → {decision}",
                description=summary,
                color=color_map.get(decision.upper(), 0x808080),
            )
            await progress.edit(content=None, embed=embed)

            report_text = _build_report_text(
                final_state,
                ticker,
                market="KR",
                analysis_symbol=analysis_symbol,
            )
            report_file, report_path = _prepare_report_attachment(
                report_text,
                market="KR",
                ticker=ticker,
                trade_date=trade_date,
                scope="TOP5",
            )
            if AUTO_REPORT_UPLOAD:
                try:
                    await channel.send(file=report_file)
                except Exception as e:
                    _log(
                        "WARN",
                        "TOP5_REPORT_UPLOAD_FAIL",
                        f"ticker={ticker} error={str(e)[:160]} path={report_path or 'N/A'}",
                    )
                    await channel.send(
                        "⚠️ 보고서 파일 업로드에 실패했습니다. "
                        + (f"로컬 저장 파일: `{report_path}`" if report_path else "로컬 저장도 실패했습니다.")
                    )

            if decision.upper() == "BUY":
                buy_targets.append({
                    "ticker": ticker,
                    "name": name,
                    "price": stock_info["price"],
                })
            elif decision.upper() == "SELL":
                sell_targets.append({
                    "ticker": ticker,
                    "name": name,
                })
        except Exception as e:
            await progress.edit(
                content=f"❌ {name} ({ticker}) 분석 실패: {str(e)[:200]}"
            )

    # ── SELL 종목: 보유 중이면 매도 버튼 표시 ──────────────────
    if sell_targets and kis.is_configured:
        try:
            loop = asyncio.get_running_loop()
            balance_data = await loop.run_in_executor(None, kis.get_balance, "KR")
            holdings_map = {h["ticker"]: h for h in balance_data["holdings"]}
        except Exception:
            holdings_map = {}

        for target in sell_targets:
            holding = holdings_map.get(target["ticker"])
            if holding and holding["qty"] > 0:
                view = SellConfirmView(
                    ticker=target["ticker"],
                    name=target["name"],
                    qty=holding["qty"],
                    avg_price=holding["avg_price"],
                    market="KR",
                    currency="KRW",
                    exchange=holding.get("exchange", "KRX"),
                )
                embed = discord.Embed(
                    title=f"🔴 {target['name']} 매도 확인",
                    description=(
                        f"**종목:** {target['name']} (`{target['ticker']}`)\n"
                        f"**보유:** {holding['qty']}주 (평균 {_format_money(holding['avg_price'], 'KRW')})\n"
                        f"**현재가:** {_format_money(holding['current_price'], 'KRW')}\n"
                        f"**손익:** {_format_money(holding['pnl'], 'KRW')} ({holding['pnl_rate']:+.2f}%)\n\n"
                        f"AI가 SELL을 권고합니다. 전량 매도하시겠습니까?"
                    ),
                    color=0xFF0000,
                )
                embed.set_footer(text=mode_label)
                await channel.send(embed=embed, view=view)

    # ── BUY 종목: 매수 버튼 표시 ──────────────────────────────
    if not buy_targets and not sell_targets:
        await channel.send("📋 **분석 완료** — BUY/SELL 추천 종목이 없습니다. 모두 HOLD입니다.")
        return
    elif not buy_targets:
        await channel.send("📋 **분석 완료** — BUY 추천 종목이 없습니다.")
        return

    if not kis.is_configured:
        buy_list = ", ".join(f"{t['name']}" for t in buy_targets)
        await channel.send(
            f"📋 **분석 완료** — BUY 추천: {buy_list}\n"
            f"⚠️ KIS API가 설정되지 않아 자동 매매를 사용할 수 없습니다."
        )
        return

    if not _is_market_open_now("KR"):
        buy_list = ", ".join(f"{t['name']}({t['ticker']})" for t in buy_targets)
        await channel.send(
            "ℹ️ **장외/휴장 상태**라 `/대형주` 수동 매수 버튼을 비활성화했습니다.\n"
            f"추천 BUY 종목: {buy_list}"
        )
        _log("INFO", "TOP5_BUY_BUTTON_BLOCKED", "market closed")
        return

    per_stock_budget = int(kis.max_order_amount // len(buy_targets))
    await channel.send(
        f"🧪 **테스트 모드 예산(수동 /대형주)**\n"
            f"총 상한: {_format_money(kis.max_order_amount, 'KRW')} | "
            f"종목당: {_format_money(per_stock_budget, 'KRW')}"
    )
    for target in buy_targets:
        qty = int(per_stock_budget // target["price"]) if target["price"] > 0 else 0
        if qty <= 0:
            await channel.send(
                f"⚠️ {target['name']} — 예산({_format_money(per_stock_budget, 'KRW')}) 부족으로 매수 불가"
            )
            continue
        view = BuyConfirmView(
            ticker=target["ticker"],
            name=target["name"],
            qty=qty,
            price=target["price"],
            market="KR",
            currency="KRW",
        )
        embed = discord.Embed(
            title=f"🛒 {target['name']} 매수 확인",
            description=(
                f"**종목:** {target['name']} (`{target['ticker']}`)\n"
                f"**현재가:** {_format_money(target['price'], 'KRW')}\n"
                f"**매수 수량:** {qty}주\n"
                f"**예산 규칙:** 수동 /대형주 테스트 상한({_format_money(per_stock_budget, 'KRW')})\n"
                f"**예상 금액:** {_format_money(qty * target['price'], 'KRW')}\n\n"
                f"매수하시겠습니까?"
            ),
            color=0x00FF00,
        )
        embed.set_footer(text=mode_label)
        await channel.send(embed=embed, view=view)


# ─── Discord UI: 매수/매도 확인 버튼 ──────────────────────────
class BuyConfirmView(discord.ui.View):
    """매수 확인/건너뛰기 버튼"""

    def __init__(
        self,
        ticker: str,
        name: str,
        qty: int,
        price: float,
        market: str = "KR",
        currency: str = "KRW",
    ):
        super().__init__(timeout=300)
        self.ticker = ticker
        self.name = name
        self.qty = qty
        self.price = float(price)
        self.market = market.upper()
        self.currency = currency.upper()

    @discord.ui.button(label="✅ 매수 확인", style=discord.ButtonStyle.green)
    async def confirm_buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, kis.buy_stock, self.ticker, self.qty, 0, self.market
            )
            if result["success"]:
                record_trade(
                    self.ticker, self.name, "BUY",
                    self.qty, self.price,
                    order_no=result.get("order_no", ""),
                    reason="AI BUY 신호",
                    market=self.market,
                    currency=self.currency,
                )
                embed = discord.Embed(
                    title=f"✅ {self.name} 매수 완료",
                    description=(
                        f"**시장:** {self.market}\n"
                        f"**주문번호:** {result['order_no']}\n"
                        f"**수량:** {self.qty}주\n"
                        f"**평균 단가:** {_format_money(self.price, self.currency)}\n"
                        f"**메시지:** {result['message']}"
                    ),
                    color=0x00FF00,
                )
            else:
                embed = discord.Embed(
                    title=f"❌ {self.name} 매수 실패",
                    description=f"**사유:** {result['message']}",
                    color=0xFF0000,
                )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ 매수 오류: {str(e)[:500]}")
        self.stop()

    @discord.ui.button(label="⏭️ 건너뛰기", style=discord.ButtonStyle.grey)
    async def skip_buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"⏭️ {self.name} 매수를 건너뛰었습니다.", ephemeral=True
        )
        self.stop()


class SellConfirmView(discord.ui.View):
    """매도 확인/취소 버튼"""

    def __init__(
        self,
        ticker: str,
        name: str,
        qty: int,
        avg_price: float = 0,
        market: str = "KR",
        currency: str = "KRW",
        exchange: str = "",
    ):
        super().__init__(timeout=120)
        self.ticker = ticker
        self.name = name
        self.qty = qty
        self.avg_price = float(avg_price)  # 평균 매수가 (실현손익 계산용)
        self.market = market.upper()
        self.currency = currency.upper()
        self.exchange = exchange

    @discord.ui.button(label="🔴 매도 확인", style=discord.ButtonStyle.danger)
    async def confirm_sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, kis.sell_stock, self.ticker, self.qty, 0, self.market
            )
            if result["success"]:
                # 현재가 조회하여 실현손익 기록
                try:
                    sell_price = await loop.run_in_executor(None, kis.get_price, self.ticker, self.market)
                except Exception:
                    sell_price = 0
                record_trade(
                    self.ticker, self.name, "SELL",
                    self.qty, sell_price,
                    order_no=result.get("order_no", ""),
                    reason="매도",
                    market=self.market,
                    currency=self.currency,
                )
                if self.avg_price > 0 and sell_price > 0:
                    record_pnl(
                        self.ticker,
                        self.name,
                        self.avg_price,
                        sell_price,
                        self.qty,
                        market=self.market,
                        currency=self.currency,
                    )
                embed = discord.Embed(
                    title=f"✅ {self.name} 매도 완료",
                    description=(
                        f"**시장:** {self.market}\n"
                        f"**종목:** `{self.ticker}`\n"
                        f"**수량:** {self.qty}주\n"
                        f"**체결 단가:** {_format_money(sell_price, self.currency)}\n"
                        f"**주문번호:** {result['order_no']}\n"
                        f"**메시지:** {result['message']}"
                    ),
                    color=0x00FF00,
                )
            else:
                embed = discord.Embed(
                    title="❌ 매도 실패",
                    description=f"**사유:** {result['message']}",
                    color=0xFF0000,
                )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ 매도 오류: {str(e)[:500]}")
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.grey)
    async def cancel_sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🚫 매도를 취소했습니다.", ephemeral=True)
        self.stop()

# ─── Slash Command: /분석 ──────────────────────────────────────
@tree.command(name="분석", description="멀티 에이전트 AI 투자 분석 보고서를 생성합니다")
@app_commands.describe(
    ticker="분석할 종목 티커 (예: AAPL, MSFT, 005930)",
    date="분석 기준일 (YYYY-MM-DD, 기본: 오늘)",
)
async def analyze(
    interaction: discord.Interaction,
    ticker: str,
    date: str | None = None,
):
    ticker = ticker.upper().strip()
    market = _market_of_ticker(ticker)
    try:
        trade_date = _parse_trade_date(date)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    _log(
        "INFO",
        "SLASH_ANALYZE_START",
        f"{_interaction_actor(interaction)} market={market} ticker={ticker} date={trade_date}",
    )

    if not _is_allowed_channel(interaction.channel_id):
        _log("WARN", "SLASH_ANALYZE_BLOCKED", f"허용되지 않은 채널 channel={interaction.channel_id}")
        await interaction.followup.send(
            "❌ 이 채널에서는 분석 명령을 사용할 수 없습니다."
        )
        return

    if _analysis_lock.locked():
        _log("WARN", "SLASH_ANALYZE_BUSY", "analysis lock already acquired")
        await interaction.followup.send(
            "⏳ 이미 다른 분석이 진행 중입니다. 잠시 후 다시 시도해주세요."
        )
        return

    is_valid_ticker, ticker_error = await _validate_analysis_ticker(ticker)
    if not is_valid_ticker:
        _log("WARN", "SLASH_ANALYZE_INVALID_TICKER", f"ticker={ticker} reason={ticker_error}")
        await interaction.followup.send(f"❌ {ticker_error}")
        return

    async with _analysis_lock:
        status_msg = await interaction.followup.send(
            f"🔍 **{ticker} ({market})** 분석을 시작합니다… (약 2~5분 소요)\n"
            f"📅 기준일: {trade_date}",
            wait=True,
        )

        try:
            loop = asyncio.get_running_loop()
            ta = TradingAgentsGraph(debug=False, config=config)
            analysis_ref_price = None
            if market == "KR" and kis.is_configured:
                try:
                    analysis_ref_price = await loop.run_in_executor(
                        None, kis.get_price, ticker, "KR"
                    )
                except Exception:
                    analysis_ref_price = None
            analysis_symbol = _yf_ticker(ticker, reference_price=analysis_ref_price)
            final_state, decision = await loop.run_in_executor(
                None, ta.propagate, analysis_symbol, trade_date
            )

            report_text = _build_report_text(
                final_state,
                ticker,
                market=market,
                analysis_symbol=analysis_symbol,
            )
            summary = _extract_decision_summary(final_state, decision, ticker, market)

            color_map = {"BUY": 0x00FF00, "SELL": 0xFF0000, "HOLD": 0xFFAA00}
            embed = discord.Embed(
                title=f"📋 {ticker} ({market}) 분석 완료",
                description=summary,
                color=color_map.get(decision.upper(), 0x808080),
                timestamp=datetime.datetime.now(),
            )
            embed.set_footer(text="TradingAgents 멀티 에이전트 분석")

            await status_msg.edit(content=None, embed=embed)

            report_file, report_path = _prepare_report_attachment(
                report_text,
                market=market,
                ticker=ticker,
                trade_date=trade_date,
                scope="SLASH",
            )
            try:
                await interaction.followup.send(
                    f"📄 **{ticker} ({market})** 전체 보고서:",
                    file=report_file,
                )
            except Exception as e:
                _log(
                    "WARN",
                    "SLASH_ANALYZE_REPORT_UPLOAD_FAIL",
                    f"ticker={ticker} error={str(e)[:160]} path={report_path or 'N/A'}",
                )
                await interaction.followup.send(
                    "⚠️ 보고서 파일 업로드에 실패했습니다. "
                    + (f"로컬 저장 파일: `{report_path}`" if report_path else "로컬 저장도 실패했습니다.")
                )

            # BUY/SELL 판정 시 자동매매 버튼
            ch = interaction.channel
            if isinstance(ch, discord.abc.Messageable):
                await _show_trade_button(ch, ticker, decision, market=market)

            _log(
                "INFO",
                "SLASH_ANALYZE_DONE",
                f"market={market} ticker={ticker} decision={decision}",
            )

        except Exception as e:
            _log("ERROR", "SLASH_ANALYZE_ERROR", f"ticker={ticker} error={str(e)[:200]}")
            await status_msg.edit(
                content=f"❌ 분석 중 오류가 발생했습니다:\n```\n{str(e)[:1500]}\n```"
            )


# ─── Slash Command: /대형주 ─────────────────────────────────────
@tree.command(name="대형주", description="코스피 시가총액 TOP5 분석 + 매수 추천")
@app_commands.describe(date="분석 기준일 (YYYY-MM-DD, 기본: 오늘)")
async def top_stocks(interaction: discord.Interaction, date: str | None = None):
    try:
        trade_date = _parse_trade_date(date)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    _log("INFO", "SLASH_TOP5_START", f"{_interaction_actor(interaction)} date={trade_date}")

    if not _is_allowed_channel(interaction.channel_id):
        _log("WARN", "SLASH_TOP5_BLOCKED", f"허용되지 않은 채널 channel={interaction.channel_id}")
        await interaction.followup.send("❌ 이 채널에서는 사용할 수 없습니다.")
        return

    if _analysis_lock.locked():
        _log("WARN", "SLASH_TOP5_BUSY", "analysis lock already acquired")
        await interaction.followup.send("⏳ 이미 다른 분석이 진행 중입니다.")
        return

    await interaction.followup.send(f"🚀 **대형주 TOP5 분석**을 시작합니다 (기준일: {trade_date})")
    async with _analysis_lock:
        ch = interaction.channel
        if isinstance(ch, discord.abc.Messageable):
            await _run_top5_analysis(ch, trade_date)
            _log("INFO", "SLASH_TOP5_DONE", f"date={trade_date}")
        else:
            _log("WARN", "SLASH_TOP5_INVALID_CHANNEL", "interaction channel is not Messageable")
            await interaction.followup.send("❌ 이 채널에서는 분석을 실행할 수 없습니다.")


# ─── Slash Command: /잔고 ──────────────────────────────────────
@tree.command(name="잔고", description="한국투자증권 계좌 잔고를 조회합니다")
async def balance_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    _log("INFO", "SLASH_BALANCE_START", _interaction_actor(interaction))

    if not kis.is_configured:
        _log("WARN", "SLASH_BALANCE_BLOCKED", "KIS API not configured")
        await interaction.followup.send("⚠️ KIS API가 설정되지 않았습니다. `.env`에 KIS 인증 정보를 추가하세요.")
        return

    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, kis.get_balance, "ALL")
        holdings = data["holdings"]
        summary = data["summary"]

        if not holdings:
            desc = "보유 종목이 없습니다."
        else:
            lines = []
            for h in holdings:
                pnl_emoji = "🟢" if h["pnl"] >= 0 else "🔴"
                currency = h.get("currency", _currency_of_market(h.get("market", "KR")))
                lines.append(
                    f"**[{h.get('market', 'KR')}] {h['name']}** (`{h['ticker']}`) — {h['qty']}주\n"
                    f"  평균가 {_format_money(h['avg_price'], currency)} → "
                    f"현재 {_format_money(h['current_price'], currency)} "
                    f"{pnl_emoji} {_format_money(h['pnl'], currency)} ({h['pnl_rate']:+.2f}%)"
                )
            desc = "\n".join(lines)

        mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
        embed = discord.Embed(
            title=f"💰 계좌 잔고 ({mode_label})",
            description=desc,
            color=0x0066FF,
            timestamp=datetime.datetime.now(),
        )
        if summary:
            krw = summary.get("KRW", {})
            usd = summary.get("USD", {})
            embed.add_field(
                name="KRW 요약",
                value=(
                    f"평가액: {_format_money(krw.get('total_eval', 0), 'KRW')}\n"
                    f"손익: {_format_money(krw.get('total_pnl', 0), 'KRW')}\n"
                    f"예수금: {_format_money(krw.get('cash', 0), 'KRW')}"
                ),
                inline=True,
            )
            embed.add_field(
                name="USD 요약",
                value=(
                    f"평가액: {_format_money(usd.get('total_eval', 0), 'USD')}\n"
                    f"손익: {_format_money(usd.get('total_pnl', 0), 'USD')}\n"
                    f"예수금: {_format_money(usd.get('cash', 0), 'USD')}"
                ),
                inline=True,
            )
            embed.add_field(name="보유 종목 수", value=f"{len(holdings)}개", inline=True)

        await interaction.followup.send(embed=embed)
        _log(
            "INFO",
            "SLASH_BALANCE_DONE",
            f"holdings={len(holdings)} krw_eval={summary.get('KRW', {}).get('total_eval', 0)} "
            f"usd_eval={summary.get('USD', {}).get('total_eval', 0)}",
        )
    except Exception as e:
        _log("ERROR", "SLASH_BALANCE_ERROR", str(e)[:200])
        await interaction.followup.send(f"❌ 잔고 조회 실패: {str(e)[:500]}")


# ─── Slash Command: /매도 ──────────────────────────────────────
@tree.command(name="매도", description="보유 종목을 매도합니다 (수량 생략 시 전량 매도)")
@app_commands.describe(
    ticker="매도할 종목 코드 (예: 005930)",
    qty="매도 수량 (생략 시 전량 매도)",
)
async def sell_cmd(
    interaction: discord.Interaction,
    ticker: str,
    qty: int | None = None,
):
    await interaction.response.defer(thinking=True)
    _log("INFO", "SLASH_SELL_START", f"{_interaction_actor(interaction)} ticker={ticker} qty={qty}")

    if not _is_allowed_channel(interaction.channel_id):
        _log("WARN", "SLASH_SELL_BLOCKED", f"허용되지 않은 채널 channel={interaction.channel_id}")
        await interaction.followup.send("❌ 이 채널에서는 사용할 수 없습니다.")
        return

    if not kis.is_configured:
        _log("WARN", "SLASH_SELL_BLOCKED", "KIS API not configured")
        await interaction.followup.send("⚠️ KIS API가 설정되지 않았습니다.")
        return

    ticker = ticker.strip().upper()
    market = _market_of_ticker(ticker)
    normalized = kis.normalize_ticker(ticker, market)
    holding: dict | None = None
    loop = asyncio.get_running_loop()

    if qty is not None and qty <= 0:
        _log("WARN", "SLASH_SELL_INVALID_QTY", f"ticker={ticker} qty={qty}")
        await interaction.followup.send("❌ 수량은 1 이상이어야 합니다.")
        return

    # 잔고에서 보유 정보 조회
    try:
        balance_data = await loop.run_in_executor(None, kis.get_balance, "ALL")
        holding = next(
            (
                h
                for h in balance_data["holdings"]
                if h["ticker"] == normalized and h.get("market", market) == market
            ),
            None,
        )
    except Exception as e:
        _log("ERROR", "SLASH_SELL_BALANCE_ERROR", str(e)[:200])
        await interaction.followup.send(f"❌ 잔고 조회 실패: {str(e)[:300]}")
        return

    if not holding:
        _log("WARN", "SLASH_SELL_NO_HOLDING", f"market={market} ticker={normalized}")
        await interaction.followup.send(f"⚠️ `{normalized}`({market}) 보유 내역이 없습니다.")
        return

    sell_qty = qty if qty is not None else holding["qty"]
    stock_name = holding["name"]
    avg_price = holding["avg_price"]
    currency = holding.get("currency", _currency_of_market(market))

    view = SellConfirmView(
        ticker=holding["ticker"],
        name=stock_name,
        qty=sell_qty,
        avg_price=avg_price,
        market=market,
        currency=currency,
        exchange=holding.get("exchange", ""),
    )
    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
    embed = discord.Embed(
        title="🔴 매도 확인",
        description=(
            f"**시장:** {market}\n"
            f"**종목:** {stock_name} (`{holding['ticker']}`)\n"
            f"**수량:** {sell_qty}주\n\n매도하시겠습니까?"
        ),
        color=0xFF0000,
    )
    embed.set_footer(text=f"{mode_label} | {currency}")
    await interaction.followup.send(embed=embed, view=view)
    _log(
        "INFO",
        "SLASH_SELL_PROMPT",
        f"market={market} ticker={holding['ticker']} qty={sell_qty} avg_price={avg_price}",
    )


# ─── Slash Command: /상태 ──────────────────────────────────────
@tree.command(name="상태", description="오늘의 자동매매 실행 상태를 확인합니다")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    _log("INFO", "SLASH_STATUS_START", _interaction_actor(interaction))

    if not _is_allowed_channel(interaction.channel_id):
        _log("WARN", "SLASH_STATUS_BLOCKED", f"허용되지 않은 채널 channel={interaction.channel_id}")
        await interaction.followup.send("❌ 이 채널에서는 사용할 수 없습니다.")
        return

    states = get_daily_state()
    if not states:
        _log("INFO", "SLASH_STATUS_EMPTY", "today has no auto-trading state")
        await interaction.followup.send("📋 오늘 실행된 자동매매가 없습니다.")
        return

    lines = []
    for s in states:
        emoji = {
            "morning_buy": "🌅",
            "afternoon_sell": "🌇",
            "us_morning_buy": "🇺🇸🌅",
            "us_afternoon_sell": "🇺🇸🌇",
        }.get(
            s["action"], "🔔"
        )
        lines.append(
            f"{emoji} **{s['action']}** — {s['completed_at'][:16]}\n"
            f"   {s['details']}"
        )

    embed = discord.Embed(
        title=f"📋 오늘의 자동매매 상태 ({datetime.date.today()})",
        description="\n\n".join(lines),
        color=0x0066FF,
        timestamp=datetime.datetime.now(),
    )
    await interaction.followup.send(embed=embed)
    _log("INFO", "SLASH_STATUS_DONE", f"state_count={len(states)}")


# ─── Slash Command: /봇정보 ────────────────────────────────────
@tree.command(name="봇정보", description="봇 스케줄 · 설정 · 계좌 · 실행 이력을 확인합니다")
async def bot_info_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    _log("INFO", "SLASH_BOTINFO_START", _interaction_actor(interaction))

    if not _is_allowed_channel(interaction.channel_id):
        _log("WARN", "SLASH_BOTINFO_BLOCKED", f"허용되지 않은 채널 channel={interaction.channel_id}")
        await interaction.followup.send("❌ 이 채널에서는 사용할 수 없습니다.")
        return

    now = datetime.datetime.now(KST)
    now_ny = datetime.datetime.now(NY_TZ)
    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"

    # 다음 실행 시각 계산
    today = now.date()
    buy_time = datetime.datetime.combine(
        today, datetime.time(_buy_h, _buy_m), tzinfo=KST
    )
    sell_time = datetime.datetime.combine(
        today, datetime.time(_sell_h, _sell_m), tzinfo=KST
    )
    if buy_time <= now:
        buy_time += datetime.timedelta(days=1)
    if sell_time <= now:
        sell_time += datetime.timedelta(days=1)

    buy_remaining = buy_time - now
    sell_remaining = sell_time - now
    buy_h_r, buy_m_r = divmod(int(buy_remaining.total_seconds()) // 60, 60)
    sell_h_r, sell_m_r = divmod(int(sell_remaining.total_seconds()) // 60, 60)

    us_buy_h_r, us_buy_m_r = 0, 0
    us_sell_h_r, us_sell_m_r = 0, 0
    if ENABLE_US_TRADING:
        us_today = now_ny.date()
        us_buy_time = datetime.datetime.combine(
            us_today, datetime.time(_us_buy_h, _us_buy_m), tzinfo=NY_TZ
        )
        us_sell_time = datetime.datetime.combine(
            us_today, datetime.time(_us_sell_h, _us_sell_m), tzinfo=NY_TZ
        )
        if us_buy_time <= now_ny:
            us_buy_time += datetime.timedelta(days=1)
        if us_sell_time <= now_ny:
            us_sell_time += datetime.timedelta(days=1)
        us_buy_remaining = us_buy_time - now_ny
        us_sell_remaining = us_sell_time - now_ny
        us_buy_h_r, us_buy_m_r = divmod(int(us_buy_remaining.total_seconds()) // 60, 60)
        us_sell_h_r, us_sell_m_r = divmod(int(us_sell_remaining.total_seconds()) // 60, 60)

    # 오늘 상태
    states = get_daily_state()
    morning_done = any(s["action"] == "morning_buy" for s in states)
    afternoon_done = any(s["action"] == "afternoon_sell" for s in states)
    us_morning_done = any(s["action"] == "us_morning_buy" for s in states)
    us_afternoon_done = any(s["action"] == "us_afternoon_sell" for s in states)
    kr_market_open = _is_market_day("KR")
    us_market_open = _is_market_day("US")

    status_lines = [
        f"**📅 KR 오늘:** {today} ({'거래일 ✅' if kr_market_open else '휴장일 ❌'})",
        f"**📅 US 오늘:** {now_ny.date()} ({'거래일 ✅' if us_market_open else '휴장일 ❌'})",
        f"**⏰ 현재 시각:** {now.strftime('%H:%M:%S')} KST",
        f"**⏰ NY 시각:** {now_ny.strftime('%H:%M:%S')} ET",
        "",
        "── **KR 자동매매 스케줄** ──",
        f"🌅 **아침 매수:** {AUTO_BUY_TIME} KST → "
        f"{'✅ 완료' if morning_done else f'⏳ {buy_h_r}시간 {buy_m_r}분 후'}",
        f"🌇 **오후 매도:** {AUTO_SELL_TIME} KST → "
        f"{'✅ 완료' if afternoon_done else f'⏳ {sell_h_r}시간 {sell_m_r}분 후'}",
    ]
    if ENABLE_US_TRADING:
        status_lines.extend(
            [
                "",
                "── **US 자동매매 스케줄** ──",
                f"🌅 **아침 매수:** {US_AUTO_BUY_TIME} ET → "
                f"{'✅ 완료' if us_morning_done else f'⏳ {us_buy_h_r}시간 {us_buy_m_r}분 후'}",
                f"🌇 **오후 매도:** {US_AUTO_SELL_TIME} ET → "
                f"{'✅ 완료' if us_afternoon_done else f'⏳ {us_sell_h_r}시간 {us_sell_m_r}분 후'}",
            ]
        )
    status_lines.extend(
        [
            "",
            f"🔔 **손절/익절:** {MONITOR_INTERVAL_MIN}분 간격 감시 중",
            "",
            "── **설정** ──",
            f"📊 **KR 매수 종목 수:** {DAY_TRADE_PICKS}개",
            f"📊 **US 매수 종목 수:** {US_DAY_TRADE_PICKS}개",
            f"🧪 **KR 수동 예산:** {_format_money(kis.max_order_amount, 'KRW')}",
            f"🧪 **US 수동 예산:** {_format_money(kis.us_max_order_amount, 'USD')}",
            f"🔴 **손절 라인:** {STOP_LOSS_PCT}%",
            f"🟢 **익절 라인:** {TAKE_PROFIT_PCT}%",
            f"🏦 **매매 모드:** {mode_label}",
            f"🤖 **분석 모델:** {config.get('deep_think_llm', 'N/A')}",
        ]
    )

    if kis.is_configured:
        try:
            loop = asyncio.get_running_loop()
            bal = await loop.run_in_executor(None, kis.get_balance, "ALL")
            sm = bal.get("summary", {})
            holdings_count = len(bal.get("holdings", []))
            status_lines.append("")
            status_lines.append("── **계좌** ──")
            status_lines.append(f"💵 **KR 예수금:** {_format_money(sm.get('KRW', {}).get('cash', 0), 'KRW')}")
            status_lines.append(f"💵 **US 예수금:** {_format_money(sm.get('USD', {}).get('cash', 0), 'USD')}")
            status_lines.append(f"📦 **보유종목:** {holdings_count}개")
            status_lines.append(
                f"📈 **KR 평가액:** {_format_money(sm.get('KRW', {}).get('total_eval', 0), 'KRW')}"
            )
            status_lines.append(
                f"📈 **US 평가액:** {_format_money(sm.get('USD', {}).get('total_eval', 0), 'USD')}"
            )
        except Exception:
            pass

    if states:
        status_lines.append("")
        status_lines.append("── **오늘 실행 이력** ──")
        for s in states:
            emoji = {
                "morning_buy": "🌅",
                "afternoon_sell": "🌇",
                "us_morning_buy": "🇺🇸🌅",
                "us_afternoon_sell": "🇺🇸🌇",
            }.get(
                s["action"], "🔔"
            )
            status_lines.append(
                f"{emoji} {s['action']} — {s['completed_at'][:16]} | {s['details']}"
            )

    embed = discord.Embed(
        title="🤖 TradingAgents 봇 정보",
        description="\n".join(status_lines),
        color=0x0066FF,
        timestamp=now,
    )
    embed.set_footer(text="TradingAgents 데이 트레이딩 시스템")
    await interaction.followup.send(embed=embed)
    _log(
        "INFO",
        "SLASH_BOTINFO_DONE",
        f"kr_open={kr_market_open} us_open={us_market_open} state_count={len(states)}",
    )


# ─── Slash Command: /수익 ──────────────────────────────────────
@tree.command(name="수익", description="누적 매매 수익 현황을 조회합니다")
async def pnl_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    _log("INFO", "SLASH_PNL_START", _interaction_actor(interaction))

    if not _is_allowed_channel(interaction.channel_id):
        _log("WARN", "SLASH_PNL_BLOCKED", f"허용되지 않은 채널 channel={interaction.channel_id}")
        await interaction.followup.send("❌ 이 채널에서는 사용할 수 없습니다.")
        return

    by_ccy = get_total_pnl_by_currency()
    krw = by_ccy.get("KRW", get_total_pnl(currency="KRW"))
    usd = by_ccy.get("USD", get_total_pnl(currency="USD"))
    ticker_krw = get_ticker_summary(currency="KRW")
    ticker_usd = get_ticker_summary(currency="USD")
    recent_krw = get_recent_pnl(5, currency="KRW")
    recent_usd = get_recent_pnl(5, currency="USD")

    desc_lines = [
        f"KRW 손익: {_format_money(krw['total_pnl'], 'KRW')} | "
        f"거래 {krw['trade_count']}회 | 승률 {krw['win_rate']}%",
        f"USD 손익: {_format_money(usd['total_pnl'], 'USD')} | "
        f"거래 {usd['trade_count']}회 | 승률 {usd['win_rate']}%",
    ]

    tone_total = krw["total_pnl"] + usd["total_pnl"]
    embed = discord.Embed(
        title="📊 매매 수익 현황 (통화 분리)",
        description="\n".join(desc_lines),
        color=0x00FF00 if tone_total >= 0 else 0xFF0000,
        timestamp=datetime.datetime.now(),
    )

    if ticker_krw:
        lines = []
        for t in ticker_krw[:5]:
            emoji = "🟢" if t["total_pnl"] >= 0 else "🔴"
            lines.append(
                f"{emoji} [{t['market']}] {t['name']} (`{t['ticker']}`) "
                f"— {t['count']}회 | {_format_money(t['total_pnl'], 'KRW')} | 평균 {t['avg_pnl_rate']:+.1f}%"
            )
        embed.add_field(name="🏢 KRW 종목별", value="\n".join(lines), inline=False)

    if ticker_usd:
        lines = []
        for t in ticker_usd[:5]:
            emoji = "🟢" if t["total_pnl"] >= 0 else "🔴"
            lines.append(
                f"{emoji} [{t['market']}] {t['name']} (`{t['ticker']}`) "
                f"— {t['count']}회 | {_format_money(t['total_pnl'], 'USD')} | 평균 {t['avg_pnl_rate']:+.1f}%"
            )
        embed.add_field(name="🌎 USD 종목별", value="\n".join(lines), inline=False)

    if recent_krw:
        lines = []
        for r in recent_krw[:3]:
            emoji = "🟢" if r["pnl"] >= 0 else "🔴"
            lines.append(
                f"{emoji} {r['name']} — {_format_money(r['pnl'], 'KRW')} "
                f"({r['pnl_rate']:+.1f}%) | {r['created_at']}"
            )
        embed.add_field(name="🕗 최근 KRW 손익", value="\n".join(lines), inline=False)

    if recent_usd:
        lines = []
        for r in recent_usd[:3]:
            emoji = "🟢" if r["pnl"] >= 0 else "🔴"
            lines.append(
                f"{emoji} {r['name']} — {_format_money(r['pnl'], 'USD')} "
                f"({r['pnl_rate']:+.1f}%) | {r['created_at']}"
            )
        embed.add_field(name="🕗 최근 USD 손익", value="\n".join(lines), inline=False)

    embed.set_footer(text="TradingAgents 매매 이력 (통화 분리)")
    await interaction.followup.send(embed=embed)
    _log(
        "INFO",
        "SLASH_PNL_DONE",
        f"krw_total={krw['total_pnl']} usd_total={usd['total_pnl']}",
    )


# ─── 스케줄: 아침 자동매수 (09:30 KST) ───────────────────


@tasks.loop(time=datetime.time(hour=_buy_h, minute=_buy_m, tzinfo=KST))
async def morning_auto_buy():
    """매일 아침(기본 09:30) 실시간 스코어링 → 상위 AI 분석 → 자동 매수.

    1) 실시간 KIS 순위 API 4종으로 멀티시그널 스코어링
    2) 상위 DAY_TRADE_PICKS개 후보만 순차 AI 분석 (BUY 판정만 수집)
    3) 통장 전액 ÷ BUY 종목수 균등분배 → 시장가 매수
    """
    if not ALLOWED_CHANNEL_IDS or not kis.is_configured:
        _log("INFO", "AUTO_BUY_SKIP", "채널 미설정 또는 KIS 미설정")
        return
    if not _is_market_day("KR"):
        _log("INFO", "AUTO_BUY_SKIP", "오늘은 휴장일")
        return
    if _analysis_lock.locked():
        _log("INFO", "AUTO_BUY_SKIP", "analysis lock 사용 중")
        return
    # 재시작 중복 방지: 오늘 이미 매수 완료했으면 스킵
    if is_action_done("morning_buy"):
        _log("INFO", "AUTO_BUY_SKIP", "오늘 morning_buy 이미 완료")
        return

    channel_id = next(iter(ALLOWED_CHANNEL_IDS))
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        _log("WARN", "AUTO_BUY_SKIP", f"채널 접근 실패 channel_id={channel_id}")
        return

    trade_date = str(datetime.date.today())
    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
    loop = asyncio.get_running_loop()

    async with _analysis_lock:
        _log("INFO", "AUTO_BUY_START", f"date={trade_date} target_picks={DAY_TRADE_PICKS}")
        await channel.send(
            f"🌅 **데이 트레이딩 — 자동매수** 시작 ({AUTO_BUY_TIME} KST)"
        )

        # ── 1) 실시간 멀티시그널 스코어링 ──
        try:
            scoring_msg = await channel.send("📊 실시간 순위 API 4종 스코어링 중…")
            candidates = await _compute_stock_scores(count=10)
        except Exception as e:
            _log("ERROR", "AUTO_BUY_SCORING_ERROR", str(e)[:200])
            await channel.send(f"❌ 순위 조회 실패: {str(e)[:300]}")
            return

        if not candidates:
            _log("INFO", "AUTO_BUY_NO_CANDIDATE", "스코어링 결과 후보 없음")
            await scoring_msg.edit(content="❌ 매수 후보가 없습니다. (시장 상황 부적합)")
            return

        # 이미 보유 중인 종목 제외
        try:
            balance_data = await loop.run_in_executor(None, kis.get_balance, "KR")
            held_tickers = {h["ticker"] for h in balance_data.get("holdings", [])}
        except Exception:
            held_tickers = set()

        filtered = [c for c in candidates if c["ticker"] not in held_tickers]
        if not filtered:
            _log("INFO", "AUTO_BUY_ALL_HELD", "후보가 모두 보유 종목")
            await scoring_msg.edit(content="📋 스코어링 후보가 모두 이미 보유 중입니다.")
            return

        _log("INFO", "AUTO_BUY_CANDIDATES", f"raw={len(candidates)} filtered={len(filtered)}")

        # 후보 리스트 임베드
        desc_lines = []
        for c in filtered:
            sig_str = ", ".join(c["signals"])
            desc_lines.append(
                f"**{c['score']}점** {c['name']} (`{c['ticker']}`) "
                f"— {c['price']:,}원 ({c['prdy_ctrt']:+.1f}%) | {sig_str}"
            )
        score_embed = discord.Embed(
            title=f"🏆 멀티시그널 후보 TOP {len(filtered)}",
            description="\n".join(desc_lines),
            color=0x0066FF,
        )
        score_embed.set_footer(text=mode_label)
        await scoring_msg.edit(content=None, embed=score_embed)

        # ── 2) 상위 후보 순차 AI 분석 → BUY만 수집 ──
        buy_targets: list[dict] = []
        analyzed_count = 0
        analysis_candidates = filtered[:DAY_TRADE_PICKS]
        for c in analysis_candidates:

            analyzed_count += 1
            progress = await channel.send(
                f"🔍 [{analyzed_count}/{len(analysis_candidates)}] "
                f"**{c['name']}** (`{c['ticker']}`) AI 분석 중… (약 3~5분)"
            )
            try:
                ta = TradingAgentsGraph(debug=False, config=config)
                analysis_symbol = _yf_ticker(c["ticker"], reference_price=c["price"])
                final_state, decision = await loop.run_in_executor(
                    None, ta.propagate, analysis_symbol, trade_date
                )
                emoji = "🟢" if decision == "BUY" else "🔴" if decision == "SELL" else "🟡"
                color_map = {"BUY": 0x00FF00, "SELL": 0xFF0000, "HOLD": 0xFFAA00}
                summary = _extract_decision_summary(final_state, decision, c["ticker"])
                embed = discord.Embed(
                    title=f"{emoji} {c['name']} ({c['ticker']}) → {decision}",
                    description=summary,
                    color=color_map.get(decision.upper(), 0x808080),
                )
                await progress.edit(content=None, embed=embed)

                report_text = _build_report_text(
                    final_state,
                    c["ticker"],
                    market="KR",
                    analysis_symbol=analysis_symbol,
                )
                report_file, report_path = _prepare_report_attachment(
                    report_text,
                    market="KR",
                    ticker=c["ticker"],
                    trade_date=trade_date,
                    scope="AUTO_KR",
                )
                if AUTO_REPORT_UPLOAD:
                    try:
                        await channel.send(file=report_file)
                    except Exception as e:
                        _log(
                            "WARN",
                            "AUTO_BUY_REPORT_UPLOAD_FAIL",
                            f"ticker={c['ticker']} error={str(e)[:160]} path={report_path or 'N/A'}",
                        )
                        await channel.send(
                            "⚠️ 보고서 파일 업로드에 실패했습니다. "
                            + (f"로컬 저장 파일: `{report_path}`" if report_path else "로컬 저장도 실패했습니다.")
                        )

                if decision.upper() == "BUY":
                    buy_targets.append({
                        "ticker": c["ticker"],
                        "name": c["name"],
                        "price": c["price"],
                        "score": c["score"],
                        "signals": c["signals"],
                    })
                _log("INFO", "AUTO_BUY_ANALYZED", f"ticker={c['ticker']} decision={decision}")
            except Exception as e:
                _log("ERROR", "AUTO_BUY_ANALYZE_ERROR", f"ticker={c['ticker']} error={str(e)[:160]}")
                await progress.edit(
                    content=f"❌ {c['name']} 분석 실패: {str(e)[:200]}"
                )

        if not buy_targets:
            _log("INFO", "AUTO_BUY_NO_BUY_TARGET", "분석 완료 후 BUY 대상 없음")
            await channel.send("📋 **AI 분석 완료** — BUY 종목이 없어 매수를 건너뜁니다.")
            return

        # ── 3) 통장 전액 균등분배 → 자동 매수 ──
        try:
            balance_data = await loop.run_in_executor(None, kis.get_balance, "KR")
            cash = balance_data.get("summary", {}).get("cash", 0)
        except Exception as e:
            _log("ERROR", "AUTO_BUY_BALANCE_ERROR", str(e)[:200])
            await channel.send(f"❌ 잔액 조회 실패: {str(e)[:300]}")
            return

        if cash <= 0:
            _log("WARN", "AUTO_BUY_NO_CASH", "예수금 0원")
            await channel.send("❌ 예수금이 0원입니다. 매수할 수 없습니다.")
            return

        per_stock_budget = int(cash // len(buy_targets))
        buy_results: list[str] = []
        total_invested = 0

        for target in buy_targets:
            # 매수 직전 현재가 재조회
            try:
                current_price = await loop.run_in_executor(None, kis.get_price, target["ticker"], "KR")
            except Exception:
                current_price = target["price"]
            if current_price <= 0:
                buy_results.append(f"⚠️ {target['name']} — 현재가 조회 실패")
                continue

            qty = int(per_stock_budget // current_price)
            if qty <= 0:
                buy_results.append(
                    f"⚠️ {target['name']} — 예산({format_krw(per_stock_budget)}) 부족"
                )
                continue

            # 잔액 재확인
            try:
                fresh_bal = await loop.run_in_executor(None, kis.get_balance, "KR")
                remaining_cash = fresh_bal.get("summary", {}).get("cash", 0)
            except Exception:
                remaining_cash = cash

            if qty * current_price > remaining_cash:
                qty = int(remaining_cash // current_price)
                if qty <= 0:
                    buy_results.append(f"⚠️ {target['name']} — 잔액 부족")
                    continue

            try:
                result = await loop.run_in_executor(
                    None, kis.buy_stock, target["ticker"], qty, 0, "KR"
                )
                if result["success"]:
                    amount = qty * current_price
                    total_invested += amount
                    record_trade(
                        target["ticker"], target["name"], "BUY",
                        qty, current_price,
                        order_no=result.get("order_no", ""),
                        reason=f"데이트레이딩 자동매수 (score={target['score']})",
                        market="KR",
                        currency="KRW",
                    )
                    buy_results.append(
                        f"✅ {target['name']} ({target['ticker']}) — "
                        f"{qty}주 × {current_price:,}원 = {format_krw(amount)}"
                    )
                else:
                    buy_results.append(
                        f"❌ {target['name']} 매수실패: {result['message'][:100]}"
                    )
            except Exception as e:
                buy_results.append(f"❌ {target['name']} 매수오류: {str(e)[:100]}")

        # ── 결과 임베드 ──
        result_embed = discord.Embed(
            title=f"🌅 자동매수 결과 ({len(buy_targets)}종목)",
            description="\n".join(buy_results),
            color=0x00FF00,
            timestamp=datetime.datetime.now(KST),
        )
        result_embed.add_field(
            name="투자금액", value=format_krw(total_invested), inline=True
        )
        result_embed.add_field(
            name="예수금 잔액", value=format_krw(cash - total_invested), inline=True
        )
        result_embed.set_footer(text=f"데이 트레이딩 | {mode_label}")
        await channel.send(embed=result_embed)

        # 매수 완료 상태 기록 (재시작 시 중복 방지)
        bought_names = ", ".join(t["name"] for t in buy_targets)
        mark_action_done("morning_buy", details=f"매수: {bought_names}")
        _log("INFO", "AUTO_BUY_DONE", f"buy_count={len(buy_targets)} invested={total_invested}")


@morning_auto_buy.before_loop
async def before_morning():
    await bot.wait_until_ready()


# ─── 스케줄: 오후 자동매도 (15:20 KST) ───────────────────


@tasks.loop(time=datetime.time(hour=_sell_h, minute=_sell_m, tzinfo=KST))
async def afternoon_auto_sell():
    """매일 오후(기본 15:20) 보유 전종목 전량 시장가 매도."""
    if not ALLOWED_CHANNEL_IDS or not kis.is_configured:
        _log("INFO", "AUTO_SELL_SKIP", "채널 미설정 또는 KIS 미설정")
        return
    if not _is_market_day("KR"):
        _log("INFO", "AUTO_SELL_SKIP", "오늘은 휴장일")
        return
    # 재시작 중복 방지: 오늘 이미 매도 완료했으면 스킵
    if is_action_done("afternoon_sell"):
        _log("INFO", "AUTO_SELL_SKIP", "오늘 afternoon_sell 이미 완료")
        return

    channel_id = next(iter(ALLOWED_CHANNEL_IDS))
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        _log("WARN", "AUTO_SELL_SKIP", f"채널 접근 실패 channel_id={channel_id}")
        return

    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
    loop = asyncio.get_running_loop()
    _log("INFO", "AUTO_SELL_START", f"time={AUTO_SELL_TIME}")

    await channel.send(
        f"🌇 **데이 트레이딩 — 오후 전량매도** 시작 ({AUTO_SELL_TIME} KST)"
    )

    # 보유종목 확인
    try:
        balance_data = await loop.run_in_executor(None, kis.get_balance, "KR")
        holdings = balance_data.get("holdings", [])
    except Exception as e:
        _log("ERROR", "AUTO_SELL_BALANCE_ERROR", str(e)[:200])
        await channel.send(f"❌ 잔고 조회 실패: {str(e)[:300]}")
        return

    if not holdings:
        _log("INFO", "AUTO_SELL_EMPTY", "보유 종목 없음")
        await channel.send("📋 보유 종목이 없습니다. 매도 생략.")
        return

    _log("INFO", "AUTO_SELL_HOLDINGS", f"count={len(holdings)}")

    # 전량 매도 실행
    sell_results = await loop.run_in_executor(None, kis.sell_all_holdings, "KR")

    # DB 기록 + 임베드 작성
    result_lines: list[str] = []
    total_pnl = 0
    total_invested = 0
    total_recovered = 0

    for sr in sell_results:
        if sr["success"]:
            record_trade(
                sr["ticker"], sr["name"], "SELL",
                sr["qty"], sr["sell_price"],
                order_no=sr.get("order_no", ""),
                reason="데이트레이딩 자동매도",
                market="KR",
                currency="KRW",
            )
            if sr["avg_price"] > 0 and sr["sell_price"] > 0:
                record_pnl(
                    sr["ticker"], sr["name"],
                    sr["avg_price"], sr["sell_price"], sr["qty"],
                    market="KR",
                    currency="KRW",
                )
            pnl = (sr["sell_price"] - sr["avg_price"]) * sr["qty"]
            pnl_rate = (
                (sr["sell_price"] - sr["avg_price"]) / sr["avg_price"] * 100
                if sr["avg_price"] > 0 else 0
            )
            invested = sr["avg_price"] * sr["qty"]
            recovered = sr["sell_price"] * sr["qty"]
            total_pnl += pnl
            total_invested += invested
            total_recovered += recovered
            emoji = "🟢" if pnl >= 0 else "🔴"
            result_lines.append(
                f"{emoji} **{sr['name']}** (`{sr['ticker']}`) — "
                f"{sr['qty']}주 | {_format_money(sr['avg_price'], 'KRW')}→"
                f"{_format_money(sr['sell_price'], 'KRW')} | "
                f"{_format_money(pnl, 'KRW')} ({pnl_rate:+.1f}%)"
            )
        else:
            result_lines.append(
                f"❌ **{sr['name']}** (`{sr['ticker']}`) 매도실패: {sr['message'][:80]}"
            )

    # 실패한 종목 1회 재시도
    failed = [sr for sr in sell_results if not sr["success"]]
    if failed:
        await channel.send(f"⚠️ 매도 실패 {len(failed)}건 — 60초 후 재시도…")
        await asyncio.sleep(60)
        for sr in failed:
            try:
                retry = await loop.run_in_executor(
                    None, kis.sell_stock, sr["ticker"], sr["qty"], 0, "KR"
                )
                if retry["success"]:
                    try:
                        sp = await loop.run_in_executor(None, kis.get_price, sr["ticker"], "KR")
                    except Exception:
                        sp = 0
                    record_trade(
                        sr["ticker"], sr["name"], "SELL", sr["qty"], sp,
                        order_no=retry.get("order_no", ""),
                        reason="데이트레이딩 재시도매도",
                        market="KR",
                        currency="KRW",
                    )
                    if sr["avg_price"] > 0 and sp > 0:
                        record_pnl(
                            sr["ticker"],
                            sr["name"],
                            sr["avg_price"],
                            sp,
                            sr["qty"],
                            market="KR",
                            currency="KRW",
                        )
                    pnl = (sp - sr["avg_price"]) * sr["qty"]
                    result_lines.append(
                        f"✅ [재시도 성공] {sr['name']} — {_format_money(pnl, 'KRW')}"
                    )
                    total_pnl += pnl
                else:
                    result_lines.append(
                        f"❌ [재시도 실패] {sr['name']}: {retry['message'][:80]}"
                    )
            except Exception as e:
                result_lines.append(
                    f"❌ [재시도 오류] {sr['name']}: {str(e)[:80]}"
                )

    # 일일 손익 요약 임베드
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    cumulative = get_total_pnl(currency="KRW")

    sell_embed = discord.Embed(
        title="🌇 오후 전량매도 결과",
        description="\n".join(result_lines) if result_lines else "매도 대상 없음",
        color=0x00FF00 if total_pnl >= 0 else 0xFF0000,
        timestamp=datetime.datetime.now(KST),
    )
    sell_embed.add_field(
        name=f"{pnl_emoji} 오늘 손익", value=_format_money(total_pnl, "KRW"), inline=True
    )
    sell_embed.add_field(
        name="투입금액", value=_format_money(total_invested, "KRW"), inline=True
    )
    sell_embed.add_field(
        name="회수금액", value=_format_money(total_recovered, "KRW"), inline=True
    )
    sell_embed.add_field(
        name="📊 누적 손익",
        value=f"{_format_money(cumulative['total_pnl'], 'KRW')} | 승률 {cumulative['win_rate']}%",
        inline=False,
    )
    sell_embed.set_footer(text=f"데이 트레이딩 | {mode_label}")
    await channel.send(embed=sell_embed)

    # 매도 완료 상태 기록 (재시작 시 중복 방지)
    mark_action_done("afternoon_sell", details=f"{len(sell_results)}종목 매도")
    _log("INFO", "AUTO_SELL_DONE", f"sold={len(sell_results)} total_pnl={total_pnl}")


@afternoon_auto_sell.before_loop
async def before_afternoon():
    await bot.wait_until_ready()


# ─── 스케줄: 미국 자동매수 (09:35 ET) ───────────────────────
@tasks.loop(time=datetime.time(hour=_us_buy_h, minute=_us_buy_m, tzinfo=NY_TZ))
async def us_morning_auto_buy():
    """매일 오전(미국 현지) 상위 후보 분석 후 자동 매수."""
    if not ENABLE_US_TRADING or not kis.enable_us_trading:
        return
    if not ALLOWED_CHANNEL_IDS or not kis.is_configured:
        _log("INFO", "US_AUTO_BUY_SKIP", "채널 미설정 또는 KIS 미설정")
        return
    if not _is_market_day("US"):
        _log("INFO", "US_AUTO_BUY_SKIP", "오늘은 미국시장 휴장일")
        return
    if not _is_market_open_now("US"):
        _log("INFO", "US_AUTO_BUY_SKIP", "미국 장시간 아님")
        return
    if _analysis_lock.locked():
        _log("INFO", "US_AUTO_BUY_SKIP", "analysis lock 사용 중")
        return
    if is_action_done("us_morning_buy"):
        _log("INFO", "US_AUTO_BUY_SKIP", "오늘 us_morning_buy 이미 완료")
        return

    channel_id = next(iter(ALLOWED_CHANNEL_IDS))
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        _log("WARN", "US_AUTO_BUY_SKIP", f"채널 접근 실패 channel_id={channel_id}")
        return

    trade_date = str(datetime.datetime.now(NY_TZ).date())
    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
    loop = asyncio.get_running_loop()

    async with _analysis_lock:
        _log("INFO", "US_AUTO_BUY_START", f"date={trade_date} target_picks={US_DAY_TRADE_PICKS}")
        await channel.send(
            f"🇺🇸🌅 **미국 자동매수** 시작 ({US_AUTO_BUY_TIME} ET)"
        )

        try:
            scoring_msg = await channel.send("📊 미국 후보 스코어링 중… (KIS 우선, yfinance fallback)")
            candidates = await _compute_us_stock_scores(count=max(10, US_DAY_TRADE_PICKS * 2))
        except Exception as e:
            _log("ERROR", "US_AUTO_BUY_SCORING_ERROR", str(e)[:200])
            await channel.send(f"❌ 미국 후보 조회 실패: {str(e)[:300]}")
            return

        if not candidates:
            _log("INFO", "US_AUTO_BUY_NO_CANDIDATE", "후보 없음")
            await scoring_msg.edit(content="❌ 미국 매수 후보가 없습니다.")
            return

        try:
            balance_data = await loop.run_in_executor(None, kis.get_balance, "US")
            held_tickers = {h["ticker"] for h in balance_data.get("holdings", [])}
        except Exception:
            held_tickers = set()

        filtered = [c for c in candidates if c["ticker"] not in held_tickers]
        if not filtered:
            await scoring_msg.edit(content="📋 후보 종목이 모두 이미 보유 중입니다.")
            return

        desc_lines = []
        for c in filtered:
            sig_str = ", ".join(c["signals"])
            desc_lines.append(
                f"**{c['score']}점** {c['name']} (`{c['ticker']}`) "
                f"— {_format_money(c['price'], 'USD')} ({c['prdy_ctrt']:+.2f}%) | {sig_str}"
            )
        score_embed = discord.Embed(
            title=f"🇺🇸 후보 TOP {len(filtered)}",
            description="\n".join(desc_lines),
            color=0x0066FF,
            timestamp=datetime.datetime.now(NY_TZ),
        )
        score_embed.set_footer(text=f"{mode_label} | USD")
        await scoring_msg.edit(content=None, embed=score_embed)

        buy_targets: list[dict] = []
        analyzed_count = 0
        analysis_candidates = filtered[:US_DAY_TRADE_PICKS]
        for c in analysis_candidates:
            analyzed_count += 1
            progress = await channel.send(
                f"🔍 [{analyzed_count}/{len(analysis_candidates)}] "
                f"**{c['name']}** (`{c['ticker']}`) AI 분석 중…"
            )
            try:
                ta = TradingAgentsGraph(debug=False, config=config)
                final_state, decision = await loop.run_in_executor(
                    None, ta.propagate, c["ticker"], trade_date
                )
                emoji = "🟢" if decision == "BUY" else "🔴" if decision == "SELL" else "🟡"
                color_map = {"BUY": 0x00FF00, "SELL": 0xFF0000, "HOLD": 0xFFAA00}
                summary = _extract_decision_summary(final_state, decision, c["ticker"], "US")
                embed = discord.Embed(
                    title=f"{emoji} {c['name']} ({c['ticker']}) → {decision}",
                    description=summary,
                    color=color_map.get(decision.upper(), 0x808080),
                )
                await progress.edit(content=None, embed=embed)

                report_text = _build_report_text(
                    final_state,
                    c["ticker"],
                    market="US",
                    analysis_symbol=c["ticker"],
                )
                report_file, report_path = _prepare_report_attachment(
                    report_text,
                    market="US",
                    ticker=c["ticker"],
                    trade_date=trade_date,
                    scope="AUTO_US",
                )
                if AUTO_REPORT_UPLOAD:
                    try:
                        await channel.send(file=report_file)
                    except Exception as e:
                        _log(
                            "WARN",
                            "US_AUTO_BUY_REPORT_UPLOAD_FAIL",
                            f"ticker={c['ticker']} error={str(e)[:160]} path={report_path or 'N/A'}",
                        )
                        await channel.send(
                            "⚠️ 보고서 파일 업로드에 실패했습니다. "
                            + (f"로컬 저장 파일: `{report_path}`" if report_path else "로컬 저장도 실패했습니다.")
                        )

                if decision.upper() == "BUY":
                    buy_targets.append(c)
                _log("INFO", "US_AUTO_BUY_ANALYZED", f"ticker={c['ticker']} decision={decision}")
            except Exception as e:
                _log("ERROR", "US_AUTO_BUY_ANALYZE_ERROR", f"ticker={c['ticker']} error={str(e)[:160]}")
                await progress.edit(content=f"❌ {c['name']} 분석 실패: {str(e)[:200]}")

        if not buy_targets:
            await channel.send("📋 **미국 AI 분석 완료** — BUY 종목이 없어 매수를 건너뜁니다.")
            return

        try:
            balance_data = await loop.run_in_executor(None, kis.get_balance, "US")
            cash = balance_data.get("summary", {}).get("USD", {}).get("cash", 0)
        except Exception as e:
            _log("ERROR", "US_AUTO_BUY_BALANCE_ERROR", str(e)[:200])
            await channel.send(f"❌ USD 잔액 조회 실패: {str(e)[:300]}")
            return

        if cash <= 0:
            await channel.send("❌ USD 예수금이 0입니다. 매수를 건너뜁니다.")
            return

        per_stock_budget = float(cash) / len(buy_targets)
        buy_results: list[str] = []
        total_invested = 0.0

        for target in buy_targets:
            try:
                current_price = await loop.run_in_executor(None, kis.get_price, target["ticker"], "US")
            except Exception:
                current_price = target["price"]
            if current_price <= 0:
                buy_results.append(f"⚠️ {target['name']} — 현재가 조회 실패")
                continue

            qty = int(per_stock_budget // current_price)
            if qty <= 0:
                buy_results.append(
                    f"⚠️ {target['name']} — 예산({_format_money(per_stock_budget, 'USD')}) 부족"
                )
                continue

            try:
                fresh_bal = await loop.run_in_executor(None, kis.get_balance, "US")
                remaining_cash = fresh_bal.get("summary", {}).get("USD", {}).get("cash", cash)
            except Exception:
                remaining_cash = cash

            if qty * current_price > remaining_cash:
                qty = int(remaining_cash // current_price)
                if qty <= 0:
                    buy_results.append(f"⚠️ {target['name']} — 잔액 부족")
                    continue

            try:
                result = await loop.run_in_executor(None, kis.buy_stock, target["ticker"], qty, 0, "US")
                if result["success"]:
                    amount = qty * current_price
                    total_invested += amount
                    record_trade(
                        target["ticker"],
                        target["name"],
                        "BUY",
                        qty,
                        current_price,
                        order_no=result.get("order_no", ""),
                        reason=f"미국 자동매수 (score={target['score']})",
                        market="US",
                        currency="USD",
                    )
                    buy_results.append(
                        f"✅ {target['name']} ({target['ticker']}) — "
                        f"{qty}주 × {_format_money(current_price, 'USD')} = {_format_money(amount, 'USD')}"
                    )
                else:
                    buy_results.append(f"❌ {target['name']} 매수실패: {result['message'][:100]}")
            except Exception as e:
                buy_results.append(f"❌ {target['name']} 매수오류: {str(e)[:100]}")

        result_embed = discord.Embed(
            title=f"🇺🇸🌅 자동매수 결과 ({len(buy_targets)}종목)",
            description="\n".join(buy_results),
            color=0x00FF00,
            timestamp=datetime.datetime.now(NY_TZ),
        )
        result_embed.add_field(name="투자금액", value=_format_money(total_invested, "USD"), inline=True)
        result_embed.add_field(
            name="예수금 잔액",
            value=_format_money(max(cash - total_invested, 0), "USD"),
            inline=True,
        )
        result_embed.set_footer(text=f"미국 데이 트레이딩 | {mode_label}")
        await channel.send(embed=result_embed)

        bought_names = ", ".join(t["name"] for t in buy_targets)
        mark_action_done("us_morning_buy", details=f"매수: {bought_names}")
        _log("INFO", "US_AUTO_BUY_DONE", f"buy_count={len(buy_targets)} invested={total_invested}")


@us_morning_auto_buy.before_loop
async def before_us_morning():
    await bot.wait_until_ready()


# ─── 스케줄: 미국 자동매도 (15:50 ET) ───────────────────────
@tasks.loop(time=datetime.time(hour=_us_sell_h, minute=_us_sell_m, tzinfo=NY_TZ))
async def us_afternoon_auto_sell():
    """매일 오후(미국 현지) 보유 미국주식 전량 시장가 매도."""
    if not ENABLE_US_TRADING or not kis.enable_us_trading:
        return
    if not ALLOWED_CHANNEL_IDS or not kis.is_configured:
        _log("INFO", "US_AUTO_SELL_SKIP", "채널 미설정 또는 KIS 미설정")
        return
    if not _is_market_day("US"):
        _log("INFO", "US_AUTO_SELL_SKIP", "미국시장 휴장일")
        return
    if not _is_market_open_now("US"):
        _log("INFO", "US_AUTO_SELL_SKIP", "미국 장시간 아님")
        return
    if is_action_done("us_afternoon_sell"):
        _log("INFO", "US_AUTO_SELL_SKIP", "오늘 us_afternoon_sell 이미 완료")
        return

    channel_id = next(iter(ALLOWED_CHANNEL_IDS))
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        _log("WARN", "US_AUTO_SELL_SKIP", f"채널 접근 실패 channel_id={channel_id}")
        return

    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"
    loop = asyncio.get_running_loop()
    _log("INFO", "US_AUTO_SELL_START", f"time={US_AUTO_SELL_TIME}")

    await channel.send(f"🇺🇸🌇 **미국 자동매도** 시작 ({US_AUTO_SELL_TIME} ET)")

    try:
        balance_data = await loop.run_in_executor(None, kis.get_balance, "US")
        holdings = balance_data.get("holdings", [])
    except Exception as e:
        _log("ERROR", "US_AUTO_SELL_BALANCE_ERROR", str(e)[:200])
        await channel.send(f"❌ 미국 잔고 조회 실패: {str(e)[:300]}")
        return

    if not holdings:
        await channel.send("📋 미국 보유 종목이 없습니다. 매도 생략.")
        return

    sell_results = await loop.run_in_executor(None, kis.sell_all_holdings, "US")

    result_lines: list[str] = []
    total_pnl = 0.0
    total_invested = 0.0
    total_recovered = 0.0

    for sr in sell_results:
        if sr["success"]:
            record_trade(
                sr["ticker"],
                sr["name"],
                "SELL",
                sr["qty"],
                sr["sell_price"],
                order_no=sr.get("order_no", ""),
                reason="미국 자동매도",
                market="US",
                currency="USD",
            )
            if sr["avg_price"] > 0 and sr["sell_price"] > 0:
                record_pnl(
                    sr["ticker"],
                    sr["name"],
                    sr["avg_price"],
                    sr["sell_price"],
                    sr["qty"],
                    market="US",
                    currency="USD",
                )
            pnl = (sr["sell_price"] - sr["avg_price"]) * sr["qty"]
            pnl_rate = (
                (sr["sell_price"] - sr["avg_price"]) / sr["avg_price"] * 100
                if sr["avg_price"] > 0 else 0
            )
            invested = sr["avg_price"] * sr["qty"]
            recovered = sr["sell_price"] * sr["qty"]
            total_pnl += pnl
            total_invested += invested
            total_recovered += recovered
            emoji = "🟢" if pnl >= 0 else "🔴"
            result_lines.append(
                f"{emoji} **{sr['name']}** (`{sr['ticker']}`) — "
                f"{sr['qty']}주 | {_format_money(sr['avg_price'], 'USD')}→{_format_money(sr['sell_price'], 'USD')} | "
                f"{_format_money(pnl, 'USD')} ({pnl_rate:+.1f}%)"
            )
        else:
            result_lines.append(f"❌ **{sr['name']}** (`{sr['ticker']}`) 매도실패: {sr['message'][:80]}")

    failed = [sr for sr in sell_results if not sr["success"]]
    if failed:
        await channel.send(f"⚠️ 미국 매도 실패 {len(failed)}건 — 60초 후 재시도…")
        await asyncio.sleep(60)
        for sr in failed:
            try:
                retry = await loop.run_in_executor(None, kis.sell_stock, sr["ticker"], sr["qty"], 0, "US")
                if retry["success"]:
                    try:
                        sp = await loop.run_in_executor(None, kis.get_price, sr["ticker"], "US")
                    except Exception:
                        sp = 0
                    record_trade(
                        sr["ticker"],
                        sr["name"],
                        "SELL",
                        sr["qty"],
                        sp,
                        order_no=retry.get("order_no", ""),
                        reason="미국 재시도매도",
                        market="US",
                        currency="USD",
                    )
                    if sr["avg_price"] > 0 and sp > 0:
                        record_pnl(
                            sr["ticker"],
                            sr["name"],
                            sr["avg_price"],
                            sp,
                            sr["qty"],
                            market="US",
                            currency="USD",
                        )
                    pnl = (sp - sr["avg_price"]) * sr["qty"]
                    result_lines.append(f"✅ [재시도 성공] {sr['name']} — {_format_money(pnl, 'USD')}")
                    total_pnl += pnl
                else:
                    result_lines.append(f"❌ [재시도 실패] {sr['name']}: {retry['message'][:80]}")
            except Exception as e:
                result_lines.append(f"❌ [재시도 오류] {sr['name']}: {str(e)[:80]}")

    cumulative = get_total_pnl(currency="USD")
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    sell_embed = discord.Embed(
        title="🇺🇸🌇 자동매도 결과",
        description="\n".join(result_lines) if result_lines else "매도 대상 없음",
        color=0x00FF00 if total_pnl >= 0 else 0xFF0000,
        timestamp=datetime.datetime.now(NY_TZ),
    )
    sell_embed.add_field(name=f"{pnl_emoji} 오늘 손익", value=_format_money(total_pnl, "USD"), inline=True)
    sell_embed.add_field(name="투입금액", value=_format_money(total_invested, "USD"), inline=True)
    sell_embed.add_field(name="회수금액", value=_format_money(total_recovered, "USD"), inline=True)
    sell_embed.add_field(
        name="📊 USD 누적 손익",
        value=f"{_format_money(cumulative['total_pnl'], 'USD')} | 승률 {cumulative['win_rate']}%",
        inline=False,
    )
    sell_embed.set_footer(text=f"미국 데이 트레이딩 | {mode_label}")
    await channel.send(embed=sell_embed)

    mark_action_done("us_afternoon_sell", details=f"{len(sell_results)}종목 매도")
    _log("INFO", "US_AUTO_SELL_DONE", f"sold={len(sell_results)} total_pnl={total_pnl}")


@us_afternoon_auto_sell.before_loop
async def before_us_afternoon():
    await bot.wait_until_ready()


# ─── 스케줄: 보유종목 손절/익절 모니터링 ─────────────────
@tasks.loop(minutes=MONITOR_INTERVAL_MIN)
async def monitor_holdings():
    """보유종목 수익률 감시 → 손절/익절 라인 도달 시 자동 매도."""
    if not ALLOWED_CHANNEL_IDS or not kis.is_configured:
        return
    if not _is_market_day("KR") and not _is_market_day("US"):
        return

    channel_id = next(iter(ALLOWED_CHANNEL_IDS))
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        loop = asyncio.get_running_loop()
        balance_data = await loop.run_in_executor(None, kis.get_balance)
        holdings = balance_data["holdings"]
    except Exception:
        return

    if holdings:
        _log("INFO", "MONITOR_SCAN", f"holdings={len(holdings)}")

    mode_label = "🧪 모의투자" if kis.virtual else "💰 실전투자"

    for h in holdings:
        rate = h["pnl_rate"]
        market = h.get("market", _market_of_ticker(h["ticker"]))
        if not _is_market_open_now(market):
            continue
        triggered = False
        title = ""
        desc_extra = ""

        if rate <= STOP_LOSS_PCT:
            triggered = True
            title = f"🚨 손절 자동매도: {h['name']} ({market})"
            desc_extra = f"⚠️ 손절 라인({STOP_LOSS_PCT}%) 도달 → 자동 시장가 매도"
        elif rate >= TAKE_PROFIT_PCT:
            triggered = True
            title = f"🎉 익절 자동매도: {h['name']} ({market})"
            desc_extra = f"✅ 익절 라인({TAKE_PROFIT_PCT}%) 도달 → 자동 시장가 매도"

        if not triggered:
            continue

        # 재시작 중복 방지: 이 종목 오늘 이미 손절/익절 했으면 스킵
        sl_action = f"stop_loss_{market}_{h['ticker']}"
        if is_action_done(sl_action):
            _log("INFO", "MONITOR_SKIP_DONE", f"ticker={h['ticker']} already_triggered_today")
            continue

        # 자동 매도 실행
        try:
            result = await loop.run_in_executor(
                None, kis.sell_stock, h["ticker"], h["qty"], 0, market
            )
            if result["success"]:
                mark_action_done(sl_action, details=f"{rate:+.1f}%")
                try:
                    sell_price = await loop.run_in_executor(None, kis.get_price, h["ticker"], market)
                except Exception:
                    sell_price = h["current_price"]
                record_trade(
                    h["ticker"], h["name"], "SELL", h["qty"], sell_price,
                    order_no=result.get("order_no", ""),
                    reason=f"손절/익절 자동매도 ({rate:+.1f}%)",
                    market=market,
                    currency=h.get("currency", _currency_of_market(market)),
                )
                if h["avg_price"] > 0 and sell_price > 0:
                    record_pnl(
                        h["ticker"],
                        h["name"],
                        h["avg_price"],
                        sell_price,
                        h["qty"],
                        market=market,
                        currency=h.get("currency", _currency_of_market(market)),
                    )
                currency = h.get("currency", _currency_of_market(market))
                embed = discord.Embed(
                    title=title,
                    description=(
                        f"**시장:** {market}\n"
                        f"**종목:** {h['name']} (`{h['ticker']}`)\n"
                        f"**매도:** {h['qty']}주 × {_format_money(sell_price, currency)}\n"
                        f"**손익:** {_format_money(h['pnl'], currency)} ({rate:+.2f}%)\n\n"
                        f"{desc_extra}"
                    ),
                    color=0xFF0000 if rate < 0 else 0x00FF00,
                )
                embed.set_footer(text=f"{mode_label} | {currency}")
                await channel.send(embed=embed)
                _log(
                    "INFO",
                    "MONITOR_SELL_DONE",
                    f"market={market} ticker={h['ticker']} qty={h['qty']} rate={rate:+.2f}%",
                )
            else:
                _log("WARN", "MONITOR_SELL_FAIL", f"ticker={h['ticker']} message={result['message'][:120]}")
                await channel.send(
                    f"❌ {h['name']} 자동매도 실패: {result['message'][:200]}"
                )
        except Exception as e:
            _log("ERROR", "MONITOR_SELL_ERROR", f"ticker={h['ticker']} error={str(e)[:160]}")
            await channel.send(
                f"❌ {h['name']} 자동매도 오류: {str(e)[:200]}"
            )


@monitor_holdings.before_loop
async def before_monitor():
    await bot.wait_until_ready()


# ─── Bot Events ────────────────────────────────────────────────
@bot.event
async def on_ready():
    synced = await tree.sync()
    if not morning_auto_buy.is_running():
        morning_auto_buy.start()
    if not afternoon_auto_sell.is_running():
        afternoon_auto_sell.start()
    if ENABLE_US_TRADING and not us_morning_auto_buy.is_running():
        us_morning_auto_buy.start()
    if ENABLE_US_TRADING and not us_afternoon_auto_sell.is_running():
        us_afternoon_auto_sell.start()
    if not monitor_holdings.is_running():
        monitor_holdings.start()
    print(f"✅ {bot.user} 로그인 완료!")
    print(f"   서버 수: {len(bot.guilds)}")
    print(f"   동기화된 슬래시 명령 수: {len(synced)}")
    print("   슬래시 명령: /분석, /대형주, /잔고, /매도, /상태, /봇정보, /수익")
    print(f"   KIS: {'✅ 설정됨' if kis.is_configured else '❌ 미설정'}")
    print(f"   모드: {'🧪 모의투자' if kis.virtual else '💰 실전투자'}")
    print(f"   KR 데이 트레이딩: 매수 {AUTO_BUY_TIME} / 매도 {AUTO_SELL_TIME} KST")
    print(f"   KR 매수 종목 수: {DAY_TRADE_PICKS}개 | 예산: 통장 전액")
    if ENABLE_US_TRADING:
        print(f"   US 데이 트레이딩: 매수 {US_AUTO_BUY_TIME} / 매도 {US_AUTO_SELL_TIME} ET")
        print(f"   US 매수 종목 수: {US_DAY_TRADE_PICKS}개 | 예산: USD 예수금")
    else:
        print("   US 데이 트레이딩: 비활성화 (ENABLE_US_TRADING=false)")
    print(f"   손절: {STOP_LOSS_PCT}% | 익절: {TAKE_PROFIT_PCT}%")
    print(f"   모니터링: {MONITOR_INTERVAL_MIN}분 간격")
    if ALLOWED_CHANNEL_IDS:
        print(f"   허용 채널: {ALLOWED_CHANNEL_IDS}")
    else:
        print("   허용 채널: 전체 (제한 없음)")
        print("   ⚠️ 자동매매: DISCORD_CHANNEL_IDS 설정 필요")


# ─── Entry Point ───────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
