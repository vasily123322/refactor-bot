from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from app.domain.content.document import CLASSIC_BLOCK_TYPES, PostDocument


class LegacyPayloadError(ValueError):
    """Raised when legacy payload conversion would lose information."""


_CONTENT_FIELDS = frozenset(
    {
        "text",
        "caption",
        "caption_html",
        "file_id",
        "media",
        "media_group",
        "items",
        "entities",
        "caption_entities",
        "parse_mode",
        "media_spoiler",
        "media_pos",
        "thumbnail",
        "duration",
        "width",
        "height",
        "supports_streaming",
        "performer",
        "title",
        "mime_type",
        "file_name",
        "question",
        "options",
        "is_anonymous",
        "allows_multiple_answers",
        "correct_option_id",
        "explanation",
    }
)

_RESERVED_FIELDS = _CONTENT_FIELDS | {"type", "buttons"}


def document_from_legacy_payload(payload: Mapping[str, Any]) -> PostDocument:
    """Convert the current editor/scheduler payload into PostDocument v1.

    Known classic payloads get a structured block. Unknown current/future payload
    types are stored as opaque `legacy` blocks so migrations and knowledge/history
    views remain lossless while publication still fails closed until a renderer exists.
    Runtime/scheduling fields supplied by the caller remain in legacy_payload_extra.
    """
    if not isinstance(payload, Mapping):
        raise LegacyPayloadError("legacy payload must be an object")

    source = deepcopy(dict(payload))
    legacy_type = str(source.get("type") or "text")

    if legacy_type not in CLASSIC_BLOCK_TYPES:
        return PostDocument(
            mode="classic",
            blocks=[
                {
                    "id": "b_1",
                    "type": "legacy",
                    "legacy_type": legacy_type,
                    "payload": source,
                }
            ],
            metadata={"legacy_type": legacy_type, "opaque_legacy": True},
        )

    block: dict[str, Any] = {"id": "b_1", "type": legacy_type}
    for key in _CONTENT_FIELDS:
        if key in source:
            block[key] = deepcopy(source[key])

    telegram: dict[str, Any] = {}
    if "buttons" in source:
        telegram["buttons"] = deepcopy(source.get("buttons") or [])

    extra = {
        key: deepcopy(value)
        for key, value in source.items()
        if key not in _RESERVED_FIELDS
    }
    metadata: dict[str, Any] = {"legacy_type": legacy_type}
    if extra:
        metadata["legacy_payload_extra"] = extra

    return PostDocument(
        mode="classic",
        blocks=[block],
        telegram=telegram,
        metadata=metadata,
    )


def legacy_payload_from_document(document: PostDocument | Mapping[str, Any]) -> dict[str, Any]:
    """Render a classic PostDocument back to the current PostTask payload shape.

    This adapter intentionally rejects rich/multi-block/opaque documents until a
    dedicated Telegram renderer exists. Failing loudly is safer than silently
    dropping blocks during the compatibility phase.
    """
    doc = document if isinstance(document, PostDocument) else PostDocument.from_dict(document)
    doc.validate()

    if doc.mode != "classic":
        raise LegacyPayloadError("rich PostDocument requires the new Telegram renderer")
    if len(doc.blocks) != 1:
        raise LegacyPayloadError(
            "legacy payload renderer supports exactly one classic content block"
        )

    block = deepcopy(doc.blocks[0])
    block_type = block.get("type")
    if block_type == "legacy":
        raise LegacyPayloadError(
            f"opaque legacy payload type {block.get('legacy_type')!r} requires a renderer"
        )
    if block_type not in CLASSIC_BLOCK_TYPES:
        raise LegacyPayloadError(f"unsupported classic block type: {block_type!r}")

    extra = doc.metadata.get("legacy_payload_extra") or {}
    if not isinstance(extra, Mapping):
        raise LegacyPayloadError("metadata.legacy_payload_extra must be an object")

    payload: dict[str, Any] = deepcopy(dict(extra))
    payload["type"] = str(doc.metadata.get("legacy_type") or block_type)

    for key in _CONTENT_FIELDS:
        if key in block:
            payload[key] = deepcopy(block[key])

    if "buttons" in doc.telegram:
        payload["buttons"] = deepcopy(doc.telegram.get("buttons") or [])

    return payload
