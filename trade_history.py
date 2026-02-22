"""
매매 이력 관리 (SQLite)
- 매수/매도 기록 저장
- 누적 수익률 조회
- 종목별 수익 요약
- 일일 상태 관리 (재시작 중복 방지)
"""

import datetime
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "trade_history.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """테이블 생성 (최초 1회)"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            side        TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
            qty         INTEGER NOT NULL,
            price       INTEGER NOT NULL,
            amount      INTEGER NOT NULL,
            order_no    TEXT DEFAULT '',
            reason      TEXT DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS pnl_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            buy_price   INTEGER NOT NULL,
            sell_price  INTEGER NOT NULL,
            qty         INTEGER NOT NULL,
            pnl         INTEGER NOT NULL,
            pnl_rate    REAL NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS daily_state (
            date         TEXT NOT NULL,
            action       TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            details      TEXT DEFAULT '',
            PRIMARY KEY (date, action)
        );
    """)
    conn.commit()
    conn.close()


def record_trade(
    ticker: str,
    name: str,
    side: str,
    qty: int,
    price: int,
    order_no: str = "",
    reason: str = "",
):
    """매수/매도 기록 저장"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO trades (ticker, name, side, qty, price, amount, order_no, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, name, side.upper(), qty, price, qty * price, order_no, reason),
    )
    conn.commit()
    conn.close()


def record_pnl(
    ticker: str,
    name: str,
    buy_price: int,
    sell_price: int,
    qty: int,
):
    """실현 손익 기록"""
    pnl = (sell_price - buy_price) * qty
    pnl_rate = ((sell_price - buy_price) / buy_price * 100) if buy_price > 0 else 0.0
    conn = _get_conn()
    conn.execute(
        """INSERT INTO pnl_log (ticker, name, buy_price, sell_price, qty, pnl, pnl_rate)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, name, buy_price, sell_price, qty, pnl, round(pnl_rate, 2)),
    )
    conn.commit()
    conn.close()


def get_total_pnl() -> dict:
    """누적 수익 요약"""
    conn = _get_conn()
    row = conn.execute(
        """SELECT
             COALESCE(SUM(pnl), 0) as total_pnl,
             COUNT(*) as trade_count,
             COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as win_count,
             COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) as loss_count
           FROM pnl_log"""
    ).fetchone()
    conn.close()

    total = row["total_pnl"]
    count = row["trade_count"]
    win = row["win_count"]
    loss = row["loss_count"]
    win_rate = (win / count * 100) if count > 0 else 0.0

    return {
        "total_pnl": total,
        "trade_count": count,
        "win_count": win,
        "loss_count": loss,
        "win_rate": round(win_rate, 1),
    }


def get_recent_trades(limit: int = 20) -> list[dict]:
    """최근 매매 이력"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_pnl(limit: int = 20) -> list[dict]:
    """최근 실현손익"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM pnl_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ticker_summary() -> list[dict]:
    """종목별 누적 수익 요약"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT
             ticker, name,
             COUNT(*) as count,
             SUM(pnl) as total_pnl,
             AVG(pnl_rate) as avg_pnl_rate
           FROM pnl_log
           GROUP BY ticker
           ORDER BY total_pnl DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── 일일 상태 관리 (재시작 중복 방지) ─────────────────────
def is_action_done(action: str, date: str | None = None) -> bool:
    """오늘 해당 액션이 이미 완료되었는지 확인.

    Args:
        action: 'morning_buy', 'afternoon_sell', 'stop_loss_005930' 등
        date: 날짜 (기본: 오늘)
    """
    if date is None:
        date = datetime.date.today().isoformat()
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM daily_state WHERE date = ? AND action = ?",
        (date, action),
    ).fetchone()
    conn.close()
    return row is not None


def mark_action_done(action: str, details: str = "", date: str | None = None):
    """해당 액션을 완료로 표시.

    Args:
        action: 'morning_buy', 'afternoon_sell', 'stop_loss_005930' 등
        details: 추가 정보 (매수 종목 등)
        date: 날짜 (기본: 오늘)
    """
    if date is None:
        date = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO daily_state (date, action, completed_at, details) VALUES (?, ?, ?, ?)",
        (date, action, now, details),
    )
    conn.commit()
    conn.close()


def get_daily_state(date: str | None = None) -> list[dict]:
    """오늘 완료된 모든 액션 조회."""
    if date is None:
        date = datetime.date.today().isoformat()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT action, completed_at, details FROM daily_state WHERE date = ? ORDER BY completed_at",
        (date,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# 모듈 로드 시 DB 초기화
init_db()
