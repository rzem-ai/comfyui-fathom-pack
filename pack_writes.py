"""Write routes on ComfyUI's server, through which Fathom changes this Backend's model files (FTH-65.3).

Every route is a POST behind the same token as the read routes in `disk_routes`, checked first, so a request without it
touches nothing. Every path a route takes is held to the read routes' rules: absolute and plain, inside one of the
Backend's directories (its model directories and ComfyUI's workflows folder, FTH-65.4.2), no segment below the
directory starting with a dot, and never through a symlinked folder; for a folder reached through a link Fathom sends its
real path, which must lie inside them. No path may lie in the folder holding the pack (`custom_nodes`), whatever
directory holds it. The entry a route changes is never the directory itself, and a name already taken is refused with
`EXISTS`, as Fathom's SFTP writer refuses it. A file or folder to delete in a folder that has gone is already gone, as
over SFTP.

Each change is made through a handle on the folder holding the entry, opened with `O_NOFOLLOW`, so that folder swapped
for a link after the path was checked is refused rather than written through. A folder above it is not covered, and a
folder's move, and a file's on a filesystem with no hard links, look and then rename, so a name taken between the two is
replaced: the same windows the SFTP writer has.

A file is written under a hidden temporary name beside its target and linked to its name only once every byte is on
disk and only while Fathom is still connected, so a write that fails, is cut short, is refused or that Fathom gave up
on leaves nothing under the target's name; the temporary file is removed, and a ComfyUI that dies mid-write leaves only
it, which no scan catalogues.

Nothing a route is sent is logged, and no answer repeats a token.
"""

import errno
import os
import secrets
import stat

from aiohttp import web

from .disk_routes import PREFIX, Refused, _off_loop, _under, check_token, directories, resolve

# Bytes taken from the request at a time while a file is written.
WRITE_CHUNK = 1024 * 1024
# The hidden name a file is written under until it is whole: a dot name, so no scan catalogues it.
TEMP_PREFIX = ".fathom-write-"
NEW_FILE_MODE = 0o644
NEW_FOLDER_MODE = 0o755

SYMLINKED = "it is reached through a symlinked folder, which is never followed"
TAKEN = "something is already at the path, and the pack never replaces a name"
# What a link that cannot be made answers when the filesystem has no hard links, so the move falls back to a rename.
NO_LINKS = {errno.EPERM, errno.EXDEV, errno.EMLINK, errno.ENOSYS, getattr(errno, "ENOTSUP", errno.EPERM), getattr(errno, "EOPNOTSUPP", errno.EPERM)}


class Partly(Exception):
    """A folder walk that failed after it had made the folder `first`."""

    def __init__(self, error, first):
        super().__init__(str(error))
        self.error = error
        self.first = first


def _plain(path):
    """Refuses a path that is not absolute and plain, as `resolve` does, before anything is looked at."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise Refused(422, "a path must be absolute")
    if path == "/" or any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise Refused(422, 'a path may not have an empty, "." or ".." segment')


def _folder(pack_dir, folder):
    """A folder inside the Backend's directories, reached through no link at all, the folder itself included."""
    try:
        here, _ = resolve(pack_dir, folder, follow_last=True)
    except Refused as refused:
        # Below the directory's real path only a link can lead out of it.
        if refused.code == "OUTSIDE":
            raise Refused(403, SYMLINKED) from None
        raise
    if os.path.islink(here):
        raise Refused(403, SYMLINKED)
    # Never the pack's own code, nor anything beside it, even under a directory that holds custom_nodes.
    if _under(os.path.realpath(here), os.path.realpath(os.path.dirname(pack_dir))) is not None:
        raise Refused(403, "it is inside the folder holding the node pack, which no route writes", "PACK")
    return here


def _entry(pack_dir, path):
    """The folder holding the entry at `path`, and the entry's name. The entry itself is never followed."""
    _plain(path)
    folder, name = path.rsplit("/", 1)
    here = _folder(pack_dir, folder or "/")
    if name.startswith("."):
        raise Refused(403, 'it has a segment starting with ".", which is never written')
    return here, name


def _open_folder(folder):
    """A handle on the folder, refused if it has become a link since it was checked."""
    try:
        return os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR) and os.path.islink(folder):
            raise Refused(403, SYMLINKED) from None
        raise


