import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import time
import asyncio
from collections import deque
from supabase import create_client
from module.analyzer import analyze, reset_signal_count
from staker import Staker


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


def get_cookie_from_db():
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

    return parse_cookie_string(result.data[0]["cookie"])


def parse_cookie_string(cookie_str):
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies[name.strip()] = value.strip()
    return cookies


def parse_played_seconds(played_str):
    try:
        parts = played_str.split(":")
        return (int(parts[0]) * 60) + int(parts[1])
    except:
        return 0


def build_headers(cookies):
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept": "*/*",
        "Accept-Language": "en",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.sportybet.com/ng/sport/football/live_list/",
        "Clientid": "web",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Operid": "2",
        "Platform": "web",
        "Sporty-Referer": "utm_source=https://www.google.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "same-origin",
        "Sec-Fetch-Site": "same-origin",
        "Sec-GPC": "1",
        "Priority": "u=4",
        "Te": "trailers",
        "Cookie": cookie_header,
    }


async def fetch_live_events(headers):
    timestamp = int(time.time() * 1000)
    url = "https://www.sportybet.com/api/ng/factsCenter/liveOrPrematchEvents"
    params = {"sportId": "sr:sport:1", "_t": timestamp}

    async with httpx.AsyncClient(http2=True, timeout=15.0, headers=headers) as client:
        response = await client.get(url, params=params)
        return response


def filter_halftime_matches(tournaments, min_seconds=2400, max_seconds=2700):
    # 2520 = 40:00,  2700 = 45:00
    total_matches = 0
    qualifying    = []

    for tournament in tournaments:
        tournament_name = tournament.get("name", "")

        if "SRL" in tournament_name:
            total_matches += len(tournament.get("events", []))
            continue

        for event in tournament.get("events", []):
            total_matches += 1
            played_str  = event.get("playedSeconds", "0:00")
            played_secs = parse_played_seconds(played_str)

            if min_seconds <= played_secs <= max_seconds:
                qualifying.append({
                    "eventId":    event.get("eventId", "N/A"),
                    "match":      f"{event.get('homeTeamName','?')} vs {event.get('awayTeamName','?')}",
                    "score":      event.get("setScore", "?"),
                    "played":     played_str,
                    "status":     event.get("matchStatus", "?"),
                    "tournament": tournament_name,
                })

    return total_matches, qualifying


async def run_once(staker: Staker, headers: dict, cycle: int, seen: deque):
    start = time.time()

    response = await fetch_live_events(headers)
    elapsed  = time.time() - start

    if response.status_code != 200:
        print(f"[cycle {cycle}] request failed: {response.status_code}")
        return

    data        = response.json()
    tournaments = data.get("data", [])
    total_matches, qualifying = filter_halftime_matches(tournaments)

    new_matches = [m for m in qualifying if m["eventId"] not in seen]
    skipped     = len(qualifying) - len(new_matches)

    print(f"\n[cycle {cycle}] Total live: {total_matches} | 42–45 mins: {len(qualifying)} | new: {len(new_matches)} | skipped: {skipped} | fetch: {elapsed:.2f}s")
    print("-" * 50)

    if not new_matches:
        print(f"[cycle {cycle}] No new qualifying matches.")
        return

    for m in new_matches:
        print(f"  eventId    : {m['eventId']}")
        print(f"  Match      : {m['match']}")
        print(f"  Score      : {m['score']}")
        print(f"  Played     : {m['played']}")
        print(f"  Tournament : {m['tournament']}")
        print("  " + "-" * 48)

        seen.append(m["eventId"])
        if len(seen) > 15:
            seen.popleft()

    reset_signal_count()

    analyzer_tasks = [
        asyncio.create_task(
            analyze(event_id=m["eventId"], headers=headers, staker=staker)
        )
        for m in new_matches
    ]

    await asyncio.gather(*analyzer_tasks)

    if not staker._queue.empty():
        await staker._queue.join()


async def main_async():
    INTERVAL = 50  # seconds between each cycle

    loop    = asyncio.get_event_loop()
    cookies = await loop.run_in_executor(None, get_cookie_from_db)
    headers = build_headers(cookies)

    staker = Staker()
    await staker.start()
    staker_task = asyncio.create_task(staker.run())

    seen  = deque()
    cycle = 1

    try:
        while True:
            await run_once(staker, headers, cycle, seen)
            cycle += 1
            print(f"\n[tracker] next cycle in {INTERVAL}s ...")
            await asyncio.sleep(INTERVAL)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        print("\n[tracker] stopped by user")
    finally:
        staker_task.cancel()
        await staker.stop()
        print("[tracker] shutdown complete")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()