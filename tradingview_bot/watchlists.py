"""Watchlist management: the repo files ARE the scanner's universe.

watchlist.txt    -> US symbols, traded on the Alpaca paper account
watchlist_in.txt -> NSE symbols (Yahoo .NS form), traded by the virtual broker

The GitHub copies are canonical (the cloud scanner checks out the repo), so
adds/removes sync through the contents API when GITHUB_TOKEN + GITHUB_REPO
are configured; otherwise they save locally and you commit/push yourself.
"""

import base64
import os
from typing import List, Optional, Tuple

import requests

US_FILE = os.getenv("TV_BOT_WATCHLIST", "watchlist.txt")
IN_FILE = os.getenv("TV_BOT_WATCHLIST_IN", "watchlist_in.txt")
FILES = {"us": US_FILE, "in": IN_FILE}
HEADERS = {
    "us": "# Symbols the auto-scanner may trade (US-listed, one per line).\n",
    "in": "# Indian (NSE) symbols the virtual paper broker may trade — Yahoo .NS form.\n",
}


def classify(symbol: str) -> Tuple[Optional[str], str]:
    """('us'|'in'|None, canonical symbol for the file)."""
    from stocks_agent.technicals.data import tradingview_to_yfinance
    yf_sym = tradingview_to_yfinance(symbol.strip().upper())
    if yf_sym.endswith(".NS") or yf_sym.endswith(".BO"):
        return "in", yf_sym
    if yf_sym and all(c not in yf_sym for c in ".-="):
        return "us", yf_sym
    return None, yf_sym


def _read(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            sym = line.split("#")[0].strip().upper()
            if sym:
                out.append(sym)
    return out


def _write(market: str, symbols: List[str]) -> None:
    with open(FILES[market], "w") as f:
        f.write(HEADERS[market])
        for s in symbols:
            f.write(s + "\n")


# ---------------- GitHub sync -------------------------------------------------

def _gh_config():
    repo = os.getenv("GITHUB_REPOSITORY") or os.getenv("GITHUB_REPO")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not repo or not token:
        return None, None
    return repo, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _gh_get(market: str) -> Optional[List[str]]:
    repo, headers = _gh_config()
    if not repo:
        return None
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{FILES[market]}",
                         headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        text = base64.b64decode(r.json()["content"]).decode()
        return [ln.split("#")[0].strip().upper() for ln in text.splitlines()
                if ln.split("#")[0].strip()]
    except Exception:
        return None


def _gh_put(market: str, symbols: List[str], message: str) -> bool:
    repo, headers = _gh_config()
    if not repo:
        return False
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{FILES[market]}"
        r = requests.get(url, headers=headers, timeout=15)
        sha = r.json().get("sha") if r.status_code == 200 else None
        content = HEADERS[market] + "".join(s + "\n" for s in symbols)
        body = {"message": message,
                "content": base64.b64encode(content.encode()).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=headers, json=body, timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False


# ---------------- public API --------------------------------------------------

def get_lists(sync: bool = True) -> dict:
    """Both lists. When configured, the GitHub copy wins and refreshes local."""
    out = {}
    for market in ("us", "in"):
        remote = _gh_get(market) if sync else None
        if remote is not None:
            _write(market, remote)
            out[market] = remote
        else:
            out[market] = _read(FILES[market])
    return out


def status(symbol: str) -> dict:
    market, yf_sym = classify(symbol)
    if market is None:
        return {"supported": False, "symbol": yf_sym}
    return {"supported": True, "market": market, "symbol": yf_sym,
            "watching": yf_sym in _read(FILES[market])}


def toggle(symbol: str) -> dict:
    """Add the symbol if absent, remove if present. Syncs to GitHub."""
    market, yf_sym = classify(symbol)
    if market is None:
        return {"error": f"{yf_sym} isn't supported (US stocks or NSE only)"}
    current = _gh_get(market)
    if current is None:
        current = _read(FILES[market])
    if yf_sym in current:
        current = [s for s in current if s != yf_sym]
        action, watching = f"Removed {yf_sym} from the {market.upper()} watchlist", False
    else:
        current.append(yf_sym)
        action, watching = f"Added {yf_sym} to the {market.upper()} watchlist", True
    _write(market, current)
    synced = _gh_put(market, current, action)
    return {"ok": True, "watching": watching, "market": market, "symbol": yf_sym,
            "action": action, "synced_to_github": synced,
            "note": None if synced else
            f"Saved locally only — commit & push {FILES[market]} so the cloud scanner sees it."}
