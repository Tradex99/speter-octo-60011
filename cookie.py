import os
import sys

from playwright.sync_api import sync_playwright


URL = "https://arb-bot.infinityfree.io/?i=1"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
COOKIE_NAME = "__test"


def load_db_config():
    here = os.path.dirname(os.path.abspath(__file__))

    candidates = [
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
    ]

    for path in candidates:
        if os.path.exists(path):
            config = {}

            with open(path, "r") as file:
                for line in file:
                    line = line.strip()

                    if not line or line.startswith("#"):
                        continue

                    if "=" in line:
                        key, _, value = line.partition("=")
                        config[key.strip()] = value.strip().strip('"')

            return config

    raise FileNotFoundError("db.txt not found")


def get_supabase():
    from supabase import create_client

    config = load_db_config()

    return create_client(
        config["SUPABASE_URL"],
        config["SUPABASE_KEY"],
    )


def fetch_test_cookie():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(3000)

        cookies = context.cookies()
        browser.close()

    for cookie in cookies:
        if cookie["name"] == COOKIE_NAME:
            return cookie["value"]

    raise RuntimeError(
        f"'{COOKIE_NAME}' cookie not found — "
        f"cookies seen: {[c['name'] for c in cookies]}"
    )


def update_all_rows(cookie_value):
    supabase = get_supabase()

    rows = (
        supabase
        .table("jumptask")
        .select("id")
        .execute()
    )

    if not rows.data:
        raise RuntimeError("No rows found in jumptask")

    for row in rows.data:
        supabase.table("jumptask").update(
            {"cookie": cookie_value}
        ).eq(
            "id", row["id"]
        ).execute()

        print(f"Updated ID={row['id']}")


def main():
    print(f"Fetching {URL} ...")

    cookie = fetch_test_cookie()

    print(f"Got {COOKIE_NAME} = {cookie}")

    update_all_rows(cookie)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)