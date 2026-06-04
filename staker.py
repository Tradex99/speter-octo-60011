import httpx
from collections import deque
import asyncio
import json
import os
from urllib.parse import urlencode


#Config
STAKE_AMOUNT = 10

PLACEBET_URL = (
    "https://m.betking.com/en-ng/sports/action/placebet"
    "?_data=routes%2F%28%24locale%29.sports.action.placebet"
)

PLACEBET_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng",
    "Content-Type":    "application/x-www-form-urlencoded;charset=UTF-8",
    "Origin":          "https://m.betking.com",
    "Dnt":             "1",
    "Sec-Gpc":         "1",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "Priority":        "u=4",
    "Te":              "trailers",
}


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


def get_cookie():
    from supabase import create_client
    config   = load_db_config("db.txt")
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    row      = supabase.table("betking").select("cookie").eq("id", 1).single().execute()
    return row.data["cookie"]


def _build_odds_entry(p):
    return {
        "IDSelectionType":    p["selectionTypeId"],
        "IDSport":            1,
        "allowFixed":         True,
        "compatibilityLevel": 0,
        "eventCategory":      "L",
        "eventId":            p["categoryId"],
        "eventName":          p["categoryName"],
        "fixed":              False,
        "gamePlay":           1,
        "incompatibleEvents": [],
        "isExpired":          False,
        "isLocked":           False,
        "isBetBuilder":       False,
        "marketId":           p["marketId"],
        "marketName":         p["marketName"],
        "marketTag":          0,
        "marketTypeId":       p["marketTypeId"],
        "matchId":            p["matchId"],
        "matchName":          p["matchName"],
        "oddValue":           p["oddValue"],
        "parentEventId":      p["parentEventId"],
        "selectionId":        p["selectionId"],
        "selectionName":      p["selectionName"],
        "smartCode":          0,
        "specialValue":       p["specialValue"],
        "sportName":          "Football",
        "tournamentId":       p["tournamentId"],
        "tournamentName":     p["tournamentName"],
        "selectionKMId":      p["selectionKMId"],
        "matchKMId":          p["matchKMId"],
        "marketKMId":         p["marketKMId"],
        "isTransitioned":     False,
    }


def _zero_grouping(grouping, combinations):
    return {
        "grouping":           grouping,
        "combinations":       combinations,
        "minWin":             0,
        "minWinNet":          0,
        "netStakeMinWin":     0,
        "maxWin":             0,
        "maxWinNet":          0,
        "netStakeMaxWin":     0,
        "minBonus":           0,
        "maxBonus":           0,
        "minPercentageBonus": 0,
        "maxPercentageBonus": 0,
        "stake":              -1,
        "netStake":           -1,
        "selected":           False,
    }


def _win_grouping(grouping, combinations, win, stake):
    return {
        "grouping":           grouping,
        "combinations":       combinations,
        "minWin":             win,
        "minWinNet":          win,
        "netStakeMinWin":     win,
        "maxWin":             win,
        "maxWinNet":          win,
        "netStakeMaxWin":     win,
        "minBonus":           0,
        "maxBonus":           0,
        "minPercentageBonus": 0,
        "maxPercentageBonus": 0,
        "stake":              stake,
        "netStake":           stake,
        "selected":           True,
    }


