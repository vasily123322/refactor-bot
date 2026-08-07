from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MEDIA_PAYLOAD_TYPES = {"photo", "video", "animation", "audio", "voice", "album"}


def apply_generated_text_to_payload(payload: Mapping[str, Any] | None, generated_text: str | None) -> dict[str, Any]:
    """Return a copied post payload with generated text applied to text/caption."""

    clean_text = (generated_text or "").strip()
    next_payload = dict(payload or {})
    payload_type = next_payload.get("type")

    if not next_payload:
        return {"type": "text", "text": clean_text}
    if payload_type == "text":
        next_payload["text"] = clean_text
        return next_payload
    if payload_type in MEDIA_PAYLOAD_TYPES:
        next_payload["caption"] = clean_text
        return next_payload

    next_payload["type"] = "text"
    next_payload["text"] = clean_text
    return next_payload


def extract_payload_text(payload: Mapping[str, Any] | None) -> str:
    """Return editable text/caption from a post payload."""

    if not isinstance(payload, Mapping):
        return ""
    payload_type = payload.get("type")
    if payload_type == "text":
        return str(payload.get("text") or "").strip()
    if payload_type in MEDIA_PAYLOAD_TYPES:
        return str(payload.get("caption") or "").strip()
    return ""
