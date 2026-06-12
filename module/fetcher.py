import asyncio
import httpx
from playwright.async_api import async_playwright


# ── Config ────────────────────────────────────────────────────────────────────
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

LIVE_BASE_URL  = "https://m.betking.com/en-ng/sports/live"
FALLBACK_ID    = 34556167

CAPTURE_RETRIES   = 5    # total attempts across all match IDs
CAPTURE_WAIT_S    = 8    # seconds to wait between failed attempts
PAGE_WAIT_SLOTS   = 30   # × 500 ms = 15 s max wait per page load


# ── Step 1: Fetch multiple live match IDs from the overview API ───────────────
async def _fetch_match_ids(limit: int = CAPTURE_RETRIES) -> list[int]:
    """Return up to `limit` live match IDs. Falls back to [FALLBACK_ID]."""
    ids = []
    try:
        async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
            resp = await client.get(TT_URL, headers=TT_HEADERS)

        if resp.status_code != 200:
            print(f"[fetcher] overview failed  : HTTP {resp.status_code}")
            return [FALLBACK_ID]

        data = resp.json()
        for sport in data.get("sportData", []):
            for tournament in sport.get("tournaments", []):
                for event in tournament.get("events", []):
                    match_id = event.get("id")
                    if match_id and match_id not in ids:
                        ids.append(match_id)
                    if len(ids) >= limit:
                        break

    except Exception as e:
        print(f"[fetcher] overview error   : {e}")

    if not ids:
        print(f"[fetcher] fallback match   : id={FALLBACK_ID}")
        return [FALLBACK_ID]

    print(f"[fetcher] found matches    : {ids}")
    return ids


# ── Step 2: Open match page in Firefox and capture the token ──────────────────
async def _capture_token(match_id: int) -> str | None:
    match_url = f"{LIVE_BASE_URL}/{match_id}"
    print(f"[fetcher] opening url      : {match_url}")

    token = None

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.5",
                "Dnt":             "1",
                "Sec-Gpc":         "1",
            },
        )
        page = await context.new_page()

        def on_request(request):
            nonlocal token
            url = request.url
            if "lmt.fn.sportradar" in url and "?T=" in url and token is None:
                token = url.split("?T=")[1]
                print(f"[fetcher] token captured   : {token[:60]}...")

        page.on("request", on_request)

        try:
            await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
            for _ in range(PAGE_WAIT_SLOTS):
                if token:
                    break
                await page.wait_for_timeout(500)
        except Exception as e:
            print(f"[fetcher] page error       : {e}")
        finally:
            await browser.close()

    return token


# ── Public entry point ────────────────────────────────────────────────────────
async def fetch_token() -> str | None:
    """
    Try each live match ID in turn. If a page fails to yield a token,
    wait CAPTURE_WAIT_S seconds then try the next match.
    Retries up to CAPTURE_RETRIES times total before giving up.
    """
    match_ids = await _fetch_match_ids()

    for attempt, match_id in enumerate(match_ids, start=1):
        print(f"[fetcher] attempt {attempt}/{len(match_ids)} : match {match_id}")
        token = await _capture_token(match_id)

        if token:
            print(f"[fetcher] token ready      : attempt {attempt}")
            return token

        print(f"[fetcher] no token         : attempt {attempt} failed")
        if attempt < len(match_ids):
            print(f"[fetcher] retrying in      : {CAPTURE_WAIT_S}s")
            await asyncio.sleep(CAPTURE_WAIT_S)

    print(f"[fetcher] all attempts failed — token not captured")
    return None


# ── Standalone run ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(fetch_token())
