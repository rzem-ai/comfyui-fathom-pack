"""A fetch from CivitAI or Hugging Face straight to this Backend's disk, so a Download's bytes never cross Fathom's machine (FTH-65.3).

Two POST routes behind the same token as every other, checked first:

- `fetch` takes one URL, the target path, the expected size and published hash, and the route: `none`, `nordvpn` with an `exit` and a `port`, or the name of a proxy in the pasted Fathom pack config (`pack_config`). The config's key comes in the `X-Fathom-Config-Key` header, needed for any route but `none`; the source's key and the proxy's address and credentials come from the config, opened for this fetch alone. It answers lines of JSON as it goes: `{"bytes": n}` at least once a second, `{"arrived": n}` once the last byte is on disk, then `{"verified": true, "blake3", "sha256", "bytes", "commit"}` once the file matches its published hash, or `{"error", "code"}`.
- `fetch-commit` gives the verified file its name, on Fathom's word, so a Download cancelled before then never leaves a file under the name.

Where a fetch may go: https only, on the hosts Fathom's own fetch allows for the source (`HOSTS`, Fathom's `sourceHosts`), never to a name that resolves to an address that is not public (loopback, private, link-local, shared, reserved, or one that carries or translates to an IPv4 address, `NOT_PUBLIC`), and each redirect followed by hand under the same rules, at most `MAX_REDIRECTS`. Through a proxy, the proxy resolves the source's name, so only a NordVPN exit's own address and an address written in the URL are checked here; a proxy the config names is trusted at whatever address it names, a LAN one included, as Alex decided ("the pack trusts all proxy addresses"). An HTTP or HTTPS proxy is asked to CONNECT to the name, and a SOCKS5 proxy is given the name to resolve (remote DNS), so the pack never resolves the source's name itself. The key goes on the first request alone, and only when its host is the source's key host (`KEY_HOSTS`, Fathom's `sourceKeyHosts`); it never follows a redirect, even to the same host, as Fathom's own fetch never lets it. A proxy is only one the config names, HTTP, HTTPS or SOCKS5 through `aiohttp-socks`, or a NordVPN HTTPS proxy on port 89 or 90 with the config's NordVPN credentials. A 429 or 5xx is retried as Fathom's own fetch retries it (`ATTEMPTS`), the key going on a retry only where it went on the first try.

What a fetch writes: one new file, its path held to the write routes' rules in `pack_writes`, under a hidden `.fathom-write-<hex>` name beside the target until it is committed, never replacing a name; never more than the expected size and a tenth, as Fathom's own staging allows; and only where that much is free. On any failure, on a cancel (Fathom closing the stream) or with no commit within `COMMIT_SECONDS`, the hidden file is removed. At most `FETCHES_MAX` fetches run at once.

The config's key, the source's key and the proxy settings live only in the one request's handler: never logged, written, answered or kept in this module once the fetch ends.
"""

import asyncio
import datetime
import email.utils
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import secrets
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
import yarl
from aiohttp import web

try:
    import aiohttp_socks
    import python_socks
except ImportError:  # A ComfyUI without the pack's requirements: the fetch routes say so.
    aiohttp_socks = python_socks = None

from . import disk_routes, pack_config
from .disk_routes import PREFIX, Refused, _off_loop, check_token
from .pack_writes import NEW_FILE_MODE, _begin_write, _body, _drop_temp, _entry, _finish_write, _give_name, _write_all, connected