def _lstat(folder_fd, name):
    try:
        return os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _refuse_taken(folder_fd, name):
    if _lstat(folder_fd, name) is not None:
        raise Refused(409, TAKEN, "EXISTS")


def _mode(raw):
    if raw is None:
        return NEW_FILE_MODE
    try:
        mode = int(raw)
    except ValueError:
        raise Refused(422, "mode is a number of permission bits") from None
    if not 0 <= mode <= 0o777:
        raise Refused(422, "mode is a number of permission bits")
    return mode


async def _body(request):
    try:
        body = await request.json()
    except ValueError:
        raise Refused(422, "the request is a JSON object") from None
    if not isinstance(body, dict):
        raise Refused(422, "the request is a JSON object")
    return body


def _count(body, key):
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Refused(422, f"{key} is a whole number")
    return value


class _Folders:
    """The folders holding a route's entries, each opened once, all closed after."""

    def __init__(self):
        self.fds = []

    def open(self, folder):
        fd = _open_folder(folder)
        self.fds.append(fd)
        return fd

    def open_if_there(self, folder):
        """The folder's handle, or None when it is not there, so what was in it is gone, as SFTP answers no such file."""
        try:
            return self.open(folder)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def close(self):
        for fd in self.fds:
            os.close(fd)


# ---------- write ----------


def _begin_write(folder, name, mode):
    folders = _Folders()
    try:
        fd = folders.open(folder)
        _refuse_taken(fd, name)
        temp = TEMP_PREFIX + secrets.token_hex(8)
        file_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    except BaseException:
        folders.close()
        raise
    return folders, fd, temp, file_fd


def _write_all(file_fd, chunk):
    view = memoryview(chunk)
    while view:
        view = view[os.write(file_fd, view):]


def _finish_write(file_fd):
    try:
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _give_name(fd, temp, name):
    """Links the whole file to its name, never over anything there, then drops the temporary name."""
    try:
        os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    except FileExistsError:
        raise Refused(409, TAKEN, "EXISTS") from None
    except OSError as error:
        if error.errno not in NO_LINKS:
            raise
        # No hard links here: look, then rename, as Fathom's SFTP writer falls back.
        _refuse_taken(fd, name)
        os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        return
    try:
        os.unlink(temp, dir_fd=fd)
    except OSError:
        pass  # Placed: the temporary name is only untidy now.


def connected(request):
    """Whether the caller is still there to be answered, checked just before a file is given its name."""
    transport = request.transport
    return transport is not None and not transport.is_closing()


def _drop_temp(fd, temp):
    try:
        os.unlink(temp, dir_fd=fd)
    except OSError:
        pass


async def _write(request, pack_dir):
    """The request's body as a new file at `?path=`, streamed to disk a chunk at a time: `{"bytes": n}` once it has its name."""
    path = request.query.get("path")
    if path is None:
        raise Refused(422, "the path is asked for as ?path=")
    folder, name = _entry(pack_dir, path)
    mode = _mode(request.query.get("mode"))
    # Refused before a byte is read, so a refusal never waits for the file to arrive.
    folders, fd, temp, file_fd = await _off_loop(_begin_write, folder, name, mode)
    named = False
    try:
        total = 0
        try:
            async for chunk in request.content.iter_chunked(WRITE_CHUNK):
                await _off_loop(_write_all, file_fd, chunk)
                total += len(chunk)
        finally:
            await _off_loop(_finish_write, file_fd)
        # The fsync of a large file on a slow disk may outlast Fathom's wait: a Fathom that gave up gets no file named.
        if not connected(request):
            raise Refused(499, "Fathom went before the file was whole on disk, so it was not given its name", "GONE")
        await _off_loop(_give_name, fd, temp, name)
        named = True
        return web.json_response({"bytes": total})
    finally:
        if not named:
            _drop_temp(fd, temp)
        folders.close()


# ---------- rename ----------