def _build_coupon(payloads, tx_id):
    """
    Build betCoupon for 1, 2, or 3 selections.
    payloads: list of bet payload dicts
    """
    from math import prod
    n     = len(payloads)
    stake = STAKE_AMOUNT

    total_odds = round(prod(p["oddValue"] for p in payloads), 10)
    win        = round(stake * total_odds, 2)

    # couponTypeId: 1 = single, 2 = multiple
    coupon_type = 1 if n == 1 else 2

    # For single: one grouping at level 1 selected
    # For multi:  lower groupings unselected, top grouping (=n) selected
    from math import comb as ncomb
    if n == 1:
        groupings      = [_win_grouping(1, 1, win, stake)]
        all_groupings  = [_win_grouping(1, 1, win, stake)]
        possible_miss  = []
    else:
        groupings     = [_win_grouping(n, 1, win, stake)]
        all_groupings = [_zero_grouping(k, ncomb(n, k)) for k in range(1, n)]
        all_groupings.append(_win_grouping(n, 1, win, stake))
        possible_miss = [{"grouping": k, "combinations": ncomb(n, k)} for k in range(1, n)]

    bet_type = "Singles" if n == 1 else "Multiple"
    # trackingData category: "football" if all same sport, "mixed" otherwise
    category = "football"

    return {
        "betCoupon": {
            "isClientSideCoupon":  True,
            "couponTypeId":        coupon_type,
            "minWin":              win,
            "minWinNet":           win,
            "netStakeMinWin":      win,
            "maxWin":              win,
            "maxWinNet":           win,
            "netStakeMaxWin":      win,
            "minBonus":            0,
            "maxBonus":            0,
            "minPercentageBonus":  0,
            "maxPercentageBonus":  0,
            "minOdd":              round(total_odds, 2),
            "maxOdd":              round(total_odds, 2),
            "totalOdds":           total_odds,
            "stake":               stake,
            "useGroupsStake":      False,
            "stakeGross":          stake,
            "stakeTaxed":          0,
            "taxPercentage":       0,
            "tax":                 0,
            "minWithholdingTax":   0,
            "maxWithholdingTax":   0,
            "turnoverTax":         0,
            "totalCombinations":   1,
            "odds":                [_build_odds_entry(p) for p in payloads],
            "groupings":           groupings,
            "possibleMissingGroupings": possible_miss,
            "currencyId":          -1,
            "isLive":              True,
            "isVirtual":           False,
            "currentEvalMotivation": 0,
            "betCouponGlobalVariable": {
                "currencyId":                -1,
                "defaultStakeGross":         100,
                "isVirtualsInstallation":    False,
                "maxBetStake":               75000000,
                "maxCombinationBetWin":      75000000,
                "maxCombinationsByGrouping": 10000,
                "maxCouponCombinations":     10000,
                "maxGroupingsBetStake":      41641682,
                "maxMultipleBetWin":         75000000,
                "maxNoOfEvents":             40,
                "maxNoOfSelections":         40,
                "maxSingleBetWin":           75000000,
                "minBetStake":               10,
                "minBonusOdd":               1.35,
                "minFlexiCutOdds":           1.05,
                "minFlexiCutSelections":     5,
                "minGroupingsBetStake":      5,
                "stakeInnerMod0Combination": 0.01,
                "stakeMod0Multiple":         0,
                "stakeMod0Single":           0,
                "stakeThresholdMultiple":    75000,
                "stakeThresholdSingle":      7500,
                "flexiCutGlobalVariable": {
                    "parameters": {
                        "formulaId":            1,
                        "minOddThreshold":      1.05,
                        "minWinningSelections": 2,
                    }
                },
            },
            "language":     "en",
            "hasLive":      True,
            "couponType":   coupon_type,
            "allGroupings": all_groupings,
        },
        "allowOddChanges":        True,
        "allowStakeReduction":    False,
        "requestTransactionId":   tx_id,
        "transferStakeFromAgent": False,
        "trackingData": {
            "category":          category,
            "product":           "sportsbook-live",
            "is_reuse_bet":      False,
            "reused_selections": 0,
            "origin_bet_status": None,
            "bet_type":          bet_type,
        },
    }


def _build_body(payloads, tx_id):
    coupon = _build_coupon(payloads, tx_id)
    adjust = {"adjustId": "", "adjustIdfa": "", "gpsAdId": ""}
    return urlencode({
        "data":      json.dumps(coupon, separators=(",", ":")),
        "adjustIds": json.dumps(adjust, separators=(",", ":")),
    })