# The hosts each source's fetch may reach, and the one host its key may go to: Fathom's `sourceHosts` and
# `sourceKeyHosts` in `app/server/src/sources/http.ts`, which `app/server/test/pack-fetch.spec.ts` holds these to.
HOSTS = {
    "civitai": (
        re.compile(r"^(?:[a-z0-9-]+\.)*civitai\.(?:com|red)$"),
        re.compile(r"^civitai-[a-z0-9-]+\.5ac0637cfd0766c97916cefa3764fbdf\.r2\.cloudflarestorage\.com$"),
    ),
    "huggingface": (re.compile(r"^(?:[a-z0-9-]+\.)*huggingface\.co$"), re.compile(r"^(?:[a-z0-9-]+\.)*hf\.co$")),
}
KEY_HOSTS = {"civitai": ("civitai.com",), "huggingface": ("huggingface.co",)}
SOURCE_NAMES = {"civitai": "CivitAI", "huggingface": "Hugging Face"}
SCHEME = "https"
# NordVPN's HTTPS CONNECT proxies, as Fathom's tunnel dials them: 89 plain, 90 with Threat Protection.
PROXY_HOST = re.compile(r"^(?:[a-z0-9-]+\.)+nordvpn\.com$")
PROXY_PORTS = (89, 90)
PROXY_SCHEME = "https"
NONE = "none"
NORDVPN = "nordvpn"
# A route name the refusal may repeat: anything else is refused without it.
ROUTE_SHOWN = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")

MAX_REDIRECTS = 5
# A 429 or 5xx is retried as Fathom's `withRetries` does: this many attempts, the first included; Retry-After honoured up
# to `RETRY_AFTER_CAP_MS`, else a jittered wait under `BACKOFF_BASE_MS` doubling to `BACKOFF_CAP_MS`.
ATTEMPTS = 5
BACKOFF_BASE_MS = 1_000
BACKOFF_CAP_MS = 30_000
RETRY_AFTER_CAP_MS = 60_000
FETCHES_MAX = 4
CHUNK = 1024 * 1024
PROGRESS_SECONDS = 1.0
CONNECT_SECONDS = 10
# How long the source may send nothing, headers or body, before the fetch is given up.
STALL_SECONDS = 60
COMMIT_SECONDS = 60
USER_AGENT = "Fathom (self-hosted; model management)"
URL_CHARACTERS = re.compile(r"^[\x21-\x7e]+$")
HEX = re.compile(r"^[0-9a-f]{64}$")


class Failed(Exception):
    """A fetch that stopped, as the line Fathom shows. `sentence` never holds the key, the proxy settings or a URL's query."""

    def __init__(self, sentence, code, **extra):
        super().__init__(sentence)
        self.sentence = sentence
        self.code = code
        self.extra = extra


class _Pending:
    """A verified file waiting for Fathom's commit: where it is, and whether the commit has taken it."""

    def __init__(self, folders, fd, temp, name):
        self.folders = folders
        self.fd = fd
        self.temp = temp
        self.name = name
        self.claimed = False
        self.done = asyncio.get_running_loop().create_future()


# The verified files waiting for a commit, by commit id, and how many fetches are running. Neither holds a key.
_PENDING = {}
_RUNNING = {"fetches": 0}


def _name(source):
    return SOURCE_NAMES.get(source, source)


def _described(url):
    """Origin and path only: a signed URL's query is a credential of its own."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{parts.path}"


def check_url(source, url):
    """The URL as one the fetch may reach, as Fathom's `checkedSourceUrl` holds it: https, no user or password, on the source's hosts."""
    name = _name(source)
    refused = Failed(f"{name} fetches may only go to {name}'s own hosts over https; {_described(url) if isinstance(url, str) else 'that'} was refused, so nothing was sent to it.", "HOST")
    if not isinstance(url, str) or not URL_CHARACTERS.fullmatch(url):
        raise refused
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        raise refused from None
    host = parts.hostname or ""
    if parts.scheme.lower() != SCHEME or parts.username is not None or parts.password is not None or not any(rule.fullmatch(host) for rule in HOSTS.get(source, ())):
        raise refused
    # An address written as one is never resolved, so it is held to the same rule here.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return url
    if not _public(host):
        raise Failed(f"{host} is an address that is not public, so nothing was sent to it.", "PRIVATE")
    return url


# Refused whatever a Python's `is_global` says of them: the ranges that carry or translate to an IPv4 address, which
# could reach one inside, and special ranges some Pythons count as global.
NOT_PUBLIC = tuple(
    ipaddress.ip_network(network)
    for network in (
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # NAT64 for local use
        "::/96",  # IPv4-compatible
        "::ffff:0:0:0/96",  # IPv4-translated
        "2002::/16",  # 6to4
        "2001::/32",  # Teredo
        "192.88.99.0/24",  # the 6to4 relay
        "fec0::/10",  # site-local
        "5f00::/16",  # SRv6
        "3fff::/20",  # documentation
        "2001:db8::/32",  # documentation
        "100::/64",  # discard
    )
)


def _public(address):
    """Whether an address is one on the public internet: never loopback, private, link-local, shared, reserved or translated."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not any(ip in network for network in NOT_PUBLIC if network.version == ip.version)


