"""Flag journal: log every generated flag, later score it against reality.

Every fresh analysis the server produces is logged to SQLite. The outcome
updater then fetches what price actually did 5 / 10 / 20 trading days later,
so /performance can show honest hit-rates per flag type.

Run a report from the CLI too:
    python -m tradingview_bot.journal
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.environ.get("TV_BOT_DB", os.path.join(DB_DIR, "flag_journal.db"))
HORIZONS = (5, 10, 20)  # trading days
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        symbol TEXT NOT NULL,
        yf_symbol TEXT NOT NULL,
        flag TEXT NOT NULL,
        score REAL,
        confidence REAL,
        price REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS outcomes (
        flag_id INTEGER NOT NULL,
        horizon_days INTEGER NOT NULL,
        price REAL NOT NULL,
        return_pct REAL NOT NULL,
        checked_ts TEXT NOT NULL,
        PRIMARY KEY (flag_id, horizon_days)
    )""")
    return conn


def log_flag(analysis: dict) -> Optional[int]:
    """Log a fresh analysis. Skipped if the same symbol+flag was logged in the
    last 6 hours (avoids spam while the user sits on one chart)."""
    if not analysis or "error" in analysis or not analysis.get("flag"):
        return None
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT flag, ts FROM flags WHERE symbol=? ORDER BY id DESC LIMIT 1",
            (analysis["symbol"],),
        ).fetchone()
        now = datetime.now(timezone.utc)
        if row:
            last_flag, last_ts = row
            age_h = (now - datetime.fromisoformat(last_ts)).total_seconds() / 3600
            if last_flag == analysis["flag"] and age_h < 6:
                return None
        cur = conn.execute(
            "INSERT INTO flags (ts, symbol, yf_symbol, flag, score, confidence, price) "
            "VALUES (?,?,?,?,?,?,?)",
            (now.isoformat(), analysis["symbol"], analysis.get("yf_symbol", analysis["symbol"]),
             analysis["flag"], analysis.get("score"), analysis.get("confidence"),
             analysis["price"]),
        )
        return cur.lastrowid


def update_outcomes(max_symbols: int = 20) -> int:
    """Fill in missing outcomes for flags old enough to be judged.

    One history fetch per symbol (batched). Returns number of outcomes written.
    """
    from stocks_agent.technicals.data import _history

    with _lock, _conn() as conn:
        pending = conn.execute("""
            SELECT f.id, f.ts, f.yf_symbol, f.price
            FROM flags f
            WHERE (SELECT COUNT(*) FROM outcomes o WHERE o.flag_id = f.id) < ?
            ORDER BY f.ts ASC
        """, (len(HORIZONS),)).fetchall()
    if not pending:
        return 0

    by_symbol: Dict[str, list] = {}
    for row in pending:
        by_symbol.setdefault(row[2], []).append(row)

    written = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for yf_symbol, rows in list(by_symbol.items())[:max_symbols]:
        try:
            hist = _history(yf_symbol, "2y", "1d")
        except Exception:
            continue
        if hist is None or hist.empty:
            continue
        idx = hist.index.tz_convert("UTC") if hist.index.tz is not None else hist.index.tz_localize("UTC")
        closes = hist["Close"].values
        with _lock, _conn() as conn:
            for flag_id, ts, _, entry_price in rows:
                entry_time = datetime.fromisoformat(ts)
                # first bar strictly after the flag was logged
                after = [i for i, t in enumerate(idx) if t.to_pydatetime() > entry_time]
                if not after:
                    continue
                start = after[0]
                done = {r[0] for r in conn.execute(
                    "SELECT horizon_days FROM outcomes WHERE flag_id=?", (flag_id,))}
                for h in HORIZONS:
                    if h in done:
                        continue
                    pos = start + h - 1
                    if pos >= len(closes):
                        continue  # not enough time has passed yet
                    out_price = float(closes[pos])
                    ret = (out_price / entry_price - 1) * 100
                    conn.execute(
                        "INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?)",
                        (flag_id, h, out_price, round(ret, 3), now_iso),
                    )
                    written += 1
    return written


