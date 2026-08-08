from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockAnchor,
    InputRichBlockBlockQuotation,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockMathematicalExpression,
    InputRichBlockParagraph,
    InputRichBlockPullQuotation,
    InputRichBlockSectionHeading,
    InputRichMessage,
    RichTextBold,
    RichTextCode,
    RichTextItalic,
    RichTextStrikethrough,
    RichTextUnderline,
    RichTextUrl,
)

from app.domain.content import PostDocument
from app.services.content import LegacyPayloadError, legacy_payload_from_document


class TelegramRenderError(ValueError):
    """Raised when a PostDocument cannot be rendered without data loss."""


@dataclass(slots=True)
class TelegramRenderPlan:
    kind: Literal["classic", "rich"]
    classic_payload: dict[str, Any] | None = None
    rich_message: InputRichMessage | None = None
    reply_markup: InlineKeyboardMarkup | None = None
    disable_notification: bool = False
    protect_content: bool = False


_RICH_STRUCTURAL_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "divider",
        "quote",
        "pull_quote",
        "list",
        "details",
        "math",
        "anchor",
    }
)

_RICH_MEDIA_TYPES = frozenset(
    {
        "image",
        "media",
        "gallery",
        "collage",
        "slideshow",
        "map",
    }
)


def _button_markup(value: object) -> InlineKeyboardMarkup | None:
    if not isinstance(value, list) or not value:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for raw_row in value:
        if not isinstance(raw_row, list):
            raise TelegramRenderError("telegram.buttons rows must be arrays")
        row: list[InlineKeyboardButton] = []
        for raw_button in raw_row:
            if not isinstance(raw_button, Mapping):
                raise TelegramRenderError("telegram.buttons entries must be objects")
            text = str(raw_button.get("text") or "").strip()
            if not text:
                raise TelegramRenderError("telegram button text is required")
            url = raw_button.get("url")
            callback_data = raw_button.get("callback_data")
            if url:
                row.append(InlineKeyboardButton(text=text, url=str(url)))
            elif callback_data:
                row.append(
                    InlineKeyboardButton(text=text, callback_data=str(callback_data))
                )
            else:
                raise TelegramRenderError(
                    "telegram button requires url or callback_data"
                )
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _plain_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def _apply_marks(text: object, marks: object) -> object:
    rendered: object = _plain_text(text)
    if not isinstance(marks, list):
        return rendered
    for raw_mark in marks:
        mark_type: str
        attrs: Mapping[str, Any]
        if isinstance(raw_mark, str):
            mark_type = raw_mark
            attrs = {}
        elif isinstance(raw_mark, Mapping):
            mark_type = str(raw_mark.get("type") or "")
            attrs = raw_mark
        else:
            continue

        if mark_type == "bold":
            rendered = RichTextBold(text=rendered)
        elif mark_type == "italic":
            rendered = RichTextItalic(text=rendered)
        elif mark_type == "underline":
            rendered = RichTextUnderline(text=rendered)
        elif mark_type in {"strike", "strikethrough"}:
            rendered = RichTextStrikethrough(text=rendered)
        elif mark_type == "code":
            rendered = RichTextCode(text=rendered)
        elif mark_type in {"link", "url"}:
            url = str(attrs.get("url") or attrs.get("href") or "").strip()
            if not url:
                raise TelegramRenderError("rich text link mark requires a URL")
            rendered = RichTextUrl(text=rendered, url=url)
        elif mark_type:
            raise TelegramRenderError(f"unsupported rich text mark: {mark_type!r}")
    return rendered


def _rich_text(value: object) -> object:
    if isinstance(value, str) or value is None:
        return _plain_text(value)
    if not isinstance(value, list):
        return _plain_text(value)

    parts: list[object] = []
    for item in value:
        if isinstance(item, Mapping):
            parts.append(_apply_marks(item.get("text"), item.get("marks")))
        else:
            parts.append(str(item))
    return parts


def _positive_size(value: object, *, field: str, default: int) -> int:
    if value is None:
        return default
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise TelegramRenderError(f"{field} must be an integer from 1 to 6") from exc
    if not 1 <= size <= 6:
        raise TelegramRenderError(f"{field} must be from 1 to 6")
    return size


def _paragraph_for(value: object) -> InputRichBlockParagraph:
    return InputRichBlockParagraph(text=_rich_text(value))


def _list_item(raw: object) -> InputRichBlockListItem:
    if isinstance(raw, Mapping):
        label = raw.get("label")
        content = raw.get("content", raw.get("text", ""))
    else:
        label = None
        content = raw
    return InputRichBlockListItem(
        blocks=[_paragraph_for(content)],
        label=(str(label) if label is not None else None),
    )