class NotPublic(OSError):
    pass


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """Resolves as aiohttp does, refusing a name any of whose addresses is not public, so a fetch never reaches inside."""

    def __init__(self):
        self._inner = aiohttp.ThreadedResolver()
        self.refused = None

    async def resolve(self, host, port=0, family=socket.AF_INET):
        found = await self._inner.resolve(host, port, family)
        if not found or not all(_public(entry["host"]) for entry in found):
            self.refused = host
            raise NotPublic(f"{host} is not public")
        return found

    async def close(self):
        await self._inner.close()


def _shown(route):
    """The route's name to repeat in a refusal, where it is plain enough to, else nothing."""
    return f" {route}" if isinstance(route, str) and ROUTE_SHOWN.fullmatch(route) else ""


def _route_of(body):
    """The route asked for, taken out of the body: `("none",)`, `("nordvpn", exit, port)` or `("proxy", name)`."""
    route, exit_host, port = body.pop("route", None), body.pop("exit", None), body.pop("port", None)
    if not isinstance(route, str) or not route:
        raise Refused(422, "the route is none, nordvpn or the name of a proxy in this Backend's pack config", "ROUTE")
    if route == NONE:
        return (NONE,)
    if route == NORDVPN:
        if not isinstance(exit_host, str) or not PROXY_HOST.fullmatch(exit_host) or type(port) is not int or port not in PROXY_PORTS:
            raise Refused(422, "a nordvpn route names its exit, a NordVPN proxy host, and its port, 89 or 90")
        return (NORDVPN, exit_host, port)
    return ("proxy", route)


def _proxy_for(route, config):
    """The proxy a route goes through, from the opened config alone, or None for `none`. Raises naming no setting."""
    if route[0] == NONE:
        return None
    if route[0] == NORDVPN:
        nordvpn = config["nordvpn"]
        if nordvpn is None:
            raise Refused(409, "this Backend's pack config holds no NordVPN credentials, so the nordvpn route cannot be taken", "ROUTE")
        return {"name": "the NordVPN proxy", "public_only": True, "type": PROXY_SCHEME, "host": route[1], "port": route[2], "username": nordvpn["username"], "password": nordvpn["password"]}
    proxy = config["proxies"].get(route[1])
    if proxy is None:
        raise Refused(422, f"the route{_shown(route[1])} is not a proxy in this Backend's pack config, so nothing was fetched", "ROUTE")
    return {"name": f"the proxy{_shown(route[1])}", "public_only": False, "type": proxy["type"], "host": proxy["host"], "port": proxy["port"], "username": proxy.get("username"), "password": proxy.get("password")}


def _asked(body):
    """What one fetch was asked for. A body that still carries a key or a proxy, as a fetch before FETCH_VERSION 2 did, is refused."""
    if body.pop("key", None) is not None or body.pop("proxy", None) is not None:
        raise Refused(422, "a fetch carries no key or proxy: they come from this Backend's pack config, named by the route")
    route = _route_of(body)
    source = body.get("source")
    if source not in HOSTS:
        raise Refused(422, "the source is civitai or huggingface")
    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise Refused(422, "size is a whole number")
    published = body.get("published")
    if not isinstance(published, dict) or published.get("algorithm") not in ("blake3", "sha256") or not isinstance(published.get("value"), str) or not HEX.fullmatch(published["value"].lower()):
        raise Refused(422, "published is the file's blake3 or sha256")
    return source, body.get("url"), body.get("path"), size, {"algorithm": published["algorithm"], "value": published["value"].lower()}, route


