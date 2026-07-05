"""
TT_betlist.py -- Balance and open-bet guard for TT_live.py.

Called by TT_live.py after every stake execution.
Returns True  → safe to continue monitoring (sufficient balance, ≤1 open bet each).
Returns False → stop TT_live completely (no balance and no open bets to wait for).
Blocks       → waits for open bets to settle then re-checks balance.
"""
import asyncio
import httpx

# ─── Config ───────────────────────────────────────────────────────────────────

MIN_BALANCE        = 20.0    # NGN minimum to consider a platform "funded"
OPEN_BET_THRESHOLD = 1       # wait while open bets >= this per platform
POLL_INTERVAL      = 30.0    # seconds between re-checks when waiting for settlement

# A balance of -1 from Bet9ja means the API call failed (expired cookie /
# transient API failure) -- it is NOT a real balance. Re-check a couple of
# times before concluding the cookie is actually dead.
B9_BALANCE_MAX_RETRIES = 2   # "X2" -- extra attempts after the first
B9_BALANCE_RETRY_DELAY = 5.0 # seconds between retries

BETKING_ACCOUNT_NAME = "Chukwuebuka"
BET9JA_ACCOUNT_NAME  = "wixnation"
REQUEST_TIMEOUT      = 10.0

# BetKing
BK_WALLET_URL = "https://m.betking.com/api/account/v1/users/me/wallet"
BK_OPEN_URL   = (
    "https://m.betking.com/en-ng/my-bets/sports/open"
    "?_data=routes%2F%28%24locale%29.my-bets.sports.%24betsType"
)
BK_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng/my-bets/sports/settled",
    "Dnt":             "1",
    "Sec-Gpc":         "1",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "Priority":        "u=0",
    "Te":              "trailers",
}

# Bet9ja
B9_BALANCE_URL  = "https://sports.bet9ja.com/desktop/feapi/ClientAjax/GetBalanceAndBonus?v_cache_version=1.317.3.243"
B9_OPEN_URL     = (
    "https://coupon.bet9ja.com/desktop/feapi/CouponAjax/GetCouponsWithCashout"
    "?numRecord=0&couponStatus=0&pageSize=5&recordStart=0&currentPageIndex=0"
    "&v_cache_version=1.317.3.243"
)
B9_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin":          "https://sports.bet9ja.com",
    "Referer":         "https://sports.bet9ja.com/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-site",
    "Sec-Gpc":         "1",
    "Te":              "trailers",
}


# ─── Exceptions ───────────────────────────────────────────────────────────────

class NeedRelogin(Exception):
    """Raised when a platform's balance check keeps returning -1 (API failure /
    expired cookie) even after retries. Caller (tracker.py) should re-run the
    relevant login script and call check_and_wait(allow_relogin=False) again."""
    def __init__(self, account: str):
        self.account = account
        super().__init__(f"{account}: balance check failed after retries — cookie likely expired")


# ─── DB / Cookie ──────────────────────────────────────────────────────────────

def _load_db_config() -> dict:
    import os
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


