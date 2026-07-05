import asyncio
import os
from playwright.async_api import async_playwright

LOGIN_API = "/desktop/feapi/AuthAjax/Login"
DOMAIN    = "sports.bet9ja.com"

USERNAME = "09120183273"
PASSWORD = "ogunjiofor99"

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"

COUPON_COUNTER_PATH = "/desktop/feapi/CouponAjax/GetRunningCouponCounter"

MAX_NAV_RETRIES = 3
MAX_LOGIN_RETRIES = 3


def _load_db_config() -> dict:
    base = os.path.dirname(os.path.abspath(__file__))
    for directory in [base, os.path.dirname(base)]:
        path = os.path.join(directory, "db.txt")
        if os.path.exists(path):
            config = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    config[k.strip()] = v.strip().strip('"')
            return config
    raise FileNotFoundError("db.txt not found")


def _get_supabase():
    from supabase import create_client
    cfg = _load_db_config()
    return create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])


def _now_timestamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"


def _save_cookie(cookie: str) -> bool:
    try:
        sb = _get_supabase()
        sb.table("cookies").update({
            "cookie":     cookie,
            "created_at": _now_timestamp(),
        }).eq("name", "wixnation").execute()
        print(f"[login] cookie saved ({len(cookie)} chars)")
        return True
    except Exception as e:
        print(f"[login] DB error : {e}")
        return False


async def _attempt_login() -> bool:
    login_ok = asyncio.Event()
    captured = {"login_failed": False}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-http2",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        context = await browser.new_context(user_agent=BROWSER_UA, ignore_https_errors=True)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        async def _on_response(response):
            if LOGIN_API in response.url and response.request.method == "POST":
                try:
                    body = await response.json()
                except Exception:
                    body = {}
                if body.get("R") == "ERROR":
                    err = (body.get("D") or {}).get("ERROR_DATA") or {}
                    print(f"[login] failed : {err.get('resultDescription', body)}")
                    captured["login_failed"] = True
                else:
                    print(f"[login] login succeeded")
                login_ok.set()

        page.on("response", _on_response)

        try:
            await page.goto(f"https://{DOMAIN}/", wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            print(f"[login] goto failed : {e}")
            await browser.close()
            return False

        await page.wait_for_timeout(2_000)
        await page.locator(".btn-login").first.click()
        await page.wait_for_selector(".login__popup", state="visible", timeout=10_000)
        await page.fill("#username", USERNAME)
        await page.fill("#password", PASSWORD)
        await page.wait_for_timeout(400)
        await page.locator(".login__popup .btn-primary-l").click()

        try:
            await asyncio.wait_for(login_ok.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            print("[login] timed out waiting for login response")
            await browser.close()
            return False

        if captured["login_failed"]:
            await browser.close()
            return False

        captured_cookie: str | None = None
        cookie_event = asyncio.Event()
        seen_urls: list[str] = []

        async def _on_request(request):
            nonlocal captured_cookie
            seen_urls.append(request.url)
            if captured_cookie is not None:
                return
            if COUPON_COUNTER_PATH not in request.url:
                return
            headers = await request.all_headers()
            cookie_header = headers.get("cookie")
            if cookie_header:
                captured_cookie = cookie_header
                cookie_event.set()

        page.on("request", _on_request)

        # Post-login, the site's own JS can fire a redirect/reload around the
        # same time we navigate, which races with our goto and makes Chromium
        # abort one of the two with net::ERR_ABORTED. Let things settle, then
        # retry the navigation itself a few times before giving up.
        navigated = False
        for nav_attempt in range(1, MAX_NAV_RETRIES + 1):
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
            await page.wait_for_timeout(1_500)

            try:
                await page.goto(
                    "https://sports.bet9ja.com/liveCompetitions",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                navigated = True
                break
            except Exception as e:
                print(f"[login] goto(liveCompetitions) attempt {nav_attempt}/{MAX_NAV_RETRIES} "
                      f"failed : {e} | current url = {page.url}")
                if "liveCompetitions" in page.url:
                    # The abort just lost a race against the site's own
                    # navigation — we're actually on the right page.
                    navigated = True
                    break

        if not navigated:
            print(f"[login] giving up on liveCompetitions navigation after {MAX_NAV_RETRIES} attempts")
            await browser.close()
            return False

        try:
            await asyncio.wait_for(cookie_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            coupon_hits = [u for u in seen_urls if "coupon.bet9ja.com" in u]
            print(f"[login] timed out — saw {len(seen_urls)} requests total, "
                  f"{len(coupon_hits)} to coupon.bet9ja.com")
            for u in coupon_hits[:5]:
                print(f"[login]   coupon host request : {u}")
            await browser.close()
            return False

        cookie_str = captured_cookie

        await browser.close()

    if not cookie_str:
        print("[login] failed : no cookies found")
        return False

    return _save_cookie(cookie_str)


async def login() -> bool:
    for attempt in range(1, MAX_LOGIN_RETRIES + 1):
        print(f"[login] starting attempt {attempt}/{MAX_LOGIN_RETRIES}")
        if await _attempt_login():
            return True
        print(f"[login] attempt {attempt}/{MAX_LOGIN_RETRIES} failed")

    print(f"[login] all {MAX_LOGIN_RETRIES} attempts failed")
    return False


if __name__ == "__main__":
    asyncio.run(login())
