import httpx
import time
import os
from datetime import datetime
from supabase import create_client

MAX_OPEN_BETS = 12  # stop staking if open bets >= this


def load_db_config(filepath="db.txt"):
    config = {}
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def parse_cookie_string(cookie_str: str) -> dict:
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies[name.strip()] = value.strip()
    return cookies


def build_headers(cookie_str: str) -> dict:
    return {
        "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept":          "*/*",
        "Accept-Language": "en",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://www.sportybet.com/ng/my_accounts/bet_history/sport_bets?isSettled=10",
        "Clientid":        "web",
        "Content-Type":    "application/x-www-form-urlencoded; charset=UTF-8",
        "Operid":          "2",
        "Platform":        "web",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "same-origin",
        "Sec-Fetch-Site":  "same-origin",
        "Sec-GPC":         "1",
        "Priority":        "u=4",
        "Te":              "trailers",
        "Cookie":          cookie_str,
    }


def get_today_str() -> str:
    now = datetime.now()
    return f"{now.day}/{now.month}/{now.year}"


def is_today(create_time_ms: int) -> bool:
    dt  = datetime.fromtimestamp(create_time_ms / 1000)
    now = datetime.now()
    return dt.year == now.year and dt.month == now.month and dt.day == now.day


def get_today_tracker_row() -> dict | None:
    config   = load_db_config("db.txt")
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    result   = (
        supabase.table("sp_tracker")
        .select("id, played, max, win, lost")
        .eq("day", get_today_str())
        .execute()
    )
    return result.data[0] if result.data else None


def update_win_loss(won: int, lost: int):
    config   = load_db_config("db.txt")
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    today    = get_today_str()

    result = (
        supabase.table("sp_tracker")
        .select("id")
        .eq("day", today)
        .execute()
    )
    if not result.data:
        print("[betlist] no sp_tracker row for today — skipping win/loss update")
        return

    supabase.table("sp_tracker").update({
        "win":  won,
        "lost": lost,
    }).eq("id", result.data[0]["id"]).execute()

    print(f"[betlist] win/loss updated -> W:{won} L:{lost}")


def check_can_bet() -> bool:
    """
    Returns True if staker should proceed, False if it should stop.
    Checks (in order):
      1. Daily played limit reached (sp_tracker)
      2. Open bets limit reached
    Also syncs today's win/loss counts into sp_tracker.
    """

    # --- Check 1: daily stake limit ---
    row = get_today_tracker_row()
    if row:
        played = row.get("played", 0)
        max_bets = row.get("max", 50)
        print(f"[betlist] daily played -> {played}/{max_bets}")
        if played >= max_bets:
            print(f"[betlist] daily limit reached ({played}/{max_bets}) — stopping")
            return False
    else:
        print("[betlist] no sp_tracker row for today — daily limit not checked")

    # --- Check 2: open bets + win/loss sync ---
    cookie_str = get_cookie_from_db()
    headers    = build_headers(cookie_str)
    timestamp  = int(time.time() * 1000)

    url = "https://www.sportybet.com/api/ng/orders/order/v2/realbetlist"
    params = {
        "isSettled": 10,
        "pageSize":  50,
        "pageNo":    1,
        "_t":        timestamp,
    }

    response = httpx.get(url, headers=headers, params=params, timeout=10.0)

    if response.status_code == 401:
        print("[betlist] session expired (401) — re-logging in")
        import subprocess, sys
        subprocess.run([sys.executable, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "login.py"
        )])
        return False

    if response.status_code != 200:
        print(f"[betlist] request failed -> HTTP {response.status_code}")
        return False

    data        = response.json().get("data", {})
    bets        = data.get("entityList", [])
    todays_bets = [b for b in bets if is_today(b.get("createTime", 0))]

    open_bets  = [b for b in todays_bets if b.get("winningStatus") == 0]
    won_bets   = [b for b in todays_bets if b.get("winningStatus") == 20]
    lost_bets  = [b for b in todays_bets if b.get("winningStatus") == 30]
    open_count = len(open_bets)

    print(f"[betlist] today -> open:{open_count}  won:{len(won_bets)}  lost:{len(lost_bets)}")

    update_win_loss(len(won_bets), len(lost_bets))

    if open_count >= MAX_OPEN_BETS:
        print(f"[betlist] open bets limit reached ({open_count}/{MAX_OPEN_BETS}) — skipping")
        return False

    print(f"[betlist] clear ({open_count}/{MAX_OPEN_BETS}) — proceeding")
    return True