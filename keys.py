"""Provider keys, read from the ComfyUI host and never from a Graph.

A key comes from its environment variable, else from the key file: `FATHOM_KEYS_FILE` when set,
else `~/.config/fathom/comfyui-keys.env`, holding `NAME=value` lines. A key is never written,
printed or logged; a missing key's error names the variable only.
"""

import os

DEFAULT_KEYS_FILE = os.path.join("~", ".config", "fathom", "comfyui-keys.env")


class MissingKey(RuntimeError):
    pass


def keys_file():
    return os.path.expanduser(os.environ.get("FATHOM_KEYS_FILE") or DEFAULT_KEYS_FILE)


def _from_file(name):
    path = keys_file()
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().removeprefix("export ").strip() == name:
                return value.strip().strip("'\"") or None
    return None


def provider_key(name):
    value = os.environ.get(name) or _from_file(name)
    if not value:
        raise MissingKey(f"{name} is not set on this ComfyUI host. Set it in ComfyUI's environment or in {keys_file()}.")
    return value
