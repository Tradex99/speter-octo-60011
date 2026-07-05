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
    it can pop back up right before the next click.

    Previously this injected a <style> tag to set pointer-events: none,
    but the site's CSP can silently block injected <style> tags from
    taking effect (the init script itself still runs fine via CDP, but
    the CSS it renders is subject to the page's style-src policy) —
    which is why the banner kept intercepting clicks anyway.

    Instead, just remove the element from the DOM outright the moment it
    appears, and keep watching for it to reappear. DOM removal isn't
    subject to CSP style restrictions, so this works regardless of policy."""
    await context.add_init_script(
        """
        (() => {
            const removeBanner = () => {
                const el = document.getElementById('cookiebanner');
                if (el) el.remove();
            };
            removeBanner();
            new MutationObserver(removeBanner).observe(document.documentElement, {
                childList: true,
                subtree: true,
            });
        })();
        """
    )


async def _strip_cookie_banner(page) -> None:
    """Belt-and-suspenders: explicitly remove the banner right before a
    click that matters, in case the MutationObserver hasn't caught a
    freshly re-rendered banner yet."""
    try:
        await page.evaluate(
            "document.getElementById('cookiebanner')?.remove()"
        )
    except Exception:
        pass


async def _click(page, selector: str, timeout: int) -> None:
    """Click a selector, stripping the cookie banner first. If a normal
    click still times out (e.g. banner reappeared mid-click), retry once
    with force=True so Playwright skips the actionability/visibility
    check entirely rather than getting stuck."""
    await _strip_cookie_banner(page)
    try:
        await page.click(selector, timeout=timeout)
    except Exception:
        await _strip_cookie_banner(page)
        await page.click(selector, timeout=timeout, force=True)


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
        await _click(page, SIGN_IN_BUTTON_SELECTOR, TIMEOUT)

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
                await _click(page, SUBMIT_BUTTON_SELECTOR, TIMEOUT)
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
        sb.table("cookies").update({
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
