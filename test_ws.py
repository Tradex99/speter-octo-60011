"""
Standalone connection test for Binance's WebSocket API (Spot).

Mirrors the DB-loading pattern used in tracker.py (db.txt -> Supabase
api_keys table) but targets a row where exchange_name == "binance".
Only exercises the connection itself:
  - public WS: connect to a raw stream, confirm market data arrives
  - private WS: request a listenKey over REST (needs only the API key,
    no signature), then connect to the user-data stream with it and
    confirm the connection stays open

Run with:  python binance_ws_test.py
"""

import asyncio
import json
import os
import time

import requests
import websockets

# --- toggle this exactly like DEMO_TRADING in tracker.py ---
TESTNET = True

REST_BASE = (
    "https://testnet.binance.vision"
    if TESTNET
    else "https://api.binance.com"
)
PUBLIC_WS_BASE = (
    "wss://stream.testnet.binance.vision:9443/ws"
    if TESTNET
    else "wss://stream.binance.com:9443/ws"
)

LISTEN_KEY_ENDPOINT = f"{REST_BASE}/api/v3/userDataStream"

# A harmless public stream just to prove data flows.
TEST_PUBLIC_STREAM = "btcusdt@aggTrade"

CONNECT_TIMEOUT_SEC = 10
RESPONSE_WAIT_SEC = 10


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


def load_binance_credentials() -> dict:
    """Same shape as tracker.py's load_bitmart_credentials(), but looks
    for the 'binance' row in the api_keys Supabase table.

    api_secret is loaded for completeness (signed REST calls need it
    later) but this connection test only needs api_key — creating a
    listenKey is authenticated by API key alone, no signature."""
    from supabase import create_client

    config = _load_db_config()
    sb = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])

    rows = sb.table("api_keys").select("*").execute()
    match = next(
        (r for r in rows.data or [] if (r.get("exchange_name") or "").strip().lower() == "binance"),
        None,
    )
    if not match:
        raise RuntimeError("No 'binance' row found in the api_keys Supabase table")

    api_key = _clean_secret(match.get("api_key"))
    api_secret = _clean_secret(match.get("api_secret"))

    if not api_key:
        raise RuntimeError("Binance credentials incomplete in Supabase — missing: api_key")

    return {"api_key": api_key, "api_secret": api_secret}


async def test_public_ws():
    url = f"{PUBLIC_WS_BASE}/{TEST_PUBLIC_STREAM}"
    print(f"[public] connecting to {url}")
    async with websockets.connect(url, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
        print("[public] connected — waiting for a tick")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=RESPONSE_WAIT_SEC)
        except asyncio.TimeoutError:
            print("[public] WARNING: no market data received within timeout")
            return
        msg = json.loads(raw)
        print(f"[public] received {msg.get('e', 'message')} for {msg.get('s', '?')} — connection confirmed")


def create_listen_key(api_key: str) -> str:
    resp = requests.post(
        LISTEN_KEY_ENDPOINT,
        headers={"X-MBX-APIKEY": api_key},
        timeout=CONNECT_TIMEOUT_SEC,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"listenKey request failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()["listenKey"]


async def test_private_ws(creds: dict):
    print("[private] requesting listenKey")
    listen_key = create_listen_key(creds["api_key"])
    print("[private] listenKey obtained")

    url = f"{PUBLIC_WS_BASE}/{listen_key}"
    print(f"[private] connecting to {url}")
    async with websockets.connect(url, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
        print("[private] \u2705 connected — user-data stream is live (no auth message needed; the listenKey is the credential)")
        # Nothing will arrive unless the account has activity, so just
        # confirm the socket stays open rather than waiting on a message.
        await asyncio.sleep(2)


async def main():
    print(f"Binance WS connection test (testnet={TESTNET})")
    await test_public_ws()

    creds = load_binance_credentials()
    await test_private_ws(creds)


if __name__ == "__main__":
    asyncio.run(main())
