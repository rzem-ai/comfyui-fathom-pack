"""Plain urllib requests over https only, so the pack adds no dependency to ComfyUI."""

import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 120


class ProviderError(RuntimeError):
    pass


def https_only(url, provider="the provider"):
    """A URL taken from a provider's reply is fetched only over https; the error names the scheme, never the reply."""
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme.lower() != "https":
        raise ProviderError(f"{provider} gave a {scheme or 'schemeless'} URL; only https is fetched.")
    return url


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to https, so a downgrade never carries a key over plain http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_only(newurl, "a redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener():
    # Built by hand rather than with build_opener, so it has no file, ftp, data or plain http handler.
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.HTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(), _HttpsRedirects()):
        opener.add_handler(handler)
    return opener


_opener = _build_opener()


def request(method, url, headers=None, body=None, provider="the provider", body_in_error=False):
    """Send a request and return the response bytes. A failure raises ProviderError naming the provider, its step and the
    status; only a submit, whose reply is the provider's own validation of the inputs, adds a short excerpt of its body."""
    https_only(url, provider)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=TIMEOUT) as response:
            return response.read()
    except ProviderError:
        raise
    except urllib.error.HTTPError as error:
        detail = f": {error.read().decode('utf-8', 'replace')[:500]}" if body_in_error else "."
        raise ProviderError(f"{provider} answered {error.code}{detail}") from None
    except urllib.error.URLError as error:
        raise ProviderError(f"{provider} could not be reached: {error.reason}") from None


def request_json(method, url, headers=None, payload=None, provider="the provider", body_in_error=False):
    headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    data = request(method, url, headers, body, provider, body_in_error)
    return json.loads(data) if data else {}
