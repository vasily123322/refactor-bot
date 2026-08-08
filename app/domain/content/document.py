from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping


POST_DOCUMENT_SCHEMA_VERSION = 1

CLASSIC_BLOCK_TYPES = frozenset(
    {
        "text",
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "video_note",
        "document",
        "album",
        "poll",
    }
)

RICH_BLOCK_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "quote",
        "pull_quote",
        "list",
        "table",
        "details",
        "divider",
        "image",
        "media",
        "gallery",
        "collage",
        "slideshow",
        "map",
        "math",
        "anchor",
        "cta",
    }
)

SUPPORTED_BLOCK_TYPES = CLASSIC_BLOCK_TYPES | RICH_BLOCK_TYPES
SUPPORTED_MODES = frozenset({"classic", "rich"})


class PostDocumentError(ValueError):
    """Raised when a PostDocument cannot be validated safely."""


def _copy_mapping(value: Mapping[str, Any] | None, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PostDocumentError(f"{field_name} must be an object")
    return deepcopy(dict(value))


@dataclass(slots=True)
class PostDocument:
    """Versioned editor-neutral representation of Telegram content.

    The document intentionally does not contain scheduling/publication runtime state.
    Editor implementations (Tiptap, inline Telegram editor, future clients) should
    adapt to this structure instead of persisting their native editor JSON.
    """

    blocks: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "classic"
    telegram: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = POST_DOCUMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.blocks = deepcopy(list(self.blocks))
        self.telegram = _copy_mapping(self.telegram, field_name="telegram")
        self.metadata = _copy_mapping(self.metadata, field_name="metadata")
        self.validate()

    @classmethod
    def empty(cls, *, mode: str = "classic") -> "PostDocument":
        return cls(blocks=[], mode=mode)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PostDocument":
        if not isinstance(value, Mapping):
            raise PostDocumentError("document must be an object")
        return cls(
            schema_version=value.get("schema_version", POST_DOCUMENT_SCHEMA_VERSION),
            mode=value.get("mode", "classic"),
            blocks=value.get("blocks") or [],
            telegram=value.get("telegram") or {},
            metadata=value.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "blocks": deepcopy(self.blocks),
            "telegram": deepcopy(self.telegram),
            "metadata": deepcopy(self.metadata),
        }

    def clone(self) -> "PostDocument":
        return PostDocument.from_dict(self.to_dict())

    def validate(self) -> None:
        if type(self.schema_version) is not int:
            raise PostDocumentError("schema_version must be an integer")
        if self.schema_version != POST_DOCUMENT_SCHEMA_VERSION:
            raise PostDocumentError(
                f"unsupported PostDocument schema_version={self.schema_version}"
            )
        if self.mode not in SUPPORTED_MODES:
            raise PostDocumentError(f"unsupported document mode: {self.mode!r}")
        if not isinstance(self.blocks, list):
            raise PostDocumentError("blocks must be an array")
        if not isinstance(self.telegram, dict):
            raise PostDocumentError("telegram must be an object")
        if not isinstance(self.metadata, dict):
            raise PostDocumentError("metadata must be an object")

        seen_ids: set[str] = set()
        for index, block in enumerate(self.blocks):
            if not isinstance(block, dict):
                raise PostDocumentError(f"block #{index + 1} must be an object")
            block_id = block.get("id")
            block_type = block.get("type")
            if not isinstance(block_id, str) or not block_id.strip():
                raise PostDocumentError(f"block #{index + 1} has no stable id")
            if block_id in seen_ids:
                raise PostDocumentError(f"duplicate block id: {block_id}")
            seen_ids.add(block_id)
            if block_type not in SUPPORTED_BLOCK_TYPES:
                raise PostDocumentError(
                    f"unsupported block type in schema v{self.schema_version}: {block_type!r}"
                )

            if block_type == "text" and "text" in block and not isinstance(
                block.get("text"), str
            ):
                raise PostDocumentError(f"text block {block_id} has non-string text")
            if block_type in {"paragraph", "heading", "quote", "pull_quote"}:
                content = block.get("content", [])
                if not isinstance(content, (str, list)):
                    raise PostDocumentError(
                        f"rich text block {block_id} content must be text or an array"
                    )

        buttons = self.telegram.get("buttons")
        if buttons is not None and not isinstance(buttons, list):
            raise PostDocumentError("telegram.buttons must be an array")

    def primary_text(self) -> str:
        """Return best-effort human text without Telegram rendering side effects."""
        parts: list[str] = []
        for block in self.blocks:
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text") or ""
            elif block_type in CLASSIC_BLOCK_TYPES:
                text = block.get("caption") or ""
            else:
                content = block.get("content") or ""
                if isinstance(content, list):
                    text = "".join(
                        str(item.get("text") or "")
                        if isinstance(item, dict)
                        else str(item)
                        for item in content
                    )
                else:
                    text = str(content)
            if text:
                parts.append(str(text))
        return "\n\n".join(parts)