async def _opened(request, source, route):
    """The source's key and the proxy for this fetch, from the config opened with the key the request carries. Needs the key for any route but none; with none and no key, neither."""
    config_key = pack_config.key_from(request)
    if config_key is None:
        if route[0] != NONE:
            raise Refused(422, f"the route needs this Backend's config key in the {pack_config.KEY_HEADER} header, and the request carries none", "NO_CONFIG_KEY")
        return None, None
    try:
        config = await _off_loop(pack_config.open_config, config_key)
    except pack_config.Missing:
        # The none route needs no config: with none pasted it goes as before, with no source key. A wrong key or an invalid config still refuses.
        if route[0] == NONE:
            return None, None
        raise
    return config["keys"].get(source), _proxy_for(route, config)


def _cap(size):
    """The most a fetch may write: the size and a tenth, Fathom's own `spaceNeeded`."""
    return size + (size + 9) // 10


def _free(folder):
    space = os.statvfs(folder)
    return space.f_bavail * space.f_frsize


def _keep(file_fd, chunk, hashers):
    _write_all(file_fd, chunk)
    for hasher in hashers:
        hasher.update(chunk)


async def _line(response, value):
    await response.write(json.dumps(value).encode("utf-8") + b"\n")


class _Progress:
    def __init__(self):
        self.bytes = 0


def _retryable(status):
    return status == 429 or 500 <= status <= 599


def _retry_after_ms(header, now):
    """Seconds or an HTTP date, as milliseconds from now, or None: Fathom's `retryAfterMs`."""
    if header is None or header.strip() == "":
        return None
    try:
        seconds = float(header)
    except ValueError:
        seconds = math.nan
    if math.isfinite(seconds):
        return seconds * 1000 if seconds >= 0 else None
    try:
        at = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, at.timestamp() * 1000 - now)


def _backoff_ms(attempt, retry_after):
    """How long to wait before the next attempt: Fathom's `backoff`."""
    if retry_after is not None:
        return min(retry_after, RETRY_AFTER_CAP_MS)
    ceiling = min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_CAP_MS)
    return round(_random() * ceiling)


# Seams for the tests, which wait no real time.
_sleep = asyncio.sleep
_random = random.random


def _proxy_url(proxy, host):
    """The proxy as `aiohttp-socks` takes it, at `host`: an HTTPS proxy is an HTTP one spoken inside TLS to it."""
    scheme = "socks5" if proxy["type"] == "socks5" else "http"
    at = f"[{host}]" if ":" in host else host
    auth = ""
    if proxy["username"] is not None:
        auth = quote(proxy["username"], safe="") + (":" + quote(proxy["password"], safe="") if proxy["password"] is not None else "") + "@"
    return f"{scheme}://{auth}{at}:{proxy['port']}"


def _connector(proxy, host, resolver):
    """Straight out through the public-address resolver, or through the proxy at `host`, which resolves the source's name itself."""
    if proxy is None:
        return aiohttp.TCPConnector(resolver=resolver, force_close=True)
    extra = {"rdns": True} if proxy["type"] == "socks5" else {}
    if proxy["type"] == "https":
        extra["proxy_ssl"] = ssl.create_default_context()
    return aiohttp_socks.ProxyConnector.from_url(_proxy_url(proxy, host), force_close=True, **extra)


