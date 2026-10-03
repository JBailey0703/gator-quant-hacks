"""Check Webull authentication without fetching prices or placing orders.

Run from the project directory:
    uv run python examples/backtest/check_webull_auth.py

Never prints credentials, signatures, tokens, or full request/response bodies.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import dotenv_values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint", choices=("api.webull.com", "api.sandbox.webull.com"),
        help="Test an endpoint without changing .env.",
    )
    args = parser.parse_args()
    env_path = Path(__file__).with_name(".env")
    file_values = dotenv_values(env_path)
    settings = dict(file_values)
    settings.update({k: v for k, v in os.environ.items() if k.startswith("WEBULL_")})
    key = settings.get("WEBULL_APP_KEY", "")
    secret = settings.get("WEBULL_APP_SECRET", "")
    host = args.endpoint or settings.get("WEBULL_API_ENDPOINT", "api.webull.com")
    if host not in ("api.webull.com", "api.sandbox.webull.com"):
        print("Use api.webull.com or api.sandbox.webull.com for this US diagnostic.")
        return 1
    if not key or not secret:
        print("Missing WEBULL_APP_KEY or WEBULL_APP_SECRET in backtest/.env or environment.")
        return 1
    if key.startswith("your_") or secret.startswith("your_"):
        print("Replace placeholder credentials in backtest/.env.")
        return 1
    print(f"Endpoint: {host}")
    for name in ("WEBULL_APP_KEY", "WEBULL_APP_SECRET"):
        if name in os.environ and os.environ[name] != file_values.get(name):
            print(f"WARNING: PowerShell environment overrides .env for {name}.")

    path = "/openapi/config"
    successes = []
    for label, digest in (("HMAC-SHA256", hashlib.sha256), ("HMAC-SHA1", hashlib.sha1)):
        headers = {
            "x-app-key": key,
            "x-timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "x-signature-algorithm": label,
            "x-signature-version": "1.0",
            "x-signature-nonce": uuid.uuid4().hex,
        }
        signing_fields = {**headers, "host": host}
        signing_text = path + "&" + "&".join(
            f"{name}={signing_fields[name]}" for name in sorted(signing_fields)
        )
        encoded = quote(signing_text, safe="").encode("utf-8")
        headers["x-signature"] = base64.b64encode(
            hmac.new((secret + "&").encode("utf-8"), encoded, digest).digest()
        ).decode("ascii")
        headers.update({"x-version": "v3", "Accept": "application/json"})
        request = Request(f"https://{host}{path}", headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                print(f"{label}: HTTP {response.status} (authentication accepted)")
                successes.append(label)
        except HTTPError as exc:
            # Print only error details, never full bodies or signed requests.
            try:
                payload = json.loads(exc.read())
                code = payload.get("error_code", "UNKNOWN")
                message = payload.get("message", "")
            except (ValueError, AttributeError):
                code = "UNKNOWN"
                message = ""
            if not isinstance(code, str) or not code.replace("_", "").isalnum():
                code = "UNKNOWN"
            print(f"{label}: HTTP {exc.code}, error code {code}")
            if isinstance(message, str) and message:
                message = message.replace(key, "[redacted]").replace(secret, "[redacted]")
                print("  " + " ".join(message.split())[:200])
        except (URLError, TimeoutError):
            print(f"{label}: connection failed; authentication result unavailable")

    if successes == ["HMAC-SHA1"]:
        print("RESULT: SHA1 works; the installed SDK's forced SHA256 needs a compatibility fix.")
    elif "HMAC-SHA256" in successes:
        print("RESULT: SDK signing method accepted. Retry the backtest in this same environment.")
    else:
        print("RESULT: Neither method succeeded. Confirm the key/secret pair and prod/sandbox account.")
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
