---
name: corporate-proxy-tls
description: This machine sits behind a TLS-intercepting corporate proxy — Python HTTPS needs truststore; Playwright browser downloads fail
metadata: 
  node_type: memory
  type: project
  originSessionId: 3920358b-7275-47f6-a920-a75550768c50
---

Shivam's Mac (Syfe corporate laptop) is behind a TLS-intercepting proxy with a self-signed root cert (as of 2026-07-17).

**Why:** Plain Python `requests`/`urllib` fail with SSLError on any external HTTPS; yfinance's internal curl_cffi client fails too but misreports it as `YFRateLimitError`; `playwright install chromium` fails with SELF_SIGNED_CERT_IN_CHAIN.

**How to apply:**
- Use `truststore.inject_into_ssl()` (uses macOS keychain) before any HTTPS in Python — already wired into `stocks_agent/technicals/data.py`.
- curl_cffi ignores truststore — avoid it here; the repo fetches Yahoo's v8 chart API directly with `requests` instead of yfinance for OHLCV.
- For Playwright, drive system Chrome via `channel="chrome"` instead of downloading Chromium.
- `uv` is not installed; use `python3 -m venv .venv` + pip (Python 3.14).
