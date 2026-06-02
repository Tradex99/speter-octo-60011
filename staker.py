import asyncio
import argparse
import os
from datetime import datetime
from playwright.async_api import async_playwright
from supabase import create_client

STAKE_AMOUNT = "10"


def load_db_config(filepath="db.txt"):
    config = {}
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, filepath), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"')
    return config


def get_cookie_from_db() -> str:
    config = load_db_config("db.txt")
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    result = (
        supabase.table("sportybet")
        .select("cookie")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise Exception("No cookie found in database.")
    return result.data[0]["cookie"]


def parse_cookie_string(cookie_str: str) -> list[dict]:
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name":   name.strip(),
            "value":  value.strip(),
            "domain": ".sportybet.com",
            "path":   "/",
        })
    return cookies


def get_today_str() -> str:
    now = datetime.now()
    return f"{now.day}/{now.month}/{now.year}"


def increment_played():
    config   = load_db_config("db.txt")
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    today    = get_today_str()

    supabase.table("sp_tracker").upsert(
        {
            "name":   "sporty_wix",
            "max":    50,
            "played": 0,
            "win":    0,
            "lost":   0,
            "day":    today,
        },
        on_conflict="day",
        ignore_duplicates=True,
    ).execute()

    result = (
        supabase.table("sp_tracker")
        .select("id, played")
        .eq("day", today)
        .single()
        .execute()
    )
    row = result.data
    supabase.table("sp_tracker") \
        .update({"played": row["played"] + 1}) \
        .eq("id", row["id"]) \
        .execute()

    print(f"[tracker] played updated -> {row['played'] + 1}")


class Staker:
    def __init__(self):
        self._queue      = asyncio.Queue()
        self._context    = None
        self._page       = None
        self._running    = False
        self._playwright = None

    async def start(self):
        cookie_str = get_cookie_from_db()
        cookies    = parse_cookie_string(cookie_str)

        self._playwright = await async_playwright().start()
        browser = await self._playwright.firefox.launch(headless=False)
        self._context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
            viewport={"width": 1504, "height": 900},
            locale="en-US",
            timezone_id="Africa/Lagos",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.5",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        await self._context.add_cookies(cookies)
        self._page = await self._context.new_page()
        self._running = True

    async def queue(self, match_url: str, desc: str, market: str):
        await self._queue.put((match_url, desc, market))

    async def run(self):
        try:
            while self._running:
                match_url, desc, market = await self._queue.get()
                try:
                    await self._process(match_url, desc, market)
                except Exception as e:
                    print(f"error -> {e}")
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _process(self, match_url: str, desc: str, market: str):
        from module.betlist import check_can_bet
        if not check_can_bet():
            print("[staker] betlist check failed — skipping bet")
            return

        print("url loading")
        page = self._page

        order_status = {}

        async def handle_response(response):
            if "/api/ng/orders/order" in response.url and response.request.method == "POST":
                try:
                    order_status["status"] = response.status
                    body = await response.json()
                    order_status["body"] = body
                except Exception:
                    order_status["status"] = response.status

        page.on("response", handle_response)

        await page.goto(match_url, wait_until="commit")

        try:
            await page.wait_for_selector(".m-table-header-title", timeout=60000)
        except Exception:
            print("markets did not load in time")
            return

        # Step 1 — click the correct market outcome
        clicked = await page.evaluate(f"""
            () => {{
                const DESC   = '{desc}';
                const MARKET = '{market}';
                const wrappers = document.querySelectorAll('.m-table__wrapper');
                for (const wrapper of wrappers) {{
                    const title = wrapper.querySelector('.m-table-header-title');
                    if (!title || title.textContent.trim() !== DESC) continue;
                    const rows = wrapper.querySelectorAll('.m-table-cell--responsive');
                    for (const row of rows) {{
                        const items = row.querySelectorAll('.m-table-cell-item');
                        for (const item of items) {{
                            if (item.textContent.trim() === MARKET) {{
                                row.click();
                                return true;
                            }}
                        }}
                    }}
                }}
                return false;
            }}
        """)

        if not clicked:
            print(f'"{market}" button not found in "{desc}"')
            return

        print(f'"{market}" market clicked')

        # Step 2 — wait for betslip to populate
        try:
            await page.wait_for_selector("#j_betslip", timeout=10000)
        except Exception:
            print("betslip did not appear")
            return

        # Step 3 — fill stake amount via JS
        filled = await page.evaluate(f"""
            () => {{
                const input = document.querySelector('#j_betslip .m-input.fs-exclude');
                if (!input) return false;
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeInputValueSetter.call(input, '{STAKE_AMOUNT}');
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}
        """)

        if not filled:
            print("stake input not found")
            return

        print(f"stake set -> {STAKE_AMOUNT}")

        # Step 4 — click "Accept Changes" if present
        await asyncio.sleep(0.5)
        await page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('.af-button--primary');
                for (const btn of buttons) {
                    if (btn.textContent.includes('Accept Changes')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
        """)

        # Step 5 — click "Place Bet"
        await asyncio.sleep(0.5)
        placed = await page.evaluate("""
            () => {
                const btn = document.querySelector('[data-op="desktop-betslip-place-bet-button"]');
                if (btn && !btn.disabled) {
                    btn.click();
                    return true;
                }
                return false;
            }
        """)

        if not placed:
            print("place bet button not found or disabled")
            return

        print("place bet clicked")

        # Step 6 — click "Confirm"
        await asyncio.sleep(1)
        confirmed = await page.evaluate("""
            () => {
                const btn = document.querySelector('[data-op="desktop-betslip-confirm-button"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }
        """)

        if not confirmed:
            print("confirm button not found — session expired, re-logging in")
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, "login.py"],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            if result.returncode == 0:
                print("re-login done — retry on next tracker run")
            else:
                print("re-login failed")
            return

        print("confirm clicked")

        # Step 7 — check order API response
        await asyncio.sleep(2)
        if order_status.get("status") == 200:
            print("bet placed successfully (200 OK)")
            increment_played()
        elif "status" in order_status:
            print(f"bet failed -> HTTP {order_status['status']}")
            if "body" in order_status:
                print(f"response -> {order_status['body']}")
        else:
            print("no order response captured")

        page.remove_listener("response", handle_response)

    async def stop(self):
        self._running = False
        if self._playwright:
            await self._playwright.stop()


async def run_once(url: str, desc: str, market: str):
    from module.betlist import check_can_bet
    if not check_can_bet():
        return
    staker = Staker()
    await staker.start()
    try:
        await staker._process(url, desc, market)
    finally:
        await staker.stop()
        print("staker closed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url",    required=True, help="Full match URL")
    parser.add_argument("--desc",   required=True, help="Market category e.g. 'Over/Under'")
    parser.add_argument("--market", required=True, help="Outcome e.g. 'Under 2.5'")
    args = parser.parse_args()

    asyncio.run(run_once(args.url, args.desc, args.market))