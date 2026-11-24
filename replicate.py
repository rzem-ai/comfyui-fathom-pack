"""The Fathom Replicate node: runs one Replicate model as a prediction and returns its images as IMAGE.

Predictions: POST https://api.replicate.com/v1/predictions with `{version, input}`, the version pinned or
else the model's `latest_version.id` from GET /v1/models/{owner}/{name}; an official model, which has no
latest version, posts `{input}` to /v1/models/{owner}/{name}/predictions instead. All with
`Authorization: Bearer <REPLICATE_API_TOKEN>`. The prediction's `urls.get` is polled until it succeeds,
fails or is cancelled. References go in as data URLs under 1 MB, as Replicate recommends, and
through its files API (POST /v1/files, then `urls.get`) above that.
"""

import base64
import json
import re
import time
import uuid

from . import fal, images, keys, progress, transport

API = "https://api.replicate.com/v1"
DATA_URL_LIMIT = 1_000_000
POLL_SECONDS = 1.0
DEADLINE_SECONDS = 1800


def _headers():
    return {"Authorization": f"Bearer {keys.provider_key('REPLICATE_API_TOKEN')}", "Accept": "application/json"}


def reference_url(png):
    if len(png) < DATA_URL_LIMIT:
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"; filename=\"reference.png\"\r\nContent-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{boundary}--\r\n".encode()
    headers = {**_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}
    uploaded = json.loads(transport.request("POST", f"{API}/files", headers, body, "Replicate files upload"))
    return transport.https_only(uploaded["urls"]["get"], "Replicate files")


def output_urls(output):
    if isinstance(output, str):
        return [output]
    if isinstance(output, list):
        return [item for item in output if isinstance(item, str)]
    return []


def run(model, version, input_values, reference_pngs=(), reference_field="input_images", reference_list=None, report=None):
    """The model's images as files. A `report` dict is given the seed the prediction's logs say it used; Replicate flags no unsafe image."""
    headers = _headers()
    stages = progress.Stages()
    stages.submitting()
    model = model.strip().strip("/")
    if reference_pngs:
        urls = [reference_url(png) for png in reference_pngs]
        input_values = {**input_values, reference_field: urls if fal.as_list(reference_field, reference_list) else urls[0]}
    version = version.strip()
    if not version:
        latest = transport.request_json("GET", f"{API}/models/{model}", headers, provider=f"Replicate {model} model", body_in_error=True).get("latest_version")
        version = (latest or {}).get("id") or ""
    if version:
        prediction = transport.request_json("POST", f"{API}/predictions", headers, {"version": version, "input": input_values}, f"Replicate {model} submit", body_in_error=True)
    else:
        prediction = transport.request_json("POST", f"{API}/models/{model}/predictions", headers, {"input": input_values}, f"Replicate {model} submit", body_in_error=True)
    waited = 0.0
    while prediction.get("status") not in ("succeeded", "failed", "canceled"):
        # Replicate says `starting` while the prediction waits for a machine, `processing` once it runs.
        if prediction.get("status") == "processing":
            stages.running()
        else:
            stages.queued()
        if waited >= DEADLINE_SECONDS:
            raise transport.ProviderError(f"Replicate {model} did not finish in {DEADLINE_SECONDS} seconds.")
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        prediction = transport.request_json("GET", transport.https_only(prediction["urls"]["get"], f"Replicate {model} status"), headers, provider=f"Replicate {model} status")
    if prediction["status"] != "succeeded":
        raise transport.ProviderError(f"Replicate {model} {prediction['status']}: {prediction.get('error') or 'no reason given'}")
    urls = output_urls(prediction.get("output"))
    if not urls:
        raise transport.ProviderError(f"Replicate {model} returned no images.")
    if report is not None:
        report.update(reported(prediction))
    files = []
    for url in urls:
        stages.downloading(len(files), len(urls))
        files.append(transport.request("GET", transport.https_only(url, f"Replicate {model} download"), provider=f"Replicate {model} download"))
    stages.downloading(len(files), len(urls))
    return files


SEED_LINE = re.compile(r"(?:using )?seed(?: used)?\s*[:=]\s*(\d+)", re.IGNORECASE)


def reported(prediction):
    """The seed a prediction's logs name, as Cog models print it (`Using seed: 42`), where they name one."""
    said = SEED_LINE.search(prediction.get("logs") or "")
    return {"seed": int(said.group(1))} if said else {}


class FathomReplicate:
    """ComfyUI caches a node whose inputs are unchanged, so a different seed in the input reruns it."""

    CATEGORY = "Fathom"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "black-forest-labs/flux-schnell"}),
                "version": ("STRING", {"default": ""}),
                "input": ("STRING", {"default": "{}", "multiline": True}),
            },
            "optional": {
                "references": ("IMAGE",),
                "reference_field": ("STRING", {"default": "input_images"}),
                "prompt": ("STRING", {"default": "", "multiline": True}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "send_seed": ("BOOLEAN", {"default": True}),
                "reference_list": ("BOOLEAN", {"default": True}),
            },
        }

    def generate(self, model, version, input, references=None, reference_field="input_images", prompt=None, seed=None, send_seed=True, reference_list=None):
        values = json.loads(input or "{}")
        if not isinstance(values, dict):
            raise ValueError("input must be a JSON object.")
        values = fal.with_prompt_and_seed(values, prompt, seed, send_seed)
        report = {}
        files = run(model, version, values, images.references(references), reference_field or "input_images", reference_list, report)
        return fal.with_report(images.to_batch(files), report)
