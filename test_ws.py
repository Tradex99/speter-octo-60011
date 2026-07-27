"""
Standalone connection test for OKX's v5 WebSocket API.

Mirrors the DB-loading pattern used in tracker.py (db.txt -> Supabase
api_keys table) but targets a row where exchange_name == "okx".
Only exercises the connection itself:
  - public WS: connect + subscribe to one channel, confirm data arrives
  - private WS: connect + login, confirm the server accepts the signature

OKX credentials are a triplet: api_key, api_secret, AND passphrase
(the passphrase is set by you when the API key was created on OKX —
it's not something OKX generates, so make sure it's stored too).

Run with:  python okx_ws_test.py
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time

import websockets

# --- toggle this exactly like DEMO_TRADING in tracker.py ---
# OKX calls this "demo trading" (paper trading), not "testnet", and it
# uses a completely different host (wspap) rather than a URL flag.
DEMO_TRADING = False

PUBLIC_WS_URL = (
    "wss://wspap.okx.com:8443/ws/v5/public"
    if DEMO_TRADING
    else "wss://ws.okx.com:8443/ws/v5/public"
)
PRIVATE_WS_URL = (
    "wss://wspap.okx.com:8443/ws/v5/private"
    if DEMO_TRADING
    else "wss://ws.okx.com:8443/ws/v5/private"
)

# A harmless public channel just to prove data flows.
TEST_PUBLIC_CHANNEL = {"channel": "tickers", "instId": "BTC-USDT"}

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

    api_key = _clean_secret(match.get("api_key"))
    api_secret = _clean_secret(match.get("api_secret"))
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


async def test_public_ws():
    print(f"[public] connecting to {PUBLIC_WS_URL}")
    async with websockets.connect(PUBLIC_WS_URL, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
        print("[public] connected — subscribing to", TEST_PUBLIC_CHANNEL)
        await ws.send(json.dumps({"op": "subscribe", "args": [TEST_PUBLIC_CHANNEL]}))

        saw_ack = False
        saw_data = False
        deadline = time.time() + RESPONSE_WAIT_SEC
        while time.time() < deadline and not (saw_ack and saw_data):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("event") == "subscribe":
                saw_ack = True
                print(f"[public] subscribe ack: {msg}")
            elif msg.get("event") == "error":
                print(f"[public] subscribe error: {msg}")
                break
            elif "data" in msg and msg.get("arg", {}).get("channel") == TEST_PUBLIC_CHANNEL["channel"]:
                saw_data = True
                print("[public] received channel data — connection confirmed")

        if not saw_ack:
            print("[public] WARNING: no subscribe ack received within timeout")
        if not saw_data:
            print("[public] WARNING: no market data received within timeout")


async def test_private_ws(creds: dict):
    print(f"[private] connecting to {PRIVATE_WS_URL}")
    async with websockets.connect(PRIVATE_WS_URL, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
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
    print(f"OKX WS connection test (demo_trading={DEMO_TRADING})")
    await test_public_ws()

    creds = load_okx_credentials()
    await test_private_ws(creds)


if __name__ == "__main__":
    asyncio.run(main())
