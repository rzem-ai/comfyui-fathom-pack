"""Fathom's ComfyUI node pack: the Fathom fal and Fathom Replicate nodes, the CLIP image and text embed nodes smart search runs, and the animatic node Storytime's MP4 export runs."""

import os

from . import disk_routes, pack_config, pack_fetch, pack_writes
from .animatic import FathomAnimatic
from .clip_embed import FathomClipImageEmbed, FathomClipTextEmbed
from .fal import FathomFal
from .replicate import FathomReplicate


# The version in the marker the publish script writes beside the pack, or blank for a copy made another way.
PACK_VERSION = disk_routes.pack_version(os.path.dirname(os.path.abspath(__file__)))

# ComfyUI answers DESCRIPTION as `description` in /object_info, so Fathom can tell which pack is loaded.
FathomFal.DESCRIPTION = f"Runs a fal endpoint. (pack {PACK_VERSION})"
FathomReplicate.DESCRIPTION = f"Runs a Replicate model. (pack {PACK_VERSION})"
FathomClipImageEmbed.DESCRIPTION = f"Saves a CLIP image embedding for smart search. (pack {PACK_VERSION})"
FathomClipTextEmbed.DESCRIPTION = f"Saves a CLIP text embedding for smart search. (pack {PACK_VERSION})"
FathomAnimatic.DESCRIPTION = f"Encodes a Story's animatic as an MP4. (pack {PACK_VERSION})"

NODE_CLASS_MAPPINGS = {
    "FathomFal": FathomFal,
    "FathomReplicate": FathomReplicate,
    "FathomClipImageEmbed": FathomClipImageEmbed,
    "FathomClipTextEmbed": FathomClipTextEmbed,
    "FathomAnimatic": FathomAnimatic,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "FathomFal": "Fathom fal",
    "FathomReplicate": "Fathom Replicate",
    "FathomClipImageEmbed": "Fathom CLIP image embed",
    "FathomClipTextEmbed": "Fathom CLIP text embed",
    "FathomAnimatic": "Fathom animatic",
}

# The settings panel's Fathom section, where the pack config is pasted (FTH-146.4).
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

try:
    from server import PromptServer
except ImportError:  # Outside ComfyUI, as in the tests, there is no server to add routes to.
    PromptServer = None

# The read routes Fathom scans through (FTH-65.1), the write and fetch routes it changes model files through (FTH-65.3) and the config-key route (FTH-146.4), each behind the token in the pack's token file,
# to which a `.fathom-token` an earlier pack read from its own folder is moved first (FTH-155).
if PromptServer is not None and getattr(PromptServer, "instance", None) is not None:
    disk_routes.move_folder_token(os.path.dirname(os.path.abspath(__file__)))
    disk_routes.register(PromptServer.instance.routes, os.path.dirname(os.path.abspath(__file__)))
    pack_writes.register(PromptServer.instance.routes, os.path.dirname(os.path.abspath(__file__)))
    pack_fetch.register(PromptServer.instance.routes, os.path.dirname(os.path.abspath(__file__)))
    pack_config.register(PromptServer.instance.routes, os.path.dirname(os.path.abspath(__file__)))
