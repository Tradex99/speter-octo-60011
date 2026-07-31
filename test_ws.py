"""
Standalone WebSocket connection test covering two things:

1. OKX -- the only exchange the bot actually trades on:
     - public WS:  connect + subscribe to a ticker channel, confirm data arrives
     - private WS: connect + login, confirm the server accepts the signature
   Mirrors the DB-loading pattern used in tracker.py (db.txt -> Supabase
   api_keys table), looking for the row where exchange_name == "okx".
   OKX credentials are a triplet: api_key, api_secret, AND passphrase
   (the passphrase is set by you when the API key was created on OKX --
   it's not something OKX generates, so make sure it's stored too).

2. Coinbase, Kraken, MEXC -- public market-data feeds only, no auth, no
   orders. These back the multi-exchange confirmation layer in
   cross_exchange_validator.py (not yet built). Each is subscribed for
   BTC and checked for a real data message, not just a subscribe ack.

Run with:  python test_ws.py
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time

import websockets

# --- toggle this exactly like DEMO_TRADING in tracker.py (OKX only --
# Coinbase/Kraken/MEXC public feeds have no demo/live distinction) ---
# OKX calls this "demo trading" (paper trading), not "testnet", and it
# uses a completely different host (wspap) rather than a URL flag.
DEMO_TRADING = True

OKX_PUBLIC_WS_URL = (
    "wss://wspap.okx.com:8443/ws/v5/public"
    if DEMO_TRADING
    else "wss://ws.okx.com:8443/ws/v5/public"
)
OKX_PRIVATE_WS_URL = (
    "wss://wspap.okx.com:8443/ws/v5/private"
    if DEMO_TRADING
    else "wss://ws.okx.com:8443/ws/v5/private"
)

CONNECT_TIMEOUT_SEC = 10
RESPONSE_WAIT_SEC = 10       # used by the OKX private login check
MESSAGE_TIMEOUT_SEC = 15     # used by the public multi-exchange checks

# One symbol per exchange, all mapping to the same underlying market
# (BTC vs. USD/USDT). OKX uses the swap instrument the bot actually
# trades; cross_exchange_validator.py will need a fuller version of this
# mapping for every symbol it evaluates.
SYMBOL_MAP = {
    "okx": "BTC-USDT-SWAP",
    "coinbase": "BTC-USD",    # Coinbase has no USDT spot pair for BTC
    "kraken": "BTC/USD",
    "mexc": "BTC_USDT",       # MEXC contract (futures) symbol format
}


def _load_db_config() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
    ]
    for path in candidates:
        if os.path.exists(path):
            config = {}
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, _, v = line.partition("=")
                        config[k.strip()] = v.strip().strip('"')
            return config
    raise FileNotFoundError(f"db.txt not found in any of: {candidates}")


def _clean_secret(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.upper() in ("", "NULL"):
            return None
    return value


def load_okx_credentials() -> dict:
    """Same shape as tracker.py's load_bitmart_credentials(), but looks
    for the 'okx' row in the api_keys Supabase table. OKX needs a
    passphrase in addition to key/secret, so that column has to be
    populated in the table for this to work."""
    from supabase import create_client

    config = _load_db_config()
    sb = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])

    rows = sb.table("api_keys").select("*").execute()
    match = next(
        (r for r in rows.data or [] if (r.get("exchange_name") or "").strip().lower() == "okx"),
        None,
    )
    if not match:
        raise RuntimeError("No 'okx' row found in the api_keys Supabase table")

    api_key = _clean_secret(match.get("api_key_demo"))
    api_secret = _clean_secret(match.get("api_secret_demo"))
    passphrase = _clean_secret(match.get("passphrase"))

    missing = [n for n, v in (("api_key", api_key), ("api_secret", api_secret), ("passphrase", passphrase)) if not v]
    if missing:
        raise RuntimeError(f"OKX credentials incomplete in Supabase — missing: {', '.join(missing)}")

    return {"api_key": api_key, "api_secret": api_secret, "passphrase": passphrase}


def build_login_args(api_key: str, api_secret: str, passphrase: str) -> dict:
    timestamp = str(int(time.time()))
    message = f"{timestamp}GET/users/self/verify"
    sign = base64.b64encode(
        hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    return {"apiKey": api_key, "passphrase": passphrase, "timestamp": timestamp, "sign": sign}


# ---------------------------------------------------------------------------
# Public multi-exchange connectivity checks (OKX, Coinbase, Kraken, MEXC)
# ---------------------------------------------------------------------------

class ExchangeWSResult:
    def __init__(self, name):
        self.name = name
        self.connected = False
        self.subscribed = False
        self.received_message = False
        self.error = None
        self.latency_sec = None


async def _time_to_first_message(name, url, subscribe_payload, is_data_message):
    """Shared skeleton: connect, subscribe, wait for the first message that
    looks like real market data (not just a subscribe ack), then close."""
    result = ExchangeWSResult(name)
    start = time.time()
    try:
        async with websockets.connect(url, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
            result.connected = True
            print(f"[{name}] connected to {url}")

            await ws.send(json.dumps(subscribe_payload))
            result.subscribed = True
            print(f"[{name}] sent subscribe request")

            deadline = time.time() + MESSAGE_TIMEOUT_SEC
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
                except asyncio.TimeoutError:
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if is_data_message(msg):
                    result.received_message = True
                    result.latency_sec = time.time() - start
                    print(f"[{name}] \u2705 received live data after {result.latency_sec:.2f}s")
                    break

            if not result.received_message:
                print(f"[{name}] WARNING: connected but no data message within {MESSAGE_TIMEOUT_SEC}s")

    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"[{name}] \u274c connection failed: {result.error}")

    return result


async def test_okx_public():
    payload = {"op": "subscribe", "args": [{"channel": "tickers", "instId": SYMBOL_MAP["okx"]}]}

    def is_data(msg):
        return msg.get("arg", {}).get("channel") == "tickers" and "data" in msg

    return await _time_to_first_message("okx", OKX_PUBLIC_WS_URL, payload, is_data)


async def test_coinbase_public():
    # Public "Exchange" feed -- ticker channel needs no auth.
    url = "wss://ws-feed.exchange.coinbase.com"
    payload = {"type": "subscribe", "product_ids": [SYMBOL_MAP["coinbase"]], "channels": ["ticker"]}

    def is_data(msg):
        return msg.get("type") == "ticker"

    return await _time_to_first_message("coinbase", url, payload, is_data)


async def test_kraken_public():
    # Kraken WS API v2, public ticker channel.
    url = "wss://ws.kraken.com/v2"
    payload = {"method": "subscribe", "params": {"channel": "ticker", "symbol": [SYMBOL_MAP["kraken"]]}}

    def is_data(msg):
        return msg.get("channel") == "ticker" and msg.get("type") in ("snapshot", "update")

    return await _time_to_first_message("kraken", url, payload, is_data)


async def test_mexc_public():
    # MEXC contract (futures) public feed -- matches perpetual-swap data,
    # not the spot feed, since that's what a futures cross-check needs.
    url = "wss://contract.mexc.com/edge"
    payload = {"method": "sub.ticker", "param": {"symbol": SYMBOL_MAP["mexc"]}}

    def is_data(msg):
        return msg.get("channel") == "push.ticker" and "data" in msg

    return await _time_to_first_message("mexc", url, payload, is_data)


async def run_public_exchange_checks() -> bool:
    print("Testing public WebSocket connectivity for: OKX, Coinbase, Kraken, MEXC")
    print(f"Test symbol per exchange: {SYMBOL_MAP}")
    print("No API keys, no authentication, no orders -- public market data only.\n")

    results = await asyncio.gather(
        test_okx_public(), test_coinbase_public(), test_kraken_public(), test_mexc_public(),
        return_exceptions=True,
    )

    print("\n" + "=" * 60)
    print("PUBLIC MARKET DATA WEBSOCKET SUMMARY")
    print("=" * 60)
    all_ok = True
    for r in results:
        if isinstance(r, Exception):
            print(f"UNKNOWN    FAIL     crashed: {r}")
            all_ok = False
            continue
        status = "PASS" if r.received_message else "FAIL"
        if not r.received_message:
            all_ok = False
        detail = r.error or (f"{r.latency_sec:.2f}s to first tick" if r.received_message else "no data received")
        print(f"{r.name.upper():10s} {status:6s} {detail}")
    print("=" * 60 + "\n")
    return all_ok


# ---------------------------------------------------------------------------
# OKX private (authenticated) check -- OKX only, since it's the only
# exchange the bot ever sends orders to.
# ---------------------------------------------------------------------------

async def test_private_ws(creds: dict):
    print(f"[private] connecting to {OKX_PRIVATE_WS_URL}")
    async with websockets.connect(OKX_PRIVATE_WS_URL, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
        login_args = build_login_args(creds["api_key"], creds["api_secret"], creds["passphrase"])
        print("[private] connected — sending login")
        await ws.send(json.dumps({"op": "login", "args": [login_args]}))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=RESPONSE_WAIT_SEC)
        except asyncio.TimeoutError:
            print("[private] WARNING: no login response received within timeout")
            return

        msg = json.loads(raw)
        if msg.get("event") == "login" and msg.get("code") == "0":
            print(f"[private] \u2705 login successful — {msg}")
        else:
            print(f"[private] \u274c login failed — {msg}")
            print("[private] if this key requires IP whitelisting, confirm this machine's outbound IP is whitelisted on OKX")


async def main():
    print(f"WebSocket connectivity test (OKX demo_trading={DEMO_TRADING})\n")

    all_public_ok = await run_public_exchange_checks()

    creds = load_okx_credentials()
    await test_private_ws(creds)

    if not all_public_ok:
        print("\nOne or more public data feeds failed -- resolve before building cross_exchange_validator.py on top of them.")


if __name__ == "__main__":
    asyncio.run(main())
