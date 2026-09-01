"""
services/streaming_stt_service.py
Proxy the app's browser-facing WebSocket protocol to a standalone WhisperLive
server while keeping the existing frontend contract stable.

Browser protocol preserved:
  - handshake: {"uid":"...","language":"en","use_vad":false}
  - audio: Float32 PCM bytes @ 16 kHz mono
  - stop: {"type":"end"}
  - server ready: {"uid":"...","message":"SERVER_READY","backend":"whisper_live"}
  - live transcript: {"uid":"...","segments":[{"text":"..."}]}
  - final transcript: {"type":"done","final_transcript":"..."}

WhisperLive protocol adapted:
  - expects JSON options first, then binary Float32 PCM
  - stop sentinel is binary b"END_OF_AUDIO"
  - streams segment arrays rather than a single running transcript
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from urllib.parse import urlparse, urlunparse

from fastapi import WebSocket
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from config import settings

logger = logging.getLogger(__name__)


def _json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _coalesce_segments(segments: list[dict]) -> str:
    return " ".join(
        str(segment.get("text", "")).strip()
        for segment in segments
        if str(segment.get("text", "")).strip()
    ).strip()


def _normalize_ws_url(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    if not parsed.scheme:
        parsed = parsed._replace(scheme='ws')
    elif parsed.scheme == 'http':
        parsed = parsed._replace(scheme='ws')
    elif parsed.scheme == 'https':
        parsed = parsed._replace(scheme='wss')
    elif parsed.scheme not in ('ws', 'wss'):
        raise ValueError(f"Invalid whisperlive_ws_url scheme: {parsed.scheme}")
    return urlunparse(parsed)


async def handle_streaming_session(websocket: WebSocket, session_id: str) -> None:
    """
    Bridge one browser WebSocket session to the external WhisperLive server.

    The browser keeps speaking the app's existing protocol; this proxy translates
    messages to and from WhisperLive so the frontend can stay largely unchanged.
    """
    logger.info("[whisperlive-proxy] %s: New session started", session_id)
    
    try:
        raw = await asyncio.wait_for(websocket.receive(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("[whisperlive-proxy] %s: handshake timeout", session_id)
        await websocket.close(code=1008)
        return

    logger.info("[whisperlive-proxy] %s: Received handshake: %s", session_id, raw)
    
    browser_config: dict = {}
    if raw.get("text"):
        try:
            browser_config = json.loads(raw["text"])
        except json.JSONDecodeError:
            logger.warning("[whisperlive-proxy] %s: invalid browser handshake", session_id)

    client_uid = str(browser_config.get("uid") or session_id)
    language = str(browser_config.get("language") or settings.whisperlive_language)
    # Use backend config for VAD, ignore frontend override
    use_vad = settings.whisperlive_use_vad
    upstream_config = {
        "uid": client_uid,
        "language": language,
        "task": "transcribe",
        "model": settings.whisperlive_model,
        "use_vad": use_vad,
        "send_last_n_segments": 10,
        # Lower threshold so short answers commit text without needing 8 repeats.
        "same_output_threshold": 3,
        # Less aggressive no-speech filtering (default 0.45 drops too much).
        "no_speech_thresh": 0.8,
        # Prevent unbounded audio accumulation across long pauses.
        "clip_audio": True,
    }

    logger.info("[whisperlive-proxy] %s: model=%s, use_vad=%s", session_id, settings.whisperlive_model, use_vad)

    latest_transcript = ""
    best_transcript = ""   # longest/best transcript seen during the session
    ready_sent = False
    end_requested = False

    try:
        upstream_url = _normalize_ws_url(settings.whisperlive_ws_url)
        logger.debug("[whisperlive-proxy] %s: connecting to upstream %s", session_id, upstream_url)
        async with connect(upstream_url, open_timeout=10, max_size=None) as upstream:
            await upstream.send(_json(upstream_config))

            async def relay_upstream_to_browser() -> None:
                nonlocal latest_transcript, best_transcript, ready_sent

                try:
                    async for upstream_message in upstream:
                        logger.info("[whisperlive-proxy] %s: upstream message: %s", session_id, str(upstream_message)[:200])
                        
                        if isinstance(upstream_message, bytes):
                            continue

                        try:
                            payload = json.loads(upstream_message)
                        except json.JSONDecodeError:
                            continue

                        logger.info("[whisperlive-proxy] %s: parsed payload keys: %s", session_id, payload.keys())

                        if payload.get("message") == "SERVER_READY":
                            ready_sent = True
                            logger.info("[whisperlive-proxy] %s: Sending SERVER_READY to browser", session_id)
                            await websocket.send_text(_json({
                                "uid": client_uid,
                                "message": "SERVER_READY",
                                "backend": "whisper_live",
                            }))
                            continue

                        if isinstance(payload.get("segments"), list):
                            latest_transcript = _coalesce_segments(payload["segments"])
                            # Keep the longest transcript seen so far as a safety net;
                            # the race in WhisperLive cleanup can drop the very last update.
                            if len(latest_transcript) > len(best_transcript):
                                best_transcript = latest_transcript
                            logger.info("[whisperlive-proxy] %s: Sending segments to browser: %s", session_id, latest_transcript[:100])
                            await websocket.send_text(_json({
                                "uid": payload.get("uid", client_uid),
                                "segments": [{
                                    "start": payload["segments"][0].get("start", "0.000") if payload["segments"] else "0.000",
                                    "end": payload["segments"][-1].get("end", "0.000") if payload["segments"] else "0.000",
                                    "text": latest_transcript,
                                    "completed": False,
                                }],
                            }))
                            continue

                        await websocket.send_text(_json(payload))
                except ConnectionClosed:
                    logger.info("[whisperlive-proxy] %s: upstream socket closed", session_id)

            upstream_task = asyncio.create_task(relay_upstream_to_browser())

            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break

                    if message.get("text") is not None:
                        try:
                            payload = json.loads(message["text"])
                        except json.JSONDecodeError:
                            continue

                        if payload.get("type") == "end":
                            end_requested = True
                            await upstream.send(b"END_OF_AUDIO")
                            break
                        continue

                    if message.get("bytes"):
                        # print(f"[PROXY] session={session_id} received audio chunk, size={len(message['bytes'])}")
                        await upstream.send(message["bytes"])
            finally:
                if end_requested:
                    try:
                        # Increased to 8 s: the transcription thread join (4 s) in
                        # WhisperLive's cleanup runs before websocket.close(), so the
                        # final segments usually arrive within a few seconds.
                        await asyncio.wait_for(upstream_task, timeout=8.0)
                    except asyncio.TimeoutError:
                        logger.warning("[whisperlive-proxy] %s: timed out waiting for final upstream segments", session_id)
                if not upstream_task.done():
                    upstream_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await upstream_task

            if end_requested:
                # Use latest_transcript if available, otherwise fall back to the
                # best (longest) transcript seen earlier in the session.
                final = latest_transcript or best_transcript
                await websocket.send_text(_json({
                    "type": "done",
                    "final_transcript": final,
                }))

    except Exception as exc:
        logger.exception("[whisperlive-proxy] %s: proxy failure: %s", session_id, exc)
        if not ready_sent:
            with suppress(Exception):
                await websocket.send_text(_json({
                    "type": "error",
                    "message": "WhisperLive is unavailable.",
                }))
        with suppress(Exception):
            await websocket.close(code=1011)
