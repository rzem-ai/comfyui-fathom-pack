"""The config Fathom's Backend tab gives to paste into ComfyUI's settings, under Fathom > Pack config (FTH-146.4).

The setting `Fathom.PackConfig` holds JSON: `{"fathom_pack_config": 1, "backend", "fingerprint", "nonce", "ciphertext"}`. The ciphertext is AES-256-GCM over the UTF-8 JSON plaintext under a key per Backend that only Fathom holds, with AAD `fathom-pack-config:1:<backend>`; the fingerprint is the first 16 bytes, in hex, of HMAC-SHA256 over the plaintext with the same key. The plaintext names the proxies a fetch may go through (`proxies`, each `type` http, https or socks5 with `host`, `port` and optional `username` and `password`), NordVPN's service credentials (`nordvpn`) and the source keys (`keys`: `civitai`, `huggingface`, `fal`, `replicate`).

ComfyUI keeps the setting in `<user dir>/default/comfy.settings.json`, which the pack reads each time it needs the config, never from a request. Its user directory is the one ComfyUI reports. Opened values are returned to the one caller and never kept here, logged or answered.

The key comes in the `X-Fathom-Config-Key` header, base64url, 32 bytes: with each fetch, and on `POST /fathom/v1/config-key`, which holds it in memory until ComfyUI restarts so the fal and Replicate nodes can open their keys (`keys.py`).
"""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re

from aiohttp import web

from .disk_routes import PREFIX, Refused, check_token

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # A ComfyUI without the pack's requirements: no config can be opened, and the fetch routes say so.
    AESGCM = None
    InvalidTag = None

