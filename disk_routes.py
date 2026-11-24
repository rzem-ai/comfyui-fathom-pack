"""Read-only routes on ComfyUI's server, through which Fathom scans this Backend's models (FTH-65.1).

Every route is refused, with nothing read, unless the request's `X-Fathom-Token` header holds the token in
`.fathom-token` beside the pack, put there by the person from Fathom's Settings, compared in constant time; with no
token file every route is refused. The token is never logged and never answered.

A path is absolute and plain (no empty, "." or ".." segment), inside one of the Backend's directories, has no segment
below that directory starting with a dot, and is never reached through a symlinked folder. The directories are the
ones `.fathom-roots.json` beside the pack names, where an earlier Fathom wrote one, and otherwise ComfyUI's models
directory and the directories of ComfyUI's own model folder types (`MODEL_TYPES`), as it reports them, either way leaving out any
that is `/`, holds ComfyUI's folder, or is, holds or lies inside a `custom_nodes` directory, the pack's own folder or
ComfyUI's user, input, output or temp directory. A link as the last segment is followed where the route says so, and only to somewhere inside
those directories.

The routes list, stat and read. None writes, moves or deletes anything.
"""

import asyncio
import errno
import hashlib
import hmac
import json
import math
import os
import stat
import struct
import sys
import time

from aiohttp import web

try:
    import blake3
except ImportError:  # A ComfyUI older than its blake3 requirement: the version route says so, and Fathom scans over SFTP.
    blake3 = None

ROUTE_VERSION = 1
# The version of the write routes in `pack_writes`, stated apart so a Fathom that only reads is not turned away (FTH-65.3).
WRITES_VERSION = 1
# The version of the fetch routes in `pack_fetch`, or none where they cannot run: without blake3, or on a Python before
# 3.11, whose asyncio cannot speak TLS inside the NordVPN proxy's own TLS (FTH-65.3).
FETCH_VERSION = 1 if blake3 is not None and sys.version_info >= (3, 11) else None
PREFIX = "/fathom/v1"
TOKEN_HEADER = "X-Fathom-Token"
TOKEN_FILE = ".fathom-token"
ROOTS_FILE = ".fathom-roots.json"
VERSION_FILE = ".fathom-pack-version"
# The folder under custom_nodes an earlier Fathom pushed the pack to.
PUSHED_FOLDER = "fathom_nodes"
# The model folder types ComfyUI itself defines in `folder_paths.py`, whose directories may be the pack's. A type a custom
# node or `extra_model_paths.yaml` adds is not one; nor are `custom_nodes`, `datasets` and `configs`, which hold no models.
MODEL_TYPES = frozenset({
    "checkpoints", "loras", "vae", "text_encoders", "diffusion_models", "clip_vision", "style_models", "embeddings",
    "diffusers", "vae_approx", "controlnet", "gligen", "upscale_models", "latent_upscale_models", "hypernetworks",
    "photomaker", "classifiers", "model_patches", "audio_encoders", "background_removal", "frame_interpolation",
    "geometry_estimation", "optical_flow", "detection",
})
# ComfyUI's own folders, which no directory of the pack's may be, hold or lie inside.
COMFYUI_OWN = ("user", "input", "output", "temp")

# Bytes read at a time when hashing, and how often a hash in progress says how far it has got.
HASH_CHUNK = 8 * 1024 * 1024
PROGRESS_SECONDS = 1.0

# Bounds on what a header may hold, so a hostile file cannot make the pack read without end.
SAFETENSORS_HEADER_MAX = 100 * 1024 * 1024
GGUF_STRING_MAX = 16 * 1024 * 1024
GGUF_COUNT_MAX = 1_000_000
GGUF_ARRAY_MAX = 100_000_000
GGUF_DIMS_MAX = 8
GGUF_DEPTH_MAX = 4
# The most a GGUF header's kept metadata and tensor list may come to, counted as the bytes each kept value takes in the file:
# each string is capped above, but a file could hold many, so the total is bounded here rather than by the file's size.
GGUF_KEPT_MAX = 64 * 1024 * 1024
# A GGUF array longer than this is answered as its type and length, as a tokenizer's vocabulary is.
GGUF_ARRAY_SHOWN = 64


class Refused(Exception):
    """A request the pack will not answer. `sentence` never holds the token."""

    def __init__(self, status, sentence, code="REFUSED"):
        super().__init__(sentence)
        self.status = status
        self.sentence = sentence
        self.code = code


