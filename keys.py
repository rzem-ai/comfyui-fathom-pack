"""Provider keys, read from the ComfyUI host and never from a Graph.

A key comes first from the pasted Fathom pack config (`pack_config`), opened with the key Fathom last gave
`POST /fathom/v1/config-key`, where the config holds one (`CONFIG_NAMES`). Otherwise it comes from its environment
variable, else from the key file: `FATHOM_KEYS_FILE` when set, else `~/.config/fathom/comfyui-keys.env`, holding
`NAME=value` lines. A key is never written, printed or logged; a missing key's error names the variable only.
"""

import os

from . import pack_config

DEFAULT_KEYS_FILE = os.path.join("~", ".config", "fathom", "comfyui-keys.env")
# Each variable's name in the config's `keys`.
CONFIG_NAMES = {"FAL_KEY": "fal", "REPLICATE_API_TOKEN": "replicate"}


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


def _from_config(name):
    """The key from the pasted config, or None and why not: a held key that does not open it. None and None with no key held, or none in it."""
    held = pack_config.held_key()
    if name not in CONFIG_NAMES or held is None:
        return None, None
    try:
        return pack_config.open_config(held)["keys"].get(CONFIG_NAMES[name]), None
    except pack_config.Refused as refused:
        return None, refused.sentence


def provider_key(name):
    value, why = _from_config(name)
    value = value or os.environ.get(name) or _from_file(name)
    if not value:
        said = f" The Fathom pack config gave none: {why}." if why else ""
        raise MissingKey(f"{name} is not set on this ComfyUI host. Set it in ComfyUI's environment or in {keys_file()}.{said}")
    return value
