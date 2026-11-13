"""Bambu Cloud client: only the calls spool-sync needs.

Unofficial API (reverse-engineered by the community, see README "Sources"); it can change without notice.
"""
import json
import time
from pathlib import Path

from curl_cffi import requests as http  # browser TLS fingerprint, Cloudflare in front of the API

API = "https://api.bambulab.com"
TASKS = "/v1/user-service/my/tasks"
FILAMENTS = "/v1/design-user-service/my/filament/v2"  # Filament Manager: GET = list, PUT full object = update
DEFAULT_LIFETIME = 90 * 86400  # login returns expiresIn=7776000; refresh endpoint is dead


class AuthError(Exception):
    """401: the token is expired or revoked, a new login is needed."""


class ApiError(Exception):
    """Anything else that went wrong talking to the cloud."""


def _call(method, path, token=None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = http.request(method, API + path, headers=headers, timeout=30, impersonate="chrome", **kw)
    except Exception as e:  # network, TLS, timeout
        raise ApiError(f"{method} {path}: {type(e).__name__}: {e}") from e
    if r.status_code == 401:
        raise AuthError(f"{method} {path}: HTTP 401")
    return r


class Bambu:
    def __init__(self, token):
        self.token = token

    def _json(self, method, path, **kw):
        r = _call(method, path, self.token, **kw)
        if r.status_code != 200:
            raise ApiError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def tasks(self, limit=20):
        return self._json("GET", TASKS, params={"limit": limit}).get("hits", [])

    def spools(self):
        return self._json("GET", FILAMENTS).get("hits", [])

    def uid(self):
        """Numeric user id, the cloud MQTT username is u_{uid} (no longer inside the token)."""
        return self._json("GET", "/v1/design-user-service/my/preference")["uid"]

    def device_serials(self):
        return [d["dev_id"] for d in self._json("GET", "/v1/iot-service/api/user/bind").get("devices", [])]

    def put_spool(self, spool):
        """Store spool['netWeight'] and return the spool as the cloud has it afterwards.

        The PUT reply only echoes the body, and a spool flagged depleted silently ignores a new weight,
        so the flag is cleared for a non-zero weight and the result is verified by reading back.
        """
        spool = dict(spool)
        if spool["netWeight"] > 0:
            spool["depleted"] = False
        self._json("PUT", FILAMENTS, json=spool)
        back = next((s for s in self.spools() if s["id"] == spool["id"]), None)
        if not back or back.get("netWeight") != spool["netWeight"]:
            got = back.get("netWeight") if back else "no such spool"
            raise ApiError(f"PUT spool {spool['id']} accepted but not stored: cloud has {got}")
        return back


# ---- token file -------------------------------------------------------------------------------------

def load_token(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_token(path: Path, access_token, expires_in=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "accessToken": access_token,
        "savedAt": int(time.time()),
        "expiresIn": int(expires_in or DEFAULT_LIFETIME),
    }))


def token_days_left(tok):
    return (tok["savedAt"] + tok.get("expiresIn", DEFAULT_LIFETIME) - time.time()) / 86400


# ---- interactive login ------------------------------------------------------------------------------

def login(email, password, ask):
    """Return (accessToken, expiresIn). `ask(prompt)` reads a code from the user when Bambu wants one."""
    r = _call("POST", "/v1/user-service/user/login", json={"account": email, "password": password, "apiError": ""})
    j = r.json() if r.text else {}
    if j.get("accessToken"):
        return j["accessToken"], j.get("expiresIn")
    kind = j.get("loginType")
    if kind == "verifyCode":
        r = _call("POST", "/v1/user-service/user/sendemail/code", json={"email": email, "type": "codeLogin"})
        if r.status_code != 200:  # success is 200 with an empty body
            raise ApiError(f"sending the email code failed: HTTP {r.status_code} {r.text[:200]}")
        code = ask("Bambu emailed you a code, enter it: ")
        j = _call("POST", "/v1/user-service/user/login", json={"account": email, "code": code}).json()
        if j.get("accessToken"):
            return j["accessToken"], j.get("expiresIn")
    elif kind == "tfa":
        code = ask("2FA code from your authenticator: ")
        r = http.post("https://bambulab.com/api/sign-in/tfa", json={"tfaKey": j.get("tfaKey"), "tfaCode": code},
                      timeout=30, impersonate="chrome")
        if r.cookies.get("token"):
            return r.cookies.get("token"), None
        j = {"status": r.status_code, "body": r.text[:200]}
    raise ApiError(f"login failed: {json.dumps(j, ensure_ascii=False)[:300]}")