class BadHeader(Exception):
    pass


def _token_held(pack_dir):
    try:
        with open(os.path.join(pack_dir, TOKEN_FILE), "rb") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def check_token(request, pack_dir):
    """Refuses the request unless it carries the token in the token file. Reads nothing else."""
    held = _token_held(pack_dir)
    if held is None:
        raise Refused(403, "this Backend has no Fathom token file, so the pack serves nothing", "TOKEN")
    sent = request.headers.get(TOKEN_HEADER)
    if not sent:
        raise Refused(401, "the request carries no token", "TOKEN")
    if not hmac.compare_digest(sent.encode("utf-8", "replace"), held):
        raise Refused(401, "the request carries a token that is not this Backend's", "TOKEN")


def comfyui_folders():
    """ComfyUI's models directory, each folder type's directories and ComfyUI's own folders, as `folder_paths` reports them; outside ComfyUI, none."""
    try:
        import folder_paths
    except ImportError:
        return None, {}, {}
    folders = {name: folder_paths.get_folder_paths(name) for name in list(folder_paths.folder_names_and_paths)}
    own = {"base": getattr(folder_paths, "base_path", None)}
    for name in COMFYUI_OWN:
        get = getattr(folder_paths, f"get_{name}_directory", None)
        own[name] = get() if callable(get) else None
    return folder_paths.models_dir, folders, own


def _plain_dir(path):
    return os.path.normpath(os.path.abspath(path)) if isinstance(path, str) and path else None


def _forms(path):
    """A directory as written and as it really is, so a link to a kept-out folder is kept out too."""
    return {path, os.path.realpath(path)}


def _kept_out(pack_dir, folders, own):
    """Whether a directory reaches ComfyUI's code, a pack or ComfyUI's own folders: it is `/`, is or holds ComfyUI's base
    folder, or is, holds or lies inside a `custom_nodes` directory, the pack's own folder or ComfyUI's user, input, output
    or temp directory."""
    base = _plain_dir(own.get("base"))
    bases = _forms(base) if base else set()
    near = [pack_dir, os.path.dirname(pack_dir), *folders.get("custom_nodes", []), *(own.get(name) for name in COMFYUI_OWN)]
    nears = {form for path in map(_plain_dir, near) if path for form in _forms(path)}

    def kept_out(path):
        for form in _forms(path):
            if form == "/" or any(_under(inside, form) is not None for inside in bases):
                return True
            if any(_under(form, other) is not None or _under(other, form) is not None for other in nears):
                return True
        return False

    return kept_out


def _comfyui_roots(pack_dir):
    """ComfyUI's models directory, then each directory of ComfyUI's own model folder types outside it, absolute and
    normalised, leaving out every one `_kept_out` names."""
    models_dir, folders, own = comfyui_folders()
    if not isinstance(models_dir, str) or not models_dir:
        return []
    kept_out = _kept_out(_plain_dir(pack_dir), folders, own)
    listed = [models_dir] + [path for name, paths in folders.items() if name in MODEL_TYPES for path in paths]
    found = []
    for path in map(_plain_dir, listed):
        if path is None or kept_out(path) or any(_under(path, root) is not None for root in found):
            continue
        found.append(path)
    return found


def roots(pack_dir):
    """The Backend's directories: the roots file's, absolute and normalised, where there is one, else ComfyUI's own.
    Either way none is one `_kept_out` names, so a roots file cannot reach ComfyUI's code or a pack either."""
    try:
        with open(os.path.join(pack_dir, ROOTS_FILE), encoding="utf-8") as handle:
            listed = json.load(handle).get("roots")
    except FileNotFoundError:
        return _comfyui_roots(pack_dir)
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(listed, list):
        return []
    _, folders, own = comfyui_folders()
    kept_out = _kept_out(_plain_dir(pack_dir), folders, own)
    return [root for root in listed if isinstance(root, str) and root.startswith("/") and os.path.normpath(root) == root and not kept_out(root)]


def pack_version(pack_dir):
    """The version in the marker beside the pack, written when the pack was published, or blank for a copy made another way."""
    try:
        with open(os.path.join(pack_dir, VERSION_FILE), encoding="utf-8") as marker:
            return marker.read().strip()
    except OSError:
        return ""


def _under(path, base):
    """The part of `path` below `base`, "" for `base` itself, or None when it is not under it."""
    if path == base:
        return ""
    prefix = base if base.endswith("/") else base + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def _dotted(rel):
    return any(part.startswith(".") for part in rel.split("/") if part)


