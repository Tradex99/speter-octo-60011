"""
TT_live.py -- Table Tennis, LIVE match cross-reference between BetKing and
Bet9ja. Runs every 5s.
"""
import asyncio
import json
import os
import re
import time
import httpx

from common import norm

INTERVAL_SECONDS = 5

BETKING_ACCOUNT_NAME = "Chukwuebuka"
BET9JA_ACCOUNT_NAME = "wixnation"


def _load_db_config() -> dict:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = {}
    with open(os.path.join(base, "db.txt"), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                config[k.strip()] = v.strip().strip('"')
    return config


def _get_supabase():
    from supabase import create_client
    config = _load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])


def get_cookie(account_name: str) -> str:
    try:
        sb = _get_supabase()
        row = (
            sb.table("betking")
            .select("cookie")
            .eq("name", account_name)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if row.data:
            return row.data[0].get("cookie") or ""
        print(f"[TT_live] no cookie row found for '{account_name}'")
        return ""
    except Exception as e:
        print(f"[TT_live] cookie lookup failed for '{account_name}': {e}")
        return ""


# ─── BetKing ──────────────────────────────────────────────────────────────────

BETKING_LIVE_URL = (
    "https://m.betking.com/en-ng/sports/live/api/overview/20"
    "?_data=routes%2F%28%24locale%29.sports.live.api.overview.%24sportId"
)
BETKING_LIVE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://m.betking.com/en-ng/sports/live/table-tennis/20",
}

REQUEST_TIMEOUT = 10.0


