"""Launch TradingView in Chromium (Playwright) and inject the sidebar bot."""

import pathlib
import time

SIDEBAR_JS = pathlib.Path(__file__).parent / "sidebar.js"
CHART_URL = "https://www.tradingview.com/chart/?symbol={symbol}"
# Stock browsing page: full sortable list of stocks — click any row to open it
MARKETS_URL = "https://www.tradingview.com/markets/stocks-usa/market-movers-all-stocks/"


def launch(port: int = 8765, symbol: str = None):
    from playwright.sync_api import sync_playwright

    # utf-8 explicitly: Windows defaults to cp1252 which chokes on the emoji in the JS
    js = SIDEBAR_JS.read_text(encoding="utf-8").replace("__PORT__", str(port))

    with sync_playwright() as p:
        # Prefer the system Chrome (no download needed — corporate proxies often
        # block Playwright's Chromium download); fall back to bundled Chromium.
        launch_args = [
            "--start-maximized",
            # let the injected sidebar (on https tradingview.com) call our
            # local http API without Private Network Access blocking
            "--disable-features=PrivateNetworkAccessChecks,LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights",
        ]
        try:
            browser = p.chromium.launch(channel="chrome", headless=False, args=launch_args)
        except Exception:
            browser = p.chromium.launch(headless=False, args=launch_args)
        # bypass_csp: TradingView's Content-Security-Policy would otherwise
        # block the injected sidebar from calling our local API
        context = browser.new_context(no_viewport=True, bypass_csp=True)
        # Re-inject on every navigation within tradingview.com
        context.add_init_script(js)
        page = context.new_page()
        start_url = CHART_URL.format(symbol=symbol) if symbol else MARKETS_URL
        page.goto(start_url, wait_until="domcontentloaded")
        # init scripts run before DOM exists on first paint; evaluate once now too
        try:
            page.evaluate(js)
        except Exception:
            pass

        print("🟢 TradingView opened. Navigate to any stock — the sidebar bot follows the symbol.")
        print("   Close the browser window (or Ctrl+C here) to stop.")
        try:
            while True:
                if not context.pages:
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                browser.close()
            except Exception:
                pass
