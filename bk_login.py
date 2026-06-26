import asyncio
import os
from datetime import datetime, timezone

from playwright.async_api import async_playwright

# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

HOME_URL       = "https://m.betking.com/en-ng"
LOGIN_API_PATH = "/islands/_actions/login/"

USERNAME = "09120183273"
PASSWORD = "Edmond99@"

SIGN_IN_BUTTON_SELECTOR = 'button[data-testid="signInButton"]'
LOGIN_FORM_SELECTOR     = 'dialog[open] form[data-testid="signIn"]'
USERNAME_INPUT_SELECTOR = 'dialog[open] input[name="username"]'
PASSWORD_INPUT_SELECTOR = 'dialog[open] input[name="password"]'
SUBMIT_BUTTON_SELECTOR  = 'dialog[open] form[data-testid="signIn"] button[type="submit"]'
COOKIE_BANNER_SELECTOR  = '#cookiebanner'

TIMEOUT = 30_000


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


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"


# ══════════════════════════════════════════════════════════════════════════════
# Login
# ══════════════════════════════════════════════════════════════════════════════

async def _neutralize_cookie_banner(context) -> None:
    """BetKing's Angular app re-renders #cookiebanner on top of the page
    (e.g. when the login dialog opens), so dismissing it once is a race —
    it can pop back up right before the next click. Instead, inject a
    style tag via an init script that runs before any page script on
    every navigation, permanently disabling pointer events on the banner
    no matter how many times Angular re-renders it."""
    await context.add_init_script(
        """
        (() => {
            const style = document.createElement('style');
            style.textContent = '#cookiebanner { pointer-events: none !important; }';
            (document.head || document.documentElement).appendChild(style);
        })();
        """
    )


async def login() -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        await _neutralize_cookie_banner(context)
        page = await context.new_page()

        print("[login] opening home page ...")
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=TIMEOUT)

        print("[login] clicking sign-in button ...")
        await page.click(SIGN_IN_BUTTON_SELECTOR, timeout=TIMEOUT)

        print("[login] waiting for login dialog ...")
        await page.wait_for_selector(LOGIN_FORM_SELECTOR, timeout=TIMEOUT)

        await page.fill(USERNAME_INPUT_SELECTOR, USERNAME, timeout=TIMEOUT)
        await page.fill(PASSWORD_INPUT_SELECTOR, PASSWORD, timeout=TIMEOUT)

        try:
            async with page.expect_response(
                lambda r: LOGIN_API_PATH in r.url and r.request.method == "POST",
                timeout=TIMEOUT,
            ) as resp_info:
                print("[login] submitting login form ...")
                await page.click(SUBMIT_BUTTON_SELECTOR, timeout=TIMEOUT)
            response = await resp_info.value
        except Exception as e:
            print(f"[login] failed     : login request err : {e}")
            await browser.close()
            return False

        if response.status != 200:
            print(f"[login] failed     : login request returned HTTP {response.status}")
            await browser.close()
            return False

        print("[login] login succeeded — waiting for page to finish loading ...")

        try:
            await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        except Exception:
            pass

        cookies = await context.cookies()
        cookie  = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        await browser.close()

    if not cookie:
        print("[login] failed     : no cookies captured")
        return False

    try:
        sb = _get_supabase()
        sb.table("betking").update({
            "cookie":     cookie,
            "created_at": _now_timestamp(),
        }).eq("name", "Chukwuebuka").execute()
        print("[login] success    : cookie saved to DB")
    except Exception as e:
        print(f"[login] db error   : {e}")
        return False

    return True


if __name__ == "__main__":
    asyncio.run(login())