def resolve(pack_dir, path, follow_last):
    """The path to open and its real path. Raises `Refused` for a path that breaks a rule in the module's docstring.

    The walk starts at the directory's real path, so the directory itself may be a link; below it, no segment may be a
    link, except the last when `follow_last`, and then where it leads must be inside the Backend's directories.
    """
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise Refused(422, "a path must be absolute")
    if path != "/" and any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise Refused(422, 'a path may not have an empty, "." or ".." segment')
    held = roots(pack_dir)
    reals = [os.path.realpath(root) for root in held]
    for root, real_root in zip(held, reals):
        rel = _under(path, root)
        if rel is None:
            rel = _under(path, real_root)
        if rel is None:
            continue
        if _dotted(rel):
            raise Refused(403, 'it has a segment starting with ".", which is never read')
        here = real_root
        parts = rel.split("/") if rel else []
        for index, part in enumerate(parts):
            here = os.path.join(here, part)
            if os.path.islink(here):
                if index < len(parts) - 1:
                    raise Refused(403, "it is reached through a symlinked folder, which is never followed")
                if not follow_last:
                    raise Refused(403, "it is a symlink, which this route never follows")
        real = os.path.realpath(here)
        if not any(_under(real, inside) is not None for inside in reals):
            # Its own code, so Fathom goes on over SFTP, which follows the link (FTH-65.1).
            raise Refused(403, "it resolves outside the Backend's directories", "OUTSIDE")
        return here, real
    raise Refused(403, "it is outside the Backend's directories")


def _asked(request):
    path = request.query.get("path")
    if path is None:
        raise Refused(422, "the path is asked for as ?path=")
    return path


def _off_loop(work, *args):
    """Runs blocking file work on a thread, so ComfyUI's server keeps answering."""
    return asyncio.get_running_loop().run_in_executor(None, work, *args)


def _shown(name):
    """A listed name as UTF-8, any byte that is not shown as U+FFFD, as SFTP shows it."""
    return os.fsencode(name).decode("utf-8", "replace")


def _entry_type(entry):
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir(follow_symlinks=False):
        return "directory"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return "other"


def _listing(path):
    with os.scandir(path) as listing:
        return [{"name": _shown(entry.name), "type": _entry_type(entry)} for entry in listing]