def _rename_file(source, target):
    (from_folder, from_name), (to_folder, to_name) = source, target
    folders = _Folders()
    try:
        from_fd = folders.open(from_folder)
        to_fd = folders.open(to_folder)
        _refuse_taken(to_fd, to_name)
        moving = os.stat(from_name, dir_fd=from_fd, follow_symlinks=False)
        if stat.S_ISDIR(moving.st_mode):
            raise Refused(422, "it is a folder, which moves as a folder")
        if not stat.S_ISLNK(moving.st_mode):
            try:
                os.link(from_name, to_name, src_dir_fd=from_fd, dst_dir_fd=to_fd, follow_symlinks=False)
            except FileExistsError:
                raise Refused(409, TAKEN, "EXISTS") from None
            except OSError as error:
                if error.errno not in NO_LINKS:
                    raise
            else:
                try:
                    os.unlink(from_name, dir_fd=from_fd)
                except OSError:
                    pass  # Moved: the old name is only untidy now, as over SFTP.
                return
        # A link moves as the link, and a filesystem with no hard links by rename, after a last look.
        _refuse_taken(to_fd, to_name)
        os.rename(from_name, to_name, src_dir_fd=from_fd, dst_dir_fd=to_fd)
    finally:
        folders.close()


def _rename_folder(source, target):
    (from_folder, from_name), (to_folder, to_name) = source, target
    folders = _Folders()
    try:
        from_fd = folders.open(from_folder)
        to_fd = folders.open(to_folder)
        _refuse_taken(to_fd, to_name)
        moving = os.stat(from_name, dir_fd=from_fd, follow_symlinks=False)
        if not stat.S_ISDIR(moving.st_mode):
            raise Refused(422, "it is not a folder")
        os.rename(from_name, to_name, src_dir_fd=from_fd, dst_dir_fd=to_fd)
    finally:
        folders.close()


async def _moved(request, pack_dir, move):
    body = await _body(request)
    source = _entry(pack_dir, body.get("from"))
    target = _entry(pack_dir, body.get("to"))
    await _off_loop(move, source, target)
    return web.json_response({})


async def _rename(request, pack_dir):
    """Moves the file or link `from` to `to`, a link as the link, refusing `EXISTS` when anything is at `to`."""
    return await _moved(request, pack_dir, _rename_file)


async def _rename_folder_route(request, pack_dir):
    """Moves the folder `from`, with everything in it, to `to`, refusing `EXISTS` when anything is at `to`."""
    return await _moved(request, pack_dir, _rename_folder)


# ---------- unlink, rmdir, unlink-placed ----------


def _unlink_entry(entry):
    folder, name = entry
    folders = _Folders()
    try:
        fd = folders.open_if_there(folder)
        if fd is None:
            return
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass  # Already gone, which is not a failure.
    finally:
        folders.close()


def _rmdir_entry(entry):
    folder, name = entry
    folders = _Folders()
    try:
        fd = folders.open_if_there(folder)
        if fd is None:
            return
        try:
            os.rmdir(name, dir_fd=fd)
        except OSError as error:
            # Missing or not empty is left, and is not a failure.
            if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                raise
    finally:
        folders.close()