async def _check_proxy(proxy, name):
    """Where to reach a NordVPN exit, its own address held to the public-address rule, as aiohttp's resolver held it before
    `aiohttp-socks`, which resolves no name through it. An HTTP proxy, as the tests make the exit, is reached at the address
    checked; an HTTPS one by its name, which its TLS certificate is checked against, so its name is resolved again as it is
    dialled."""
    host = proxy["host"]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        resolver = _PublicResolver()
        try:
            found = await resolver.resolve(host, proxy["port"])
        except NotPublic:
            raise Failed(f"{proxy['name']} resolves to an address that is not public, so nothing was sent to it.", "PRIVATE") from None
        except OSError:
            raise Failed(f"{proxy['name']} could not be reached for the fetch from {name}, so nothing was fetched", "PROXY") from None
        finally:
            await resolver.close()
        return host if proxy["type"] == "https" else found[0]["host"]
    if not _public(host):
        raise Failed(f"{proxy['name']} is at an address that is not public, so nothing was sent to it.", "PRIVATE")
    return host


async def _download(source, url, size, key, proxy, file_fd, hashers, progress):
    """Fetches `url` into the open file, through each redirect under the same rules, retrying a 429 or 5xx as Fathom does. Raises `Failed`."""
    name = _name(source)
    cap = _cap(size)
    at = check_url(source, url)
    # A proxy the config names is reached as it names it; only a NordVPN exit, named by the request, is held to the public-address rule.
    proxy_host = None if proxy is None else await _check_proxy(proxy, name) if proxy["public_only"] else proxy["host"]
    resolver = _PublicResolver()
    connector = _connector(proxy, proxy_host, resolver)
    timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=CONNECT_SECONDS, sock_read=STALL_SECONDS)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, auto_decompress=False, trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
            hop = 0
            attempt = 1
            while True:
                headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
                # Only the first request and its retries, and only to the key host: the key never follows a redirect.
                if hop == 0 and key is not None and urlsplit(at).hostname in KEY_HOSTS[source]:
                    headers["Authorization"] = f"Bearer {key}"
                try:
                    async with session.get(
                        yarl.URL(at, encoded=True),
                        headers=headers,
                        allow_redirects=False,
                    ) as answer:
                        if 300 <= answer.status < 400 and answer.status != 304:
                            location = answer.headers.get("Location")
                            if location is None:
                                raise Failed(f"{name} answered {answer.status} to GET {_described(at)} with nowhere to go.", "SOURCE_HTTP", status=answer.status, url=_described(at))
                            if hop == MAX_REDIRECTS:
                                raise Failed(f"{name} redirected more than {MAX_REDIRECTS} times from {_described(url)}.", "REDIRECTS")
                            at = check_url(source, urljoin(at, location))
                            hop += 1
                            attempt = 1
                            continue
                        if answer.status != 200:
                            if not _retryable(answer.status) or attempt >= ATTEMPTS:
                                times = f" {attempt} times in a row" if _retryable(answer.status) and attempt > 1 else ""
                                raise Failed(f"{name} answered {answer.status}{times} to GET {_described(at)}", "SOURCE_HTTP", status=answer.status, url=_described(at))
                            # Nothing of this answer is written, so the next attempt starts on an empty file and fresh hashes.
                            wait = _backoff_ms(attempt, _retry_after_ms(answer.headers.get("Retry-After"), time.time() * 1000))
                        else:
                            if answer.content_length is not None and answer.content_length > cap:
                                raise Failed(f"the source sent more than the {size} bytes it declared, so the download was stopped", "TOO_LARGE")
                            async for chunk in answer.content.iter_chunked(CHUNK):
                                if progress.bytes + len(chunk) > cap:
                                    raise Failed(f"the source sent more than the {size} bytes it declared, so the download was stopped", "TOO_LARGE")
                                await _off_loop(_keep, file_fd, chunk, hashers)
                                progress.bytes += len(chunk)
                            return
                except Failed:
                    raise
                except (aiohttp_socks.ProxyConnectionError, aiohttp_socks.ProxyError, aiohttp_socks.ProxyTimeoutError) as error:
                    # The library's own message names the proxy's address; only its reply code is repeated.
                    status = getattr(error, "error_code", None)
                    said = f" (it answered {status})" if isinstance(status, int) else ""
                    raise Failed(f"{proxy['name'] if proxy else 'the proxy'} could not carry the fetch from {name}{said}, so nothing more was fetched", "PROXY") from None
                except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
                    raise Failed(f"{name} sent nothing for {STALL_SECONDS} s, so the download was stopped", "STALLED") from None
                except aiohttp.ClientError as error:
                    if resolver.refused is not None:
                        raise Failed(f"{resolver.refused} resolves to an address that is not public, so nothing was sent to it.", "PRIVATE") from None
                    if isinstance(error, aiohttp.ClientPayloadError):
                        raise Failed(f"the connection to {name} broke part way through the file", "UNREACHABLE") from None
                    # An aiohttp-socks before 0.11 raises python-socks's own error for a proxy it cannot reach, an OSError aiohttp wraps as its cause.
                    if proxy is not None and isinstance(error.__cause__, python_socks.ProxyConnectionError):
                        raise Failed(f"{proxy['name']} could not carry the fetch from {name}, so nothing more was fetched", "PROXY") from None
                    raise Failed(f"{name} could not be reached for GET {_described(at)}", "UNREACHABLE") from None
                # A cancel ends the wait as it ends a read, and the fetch's slot stays held through it.
                await _sleep(wait / 1000)
                attempt += 1
    finally:
        # The session's requests and the connector held the key and the proxy's credentials: gone with them.
        await connector.close()


