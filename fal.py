"""The Fathom fal node: runs one fal endpoint through fal's queue and returns its images as IMAGE.

Queue: POST https://queue.fal.run/{endpoint} with `Authorization: Key <FAL_KEY>` returns a request's
`status_url` and `response_url`; the status reads COMPLETED when done, and the response holds the
result. References are uploaded to fal storage (initiate, then PUT to the signed URL) and passed as URLs.
"""

import base64
import json
import time

from . import images, keys, progress, transport

QUEUE = "https://queue.fal.run"
STORAGE = "https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3"
POLL_SECONDS = 1.0
DEADLINE_SECONDS = 1800


def _headers():
    return {"Authorization": f"Key {keys.provider_key('FAL_KEY')}", "Accept": "application/json"}


def upload(png):
    headers = _headers()
    target = transport.request_json("POST", STORAGE, headers, {"content_type": "image/png", "file_name": "reference.png"}, "fal storage initiate")
    transport.request("PUT", transport.https_only(target["upload_url"], "fal storage"), {"Content-Type": "image/png"}, png, "fal storage upload")
    return transport.https_only(target["file_url"], "fal storage")


def output_urls(result):
    found = []
    for entry in result.get("images") or []:
        if isinstance(entry, dict) and entry.get("url"):
            found.append(entry["url"])
    image = result.get("image")
    if isinstance(image, dict) and image.get("url"):
        found.append(image["url"])
    return found


def run(endpoint, arguments, reference_pngs=(), reference_field="image_urls", reference_list=None, report=None):
    """The endpoint's images as files. A `report` dict is given the seed fal used and whether it flagged each image as unsafe."""
    headers = _headers()
    stages = progress.Stages()
    stages.submitting()
    endpoint = endpoint.strip().strip("/")
    if reference_pngs:
        urls = [upload(png) for png in reference_pngs]
        arguments = {**arguments, reference_field: urls if as_list(reference_field, reference_list) else urls[0]}
    queued = transport.request_json("POST", f"{QUEUE}/{endpoint}", headers, arguments, f"fal {endpoint} submit", body_in_error=True)
    status_url = queued.get("status_url") or f"{QUEUE}/{endpoint}/requests/{queued['request_id']}/status"
    response_url = queued.get("response_url") or f"{QUEUE}/{endpoint}/requests/{queued['request_id']}"
    transport.https_only(status_url, f"fal {endpoint}")
    transport.https_only(response_url, f"fal {endpoint}")
    waited = 0.0
    while True:
        status = transport.request_json("GET", status_url, headers, provider=f"fal {endpoint} status")
        state = status.get("status")
        if state == "COMPLETED":
            break
        if state == "IN_QUEUE":
            stages.queued(status.get("queue_position"))
        elif state == "IN_PROGRESS":
            stages.running()
        else:
            raise transport.ProviderError(f"fal {endpoint} ended with status {state}.")
        if waited >= DEADLINE_SECONDS:
            raise transport.ProviderError(f"fal {endpoint} did not finish in {DEADLINE_SECONDS} seconds.")
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    result = transport.request_json("GET", response_url, headers, provider=f"fal {endpoint} result")
    if result.get("error") or result.get("detail"):
        raise transport.ProviderError(f"fal {endpoint} failed: {result.get('error') or result.get('detail')}")
    urls = output_urls(result)
    if not urls:
        raise transport.ProviderError(f"fal {endpoint} returned no images.")
    if report is not None:
        report.update(reported(result))
    files = []
    for url in urls:
        stages.downloading(len(files), len(urls))
        files.append(download(url, f"fal {endpoint} download"))
    stages.downloading(len(files), len(urls))
    return files


def reported(result):
    """The seed fal says it used and its `has_nsfw_concepts` flags, one per image, each where fal returns it."""
    found = {}
    if isinstance(result.get("seed"), int) and not isinstance(result.get("seed"), bool):
        found["seed"] = result["seed"]
    flags = result.get("has_nsfw_concepts")
    if isinstance(flags, list) and all(isinstance(flag, bool) for flag in flags):
        found["unsafe"] = flags
    return found


def with_report(batch, report):
    """The node's output with what the provider reported, which ComfyUI keeps in the Run's history for Fathom to read."""
    return {"ui": {"fathom_cloud": [report]}, "result": (batch,)}


def download(url, step):
    """An output image: a `data:image/...` URL, which fal returns in sync mode, is decoded here; any other is fetched over https."""
    if url.startswith("data:image/") and ";base64," in url:
        return base64.b64decode(url.split(",", 1)[1])
    return transport.request("GET", transport.https_only(url, step), provider=step)


def parse_arguments(text):
    value = json.loads(text or "{}")
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object.")
    return value


def as_list(reference_field, reference_list):
    """Whether the references go as a list: as `reference_list` says, from the model's schema, else by a field name ending in `s`."""
    return reference_field.endswith("s") if reference_list is None else bool(reference_list)


def with_prompt_and_seed(arguments, prompt, seed, send_seed=True):
    """The prompt and seed inputs, when the Graph gives them, go in the arguments as `prompt` and `seed`, so Fathom fills and varies them as node inputs.
    With `send_seed` false the seed stays out of the arguments: it only changes the node's inputs, so each Run reruns rather than reusing ComfyUI's cache."""
    if prompt:
        arguments = {**arguments, "prompt": prompt}
    if seed is not None and send_seed is not False:
        arguments = {**arguments, "seed": seed}
    return arguments


class FathomFal:
    """ComfyUI caches a node whose inputs are unchanged, so a different seed in the arguments reruns it."""

    CATEGORY = "Fathom"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "endpoint": ("STRING", {"default": "fal-ai/flux/schnell"}),
                "arguments": ("STRING", {"default": "{}", "multiline": True}),
            },
            "optional": {
                "references": ("IMAGE",),
                "reference_field": ("STRING", {"default": "image_urls"}),
                "prompt": ("STRING", {"default": "", "multiline": True}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "send_seed": ("BOOLEAN", {"default": True}),
                "reference_list": ("BOOLEAN", {"default": True}),
            },
        }

    def generate(self, endpoint, arguments, references=None, reference_field="image_urls", prompt=None, seed=None, send_seed=True, reference_list=None):
        values = with_prompt_and_seed(parse_arguments(arguments), prompt, seed, send_seed)
        report = {}
        files = run(endpoint, values, images.references(references), reference_field or "image_urls", reference_list, report)
        return with_report(images.to_batch(files), report)