def _found(info):
    kind = "file" if stat.S_ISREG(info.st_mode) else "other"
    return {"kind": kind, "size": info.st_size, "mtime": info.st_mtime_ns // 1_000_000_000}


def _unlink_placed(entry, size, mtime):
    folder, name = entry
    folders = _Folders()
    try:
        fd = folders.open_if_there(folder)
        info = None if fd is None else _lstat(fd, name)
        if info is None:
            return "gone"
        link = stat.S_ISLNK(info.st_mode)
        if link:
            try:
                info = os.stat(name, dir_fd=fd)
            except OSError:
                raise _NotPlaced({"link": True, "unreadable": True}) from None
        found = _found(info)
        if found["kind"] != "file" or found["size"] != size or found["mtime"] != mtime:
            raise _NotPlaced({**found, "link": link})
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            return "gone"
        return "link-deleted" if link else "deleted"
    finally:
        folders.close()


class _NotPlaced(Exception):
    def __init__(self, found):
        super().__init__("it is not the file catalogued there")
        self.found = found


async def _unlink(request, pack_dir):
    """Removes a file or link; one already gone, or in a folder that has gone, is not a failure."""
    body = await _body(request)
    await _off_loop(_unlink_entry, _entry(pack_dir, body.get("path")))
    return web.json_response({})


async def _rmdir(request, pack_dir):
    """Removes a folder only if it is empty; a missing or non-empty one, or one in a folder that has gone, is left, and is not a failure."""
    body = await _body(request)
    await _off_loop(_rmdir_entry, _entry(pack_dir, body.get("path")))
    return web.json_response({})


async def _unlink_placed_route(request, pack_dir):
    """Deletes the file at `path` only if it is still a regular file of `size` bytes and whole-second `mtime`, or a link to one, when only the link goes.

    Answers `{"done": "deleted" | "link-deleted" | "gone"}`, or `NOT_PLACED` with what was found, so Fathom can say how it differs.
    """
    body = await _body(request)
    entry = _entry(pack_dir, body.get("path"))
    size, mtime = _count(body, "size"), _count(body, "mtime")
    try:
        done = await _off_loop(_unlink_placed, entry, size, mtime)
    except _NotPlaced as not_placed:
        return web.json_response({"error": str(not_placed), "code": "NOT_PLACED", "found": not_placed.found}, status=409)
    return web.json_response({"done": done})


# ---------- mkdirs ----------


def _make_folders(pack_dir, here):
    top = next((real for real in (os.path.realpath(root) for root in directories(pack_dir)) if _under(here, real) is not None), None)
    if top is None:
        raise Refused(403, "it is outside the Backend's directories")
    missing = []
    at = here
    while at != top and not os.path.lexists(at):
        missing.insert(0, at)
        at = os.path.dirname(at)
    info = os.lstat(at)
    if stat.S_ISLNK(info.st_mode):
        raise Refused(403, SYMLINKED)
    if not stat.S_ISDIR(info.st_mode):
        raise Refused(409, f"{at} is not a folder", "NOT_DIR")
    first = None
    for path in missing:
        try:
            os.mkdir(path, NEW_FOLDER_MODE)
            first = first or path
        except FileExistsError:
            # Made by someone else meanwhile: theirs, and fine, if it is a folder.
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                raise Partly(Refused(409, f"{path} is not a folder", "NOT_DIR"), first) from None
        except OSError as error:
            raise Partly(error, first) from None
    return first


async def _mkdirs(request, pack_dir):
    """Makes the folder and any parents; one already there is fine. `{"first": path}` names the shallowest it made, or null."""
    body = await _body(request)
    path = body.get("path")
    _plain(path)
    return web.json_response({"first": await _off_loop(_make_folders, pack_dir, _folder(pack_dir, path))})


ROUTES = {
    "mkdirs": _mkdirs,
    "write": _write,
    "rename": _rename,
    "rename-folder": _rename_folder_route,
    "unlink": _unlink,
    "rmdir": _rmdir,
    "unlink-placed": _unlink_placed_route,
}


def _failed(error, extra=None):
    code = errno.errorcode.get(error.errno, "EIO")
    missing = error.errno in (errno.ENOENT, errno.ENOTDIR)
    body = {"error": "it could not be written", "code": code, **(extra or {})}
    return web.json_response(body, status=404 if missing else 500)


def _guarded(pack_dir, handler):
    """The token first, before anything else is read; then the route. A refusal or a failure is JSON naming no token and nothing sent."""

    async def route(request):
        try:
            check_token(request, pack_dir)
            return await handler(request, pack_dir)
        except Refused as refused:
            return refused.answer()
        except Partly as partly:
            first = {"first": partly.first}
            if isinstance(partly.error, Refused):
                return web.json_response({"error": partly.error.sentence, "code": partly.error.code, **first}, status=partly.error.status)
            if isinstance(partly.error, OSError):
                return _failed(partly.error, first)
            return web.json_response({"error": "the pack could not answer", "code": "EIO", **first}, status=500)
        except OSError as error:
            return _failed(error)
        except Exception:  # noqa: BLE001  Anything else answers alike, so no traceback goes back to the caller.
            return web.json_response({"error": "the pack could not answer", "code": "EIO"}, status=500)

    return route


def register(routes, pack_dir):
    """Adds every write route to ComfyUI's route table, each POST only and each behind the token."""
    for name, handler in ROUTES.items():
        routes.post(f"{PREFIX}/{name}")(_guarded(pack_dir, handler))