async def _wait_commit(request, response, pending):
    """Waits for Fathom's commit, writing a line each second so a Fathom that went is seen. True once the commit has taken it."""
    deadline = time.monotonic() + COMMIT_SECONDS
    while not pending.claimed:
        if time.monotonic() >= deadline:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(pending.done), PROGRESS_SECONDS)
        except asyncio.TimeoutError:
            pass
        if pending.claimed:
            break
        try:
            await _line(response, {"waiting": True})
        except (ConnectionError, RuntimeError):
            return False
    return True


async def _fetch(request, pack_dir):
    """Fetches one URL to a new file at `path`, answering lines of JSON; the file takes its name only on `fetch-commit`."""
    if disk_routes.blake3 is None:
        raise Refused(503, "this ComfyUI has no blake3 package, so the pack cannot fetch", "NO_BLAKE3")
    if aiohttp_socks is None or pack_config.AESGCM is None:
        raise Refused(503, "this ComfyUI lacks the pack's aiohttp-socks or cryptography package, so the pack cannot fetch", "NO_REQUIREMENTS")
    source, url, path, size, published, route = _asked(await _body(request))
    key, proxy = await _opened(request, source, route)
    folder, name = _entry(pack_dir, path)
    try:
        check_url(source, url)
    except Failed as failed:
        raise Refused(403, failed.sentence, failed.code) from None
    if _RUNNING["fetches"] >= FETCHES_MAX:
        raise Refused(429, f"the pack is already running {FETCHES_MAX} fetches", "BUSY")
    _RUNNING["fetches"] += 1
    folders = None
    pending = None
    try:
        free = await _off_loop(_free, folder)
        if free < _cap(size):
            raise Refused(507, f"not enough free space on the Backend: {_cap(size)} bytes needed (the file and 10%), {free} free", "SPACE")
        folders, fd, temp, file_fd = await _off_loop(_begin_write, folder, name, NEW_FILE_MODE)
        file_open = True
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        progress = _Progress()
        hashers = (disk_routes.blake3.blake3(), hashlib.sha256())
        work = asyncio.ensure_future(_download(source, url, size, key, proxy, file_fd, hashers, progress))
        # The handler keeps them no longer than the fetch needs them.
        key = proxy = None
        try:
            while True:
                finished, _ = await asyncio.wait({work}, timeout=PROGRESS_SECONDS)
                if finished:
                    break
                await _line(response, {"bytes": progress.bytes})
            work.result()
            await _off_loop(_finish_write, file_fd)
            file_open = False
            await _line(response, {"arrived": progress.bytes})
            digests = {"blake3": hashers[0].hexdigest(), "sha256": hashers[1].hexdigest()}
            if digests[published["algorithm"]] != published["value"]:
                raise Failed(f"hash mismatch: published {published['algorithm']} {published['value']}, got {digests[published['algorithm']]}", "HASH")
            commit = secrets.token_hex(16)
            pending = _Pending(folders, fd, temp, name)
            _PENDING[commit] = pending
            await _line(response, {"verified": True, **digests, "bytes": progress.bytes, "commit": commit})
            await _wait_commit(request, response, pending)
        except Failed as failed:
            await _line(response, {"error": failed.sentence, "code": failed.code, **failed.extra})
        except (ConnectionError, RuntimeError):
            raise
        except OSError as error:
            await _line(response, {"error": "the file could not be written", "code": disk_routes.errno.errorcode.get(error.errno, "EIO")})
        except Exception:  # noqa: BLE001  One this does not name, from aiohttp or yarl, may quote the URL or a header: a fixed sentence ends the stream.
            await _line(response, {"error": "the pack could not fetch the file", "code": "EIO"})
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            if file_open:
                await _off_loop(_finish_write, file_fd)
        try:
            await response.write_eof()
        except (ConnectionError, RuntimeError):
            pass
        return response
    except (ConnectionError, RuntimeError):
        # Fathom went: the stream is cancelled, and nothing is left under a name.
        return web.Response(status=499)
    finally:
        _RUNNING["fetches"] -= 1
        if pending is not None:
            for commit, waiting in list(_PENDING.items()):
                if waiting is pending:
                    del _PENDING[commit]
        if folders is not None and (pending is None or not pending.claimed):
            _drop_temp(fd, temp)
            folders.close()


