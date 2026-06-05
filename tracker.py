import httpx
import time
import asyncio
import random
from module.analyzer import analyze
from staker import Staker


#Config
TIME_WINDOWS = [
    (25, 30),
    (30, 35),
]

INTERVAL        = 50
MIN_SINGLE_ODD  = 1.10   # single bet minimum odd
MIN_MULTI_ODD   = 1.10   # multi bet minimum combined odd

BETKING_URL = (
    "https://sportsapicdn-desktop.betking.com"
    "/api/feeds/live/areaOddsByLayout/en/1/3/0/false/false/true/true"
)

BETKING_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin":          "https://www.betking.com",
    "Referer":         "https://www.betking.com/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-site",
    "Sec-Gpc":         "1",
    "Priority":        "u=6",
    "Cache-Control":   "max-age=0",
    "Te":              "trailers",
}


def in_time_window(match_time):
    for lo, hi in TIME_WINDOWS:
        if lo <= match_time <= hi:
            return True
    return False


def get_window_label(match_time):
    for lo, hi in TIME_WINDOWS:
        if lo <= match_time <= hi:
            return f"{lo}-{hi}'"
    return "?"


async def fetch_live_events():
    async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
        response = await client.get(BETKING_URL, headers=BETKING_HEADERS)
    if response.status_code != 200:
        print(f"[tracker] fetch failed: HTTP {response.status_code}")
        return None
    return response.json()


def _is_simulated(event_name: str, tournament_name: str) -> bool:
    if "simulated" in tournament_name.lower():
        return True
    parts = event_name.split(" - ")
    if len(parts) == 2:
        home, away = parts
        if home.strip().upper().endswith("SRL") and away.strip().upper().endswith("SRL"):
            return True
    return False


def filter_qualifying_matches(data):
    total    = 0
    matching = []
    for sport in data.get("Sports", []):
        if sport.get("Id") != 1:
            continue
        for tournament in sport.get("Tournaments", []):
            for event in tournament.get("Events", []):
                total += 1
                if _is_simulated(event.get("Name", ""), tournament.get("Name", "")):
                    continue
                match_time = event.get("MatchTime", -1)
                if not in_time_window(match_time):
                    continue
                matching.append({
                    "id":         event["Id"],
                    "name":       event.get("Name", "?"),
                    "score":      event.get("Score", "?"),
                    "match_time": match_time,
                    "window":     get_window_label(match_time),
                    "tournament": tournament.get("Name", "?"),
                })
    return total, matching


async def run_once(cycle, staker):
    start   = time.time()
    data    = await fetch_live_events()
    elapsed = time.time() - start

    if data is None:
        return

    total, qualifying = filter_qualifying_matches(data)

    print(f"\n[cycle {cycle}] Total live: {total} | qualifying: {len(qualifying)} | fetch: {elapsed:.2f}s")
    print("-" * 50)

    if not qualifying:
        print(f"[cycle {cycle}] No qualifying matches.")
        return

    for m in qualifying:
        print(f"  eventId    : {m['id']}")
        print(f"  Match      : {m['name']}")
        print(f"  Score      : {m['score']}")
        print(f"  MatchTime  : {m['match_time']}' ({m['window']})")
        print(f"  Tournament : {m['tournament']}")
        print("  " + "-" * 48)

    results = await asyncio.gather(*[
        asyncio.create_task(
            analyze(event_id=m["id"], match_time=m["match_time"])
        )
        for m in qualifying
    ])

    signals = [r for r in results if r is not None]

    if not signals:
        return

    # Sort by odd descending so highest odds are considered first
    signals.sort(key=lambda s: s["oddValue"], reverse=True)

    print(f"[tracker] signals    : {len(signals)} found, selecting best combo")

    from itertools import combinations as iter_combos
    from math import prod

    best_batch   = None
    best_combined = 0.0

    # Try largest combos first (3 → 2 → 1), pick highest combined odd that passes min
    for size in (3, 2, 1):
        if len(signals) < size:
            continue
        min_odd = MIN_MULTI_ODD if size > 1 else MIN_SINGLE_ODD
        for combo in iter_combos(signals, size):
            combined = round(prod(s["oddValue"] for s in combo), 10)
            if combined >= min_odd and combined > best_combined:
                best_combined = combined
                best_batch    = sorted(list(combo), key=lambda s: s["oddValue"], reverse=True)
        # Only move to smaller size if nothing qualifies at this size
        if best_batch is not None:
            break

    if best_batch is None:
        odds_list = [s["oddValue"] for s in signals]
        print(f"[tracker] skipped    : no combo meets min odd | signals={odds_list}")
        return

    n        = len(best_batch)
    bet_type = "Single" if n == 1 else f"Multi x{n}"
    odds_str = " x ".join(str(s["oddValue"]) for s in best_batch)
    print(f"[tracker] selected   : [{bet_type}] odds={odds_str} combined={best_combined}")

    tx_id = str(random.randint(10_000_000_000, 99_999_999_999))
    await staker.queue(best_batch, tx_id)

    if not staker._queue.empty():
        await staker._queue.join()


async def main_async():
    staker      = Staker()
    staker_task = asyncio.create_task(staker.run())
    cycle       = 1

    try:
        while True:
            await run_once(cycle, staker)
            cycle += 1
            print(f"\n[tracker] next cycle in {INTERVAL}s ...")
            await asyncio.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\n[tracker] stopped by user")
    finally:
        staker_task.cancel()
        print("[tracker] shutdown complete")


if __name__ == "__main__":
    asyncio.run(main_async())
