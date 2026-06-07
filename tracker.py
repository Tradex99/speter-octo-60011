import httpx
import asyncio
import time
from module.analyzer import analyze
from staker import Staker


#Config
INTERVAL      = 10
TARGET_STATUS = "4th pause"

TT_URL = (
    "https://m.betking.com/en-ng/sports/live/api/overview/20"
    "?_data=routes%2F%28%24locale%29.sports.live.api.overview.%24sportId"
)

TT_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng/sports/live/table-tennis/20",
    "Dnt":             "1",
    "Sec-Gpc":         "1",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "Priority":        "u=0",
    "Te":              "trailers",
}


async def fetch_live_events() -> dict | None:
    async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
        resp = await client.get(TT_URL, headers=TT_HEADERS)
    if resp.status_code != 200:
        print(f"[tracker] fetch failed : HTTP {resp.status_code}")
        return None
    if not resp.text or not resp.text.strip():
        print(f"[tracker] fetch failed : empty response")
        return None
    return resp.json()


def filter_qualifying_events(data: dict) -> list[dict]:
    qualifying = []
    for sport in data.get("sportData", []):
        for tournament in sport.get("tournaments", []):
            for event in tournament.get("events", []):
                label = event.get("matchStatusLabel", "")
                if label.lower() == TARGET_STATUS.lower():
                    qualifying.append({
                        "id":         event["id"],
                        "name":       event["name"],
                        "score":      event.get("score", "?"),
                        "set_scores": event.get("setScores", ""),
                        "status":     label,
                        "tournament": tournament.get("name", "?"),
                    })
    return qualifying


async def run_once(cycle: int, seen: set, staker: Staker):
    start   = time.time()
    data    = await fetch_live_events()
    elapsed = time.time() - start

    if data is None:
        return

    qualifying = filter_qualifying_events(data)
    new_events = [e for e in qualifying if e["id"] not in seen]

    total = sum(
        len(t.get("events", []))
        for s in data.get("sportData", [])
        for t in s.get("tournaments", [])
    )

    print(
        f"\n[cycle {cycle}] live={total} | in_pause={len(qualifying)} "
        f"| new={len(new_events)} | fetch={elapsed:.2f}s"
    )
    print("-" * 50)

    if not new_events:
        return

    for e in new_events:
        print(f"  eventId    : {e['id']}")
        print(f"  Match      : {e['name']}")
        print(f"  Sets       : {e['set_scores']}")
        print(f"  Status     : {e['status']}")
        print(f"  Tournament : {e['tournament']}")
        print("  " + "-" * 48)
        seen.add(e["id"])

    await asyncio.gather(*[
        asyncio.create_task(analyze(event_id=e["id"], staker=staker))
        for e in new_events
    ])


async def main_async():
    staker      = Staker()
    staker_task = asyncio.create_task(staker.run())
    seen        = set()
    cycle       = 1
    try:
        while True:
            try:
                await run_once(cycle, seen, staker)
            except Exception as e:
                print(f"[tracker] cycle error: {e}")
            cycle += 1
            print(f"[tracker] next cycle in {INTERVAL}s ...")
            await asyncio.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\n[tracker] stopped by user")
    finally:
        staker_task.cancel()
        print("[tracker] shutdown complete")


if __name__ == "__main__":
    asyncio.run(main_async())