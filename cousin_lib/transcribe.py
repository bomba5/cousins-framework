"""Speech to text through the [transcribe] provider in config/media.toml.

Off until configured, like every media kind: no section, no url, no
request. The provider is any server that takes the OpenAI
transcription request, a multipart POST with `file`, `model`,
`language` and `response_format=json`, and answers `{"text": ...}`:
OpenAI's /v1/audio/transcriptions, or whisper.cpp's whisper-server
(/inference, run with --convert so it takes Telegram's ogg), or a
local shim in front of a speech-to-text engine. The audio
goes to that url and nowhere else; a plain-text answer is taken as the
text.
"""
import json
import urllib.error
import urllib.request
import uuid

from cousin_lib.media import MediaError, NoProviderConfigured, _read_key, load_provider

KIND = "transcribe"
_DEFAULT_TIMEOUT_S = 300


def _multipart(fields, filename, payload):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, value)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n"
                  % (boundary, filename)).encode())
    parts.append(payload)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


def _text_of(body):
    """The transcript in a provider answer: the JSON `text`, else the
    body itself."""
    raw = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"].strip()
    raise MediaError("the transcription answer has no text field")


def transcribe(payload, filename):
    """The text spoken in `payload` (audio bytes; `filename` carries the
    format, e.g. voice/file_7.oga). Raises NoProviderConfigured when
    [transcribe] is off and MediaError when the provider fails."""
    provider = load_provider(KIND)
    if provider is None:
        raise NoProviderConfigured(
            "no transcribe provider configured; declare [transcribe] in"
            " config/media.toml to turn voice into text")
    fields = {"response_format": "json"}
    if provider.get("model"):
        fields["model"] = provider["model"]
    if provider.get("language"):
        fields["language"] = provider["language"]
    if provider.get("prompt"):
        fields["prompt"] = provider["prompt"]
    body, content_type = _multipart(fields, filename.rsplit("/", 1)[-1] or "audio", payload)
    request = urllib.request.Request(provider["url"], data=body,
                                     headers={"Content-Type": content_type})
    key = _read_key(provider)
    if key:
        request.add_header("Authorization", "Bearer %s" % key)
    timeout = provider.get("timeout_s") or _DEFAULT_TIMEOUT_S
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            answer = resp.read()
    except urllib.error.HTTPError as err:
        with err:
            detail = err.read(300).decode("utf-8", "replace").strip()
        raise MediaError("transcribe provider %s failed: HTTP %d %s"
                         % (provider["url"], err.code, detail))
    except (urllib.error.URLError, OSError) as err:
        raise MediaError("transcribe provider %s failed: %s" % (provider["url"], err))
    return _text_of(answer)