async def _fetch_commit(request, pack_dir):
    """Gives the verified file waiting under `commit` its name, never over anything there: `{"placed": true}`, or `EXISTS`."""
    body = await _body(request)
    pending = _PENDING.pop(body.get("commit"), None) if isinstance(body.get("commit"), str) else None
    if pending is None or pending.claimed:
        raise Refused(404, "no verified fetch is waiting under that commit", "NO_FETCH")
    # Taken at once, with no await before it, so the fetch's own end leaves the file to this.
    pending.claimed = True
    named = False
    try:
        # As a write does: a Fathom that gave up on the commit gets no file named.
        if not connected(request):
            raise Refused(499, "Fathom went before the file was given its name, so it was not", "GONE")
        await _off_loop(_give_name, pending.fd, pending.temp, pending.name)
        named = True
        return web.json_response({"placed": True})
    finally:
        if not named:
            _drop_temp(pending.fd, pending.temp)
        pending.folders.close()
        if not pending.done.done():
            pending.done.set_result(named)


ROUTES = {"fetch": _fetch, "fetch-commit": _fetch_commit}


def _guarded(pack_dir, handler):
    """The token first, before anything else is read; then the route. A refusal is JSON naming no token, key or proxy setting."""

    async def route(request):
        try:
            check_token(request, pack_dir)
            return await handler(request, pack_dir)
        except Refused as refused:
            return refused.answer()
        except OSError as error:
            code = disk_routes.errno.errorcode.get(error.errno, "EIO")
            return web.json_response({"error": "it could not be written", "code": code}, status=500)
        except Exception:  # noqa: BLE001  Anything else answers alike, so no traceback, and nothing sent, goes back.
            return web.json_response({"error": "the pack could not answer", "code": "EIO"}, status=500)

    return route


def register(routes, pack_dir):
    """Adds the fetch routes to ComfyUI's route table, each POST only and each behind the token."""
    for name, handler in ROUTES.items():
        routes.post(f"{PREFIX}/{name}")(_guarded(pack_dir, handler))