def _get_cookie(account_name: str) -> str:
    try:
        sb = _get_supabase()
        row = (
            sb.table("cookies")
            .select("cookie")
            .eq("name", account_name)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if row.data:
            return row.data[0].get("cookie") or ""
        return ""
    except Exception as e:
        print(f"[TT_betlist] cookie error ({account_name}): {e}")
        return ""


# ─── BetKing checks ───────────────────────────────────────────────────────────

async def _bk_balance(client: httpx.AsyncClient) -> float:
    """Return BetKing wallet balance. Returns -1 on error."""
    try:
        headers = {**BK_HEADERS, "Cookie": _get_cookie(BETKING_ACCOUNT_NAME)}
        resp = await client.get(BK_WALLET_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("data", {}).get("balance", -1))
    except Exception as e:
        print(f"[TT_betlist] BetKing balance error: {e}")
        return -1


async def _bk_open_bets(client: httpx.AsyncClient) -> int:
    """Return number of open BetKing bets. Returns -1 on error."""
    try:
        headers = {**BK_HEADERS, "Cookie": _get_cookie(BETKING_ACCOUNT_NAME)}
        resp = await client.get(BK_OPEN_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code in (401, 301):
            print(f"[TT_betlist] BetKing open bets: session expired ({resp.status_code})")
            return -1
        if not resp.text or not resp.text.strip():
            print("[TT_betlist] BetKing open bets: empty response")
            return -1
        data = resp.json()
        # Count coupons with a valid couponCode (same pattern as betlist.py)
        coupons = data.get("couponsData", {}).get("coupons", [])
        return sum(1 for c in coupons if c.get("couponCode"))
    except Exception as e:
        print(f"[TT_betlist] BetKing open bets error: {e}")
        return -1


# ─── Bet9ja checks ────────────────────────────────────────────────────────────

async def _b9_balance(client: httpx.AsyncClient) -> float:
    """Return Bet9ja account balance. Returns -1 on error."""
    try:
        headers = {**B9_HEADERS, "Cookie": _get_cookie(BET9JA_ACCOUNT_NAME)}
        resp = await client.get(B9_BALANCE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return float((data.get("D") or {}).get("amount", -1))
    except Exception as e:
        print(f"[TT_betlist] Bet9ja balance error: {e}")
        return -1


async def _b9_balance_with_retry(client: httpx.AsyncClient) -> float:
    """
    Wrap _b9_balance with retries. A -1 result means the API call failed
    (expired cookie / transient failure), not a real balance, so it's worth
    re-checking a couple of times before treating it as authoritative.
    """
    bal = await _b9_balance(client)
    attempt = 0
    while bal == -1 and attempt < B9_BALANCE_MAX_RETRIES:
        attempt += 1
        print(
            f"[TT_betlist] Bet9ja balance returned -1, retrying "
            f"({attempt}/{B9_BALANCE_MAX_RETRIES}) in {B9_BALANCE_RETRY_DELAY}s..."
        )
        await asyncio.sleep(B9_BALANCE_RETRY_DELAY)
        bal = await _b9_balance(client)
    return bal


async def _b9_open_bets(client: httpx.AsyncClient) -> int:
    """
    Return number of open Bet9ja bets.
    recordsFiltered >= 2 means 2+ open bets → wait.
    recordsFiltered == 1 → 1 open bet → check balance then decide.
    recordsFiltered == 0 → no open bets.
    Returns -1 on error.
    """
    try:
        headers = {**B9_HEADERS, "Cookie": _get_cookie(BET9JA_ACCOUNT_NAME)}
        resp = await client.get(B9_OPEN_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        D = data.get("D") or {}
        # Count individual couponIds in data array
        coupons = D.get("data") or []
        return len([c for c in coupons if c.get("couponId")])
    except Exception as e:
        print(f"[TT_betlist] Bet9ja open bets error: {e}")
        return -1


# ─── Main guard function ──────────────────────────────────────────────────────

async def check_and_wait(allow_relogin: bool = True) -> bool:
    """
    Called after a stake execution. Returns:
      True  → both platforms have sufficient balance (>= MIN_BALANCE), safe to continue.
      False → no balance on either platform AND no open bets to wait for → stop TT_live.

    Raises:
      NeedRelogin → Bet9ja balance kept returning -1 after retries (likely an
        expired cookie). Only raised when allow_relogin=True. Caller should
        run b9_login.py and call check_and_wait(allow_relogin=False) again.

    Blocks until:
      - Both platforms reach sufficient balance (after bets settle), OR
      - No open bets remain on both platforms and balance is still insufficient → returns False.
    """
    async with httpx.AsyncClient(http2=True) as client:
        while True:
            bk_bal, b9_bal, bk_open, b9_open = await asyncio.gather(
                _bk_balance(client),
                _b9_balance_with_retry(client),
                _bk_open_bets(client),
                _b9_open_bets(client),
            )

            print(
                f"[TT_betlist] BetKing: balance=₦{bk_bal:.2f} open={bk_open} | "
                f"Bet9ja: balance=₦{b9_bal:.2f} open={b9_open}"
            )

            # -1 after retries means the API/cookie is broken, not that the
            # account is actually empty. Signal the caller to re-login rather
            # than treating it as a normal low-balance condition.
            if b9_bal == -1 and allow_relogin:
                print("[TT_betlist] ⚠️ Bet9ja balance still -1 after retries — signaling relogin")
                raise NeedRelogin("bet9ja")

            bk_ok = bk_bal >= MIN_BALANCE
            b9_ok = b9_bal >= MIN_BALANCE

            # Both platforms have sufficient balance — continue
            if bk_ok and b9_ok:
                print("[TT_betlist] ✅ sufficient balance on both platforms — continuing")
                return True

            # Check which platform is low
            low_platforms = []
            if not bk_ok:
                low_platforms.append(f"BetKing(₦{bk_bal:.2f})")
            if not b9_ok:
                low_platforms.append(f"Bet9ja(₦{b9_bal:.2f})")
            print(f"[TT_betlist] low balance on: {', '.join(low_platforms)}")

            # Check if there are open bets to wait for
            # Wait while >= OPEN_BET_THRESHOLD open bets exist on either platform
            has_open = (bk_open >= OPEN_BET_THRESHOLD) or (b9_open >= OPEN_BET_THRESHOLD)

            if not has_open:
                # No open bets and insufficient balance — stop
                print("[TT_betlist] ❌ no open bets and insufficient balance — stopping TT_live")
                return False

            # There are open bets — wait for them to settle then re-check
            total_open = max(bk_open, b9_open)
            print(f"[TT_betlist] waiting for {total_open} open bet(s) to settle... (retry in {POLL_INTERVAL}s)")
            await asyncio.sleep(POLL_INTERVAL)
