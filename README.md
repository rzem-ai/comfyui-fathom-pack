# Fathom node pack

Fathom's own ComfyUI nodes, in the Fathom category: Fathom fal, Fathom Replicate, Fathom CLIP image embed, Fathom CLIP text embed and Fathom animatic. The pack also adds routes under `/fathom/v1/` that Fathom scans this ComfyUI's models through, writes its model files through, and fetches a Download from CivitAI or Hugging Face straight onto this disk through (`fetch` and `fetch-commit`). The fetch routes need Python 3.11 or later and `blake3`; on an older Python the pack serves the rest and Fathom fetches each Download on its own machine.

This copy is pack 15d7c20b8f7a874e, published with Fathom v0.249.0. Its source lives in Fathom's repository at `extensions/comfyui/fathom_nodes`; change it there, not here.

## Install

In ComfyUI-Manager, open the Install Custom Nodes list, find Fathom node pack and choose Install, then restart ComfyUI. ComfyUI-Manager installs it from the Comfy Registry into `custom_nodes/fathom-node-pack` with its requirements, at its default security level and on a ComfyUI that listens on the network too.

Or clone it into ComfyUI's `custom_nodes` and install its requirements in ComfyUI's Python:

```sh
cd custom_nodes
git clone https://github.com/rzem-ai/fathom-node-pack
pip install -r fathom-node-pack/requirements.txt
```

Then restart ComfyUI. It loads custom nodes only at start.

## The token

The pack's routes answer only a request carrying this Backend's pack token. In Fathom, open the Backend in Settings > Connections and choose Make token. Fathom shows it once. Save it, on its own line, as `.fathom-token` in this folder (`custom_nodes/fathom-node-pack/.fathom-token`), owned by the user ComfyUI runs as and readable by that user alone:

```sh
chmod 600 custom_nodes/fathom-node-pack/.fathom-token
```

Making a new token in Fathom replaces the old one, so replace the file too. When more than one Fathom uses this ComfyUI, they share the one token: make it in one, keep it, and in each other Fathom choose Enter a token on the same Backend and paste it there, rather than making another, which would replace the file the first one needs. The pack reads it on every request, so no restart is needed. Without it the routes serve nothing: Fathom scans and writes over SFTP and fetches each Download on its own machine.

## The directories

The routes reach only inside the model directories ComfyUI itself reports: its models directory, and every directory one of ComfyUI's own model folder types, such as `checkpoints` or `loras`, lists outside it, from `extra_model_paths.yaml` too. A folder type another custom node adds is never one, and no directory that is `/`, holds ComfyUI's folder, or reaches `custom_nodes`, this folder or ComfyUI's user, input, output or temp directory is either. A `.fathom-roots.json` an earlier Fathom wrote here still wins.

## Replacing a pushed copy

An earlier Fathom pushed this pack to `custom_nodes/fathom_nodes` over SSH. ComfyUI-Manager cannot update that folder, and ComfyUI loads it beside this one. Delete `custom_nodes/fathom_nodes`, install this pack as above, move its `.fathom-token` into this folder or make a new token in Fathom, then restart ComfyUI.

## Update

When Fathom says to update its node pack, choose Update for Fathom node pack in ComfyUI-Manager's Install Custom Nodes list, or run `git pull` in this folder, then restart ComfyUI. `.fathom-token` and `.fathom-roots.json` are ignored by git, so they are never part of the pack ComfyUI-Manager downloads, and both a ComfyUI-Manager update and a git pull leave them in place.

## Keys and proxies

Each Backend's source keys, named proxies and NordVPN credentials come from a config Fathom seals for it. In Fathom, open the Backend's Pack config tab and copy the config. Then open this ComfyUI in a browser, go to Settings, then Fathom, then Pack config, paste it into the field and close the settings. ComfyUI saves it in its own settings file and the pack reads it from there, so no restart is needed. Every secret in it is encrypted under a key only Fathom holds, which Fathom sends with each call. Paste it again whenever Fathom says the Backend's config is out of date.

Fathom fal and Fathom Replicate take their keys from that config first, once Fathom has sent its key. Where the config holds none, they read `FAL_KEY` and `REPLICATE_API_TOKEN` from ComfyUI's environment, then from `~/.config/fathom/comfyui-keys.env` for the user ComfyUI runs as, or the path in `FATHOM_KEYS_FILE`: `NAME=value` lines, mode 600. The nodes never write, print or log a key.

## Licence

GPL-3.0. The full text is in `LICENSE`.