def _render_rich_block(block: Mapping[str, Any]) -> object:
    block_type = str(block.get("type") or "")
    if block_type not in _RICH_STRUCTURAL_TYPES:
        if block_type in _RICH_MEDIA_TYPES:
            raise TelegramRenderError(
                f"rich media block {block_type!r} requires a file-attachment renderer"
            )
        raise TelegramRenderError(f"unsupported rich block type: {block_type!r}")

    if block_type == "paragraph":
        return InputRichBlockParagraph(text=_rich_text(block.get("content")))
    if block_type == "heading":
        return InputRichBlockSectionHeading(
            text=_rich_text(block.get("content")),
            size=_positive_size(block.get("size"), field="heading.size", default=2),
        )
    if block_type == "divider":
        return InputRichBlockDivider()
    if block_type == "quote":
        content_blocks = block.get("blocks")
        if isinstance(content_blocks, list) and content_blocks:
            rendered_blocks = [
                _render_rich_block(child)
                for child in content_blocks
                if isinstance(child, Mapping)
            ]
            if len(rendered_blocks) != len(content_blocks):
                raise TelegramRenderError("quote.blocks must contain block objects")
        else:
            rendered_blocks = [_paragraph_for(block.get("content"))]
        credit = block.get("credit")
        return InputRichBlockBlockQuotation(
            blocks=rendered_blocks,
            credit=(_rich_text(credit) if credit is not None else None),
        )
    if block_type == "pull_quote":
        credit = block.get("credit")
        return InputRichBlockPullQuotation(
            text=_rich_text(block.get("content")),
            credit=(_rich_text(credit) if credit is not None else None),
        )
    if block_type == "list":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise TelegramRenderError("rich list requires non-empty items")
        return InputRichBlockList(items=[_list_item(item) for item in items])
    if block_type == "details":
        summary = block.get("summary")
        if summary is None:
            raise TelegramRenderError("rich details requires summary")
        children = block.get("blocks")
        if isinstance(children, list) and children:
            rendered_children = [
                _render_rich_block(child)
                for child in children
                if isinstance(child, Mapping)
            ]
            if len(rendered_children) != len(children):
                raise TelegramRenderError("details.blocks must contain block objects")
        else:
            rendered_children = [_paragraph_for(block.get("content"))]
        return InputRichBlockDetails(
            summary=_rich_text(summary),
            blocks=rendered_children,
            is_open=(bool(block.get("is_open")) if "is_open" in block else None),
        )
    if block_type == "math":
        formula = str(block.get("formula") or block.get("content") or "").strip()
        if not formula:
            raise TelegramRenderError("rich math block requires formula")
        return InputRichBlockMathematicalExpression(
            formula=formula,
            size=_positive_size(block.get("size"), field="math.size", default=1),
        )
    if block_type == "anchor":
        name = str(block.get("name") or "").strip()
        if not name:
            raise TelegramRenderError("rich anchor block requires name")
        return InputRichBlockAnchor(name=name)

    raise TelegramRenderError(f"unhandled rich block type: {block_type!r}")


def _telegram_bool(document: PostDocument, *names: str) -> bool:
    for name in names:
        if name in document.telegram:
            return bool(document.telegram.get(name))
    return False


class TelegramRenderer:
    """Compile PostDocument into a transport-edge Telegram render plan."""

    def render(self, document: PostDocument | Mapping[str, Any]) -> TelegramRenderPlan:
        doc = (
            document
            if isinstance(document, PostDocument)
            else PostDocument.from_dict(document)
        )
        doc.validate()
        reply_markup = _button_markup(doc.telegram.get("buttons"))
        disable_notification = _telegram_bool(doc, "silent", "disable_notification")
        protect_content = _telegram_bool(doc, "protect_content")

        if doc.mode == "classic":
            try:
                payload = legacy_payload_from_document(doc)
            except LegacyPayloadError as exc:
                raise TelegramRenderError(str(exc)) from exc
            return TelegramRenderPlan(
                kind="classic",
                classic_payload=payload,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                protect_content=protect_content,
            )

        if doc.mode != "rich":
            raise TelegramRenderError(f"unsupported document mode: {doc.mode!r}")
        if not doc.blocks:
            raise TelegramRenderError("rich document requires at least one block")

        blocks = [_render_rich_block(block) for block in doc.blocks]
        return TelegramRenderPlan(
            kind="rich",
            rich_message=InputRichMessage(blocks=blocks),
            reply_markup=reply_markup,
            disable_notification=disable_notification,
            protect_content=protect_content,
        )
