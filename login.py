import httpx
import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import quote

from playwright.async_api import async_playwright


# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

LOGIN_URL = "https://m.betking.com/islands/_actions/login/"
USERNAME  = "09120183273"
PASSWORD  = "Edmond99@"

LOGIN_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng",
    "Origin":          "https://m.betking.com",
    "Dnt":             "1",
    "Sec-Gpc":         "1",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "Priority":        "u=0",
    "Te":              "trailers",
    "Cookie":          "ABTestHomePrematchNewApi=true; ABTestHomePrematchBoostedNewApi=true",
}

OPEN_BETS_URL    = "https://m.betking.com/en-ng/my-bets/sports/open"
SETTLED_API_PATH = "/en-ng/my-bets/sports/settled"

SETTLED_BUTTON_SELECTOR = 'button[data-testid="coupon-filters-category-settled"]'

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
)


# ══════════════════════════════════════════════════════════════════════════════
# DB Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_db_config():
    base   = os.path.dirname(os.path.abspath(__file__))
    config = {}
    with open(os.path.join(base, "db.txt"), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"')
    return config


def _get_supabase():
    from supabase import create_client
    config = _load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])


# ══════════════════════════════════════════════════════════════════════════════
# Cookie Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _build_seed_cookie(access_token: str, refresh_token: str) -> str:
    """
    Builds the initial cookie used only to seed the Playwright browser context
    so the site treats us as logged in when it's opened. This is NOT what gets
    saved to the DB anymore — the real cookie is captured later from the
    browser's actual outgoing request to the settled-bets API.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
                f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    ts_encoded = quote(timestamp, safe="")

    return (
        "ABTestHomePrematchNewApi=true; "
        "ABTestHomePrematchBoostedNewApi=true; "
        f"accessToken={access_token}; "
        f"lastLoginTimeStamp={ts_encoded}; "
        f"refreshToken={refresh_token}; "
        "ABTestPrematchToLiveTransition=true; "
        "ABTestCrossSellRollout=true; "
        "ABTestNewBuildCoupon=false; "
        "ABTestMybetSettlementAwareRollout=C"
    )


def _seed_cookie_to_playwright_cookies(seed_cookie: str, domain: str = "m.betking.com") -> list[dict]:
    """
    Converts a "key=value; key=value" cookie string into the list-of-dicts
    format Playwright's add_cookies() expects.
    """
    cookies = []
    for part in seed_cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name":   name.strip(),
            "value":  value.strip(),
            "domain": domain,
            "path":   "/",
        })
    return cookies


def _strip_cookie_prefix(raw_cookie_header: str) -> str:
    """
    The captured request header value is just the cookie string itself
    (Playwright's request.headers already excludes the "Cookie:" name),
    but we strip a leading "Cookie:" defensively in case it's ever present.
    """
    value = raw_cookie_header.strip()
    if value.lower().startswith("cookie:"):
        value = value[len("cookie:"):].strip()
    return value


# ══════════════════════════════════════════════════════════════════════════════
# Browser Cookie Capture
# ══════════════════════════════════════════════════════════════════════════════

async def _capture_real_cookie_via_browser(seed_cookie: str) -> str | None:
    """
    Launches Firefox via Playwright, seeds it with the tokens from the login
    request, opens the open-bets page, clicks the "Settled" filter pill, and
    listens for the outgoing request to the settled-bets API. Returns the
    exact Cookie header value Firefox sent on that request — this is the
    accurate, complete cookie (including anything the browser/server adds
    that our manually-built string wouldn't have).
    """
    captured_cookie: str | None = None
    capture_event = asyncio.Event()

    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)
        context = await browser.new_context(user_agent=BROWSER_USER_AGENT)

        await context.add_cookies(_seed_cookie_to_playwright_cookies(seed_cookie))

        page = await context.new_page()

        def _on_request(request):
            nonlocal captured_cookie
            if captured_cookie is not None:
                return
            if SETTLED_API_PATH in request.url:
                cookie_header = request.headers.get("cookie")
                if cookie_header:
                    captured_cookie = _strip_cookie_prefix(cookie_header)
                    capture_event.set()

        page.on("request", _on_request)

        print("[login] opening open-bets page ...")
        await page.goto(OPEN_BETS_URL, wait_until="domcontentloaded")

        try:
            await page.click(SETTLED_BUTTON_SELECTOR, timeout=15000)
        except Exception as e:
            print(f"[login] settled button click err : {e}")
            await browser.close()
            return None

        print("[login] clicked Settled — waiting for API request ...")

        try:
            await asyncio.wait_for(capture_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            print("[login] timed out waiting for settled API request")
            await browser.close()
            return None

        await browser.close()

    return captured_cookie


# ══════════════════════════════════════════════════════════════════════════════
# Login
# ══════════════════════════════════════════════════════════════════════════════

async def login() -> bool:
    print("[login] sending login request ...")

    boundary = "----geckoformboundary837ee54a32476e6e6acfdfd4198332af"

    fields = [
        ("username",         USERNAME),
        ("password",         PASSWORD),
        ("anonymousId",      ""),
        ("locale",           "en-ng"),
        ("discriminationId", "19010103"),
        ("loginSource",      "login_button"),
        ("usernameType",     "text"),
    ]

    body_parts = []
    for name, value in fields:
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    body_parts.append(f"--{boundary}--\r\n")
    body = "".join(body_parts).encode()

    headers = {
        **LOGIN_HEADERS,
        "Content-Type":   f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    async with httpx.AsyncClient(http2=True, timeout=20.0) as client:
        resp = await client.post(LOGIN_URL, headers=headers, content=body)

    if resp.status_code != 200:
        print(f"[login] failed     : HTTP {resp.status_code}")
        return False

    try:
        data = resp.json()
    except Exception:
        print(f"[login] failed     : could not parse response")
        return False

    # Response is a JSON array, tokens are at fixed indices
    # index 3 = accessToken, index 5 = refreshToken
    try:
        access_token  = data[3]
        refresh_token = data[5]
    except (IndexError, KeyError):
        print(f"[login] failed     : tokens not found in response")
        return False

    if not access_token or not refresh_token:
        print(f"[login] failed     : empty tokens in response")
        return False

    print("[login] request login succeeded — launching browser to capture real cookie ...")

    seed_cookie = _build_seed_cookie(access_token, refresh_token)

    try:
        real_cookie = await _capture_real_cookie_via_browser(seed_cookie)
    except Exception as e:
        print(f"[login] browser capture err : {e}")
        return False

    if not real_cookie:
        print(f"[login] failed     : could not capture real cookie from browser")
        return False

    try:
        sb = _get_supabase()
        sb.table("betking").update({"cookie": real_cookie}).eq("id", 1).execute()
        print(f"[login] success    : real cookie saved to DB")
    except Exception as e:
        print(f"[login] db error   : {e}")
        return False

    return True


if __name__ == "__main__":
    asyncio.run(login())
