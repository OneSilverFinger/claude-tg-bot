"""Voice transcription via any OpenAI-compatible /audio/transcriptions endpoint.

Works with Groq, OpenAI, or a local Whisper server — only the base URL, key and
model differ. Telegram voice notes are OGG/Opus and video notes are MP4; both
formats are accepted by Whisper directly, so no ffmpeg/transcoding is needed.
"""

import logging
import os

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=120)


async def transcribe(audio: bytes, filename: str, *,
                     base_url: str, api_key: str, model: str,
                     language: str | None = None) -> str:
    """Send audio bytes to the STT endpoint and return the recognized text.

    Raises RuntimeError on a non-200 response.
    """
    url = base_url.rstrip("/") + "/audio/transcriptions"
    # The bot reaches the internet through the same proxy as the Telegram API.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    form = aiohttp.FormData()
    form.add_field("file", audio, filename=filename,
                   content_type="application/octet-stream")
    form.add_field("model", model)
    form.add_field("response_format", "json")
    if language:
        form.add_field("language", language)

    headers = {"Authorization": f"Bearer {api_key}"}

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(url, data=form, headers=headers, proxy=proxy) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise RuntimeError(f"STT {resp.status}: {body}")
            data = await resp.json()

    return (data.get("text") or "").strip()
