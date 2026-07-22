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


def _norm_tier(t) -> str:
    return "mediocre" if str(t).strip().lower() in ("mediocre", "m", "med") else "winner"


DEFAULT_TIER = _norm_tier(os.getenv("TV_BOT_DEFAULT_TIER", "winner"))


def _parse(text: str) -> List[Tuple[str, str]]:
    """Parse watchlist text into [(symbol, tier)]; tier defaults per DEFAULT_TIER.
    A line is 'SYMBOL' or 'SYMBOL winner|mediocre' (# comments ignored)."""
    out = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        sym = parts[0].upper()
        tier = _norm_tier(parts[1]) if len(parts) > 1 else DEFAULT_TIER
        out.append((sym, tier))
    return out


def _serialize(market: str, entries: List[Tuple[str, str]]) -> str:
    # winners written bare (the default); only 'mediocre' is tagged explicitly.
    lines = [HEADERS[market]]
    for sym, tier in entries:
        lines.append(f"{sym} mediocre\n" if _norm_tier(tier) == "mediocre" else f"{sym}\n")
    return "".join(lines)


def _read(path: str) -> List[Tuple[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return _parse(f.read())


def _write(market: str, entries: List[Tuple[str, str]]) -> None:
    with open(FILES[market], "w") as f:
        f.write(_serialize(market, entries))


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
        return _parse(text)
    except Exception:
        return None


def _gh_put(market: str, entries: List[Tuple[str, str]], message: str) -> bool:
    repo, headers = _gh_config()
    if not repo:
        return False
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{FILES[market]}"
        r = requests.get(url, headers=headers, timeout=15)
        sha = r.json().get("sha") if r.status_code == 200 else None
        content = _serialize(market, entries)
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
    """Both lists as [{'symbol','tier'}]. When configured, GitHub copy wins."""
    out = {}
    for market in ("us", "in"):
        remote = _gh_get(market) if sync else None
        if remote is not None:
            _write(market, remote)
            entries = remote
        else:
            entries = _read(FILES[market])
        out[market] = [{"symbol": s, "tier": t} for s, t in entries]
    return out


def _current(market: str) -> List[Tuple[str, str]]:
    """Freshest copy — prefer the GitHub repo (the cloud scanner's truth)."""
    remote = _gh_get(market)
    return remote if remote is not None else _read(FILES[market])


def status(symbol: str) -> dict:
    market, yf_sym = classify(symbol)
    if market is None:
        return {"supported": False, "symbol": yf_sym}
    entries = dict(_read(FILES[market]))
    return {"supported": True, "market": market, "symbol": yf_sym,
            "watching": yf_sym in entries, "tier": entries.get(yf_sym)}


def toggle(symbol: str) -> dict:
    """Add the symbol if absent (with DEFAULT_TIER), remove if present. Syncs to GitHub."""
    market, yf_sym = classify(symbol)
    if market is None:
        return {"error": f"{yf_sym} isn't supported (US stocks or NSE only)"}
    current = _current(market)
    if any(s == yf_sym for s, _ in current):
        current = [(s, t) for s, t in current if s != yf_sym]
        action, watching, tier = f"Removed {yf_sym} from the {market.upper()} watchlist", False, None
    else:
        current.append((yf_sym, DEFAULT_TIER))
        action, watching, tier = f"Added {yf_sym} ({DEFAULT_TIER}) to the {market.upper()} watchlist", True, DEFAULT_TIER
    _write(market, current)
    synced = _gh_put(market, current, action)
    return {"ok": True, "watching": watching, "market": market, "symbol": yf_sym, "tier": tier,
            "action": action, "synced_to_github": synced,
            "note": None if synced else
            f"Saved locally only — commit & push {FILES[market]} so the cloud scanner sees it."}


def set_tier(symbol: str, tier: str) -> dict:
    """Set a symbol's tier (winner/mediocre). Adds it if not present. Syncs to GitHub."""
    market, yf_sym = classify(symbol)
    if market is None:
        return {"error": f"{yf_sym} isn't supported (US stocks or NSE only)"}
    tier = _norm_tier(tier)
    current = _current(market)
    found = False
    for i, (s, _) in enumerate(current):
        if s == yf_sym:
            current[i] = (s, tier); found = True; break
    if not found:
        current.append((yf_sym, tier))
    action = f"Set {yf_sym} to {tier} on the {market.upper()} watchlist"
    _write(market, current)
    synced = _gh_put(market, current, action)
    return {"ok": True, "market": market, "symbol": yf_sym, "tier": tier,
            "action": action, "synced_to_github": synced,
            "note": None if synced else
            f"Saved locally only — commit & push {FILES[market]} so the cloud scanner sees it."}
