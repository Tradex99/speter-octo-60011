import httpx
import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import quote


#Config
LOGIN_URL = "https://m.betking.com/islands/_actions/login/"
USERNAME  = "09120183273"
PASSWORD  = "Edmond99@"

LOGIN_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng",
    "Origin":          "https://m.betking.com",
    "Dnt":             "1",
    "Sec-Gpc":         "1",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "Priority":        "u=0",
    "Te":              "trailers",
    "Cookie":          "ABTestHomePrematchNewApi=true; ABTestHomePrematchBoostedNewApi=true",
}


def _load_db_config():
    base   = os.path.dirname(os.path.abspath(__file__))
    config = {}
    with open(os.path.join(base, "db.txt"), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"')
    return config


def _get_supabase():
    from supabase import create_client
    config = _load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])


def _build_cookie(access_token: str, refresh_token: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
                f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    ts_encoded = quote(timestamp, safe="")

    return (
        "ABTestHomePrematchNewApi=true; "
        "ABTestHomePrematchBoostedNewApi=true; "
        f"accessToken={access_token}; "
        f"lastLoginTimeStamp={ts_encoded}; "
        f"refreshToken={refresh_token}; "
        "ABTestPrematchToLiveTransition=true; "
        "ABTestCrossSellRollout=true; "
        "ABTestNewBuildCoupon=false; "
        "ABTestMybetSettlementAwareRollout=C;"
"ABTestRebetBuildCoupon=true;"
"ajs_user_id=11779815;"
"ajs_anonymous_id=254f2702-09e1-4a95-9ac2-2cc68528cb15;"
"analytics_session_id=1781619216294;"
"analytics_session_id.last_access=1781619282533"
    )


async def login() -> bool:
    print("[login] sending login request ...")

    boundary = "----geckoformboundary837ee54a32476e6e6acfdfd4198332af"

    fields = [
        ("username",         USERNAME),
        ("password",         PASSWORD),
        ("anonymousId",      ""),
        ("locale",           "en-ng"),
        ("discriminationId", "19010103"),
        ("loginSource",      "login_button"),
        ("usernameType",     "text"),
    ]

    body_parts = []
    for name, value in fields:
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    body_parts.append(f"--{boundary}--\r\n")
    body = "".join(body_parts).encode()

    headers = {
        **LOGIN_HEADERS,
        "Content-Type":   f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    async with httpx.AsyncClient(http2=True, timeout=20.0) as client:
        resp = await client.post(LOGIN_URL, headers=headers, content=body)

    if resp.status_code != 200:
        print(f"[login] failed     : HTTP {resp.status_code}")
        return False

    try:
        data = resp.json()
    except Exception:
        print(f"[login] failed     : could not parse response")
        return False

    # Response is a JSON array, tokens are at fixed indices
    # index 3 = accessToken, index 5 = refreshToken
    try:
        access_token  = data[3]
        refresh_token = data[5]
    except (IndexError, KeyError):
        print(f"[login] failed     : tokens not found in response")
        return False

    if not access_token or not refresh_token:
        print(f"[login] failed     : empty tokens in response")
        return False

    cookie = _build_cookie(access_token, refresh_token)

    try:
        sb = _get_supabase()
        sb.table("betking").update({"cookie": cookie}).eq("id", 1).execute()
        print(f"[login] success    : cookie saved to DB")
    except Exception as e:
        print(f"[login] db error   : {e}")
        return False

    return True


if __name__ == "__main__":
    asyncio.run(login())
