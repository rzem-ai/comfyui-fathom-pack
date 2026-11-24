"""The Fathom CLIP embed nodes (FTH-549): an image's and a text's smart search embedding from a CLIP model.

Each loads its file through ComfyUI's own loader and runs ComfyUI's own encoder, so its preprocessing is
ComfyUI's:

- the image node loads the clip_vision file as the CLIP Vision Loader does (`comfy.clip_vision.load`) and
  answers `encode_image`'s `image_embeds`, the projected image embedding, centre-cropped to the model's size;
- the text node loads the text_encoders file as the CLIP Loader does for Stable Diffusion (`comfy.sd.load_clip`),
  but with `sd1_clip.SDClipModel` as the CLIP-L class, whose pooled output is taken after the text projection,
  and answers that pooled output, the projected text embedding.

Each node saves its embeddings as Save Conditioning names its files, `<prefix>_<counter>_.safetensors` in the
output folder, so Fathom reads them over `/view`: `image_embeds` or `text_embeds`, float32, [batch, size].

A loaded model stays loaded for the next prompt, one of each kind; ComfyUI's model management moves it to and
from the GPU as it does a loader's. torch, `comfy` and `folder_paths` come with ComfyUI and are imported only
when a node runs.
"""

import json
import os
import struct

import numpy as np

DEFAULT_PREFIX = "fathom-embeddings/clip"
VISION_FOLDER = "clip_vision"
TEXT_FOLDER = "text_encoders"


def filenames(folder):
    import folder_paths

    return folder_paths.get_filename_list(folder)


def full_path(folder, name):
    """The file the folder lists under `name`; any other name is refused before a path is made of it."""
    import folder_paths

    if not isinstance(name, str) or name not in filenames(folder):
        raise FileNotFoundError(f"There is no {name!r} in models/{folder}.")
    return folder_paths.get_full_path_or_raise(folder, name)


def save_path(prefix):
    """Where Save Conditioning would put a file under this prefix: (folder, file name, counter, subfolder)."""
    import folder_paths

    folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(prefix, folder_paths.get_output_directory())
    return folder, filename, counter, subfolder


def load_vision(path):
    import comfy.clip_vision

    model = comfy.clip_vision.load(path)
    if model is None:
        raise RuntimeError(f"{os.path.basename(path)} holds no CLIP vision model ComfyUI can load.")
    return model


def load_text(path):
    import comfy.sd
    import comfy.sd1_clip
    import folder_paths

    clip = comfy.sd.load_clip(
        ckpt_paths=[path],
        embedding_directory=folder_paths.get_folder_paths("embeddings"),
        clip_type=comfy.sd.CLIPType.STABLE_DIFFUSION,
        model_options={"clip_l_class": comfy.sd1_clip.SDClipModel},
    )
    # A ComfyUI that built Stable Diffusion's own class instead would answer the pooled output before the projection,
    # 768 long all the same, in another space from the image embeddings: refuse it rather than save that.
    clip_l = getattr(getattr(clip, "cond_stage_model", None), "clip_l", None)
    if getattr(clip_l, "return_projected_pooled", None) is not True:
        raise RuntimeError(f"This ComfyUI did not load {os.path.basename(path)} with its projected pooled output, so it cannot give a CLIP text embedding. Update ComfyUI.")
    return clip


_models = {}


def forget_models():
    _models.clear()


def loaded(kind, folder, name, load):
    """The model in `folder` named `name`, loaded once and kept until another file of its kind is asked for or this one changes."""
    path = full_path(folder, name)
    key = (path, os.stat(path).st_mtime_ns)
    held = _models.get(kind)
    if held is None or held[0] != key:
        _models.pop(kind, None)
        _models[kind] = (key, load(path))
    return _models[kind][1]


def as_array(values):
    """A tensor, or an array in the tests, as float32 numpy [batch, size]."""
    if hasattr(values, "detach"):
        values = values.detach().to("cpu").float().numpy()
    array = np.asarray(values, dtype=np.float32)
    return array.reshape(array.shape[0], -1) if array.ndim != 2 else array


def safetensors_bytes(tensors, metadata):
    """float32 tensors as a safetensors file: the 8-byte header length, the JSON header padded to 8 bytes, then the data."""
    header = {"__metadata__": metadata}
    blobs = []
    offset = 0
    for name, values in tensors.items():
        blob = np.ascontiguousarray(values, dtype="<f4").tobytes()
        header[name] = {"dtype": "F32", "shape": list(values.shape), "data_offsets": [offset, offset + len(blob)]}
        blobs.append(blob)
        offset += len(blob)
    text = json.dumps(header, separators=(",", ":")).encode("utf-8")
    text += b" " * (-len(text) % 8)
    return struct.pack("<Q", len(text)) + text + b"".join(blobs)


def save(name, vectors, prefix, model):
    folder, filename, counter, subfolder = save_path(prefix)
    file = f"{filename}_{counter:05}_.safetensors"
    with open(os.path.join(folder, file), "wb") as out:
        out.write(safetensors_bytes({name: vectors}, {"model": model}))
    return {"ui": {"embeddings": [{"filename": file, "subfolder": subfolder, "type": "output"}]}}


def _inputs(first, folder):
    return {
        "required": {
            **first,
            "clip_name": (filenames(folder),),
            "filename_prefix": ("STRING", {"default": DEFAULT_PREFIX}),
        }
    }


class FathomClipImageEmbed:
    CATEGORY = "Fathom"
    RETURN_TYPES = ()
    FUNCTION = "embed"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return _inputs({"image": ("IMAGE",)}, VISION_FOLDER)

    def embed(self, image, clip_name, filename_prefix):
        model = loaded("vision", VISION_FOLDER, clip_name, load_vision)
        return save("image_embeds", as_array(model.encode_image(image, crop=True).image_embeds), filename_prefix, clip_name)


class FathomClipTextEmbed:
    CATEGORY = "Fathom"
    RETURN_TYPES = ()
    FUNCTION = "embed"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return _inputs({"text": ("STRING", {"default": "", "multiline": True})}, TEXT_FOLDER)

    def embed(self, text, clip_name, filename_prefix):
        clip = loaded("text", TEXT_FOLDER, clip_name, load_text)
        _, pooled = clip.encode_from_tokens(clip.tokenize(text), return_pooled=True)
        return save("text_embeds", as_array(pooled), filename_prefix, clip_name)