def performance_summary() -> dict:
    """Aggregate hit-rates and average returns per flag type and horizon.

    'Correct' = BUY with positive forward return, SELL with negative.
    HOLD entries are reported for context but have no correctness notion.
    """
    with _lock, _conn() as conn:
        rows = conn.execute("""
            SELECT f.flag, o.horizon_days, o.return_pct
            FROM outcomes o JOIN flags f ON f.id = o.flag_id
        """).fetchall()
        recent = conn.execute("""
            SELECT f.ts, f.symbol, f.flag, f.score, f.confidence, f.price,
                   (SELECT o.return_pct FROM outcomes o
                    WHERE o.flag_id = f.id AND o.horizon_days = 5) AS r5,
                   (SELECT o.return_pct FROM outcomes o
                    WHERE o.flag_id = f.id AND o.horizon_days = 10) AS r10,
                   (SELECT o.return_pct FROM outcomes o
                    WHERE o.flag_id = f.id AND o.horizon_days = 20) AS r20
            FROM flags f ORDER BY f.id DESC LIMIT 50
        """).fetchall()
        total_flags = conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0]

    buckets: Dict[tuple, List[float]] = {}
    for flag, horizon, ret in rows:
        buckets.setdefault((flag, horizon), []).append(ret)

    stats = []
    for (flag, horizon), rets in sorted(buckets.items()):
        n = len(rets)
        avg = sum(rets) / n
        if flag == "BUY":
            hits = sum(1 for r in rets if r > 0)
        elif flag == "SELL":
            hits = sum(1 for r in rets if r < 0)
        else:
            hits = None
        stats.append({
            "flag": flag, "horizon_days": horizon, "n": n,
            "avg_return_pct": round(avg, 2),
            "hit_rate_pct": round(100 * hits / n, 1) if hits is not None else None,
        })

    return {
        "total_flags_logged": total_flags,
        "scored": stats,
        "recent": [
            {"ts": r[0][:16], "symbol": r[1], "flag": r[2], "score": r[3],
             "confidence": r[4], "price": r[5], "ret_5d": r[6], "ret_10d": r[7],
             "ret_20d": r[8]}
            for r in recent
        ],
    }


def render_html() -> str:
    """Minimal self-contained scoreboard page for GET /performance."""
    s = performance_summary()
    flag_color = {"BUY": "#16a34a", "SELL": "#dc2626", "HOLD": "#d97706"}

    def fmt(v, suffix=""):
        return "—" if v is None else f"{v}{suffix}"

    stat_rows = "".join(
        f"<tr><td style='color:{flag_color.get(r['flag'], '#ccc')};font-weight:700'>{r['flag']}</td>"
        f"<td>{r['horizon_days']}d</td><td>{r['n']}</td>"
        f"<td>{fmt(r['avg_return_pct'], '%')}</td><td>{fmt(r['hit_rate_pct'], '%')}</td></tr>"
        for r in s["scored"]
    ) or "<tr><td colspan=5>No scored flags yet — outcomes need 5+ trading days to mature.</td></tr>"

    recent_rows = "".join(
        f"<tr><td>{r['ts']}</td><td>{r['symbol']}</td>"
        f"<td style='color:{flag_color.get(r['flag'], '#ccc')};font-weight:700'>{r['flag']}</td>"
        f"<td>{fmt(r['score'])}</td><td>{r['price']}</td>"
        f"<td>{fmt(r['ret_5d'], '%')}</td><td>{fmt(r['ret_10d'], '%')}</td><td>{fmt(r['ret_20d'], '%')}</td></tr>"
        for r in s["recent"]
    ) or "<tr><td colspan=8>No flags logged yet.</td></tr>"

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Flag Performance</title>
<style>
 body{{background:#0f1117;color:#e5e7eb;font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:24px;max-width:900px;margin:auto}}
 table{{border-collapse:collapse;width:100%;margin:12px 0 28px;font-size:13px}}
 th,td{{padding:6px 10px;border-bottom:1px solid #262a35;text-align:left}}
 th{{color:#9ca3af;font-weight:600}}
 h1{{font-size:20px}} h2{{font-size:15px;color:#9ca3af}}
 .note{{color:#6b7280;font-size:12px}}
</style></head><body>
<h1>🤖 Stock Flag Bot — Performance</h1>
<p class="note">{s['total_flags_logged']} flags logged. Hit rate: BUY counted correct when forward
return &gt; 0, SELL when &lt; 0. Educational only — not financial advice.</p>
<h2>Scoreboard by flag &amp; horizon</h2>
<table><tr><th>Flag</th><th>Horizon</th><th>N</th><th>Avg return</th><th>Hit rate</th></tr>{stat_rows}</table>
<h2>Recent flags (latest 50)</h2>
<table><tr><th>Time (UTC)</th><th>Symbol</th><th>Flag</th><th>Score</th><th>Entry</th>
<th>+5d</th><th>+10d</th><th>+20d</th></tr>{recent_rows}</table>
</body></html>"""


if __name__ == "__main__":
    n = update_outcomes()
    print(f"outcomes updated: {n}")
    s = performance_summary()
    print(f"total flags logged: {s['total_flags_logged']}")
    for r in s["scored"]:
        hr = f"{r['hit_rate_pct']}%" if r["hit_rate_pct"] is not None else "n/a"
        print(f"  {r['flag']:4s} {r['horizon_days']:>2d}d  n={r['n']:<4d} avg={r['avg_return_pct']:+.2f}%  hit={hr}")
