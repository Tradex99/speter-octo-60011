import httpx
import asyncio
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from module.analyzer import analyze
from module.betlist  import check_can_bet


# ── Config ────────────────────────────────────────────────────────────────────
INTERVAL        = 3
MAX_WORKERS     = 9

TARGET_STATUSES = ["1st Set", "2nd Set", "3rd Set"]

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


# ── Fetch overview ────────────────────────────────────────────────────────────
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


# ── Parse events ──────────────────────────────────────────────────────────────
def _extract_all_events(data: dict) -> list[dict]:
    events = []
    for sport in data.get("sportData", []):
        for tournament in sport.get("tournaments", []):
            for event in tournament.get("events", []):
                events.append({
                    "id":         event["id"],
                    "name":       event["name"],
                    "score":      event.get("score", "?"),
                    "set_scores": event.get("setScores", ""),
                    "status":     event.get("matchStatusLabel", ""),
                    "tournament": tournament.get("name", "?"),
                })
    return events


def filter_qualifying_events(data: dict) -> list[dict]:
    all_events = _extract_all_events(data)
    collected  = []
    for status in TARGET_STATUSES:
        if len(collected) >= MAX_WORKERS:
            break
        for event in all_events:
            if len(collected) >= MAX_WORKERS:
                break
            if event["status"].lower() == status.lower():
                collected.append(event)
    return collected


# ── Betlist check ─────────────────────────────────────────────────────────────
async def _run_betlist_check(can_bet: list):
    try:
        result     = await check_can_bet()
        can_bet[0] = result
        if not result:
            print(f"[tracker] betting halted : daily limit reached — no new workers will be dispatched")
    except Exception as e:
        print(f"[tracker] betlist error  : {e}")


# ── One tracker cycle ─────────────────────────────────────────────────────────
async def run_once(cycle: int, seen: set, active_workers: set, quiet_cycles: list, can_bet: list):
    start   = time.time()
    data    = await fetch_live_events()
    elapsed = time.time() - start

    if data is None:
        return

    qualifying = filter_qualifying_events(data)
    new_events = [
        e for e in qualifying
        if e["id"] not in seen and e["id"] not in active_workers
    ]

    total = sum(
        len(t.get("events", []))
        for s in data.get("sportData", [])
        for t in s.get("tournaments", [])
    )

    if not new_events:
        quiet_cycles[0] += 1
        if quiet_cycles[0] % 10 == 0:
            print(
                f"\n[cycle {cycle}] live={total} | qualifying={len(qualifying)} "
                f"| new=0 | workers={len(active_workers)} | fetch={elapsed:.2f}s"
                f"  (x{quiet_cycles[0]} quiet cycles)"
            )
            print("-" * 55)
        return

    quiet_cycles[0] = 0

    print(
        f"\n[cycle {cycle}] live={total} | qualifying={len(qualifying)} "
        f"| new={len(new_events)} | workers={len(active_workers)} | fetch={elapsed:.2f}s"
    )
    print("-" * 55)

    if not can_bet[0]:
        print(f"[tracker] skipping dispatch : betting halted for today")
        return

    for e in new_events:
        print(f"  eventId    : {e['id']}")
        print(f"  Match      : {e['name']}")
        print(f"  Sets       : {e['set_scores']}")
        print(f"  Status     : {e['status']}")
        print(f"  Tournament : {e['tournament']}")
        print("  " + "-" * 53)
        seen.add(e["id"])
        active_workers.add(e["id"])

    async def worker_wrapper(event: dict):
        async def on_bet_placed():
            asyncio.create_task(_run_betlist_check(can_bet))
        try:
            await analyze(event_id=event["id"], on_bet_placed=on_bet_placed)
        finally:
            active_workers.discard(event["id"])
            print(f"[tracker] worker released : eventId={event['id']}")

    for e in new_events:
        asyncio.create_task(worker_wrapper(e))


# ── Main loop ─────────────────────────────────────────────────────────────────
async def main_async():
    seen           = set()
    active_workers = set()
    cycle          = 1
    quiet_cycles   = [0]
    can_bet        = [True]

    print(f"[tracker] startup check  : verifying daily limit ...")
    try:
        can_bet[0] = await check_can_bet()
        if not can_bet[0]:
            print(f"[tracker] betting halted : daily limit already reached at startup")
            print(f"[tracker] shutdown complete")
            return
    except Exception as e:
        print(f"[tracker] betlist error  : {e}")

    try:
        while True:
            if len(active_workers) >= MAX_WORKERS:
                print(f"[tracker] all {MAX_WORKERS} workers busy — pausing tracker ...")
                while len(active_workers) >= MAX_WORKERS:
                    await asyncio.sleep(1)
                print(f"[tracker] slot freed — resuming tracker")

            try:
                await run_once(cycle, seen, active_workers, quiet_cycles, can_bet)
            except Exception as e:
                print(f"[tracker] cycle error    : {e}")

            cycle += 1

            if not can_bet[0]:
                if active_workers:
                    print(f"[tracker] betting halted : waiting for {len(active_workers)} worker(s) to finish ...")
                    while active_workers:
                        await asyncio.sleep(1)
                print(f"[tracker] betting halted : daily limit reached — exiting")
                break

            if len(active_workers) < MAX_WORKERS:
                if quiet_cycles[0] == 0 or quiet_cycles[0] % 10 == 0:
                    print(f"[tracker] next cycle in {INTERVAL}s ...")
                await asyncio.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\n[tracker] stopped by user")
    finally:
        print("[tracker] shutdown complete")


if __name__ == "__main__":
    asyncio.run(main_async())