def _stat_of(path, real):
    found = os.stat(path)
    kind = "file" if stat.S_ISREG(found.st_mode) else "directory" if stat.S_ISDIR(found.st_mode) else "other"
    answer = {"type": kind, "size": found.st_size, "mtime": found.st_mtime_ns // 1_000_000_000, "real": real}
    if kind == "directory":
        space = os.statvfs(path)
        answer["free"] = space.f_bavail * space.f_frsize
    return answer


def _no_dot_below(pack_dir, real):
    """A file is read only where its real path has no dot segment below its directory; a stat may still say where a link leads."""
    for inside in (os.path.realpath(root) for root in roots(pack_dir)):
        rel = _under(real, inside)
        if rel is not None and not _dotted(rel):
            return
    raise Refused(403, 'it resolves to a path with a segment starting with ".", which is never read')


def _open_file(real):
    """Opens a regular file for reading: never a link in the last part, never waiting on a fifo."""
    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Refused(422, "it is not a file")
    return fd


def pushed_copy_beside(pack_dir):
    """Whether `custom_nodes/fathom_nodes`, where an earlier Fathom pushed the pack, is a folder beside this one, which ComfyUI then loads too."""
    pushed = os.path.join(os.path.dirname(pack_dir), PUSHED_FOLDER)
    return os.path.isdir(pushed) and os.path.realpath(pushed) != os.path.realpath(pack_dir)


# Whether a pushed copy sat beside each registered pack when ComfyUI loaded it, which holds until ComfyUI restarts.
_pushed_at_load = {}


async def _version(request, pack_dir):
    return web.json_response({
        "routes": ROUTE_VERSION, "writes": WRITES_VERSION, "fetch": FETCH_VERSION, "roots": roots(pack_dir), "blake3": blake3 is not None,
        "pack": pack_version(pack_dir) or None, "pushed_copy": _pushed_at_load.get(pack_dir, False),
    })


async def _list(request, pack_dir):
    here, _ = resolve(pack_dir, _asked(request), follow_last=False)
    return web.json_response({"entries": await _off_loop(_listing, here)})


async def _stat(request, pack_dir):
    here, real = resolve(pack_dir, _asked(request), follow_last=True)
    return web.json_response(await _off_loop(_stat_of, here, real))


def _feed(fd, hashers):
    chunk = os.read(fd, HASH_CHUNK)
    for hasher in hashers:
        hasher.update(chunk)
    return len(chunk)


async def _line(response, value):
    await response.write(json.dumps(value).encode("utf-8") + b"\n")


async def _hash(request, pack_dir):
    """blake3 and sha256 of one file, as Fathom's scan hashes it, as lines of JSON: `{"bytes": n}` while it reads, then the digests."""
    if blake3 is None:
        raise Refused(503, "this ComfyUI has no blake3 package, so the pack cannot hash", "NO_BLAKE3")
    _, real = resolve(pack_dir, _asked(request), follow_last=True)
    _no_dot_below(pack_dir, real)
    fd = await _off_loop(_open_file, real)
    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    try:
        await response.prepare(request)
        hashers = (blake3.blake3(), hashlib.sha256())
        total = 0
        told = time.monotonic()
        while True:
            try:
                read = await _off_loop(_feed, fd, hashers)
            except OSError as error:
                await _line(response, {"error": "the file could not be read", "code": errno.errorcode.get(error.errno, "EIO")})
                return response
            if read == 0:
                break
            total += read
            if time.monotonic() - told >= PROGRESS_SECONDS:
                told = time.monotonic()
                await _line(response, {"bytes": total})
        await _line(response, {"blake3": hashers[0].hexdigest(), "sha256": hashers[1].hexdigest(), "bytes": total})
        await response.write_eof()
    except ConnectionError:
        pass  # Fathom stopped the hash and went; there is nobody to answer.
    finally:
        os.close(fd)
    return response


def _finite(value):
    """JSON with every NaN or infinity as null, which JSON itself cannot carry."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


class _Reader:
    def __init__(self, handle):
        self.handle = handle
        self.kept = 0

    def keep(self, size):
        """Counts `size` more bytes kept, refusing the header once they pass `GGUF_KEPT_MAX`."""
        self.kept += size
        if self.kept > GGUF_KEPT_MAX:
            raise BadHeader(f"it holds more than the {GGUF_KEPT_MAX} bytes of metadata read")

    def take(self, size):
        data = self.handle.read(size)
        if len(data) != size:
            raise BadHeader("the file ends inside its header")
        return data

    def unpack(self, layout):
        return struct.unpack(layout, self.take(struct.calcsize(layout)))[0]

    def skip(self, size):
        self.handle.seek(size, os.SEEK_CUR)


def _safetensors(handle):
    reader = _Reader(handle)
    size = reader.unpack("<Q")
    if size > SAFETENSORS_HEADER_MAX:
        raise BadHeader(f"its header says it is {size} bytes, more than the {SAFETENSORS_HEADER_MAX} read")
    try:
        header = json.loads(reader.take(size).decode("utf-8"))
    except ValueError:
        raise BadHeader("its header is not JSON") from None
    if not isinstance(header, dict):
        raise BadHeader("its header is not a JSON object")
    return {"format": "safetensors", "header": _finite(header)}


# GGUF value types, from ggml's gguf.h: each fixed-size type's layout, and the string and array types.
_GGUF_FIXED = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_GGUF_NAMES = {0: "uint8", 1: "int8", 2: "uint16", 3: "int16", 4: "uint32", 5: "int32", 6: "float32", 7: "bool", 8: "string", 9: "array", 10: "uint64", 11: "int64", 12: "float64"}
_GGUF_STRING = 8
_GGUF_ARRAY = 9


def _gguf_string(reader, keep=True):
    size = reader.unpack("<Q")
    if size > GGUF_STRING_MAX:
        raise BadHeader(f"it holds a string of {size} bytes, more than the {GGUF_STRING_MAX} read")
    if not keep:
        reader.skip(size)
        return None
    reader.keep(size)
    return reader.take(size).decode("utf-8", "replace")


def _gguf_value(reader, kind, depth=0, keep=True):
    if kind in _GGUF_FIXED:
        layout = _GGUF_FIXED[kind]
        if not keep:
            reader.skip(struct.calcsize(layout))
            return None
        reader.keep(struct.calcsize(layout))
        return reader.unpack(layout)
    if kind == _GGUF_STRING:
        return _gguf_string(reader, keep)
    if kind == _GGUF_ARRAY:
        if depth >= GGUF_DEPTH_MAX:
            raise BadHeader("it nests arrays too deeply")
        inner = reader.unpack("<I")
        length = reader.unpack("<Q")
        if length > GGUF_ARRAY_MAX:
            raise BadHeader(f"it holds an array of {length} values, more than the {GGUF_ARRAY_MAX} read")
        shown = keep and length <= GGUF_ARRAY_SHOWN
        if inner in _GGUF_FIXED and not shown:
            reader.skip(length * struct.calcsize(_GGUF_FIXED[inner]))
        else:
            values = [_gguf_value(reader, inner, depth + 1, shown) for _ in range(length)]
            if shown:
                return values
        return {"type": _GGUF_NAMES.get(inner, str(inner)), "length": length} if keep else None
    raise BadHeader(f"it holds a value of unknown type {kind}")


def _gguf(handle):
    reader = _Reader(handle)
    if reader.take(4) != b"GGUF":
        raise BadHeader("it does not start with GGUF")
    version = reader.unpack("<I")
    if version not in (2, 3):
        raise BadHeader(f"it is GGUF version {version}; only 2 and 3 are read")
    tensor_count = reader.unpack("<Q")
    kv_count = reader.unpack("<Q")
    if tensor_count > GGUF_COUNT_MAX or kv_count > GGUF_COUNT_MAX:
        raise BadHeader("it says it holds more tensors or values than are read")
    metadata = {}
    for _ in range(kv_count):
        key = _gguf_string(reader)
        metadata[key] = _gguf_value(reader, reader.unpack("<I"))
    tensors = []
    for _ in range(tensor_count):
        name = _gguf_string(reader)
        dims = reader.unpack("<I")
        if dims > GGUF_DIMS_MAX:
            raise BadHeader(f"its tensor {name} has {dims} dimensions")
        reader.keep(8 * dims + 12)
        shape = [reader.unpack("<Q") for _ in range(dims)]
        kind = reader.unpack("<I")
        reader.unpack("<Q")  # the tensor's offset in the data, which says nothing of what it is
        tensors.append({"name": name, "shape": shape, "type": kind})
    return {"format": "gguf", "version": version, "metadata": _finite(metadata), "tensors": tensors}


_HEADER_READERS = {".safetensors": _safetensors, ".gguf": _gguf}


def _header_of(real, read):
    fd = _open_file(real)
    with os.fdopen(fd, "rb") as handle:
        return read(handle)


async def _header(request, pack_dir):
    """One `.safetensors` or GGUF file's header, read and answered as it is; what it means is Fathom's to work out."""
    path = _asked(request)
    _, real = resolve(pack_dir, path, follow_last=True)
    _no_dot_below(pack_dir, real)
    read = _HEADER_READERS.get(os.path.splitext(path)[1].lower())
    if read is None:
        raise Refused(422, "only a .safetensors or GGUF file's header is read")
    try:
        return web.json_response(await _off_loop(_header_of, real, read))
    except BadHeader as bad:
        raise Refused(422, f"it has no header that can be read: {bad}", "BAD_HEADER") from None


ROUTES = {"version": _version, "list": _list, "stat": _stat, "hash": _hash, "header": _header}


def _guarded(pack_dir, handler):
    """The token first, before anything else is read; then the route. A refusal or a failure is JSON naming no token."""

    async def route(request):
        try:
            check_token(request, pack_dir)
            return await handler(request, pack_dir)
        except Refused as refused:
            return web.json_response({"error": refused.sentence, "code": refused.code}, status=refused.status)
        except OSError as error:
            code = errno.errorcode.get(error.errno, "EIO")
            missing = error.errno in (errno.ENOENT, errno.ENOTDIR)
            return web.json_response({"error": "it could not be read", "code": code}, status=404 if missing else 500)
        except Exception:  # noqa: BLE001  Anything else answers alike, so no traceback goes back to the caller.
            return web.json_response({"error": "the pack could not answer", "code": "EIO"}, status=500)

    return route


def register(routes, pack_dir):
    """Adds every route to ComfyUI's route table, each GET only and each behind the token."""
    _pushed_at_load[pack_dir] = pushed_copy_beside(pack_dir)
    for name, handler in ROUTES.items():
        routes.get(f"{PREFIX}/{name}")(_guarded(pack_dir, handler))