async def fetch_betking_live(client: httpx.AsyncClient) -> dict:
    headers = dict(BETKING_LIVE_HEADERS)
    cookie = get_cookie(BETKING_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(BETKING_LIVE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def fetch_betking_event(client: httpx.AsyncClient, fixture_id: int, area_id: int) -> dict:
    url = (
        f"https://m.betking.com/en-ng/sports/live/api/event"
        f"?areaId={area_id}&fixtureId={fixture_id}"
        f"&_data=routes%2F%28%24locale%29.sports.live.api.event"
    )
    headers = dict(BETKING_LIVE_HEADERS)
    cookie = get_cookie(BETKING_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def parse_betking_live(bk: dict) -> dict:
    sport_data = bk.get("sportData") or []
    if not sport_data:
        return {}

    matches = {}
    for tournament in sport_data[0].get("tournaments", []):
        tname = tournament.get("name")
        for ev in tournament.get("events", []):
            home = ev.get("homeTeam")
            away = ev.get("awayTeam")
            if not home or not away:
                teams = ev.get("teams", [])
                if len(teams) == 2:
                    home, away = teams[0].get("name"), teams[1].get("name")
            if not home or not away:
                continue

            key = (norm(home), norm(away))
            matches[key] = {
                "home": home,
                "away": away,
                "event_id": ev.get("id"),
                "tournament": tname,
                "status_label": ev.get("matchStatusLabel"),
                "score": ev.get("score"),
            }
    return matches


def _flatten_betking_markets(raw_markets: list) -> list:
    seen = set()
    out = []
    for m in raw_markets:
        mid = m.get("marketId")
        if mid not in seen:
            seen.add(mid)
            out.append(m)
        for sub in m.get("spreadMarkets", []) or []:
            smid = sub.get("marketId")
            if smid not in seen:
                seen.add(smid)
                out.append(sub)
    return out


def parse_betking_event(markets_area1: list, markets_area2: list) -> dict:
    seen_ids = set()
    area1_flat = _flatten_betking_markets(markets_area1)
    for m in area1_flat:
        seen_ids.add(m.get("marketId"))
    area2_extra = [m for m in _flatten_betking_markets(markets_area2) if m.get("marketId") not in seen_ids]
    markets = area1_flat + area2_extra
    out = {}
    for m in markets:
        name = m.get("name")
        sels = m.get("selections") or []
        if len(sels) != 2:
            continue
        odd_by_name = {s.get("name"): s.get("odd", {}).get("value") for s in sels}

        if name == "Winner":
            key = ("winner",)
            label, mtype = "Winner", "Winner"
            side_a, side_b = odd_by_name.get("1"), odd_by_name.get("2")
        elif name == "Total points {line}":
            line = m.get("lineValue")
            if line is None:
                continue
            key = ("total", round(float(line), 1))
            label, mtype = f"Total Points {line}", "Total Points"
            side_a, side_b = odd_by_name.get("Over"), odd_by_name.get("Under")
        elif name == "{gamenr} game - winner":
            set_num = m.get("specialValue")
            if not set_num:
                continue
            set_num = int(float(set_num))
            key = ("set_winner", set_num)
            label, mtype = f"Set {set_num} Winner", "Set Winner"
            side_a, side_b = odd_by_name.get("1"), odd_by_name.get("2")
        elif name == "{gamenr} game - total points {line}":
            set_num, line = m.get("specialValue"), m.get("lineValue")
            if not set_num or line is None:
                continue
            set_num = int(float(set_num))
            key = ("set_total", set_num, round(float(line), 1))
            label, mtype = f"Set {set_num} Total Points {line}", "Set Total Points"
            side_a, side_b = odd_by_name.get("Over"), odd_by_name.get("Under")
        else:
            continue

        if not side_a or not side_b:
            continue
        out[key] = {"label": label, "market_type": mtype, "side_a": float(side_a), "side_b": float(side_b)}
    return out


def _betking_match_ended(area1_raw: dict) -> bool:
    return area1_raw.get("event") is None and not area1_raw.get("markets")


# ─── Bet9ja ───────────────────────────────────────────────────────────────────

BET9JA_SPORT_ID = "3000020"  # Table Tennis
BET9JA_LIVE_URL = (
    "https://sports.bet9ja.com/desktop/feapi/PalimpsestLiveAjax/GetLiveEventsV3"
    f"?SID={BET9JA_SPORT_ID}&v_cache_version=1.317.3.243"
)
BET9JA_EVENT_URL = "https://sports.bet9ja.com/desktop/feapi/PalimpsestLiveAjax/GetLiveEventV2"
BET9JA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://sports.bet9ja.com/",
}


async def fetch_bet9ja_live(client: httpx.AsyncClient) -> dict:
    headers = dict(BET9JA_HEADERS)
    cookie = get_cookie(BET9JA_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(BET9JA_LIVE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def fetch_bet9ja_event(client: httpx.AsyncClient, event_id: str) -> dict:
    """
    GetLiveEventV2 — returns full market odds for a single event.
    Response structure:
      D.A   = live status (ES, T2, R.S score)
      D.AA  = event metadata (ST, DS, SID)
      D.O   = flat dict of odds keyed by selection code e.g. LIVETT_12_1HH: {v: 1.68}
      D.MKT = market definitions (we use to know which markets exist)
    """
    headers = dict(BET9JA_HEADERS)
    headers["Referer"] = f"https://sports.bet9ja.com/liveEvent/{event_id}"
    cookie = get_cookie(BET9JA_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    url = f"{BET9JA_EVENT_URL}?EVENTID={event_id}&ISMKT=1&v_cache_version=1.317.3.243"
    resp = await client.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def parse_bet9ja_live(b9: dict) -> dict:
    """
    Parse GetLiveEventsV3 response.
    Events live in b9['D']['E'], keyed by event ID string (or a list in some responses).
    Only includes Table Tennis events (SID == 3000020).
    DS field format: "Player A - Player B" or "Player A v Player B"
    """
    try:
        E = b9["D"]["E"]
    except (KeyError, TypeError):
        return {}

    # E can be a dict {id: event} or a list [event, ...] depending on response variant
    if isinstance(E, list):
        events_iter = ((str(ev.get("ID", i)), ev) for i, ev in enumerate(E))
    elif isinstance(E, dict):
        events_iter = E.items()
    else:
        return {}

    matches = {}
    for eid, ev in events_iter:
        if ev.get("SID") != int(BET9JA_SPORT_ID):
            continue

        ds = ev.get("DS", "")
        if " - " in ds:
            home, away = ds.split(" - ", 1)
        elif " v " in ds:
            home, away = ds.split(" v ", 1)
        else:
            continue
        home, away = home.strip(), away.strip()

        eod = ev.get("EOD", {})
        aod = eod.get("A", {})
        r = aod.get("R", {})

        key = (norm(home), norm(away))
        matches[key] = {
            "home": home,
            "away": away,
            "event_id": str(ev["ID"]),
            "status_label": aod.get("ES", ""),
            "score": r.get("S", ""),
        }
    return matches


def parse_bet9ja_event(b9_raw: dict) -> dict:
    """
    Parse markets from a GetLiveEventV2 response.

    D.O is a flat dict of all selection odds, keyed by selection code.
    Key patterns:
      LIVETT_12_1HH / LIVETT_12_2HH          → Match Winner (home / away)
      LIVETT_OU@{line}_Over / _Under          → Match Total Points {line}
      LIVETT_SW@{set}_1 / LIVETT_SW@{set}_2  → Set {set} Winner
      LIVETT_OU{set}PN@{line}_O / _U         → Set {set} Total Points {line}
        (set number encoded in market name: OU1PN=set1, OU2PN=set2, etc.)

    A key is active if it exists in D.O with a valid numeric "v" value.
    D.AA.ST == 1 means the event is live.
    """
    try:
        D = b9_raw["D"]
    except (KeyError, TypeError):
        return {}

    o = D.get("O") or {}
    out = {}

    def _val(ok: str):
        """Return float odd for key ok, or None if missing/invalid."""
        entry = o.get(ok)
        if not entry:
            return None
        try:
            return float(entry["v"])
        except (KeyError, TypeError, ValueError):
            return None

    # ── Match Winner ──────────────────────────────────────────────────────────
    h = _val("LIVETT_12_1HH")
    a = _val("LIVETT_12_2HH")
    if h and a:
        out[("winner",)] = {
            "label": "Winner", "market_type": "Winner",
            "side_a": h, "side_b": a,
        }

    # ── Scan remaining keys for Set Winner, Match Total, Set Total ────────────
    seen_sw = set()
    seen_ou = set()
    seen_set_ou = set()

    for ok in o:
        # Set Winner: LIVETT_SW@{set}_1  paired with  LIVETT_SW@{set}_2
        if ok.startswith("LIVETT_SW@") and ok.endswith("_1"):
            try:
                set_num = int(ok.split("@")[1].split("_")[0])
            except (IndexError, ValueError):
                continue
            if set_num in seen_sw:
                continue
            v1 = _val(ok)
            v2 = _val(f"LIVETT_SW@{set_num}_2")
            if v1 and v2:
                seen_sw.add(set_num)
                out[("set_winner", set_num)] = {
                    "label": f"Set {set_num} Winner", "market_type": "Set Winner",
                    "side_a": v1, "side_b": v2,
                }

        # Match Total Points: LIVETT_OU@{line}_Over  paired with  _Under
        elif ok.startswith("LIVETT_OU@") and ok.endswith("_Over"):
            try:
                raw_line = ok[len("LIVETT_OU@"):-len("_Over")]
                line = round(float(raw_line), 1)
            except ValueError:
                continue
            if line in seen_ou:
                continue
            v_o = _val(ok)
            v_u = _val(f"LIVETT_OU@{raw_line}_Under")
            if v_o and v_u:
                seen_ou.add(line)
                out[("total", line)] = {
                    "label": f"Total Points {line}", "market_type": "Total Points",
                    "side_a": v_o, "side_b": v_u,
                }

        # Set Total Points: LIVETT_OU{set}PN@{line}_O  paired with  _U
        # e.g. LIVETT_OU3PN@18.5_O → set 3, line 18.5
        elif "PN@" in ok and ok.endswith("_O") and ok.startswith("LIVETT_OU"):
            try:
                # Extract set number from between "OU" and "PN"
                after_ou = ok[len("LIVETT_OU"):]          # "3PN@18.5_O"
                set_str, rest = after_ou.split("PN@", 1)  # "3", "18.5_O"
                set_num = int(set_str)
                raw_line = rest[:-len("_O")]               # "18.5"
                line = round(float(raw_line), 1)
            except (ValueError, IndexError):
                continue
            if (set_num, line) in seen_set_ou:
                continue
            v_o = _val(ok)
            v_u = _val(f"LIVETT_OU{set_str}PN@{raw_line}_U")
            if v_o and v_u:
                seen_set_ou.add((set_num, line))
                out[("set_total", set_num, line)] = {
                    "label": f"Set {set_num} Total Points {line}", "market_type": "Set Total Points",
                    "side_a": v_o, "side_b": v_u,
                }

    return out


def _bet9ja_match_ended(b9_raw: dict) -> bool:
    """
    Match ended if:
      - Response is {"R":"OK","D":false}  (event gone from live)
      - D.AA.ST != 1
    """
    if b9_raw.get("D") is False:
        return True
    try:
        return b9_raw["D"]["AA"].get("ST") != 1
    except (KeyError, TypeError):
        return True


def _betking_event_status(area1_raw: dict) -> str:
    """Return matchStatus string from BetKing event detail e.g. '3rd pause', '3rd Set'."""
    event = area1_raw.get("liveEventDetail") or area1_raw.get("event") or {}
    return event.get("matchStatus", "")


def _bet9ja_event_status(b9_raw: dict) -> str:
    """Return ES field from Bet9ja event detail e.g. 'pause3', '3rd Set'."""
    try:
        return b9_raw["D"]["A"].get("ES", "")
    except (KeyError, TypeError):
        return ""


def _bk_pause_set(match_status: str):
    """BetKing: '3rd pause' → 3, '2nd Set' → None."""
    if not match_status:
        return None
    s = match_status.lower()
    if "pause" not in s:
        return None
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _b9_pause_set(es: str):
    """Bet9ja: 'pause3' → 3, '3rd Set' → None."""
    if not es:
        return None
    s = es.lower()
    if not s.startswith("pause"):
        return None
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _both_paused_same_set(bk_status: str, b9_es: str):
    """Returns set number if both platforms are paused on the same set, else None."""
    bk_set = _bk_pause_set(bk_status)
    b9_set = _b9_pause_set(b9_es)
    if bk_set is None or b9_set is None:
        return None
    return bk_set if bk_set == b9_set else None


# ─── Arb detection & storage ──────────────────────────────────────────────────

def _arb_margin_pct(odd_a: float, odd_b: float) -> float:
    return (1 - (1 / odd_a + 1 / odd_b)) * 100


MIN_ARB_PCT = 9.0


async def _store_arb(match_label: str, arb: dict, duration_seconds: int):
    margin = arb["snapshot"]["margin_pct"]
    if margin < MIN_ARB_PCT:
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    date_str = f"{now.day}/{now.month}/{now.year}"
    try:
        sb = _get_supabase()
        sb.table("arbitrage").insert({
            "name": arb["label"],
            "match": match_label,
            "market_type": arb["market_type"],
            "market_options_odds": json.dumps(arb["snapshot"]),
            "margin_pct": margin,
            "duration_seconds": duration_seconds,
            "date": date_str,
        }).execute()
        print(f"[TT_live worker] arb stored: {match_label} | {arb['label']} | {margin}% | {duration_seconds}s")
    except Exception as e:
        print(f"[TT_live worker] arb store failed: {e}")


# ─── Workers ──────────────────────────────────────────────────────────────────

MAX_WORKERS = 10
WORKER_POLL_SECONDS = 2

_workers: dict = {}


async def _match_worker(key, bk_fixture_id: int, b9_event_id: str, match_label: str, worker_num: int):
    tag = f"[worker {worker_num}]"
    active_arbs = {}
    last_logged_pause_set = None

    print(f"{tag} monitoring: {match_label}")

    async with httpx.AsyncClient(http2=True) as client:
        while True:
            await asyncio.sleep(WORKER_POLL_SECONDS)

            try:
                bk1_raw, bk2_raw, b9_raw = await asyncio.gather(
                    fetch_betking_event(client, bk_fixture_id, 1),
                    fetch_betking_event(client, bk_fixture_id, 2),
                    fetch_bet9ja_event(client, b9_event_id),
                )
            except Exception as e:
                print(f"{tag} {match_label} fetch failed: {e}")
                continue

            if _betking_match_ended(bk1_raw) or _bet9ja_match_ended(b9_raw):
                print(f"{tag} {match_label} -- match ended, freeing worker")
                break

            bk_status = _betking_event_status(bk1_raw)
            b9_es     = _bet9ja_event_status(b9_raw)
            pause_set = _both_paused_same_set(bk_status, b9_es)

            if pause_set is None:
                # Not paused — close any open arbs (set resumed)
                for mkey in list(active_arbs.keys()):
                    arb = active_arbs.pop(mkey)
                    duration = int(time.monotonic() - arb["start"])
                    print(f"{tag} arb CLOSED (resumed): {match_label} | {arb['label']} | {duration}s")
                    await _store_arb(match_label, arb, duration)
                if last_logged_pause_set is not None:
                    print(f"{tag} {match_label} -- set resumed, waiting for next pause")
                    last_logged_pause_set = None
                continue

            # Both paused on same set — log once per pause
            if pause_set != last_logged_pause_set:
                print(f"{tag} {match_label} -- PAUSE Set {pause_set} | BK: {bk_status} | B9J: {b9_es}")
                last_logged_pause_set = pause_set

            # ── Arb comparison ────────────────────────────────────────────────
            bk_markets = parse_betking_event(bk1_raw.get("markets", []), bk2_raw.get("markets", []))
            b9_markets = parse_bet9ja_event(b9_raw)

            common       = set(bk_markets) & set(b9_markets)
            still_active = set()

            for mkey in common:
                bkm, b9m = bk_markets[mkey], b9_markets[mkey]
                best_a      = max(bkm["side_a"], b9m["side_a"])
                best_a_book = "betking" if bkm["side_a"] >= b9m["side_a"] else "bet9ja"
                best_b      = max(bkm["side_b"], b9m["side_b"])
                best_b_book = "betking" if bkm["side_b"] >= b9m["side_b"] else "bet9ja"

                if _arb_margin_pct(best_a, best_b) <= 0:
                    continue

                still_active.add(mkey)
                snapshot = {
                    "side_a_odd": best_a, "side_a_book": best_a_book,
                    "side_b_odd": best_b, "side_b_book": best_b_book,
                    "margin_pct": round(_arb_margin_pct(best_a, best_b), 2),
                }

                if mkey not in active_arbs:
                    active_arbs[mkey] = {
                        "start":       time.monotonic(),
                        "label":       bkm["label"],
                        "market_type": bkm["market_type"],
                    }
                    print(f"{tag} arb OPEN: {match_label} | {bkm['label']} | {snapshot['margin_pct']}% | A:{snapshot['side_a_odd']}({snapshot['side_a_book']}) B:{snapshot['side_b_odd']}({snapshot['side_b_book']})")
                active_arbs[mkey]["snapshot"] = snapshot

            for mkey in [k for k in active_arbs if k not in still_active]:
                arb = active_arbs.pop(mkey)
                duration = int(time.monotonic() - arb["start"])
                print(f"{tag} arb CLOSED: {match_label} | {arb['label']} | {duration}s")
                await _store_arb(match_label, arb, duration)

    for arb in active_arbs.values():
        duration = int(time.monotonic() - arb["start"])
        print(f"{tag} arb CLOSED (match end): {match_label} | {arb['label']} | {duration}s")
        await _store_arb(match_label, arb, duration)

    _workers.pop(key, None)


# ─── Main loop ────────────────────────────────────────────────────────────────

async def run_once():
    async with httpx.AsyncClient(http2=True) as client:
        try:
            bk_raw, b9_raw = await asyncio.gather(
                fetch_betking_live(client),
                fetch_bet9ja_live(client),
            )
        except Exception as e:
            print(f"[TT_live] fetch failed: {e}")
            return

    bk_matches = parse_betking_live(bk_raw)
    b9_matches = parse_bet9ja_live(b9_raw)

    common_keys = set(bk_matches) & set(b9_matches)

    for key in [k for k, t in _workers.items() if t.done()]:
        _workers.pop(key, None)

    new_matches = [k for k in common_keys if k not in _workers]

    if not common_keys:
        print("[TT_live] no TT live fixtures available")
        return

    if new_matches:
        print(f"[TT_live] BetKing={len(bk_matches)} Bet9ja={len(b9_matches)} matched={len(common_keys)} | {len(new_matches)} new")

    for key in new_matches:
        if len(_workers) >= MAX_WORKERS:
            print(f"[TT_live] worker cap reached ({MAX_WORKERS}), skipping remaining matches")
            break

        bkm, b9m = bk_matches[key], b9_matches[key]
        b9_event_id = b9m["event_id"]
        match_label = f"{bkm['home']} vs {bkm['away']}"
        worker_num = len(_workers) + 1
        _workers[key] = asyncio.create_task(_match_worker(key, bkm["event_id"], b9_event_id, match_label, worker_num))
        print(f"[TT_live] worker {worker_num} started: {match_label} | BetKing: {bkm['status_label']} ({bkm['score']}) | Bet9ja: {b9m['status_label']} ({b9m['score']})")


if __name__ == "__main__":
    asyncio.run(run_once())