class Staker:
    def __init__(self):
        self._queue     = asyncio.Queue()
        self._bet_cache = deque(maxlen=20)  # event_ids of successfully placed bets

    async def queue(self, payloads: list, tx_id: str):
        """Queue a batch of 1-3 payloads to be staked together."""
        await self._queue.put((payloads, tx_id))

    async def run(self):
        while True:
            item = await self._queue.get()
            try:
                payloads, tx_id = item
                await self._place_bet(payloads, tx_id)
            except Exception as e:
                print(f"[staker] error      : {e}")
            finally:
                self._queue.task_done()

    async def _place_bet(self, payloads: list, tx_id: str):
        n = len(payloads)

        # Filter out already-cached event_ids
        fresh = [p for p in payloads if p["eventId"] not in self._bet_cache]
        if not fresh:
            print(f"[staker] cached     : all {n} selections already bet, skipping")
            return
        if len(fresh) < n:
            skipped = n - len(fresh)
            print(f"[staker] cached     : {skipped} selection(s) already bet, continuing with {len(fresh)}")
            payloads = fresh
            n = len(fresh)

        bet_type = "Single" if n == 1 else f"Multi x{n}"
        names    = " + ".join(p["matchName"] for p in payloads)
        lines    = " + ".join(str(p["targetLine"]) for p in payloads)
        odds_str = " x ".join(str(p["oddValue"]) for p in payloads)

        print(f"[staker] placing    : [{bet_type}] {names}")
        print(f"[staker] bet        : {lines} | odds: {odds_str}")

        from module.betlist import check_can_bet
        allowed = await check_can_bet()
        if not allowed:
            print(f"[staker] skipped    : betlist check failed")
            return

        try:
            cookie = get_cookie()
        except Exception as e:
            print(f"[staker] cookie err : {e}")
            return

        headers     = {**PLACEBET_HEADERS, "Cookie": cookie}
        tickets     = 5
        placed      = 0
        any_session_error = False

        for ticket_num in range(1, tickets + 1):
            import random
            ticket_tx_id = str(random.randint(10_000_000_000, 99_999_999_999))
            body         = _build_body(payloads, ticket_tx_id)

            try:
                async with httpx.AsyncClient(http2=True, timeout=20.0) as client:
                    resp = await client.put(PLACEBET_URL, headers=headers, content=body.encode())
            except Exception as e:
                print(f"[staker] ticket {ticket_num}  : request error - {e}")
                continue

            if resp.status_code in (401, 301):
                print(f"[staker] ticket {ticket_num}  : expired ({resp.status_code}) - re-login")
                any_session_error = True
                break

            if resp.status_code != 200:
                print(f"[staker] ticket {ticket_num}  : HTTP {resp.status_code}")
                continue

            if not resp.text or not resp.text.strip():
                print(f"[staker] ticket {ticket_num}  : expired (empty body) - re-login")
                any_session_error = True
                break

            try:
                data        = resp.json()
                coupon_code = data.get("couponCode")
                status      = data.get("responseStatus")
            except Exception:
                print(f"[staker] ticket {ticket_num}  : could not parse response")
                continue

            if coupon_code:
                placed += 1
                print(f"[staker] ticket {ticket_num}  : placed | coupon: {coupon_code}")
                from module.betlist import increment_played
                await increment_played()
            else:
                errors = data.get("errorsList", {})
                print(f"[staker] ticket {ticket_num}  : rejected | status={status} | errors={errors}")

        if any_session_error:
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from login import login
            await login()

        if placed > 0:
            for p in payloads:
                self._bet_cache.append(p["eventId"])
            print(f"[staker] summary    : {placed}/{tickets} tickets placed | {names} | {lines}")
            print(f"[staker] cache      : {len(self._bet_cache)}/20 slots used")
        else:
            print(f"[staker] summary    : 0/{tickets} tickets placed | {names}")
