"""Between ComfyUI's IMAGE (a float tensor of shape [batch, height, width, channels] in 0..1) and image files."""

import io

import numpy as np
from PIL import Image


def as_tensor(array):
    import torch

    return torch.from_numpy(array)


def to_png(image):
    """One image of an IMAGE batch as PNG bytes."""
    array = image.cpu().numpy() if hasattr(image, "cpu") else np.asarray(image)
    pixels = np.clip(array * 255.0, 0, 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format="PNG")
    return buffer.getvalue()


def references(images):
    """Each image of an optional IMAGE input as PNG bytes; none when the input is not connected."""
    if images is None:
        return []
    return [to_png(images[at]) for at in range(len(images))]


def to_batch(files):
    """Downloaded image files as one IMAGE batch, each resized to the first one's size."""
    if not files:
        raise RuntimeError("The provider returned no images.")
    pictures = [Image.open(io.BytesIO(data)).convert("RGB") for data in files]
    size = pictures[0].size
    arrays = [np.asarray(picture if picture.size == size else picture.resize(size, Image.LANCZOS), dtype=np.float32) / 255.0 for picture in pictures]
    return as_tensor(np.stack(arrays))