SETTING = "Fathom.PackConfig"
SETTINGS_FILE = os.path.join("default", "comfy.settings.json")
FORMAT = 1
AAD_PREFIX = "fathom-pack-config:1:"
KEY_HEADER = "X-Fathom-Config-Key"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
FINGERPRINT = re.compile(r"^[0-9a-f]{32}$")
KEY_TEXT = re.compile(r"^[A-Za-z0-9_-]{43}=?$")
BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
BACKEND = re.compile(r"^[\x21-\x7e]{1,200}$")
# A key or a credential is visible ASCII with no spaces, as Fathom's own fetch holds a key to.
TOKEN = re.compile(r"^[\x21-\x7e]+$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
PROXY_TYPES = ("http", "https", "socks5")
KEY_NAMES = ("civitai", "huggingface", "fal", "replicate")

MISSING = "this Backend has no Fathom pack config: paste it from the Backend's Pack config tab in Fathom into ComfyUI's Settings, Fathom, Pack config"
INVALID = "this Backend's Fathom pack config is not one Fathom made: paste it again from the Backend's Pack config tab in Fathom"
WRONG_KEY = "the config key Fathom sent does not open this Backend's pack config, which is another Backend's or out of date: paste it again from the Backend's Pack config tab in Fathom"


class ConfigRefused(Refused):
    """The config could not be had or opened. `sentence` never holds the key or anything the config holds."""


class Missing(ConfigRefused):
    def __init__(self):
        super().__init__(409, MISSING, "CONFIG_MISSING")


class Invalid(ConfigRefused):
    def __init__(self, sentence=INVALID):
        super().__init__(409, sentence, "CONFIG_INVALID")


class WrongKey(ConfigRefused):
    def __init__(self):
        super().__init__(403, WRONG_KEY, "CONFIG_KEY")


def settings_file():
    """ComfyUI's settings file for its default user, under the user directory ComfyUI reports, or None outside ComfyUI."""
    try:
        import folder_paths

        user = folder_paths.get_user_directory()
    except Exception:  # noqa: BLE001  Not inside ComfyUI, or one with no user directory: there is no settings file.
        return None
    if not isinstance(user, str) or not user:
        return None
    return os.path.join(user, SETTINGS_FILE)


def _decoded(text, length=None):
    if not isinstance(text, str) or not BASE64.fullmatch(text):
        raise Invalid()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise Invalid() from None
    if length is not None and len(raw) != length:
        raise Invalid()
    return raw


def stored():
    """The pasted config as it is stored, checked for its form: `{"backend", "fingerprint", "nonce", "ciphertext"}`. Raises `Missing` or `Invalid`."""
    path = settings_file()
    if path is None:
        raise Missing()
    try:
        with open(path, encoding="utf-8") as handle:
            settings = json.load(handle)
    except FileNotFoundError:
        raise Missing() from None
    except (OSError, ValueError):
        # ComfyUI's own file, unreadable or not JSON: no config can be read from it.
        raise Missing() from None
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise Missing()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise Invalid() from None
    if not isinstance(value, dict):
        raise Invalid()
    version, backend, fingerprint = value.get("fathom_pack_config"), value.get("backend"), value.get("fingerprint")
    if type(version) is not int or version != FORMAT or not isinstance(backend, str) or not BACKEND.fullmatch(backend):
        raise Invalid()
    if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
        raise Invalid()
    nonce = _decoded(value.get("nonce"), NONCE_BYTES)
    ciphertext = _decoded(value.get("ciphertext"))
    if len(ciphertext) <= TAG_BYTES:
        raise Invalid()
    return {"backend": backend, "fingerprint": fingerprint, "nonce": nonce, "ciphertext": ciphertext}


def state():
    """What the version route says of the config: `{"fingerprint"}` when one of the right form is pasted, else `{"state": "missing"}` or `{"state": "invalid"}`."""
    try:
        return {"fingerprint": stored()["fingerprint"]}
    except Missing:
        return {"state": "missing"}
    except Invalid:
        return {"state": "invalid"}


def parse_key(text):
    """The 32-byte key from its base64url header value. Raises `Refused` naming none of it."""
    if not isinstance(text, str) or not KEY_TEXT.fullmatch(text):
        raise Refused(422, f"the {KEY_HEADER} header is not a 32-byte key in base64url", "BAD_CONFIG_KEY")
    raw = base64.urlsafe_b64decode(text.rstrip("=") + "=")
    if len(raw) != KEY_BYTES:
        raise Refused(422, f"the {KEY_HEADER} header is not a 32-byte key in base64url", "BAD_CONFIG_KEY")
    return raw


def key_from(request):
    """The key the request carries, or None when it carries none."""
    sent = request.headers.get(KEY_HEADER)
    return None if sent is None else parse_key(sent)


def _credential(value):
    return value is None or (isinstance(value, str) and TOKEN.fullmatch(value) is not None)


def _host(value):
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return HOSTNAME.fullmatch(value) is not None


def _checked(plain):
    """The plaintext as the config's form holds it. Raises `Invalid`."""
    if not isinstance(plain, dict):
        raise Invalid()
    proxies = plain.get("proxies", {})
    if not isinstance(proxies, dict):
        raise Invalid()
    for name, proxy in proxies.items():
        if not isinstance(name, str) or not name or not isinstance(proxy, dict) or proxy.get("type") not in PROXY_TYPES or not _host(proxy.get("host")):
            raise Invalid()
        port = proxy.get("port")
        if type(port) is not int or not 0 < port < 65536 or not _credential(proxy.get("username")) or not _credential(proxy.get("password")):
            raise Invalid()
    nordvpn = plain.get("nordvpn")
    if nordvpn is not None and (not isinstance(nordvpn, dict) or not all(isinstance(nordvpn.get(field), str) and TOKEN.fullmatch(nordvpn[field]) for field in ("username", "password"))):
        raise Invalid()
    keys = plain.get("keys", {})
    if not isinstance(keys, dict) or not all(_credential(keys.get(name)) for name in KEY_NAMES):
        raise Invalid()
    return {"proxies": proxies, "nordvpn": nordvpn, "keys": {name: keys[name] for name in KEY_NAMES if keys.get(name)}}


def open_config(key):
    """The pasted config, opened with `key` for the one caller: `{"proxies", "nordvpn", "keys"}`. Raises `Missing`, `Invalid` or `WrongKey`."""
    if AESGCM is None:
        raise Refused(503, "this ComfyUI has no cryptography package, so the pack cannot open its config", "NO_CRYPTOGRAPHY")
    sealed = stored()
    try:
        plaintext = AESGCM(key).decrypt(sealed["nonce"], sealed["ciphertext"], (AAD_PREFIX + sealed["backend"]).encode("utf-8"))
    except InvalidTag:
        raise WrongKey() from None
    fingerprint = hmac.new(key, plaintext, hashlib.sha256).digest()[:16].hex()
    if not hmac.compare_digest(fingerprint, sealed["fingerprint"]):
        raise Invalid("this Backend's Fathom pack config carries a fingerprint that does not match what it holds: paste it again from the Backend's Pack config tab in Fathom")
    try:
        plain = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Invalid() from None
    return _checked(plain)


# The key `config-key` was last given, held in memory alone until ComfyUI restarts, for the fal and Replicate nodes.
_HELD = {"key": None}


def held_key():
    return _HELD["key"]


async def _config_key(request):
    """Holds the key the request carries, once it opens the pasted config or there is none pasted yet."""
    key = key_from(request)
    if key is None:
        raise Refused(422, f"the request carries no {KEY_HEADER} header", "NO_CONFIG_KEY")
    try:
        open_config(key)
        answer = {"held": True, "config": state()}
    except Missing:
        answer = {"held": True, "config": {"state": "missing"}}
    _HELD["key"] = key
    return web.json_response(answer)


def register(routes, pack_dir):
    """Adds `POST /fathom/v1/config-key`, behind the token, the token checked first."""

    async def route(request):
        try:
            check_token(request, pack_dir)
            return await _config_key(request)
        except Refused as refused:
            return refused.answer()
        except Exception:  # noqa: BLE001  Anything else answers alike, so no traceback goes back to the caller.
            return web.json_response({"error": "the pack could not answer", "code": "EIO"}, status=500)

    routes.post(f"{PREFIX}/config-key")(route)
