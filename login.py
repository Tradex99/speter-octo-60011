import asyncio
import os
from playwright.async_api import async_playwright
from supabase import create_client

PHONE     = "9120183273"
PASSWORD  = "Edmond99"
LOGIN_URL = "https://www.sportybet.com/ng/login/"


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


def save_cookie_to_db(cookie_str: str):
    config = load_db_config("db.txt")
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    supabase.table("sportybet").update({"cookie": cookie_str}).eq("id", 1).execute()
    print("cookie saved to DB")


async def run_login():
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
                "Gecko/20100101 Firefox/140.0"
            ),
            locale="en-US",
            timezone_id="Africa/Lagos",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-GPC":         "1",
            },
        )

        page = await context.new_page()

        print("opening login page")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Wait for phone input
        try:
            await page.wait_for_selector('input[name="phone"]', timeout=15000)
        except Exception:
            print("login page did not load")
            await browser.close()
            return False

        print("filling credentials")
        await page.fill('input[name="phone"]', PHONE)
        await asyncio.sleep(0.3)
        await page.fill('input[placeholder="Password"]', PASSWORD)
        await asyncio.sleep(0.3)

        # Track login success and cookie
        login_ok        = asyncio.Event()
        captured_cookie = {"value": None}

        async def handle_response(response):
            if "/api/ng/patron/accessToken" in response.url and response.request.method == "POST":
                print(f"accessToken response -> HTTP {response.status}")
                if response.status == 200:
                    login_ok.set()
                else:
                    try:
                        body = await response.text()
                        print(f"accessToken body -> {body[:300]}")
                    except Exception:
                        pass

        async def handle_request(request):
            if login_ok.is_set() and captured_cookie["value"] is None:
                cookie_header = request.headers.get("cookie") or request.headers.get("Cookie")
                if cookie_header and "accessToken" in cookie_header:
                    captured_cookie["value"] = cookie_header

        page.on("response", handle_response)
        page.on("request",  handle_request)

        # Click login
        await page.click('.af-button--primary:has-text("Login")')
        print("login button clicked")

        # Wait for accessToken response
        try:
            await asyncio.wait_for(login_ok.wait(), timeout=15)
        except asyncio.TimeoutError:
            print("login failed — no 200 from accessToken endpoint")
            await browser.close()
            return False

        print("login successful")

        # Wait for a request with the full cookie
        for _ in range(30):
            if captured_cookie["value"]:
                break
            await asyncio.sleep(0.5)

        if not captured_cookie["value"]:
            print("could not capture cookie from requests")
            await browser.close()
            return False

        save_cookie_to_db(captured_cookie["value"])
        await browser.close()
        return True


if __name__ == "__main__":
    asyncio.run(run_login())
