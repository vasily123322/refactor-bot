from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import MediaAsset


class RichMediaAssetError(RuntimeError):
    pass


_ASSET_ID_FIELDS = ("media_asset_id", "asset_id")
_MEDIA_KIND_ALIASES = {
    "image": "photo",
    "photo": "photo",
    "video": "video",
    "animation": "animation",
    "audio": "audio",
    "voice": "voice_note",
    "voice_note": "voice_note",
}


def _asset_id(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RichMediaAssetError("media asset id must be a positive integer") from exc
    if parsed <= 0:
        raise RichMediaAssetError("media asset id must be a positive integer")
    return parsed


def _normalized_kind(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    return _MEDIA_KIND_ALIASES.get(raw, raw)


def _walk_mappings(value: object):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _referenced_asset_ids(document: PostDocument) -> set[int]:
    ids: set[int] = set()
    for block in document.blocks:
        for node in _walk_mappings(block):
            for field in _ASSET_ID_FIELDS:
                if node.get(field) is not None:
                    ids.add(_asset_id(node[field]))
                    break
    return ids


def _transport_reference(asset: MediaAsset) -> tuple[str, str]:
    file_id = str(asset.telegram_file_id or "").strip()
    if file_id:
        return "telegram_file_id", file_id
    storage_url = str(asset.storage_url or "").strip()
    if storage_url:
        return "storage_url", storage_url
    raise RichMediaAssetError("media asset has no transport reference")


def _apply_asset(node: dict[str, Any], asset: MediaAsset) -> None:
    block_type = str(node.get("type") or "").strip().lower()
    asset_kind = _normalized_kind(asset.kind)
    explicit_kind = _normalized_kind(node.get("kind") or node.get("media_type"))

    if block_type == "image":
        expected_kind = "photo"
        if asset_kind not in {None, expected_kind}:
            raise RichMediaAssetError("media asset kind does not match document block")
    elif block_type == "media":
        expected_kind = explicit_kind or asset_kind
        if expected_kind is None:
            raise RichMediaAssetError("media asset kind is required")
        if asset_kind is not None and expected_kind != asset_kind:
            raise RichMediaAssetError("media asset kind does not match document block")
        node["kind"] = expected_kind
    elif explicit_kind is not None and asset_kind is not None and explicit_kind != asset_kind:
        raise RichMediaAssetError("media asset kind does not match document block")

    reference_field, reference = _transport_reference(asset)
    node[reference_field] = reference
    for field in _ASSET_ID_FIELDS:
        node.pop(field, None)

    if node.get("width") is None and asset.width is not None:
        node["width"] = int(asset.width)
    if node.get("height") is None and asset.height is not None:
        node["height"] = int(asset.height)
    if node.get("duration") is None and asset.duration_seconds is not None:
        node["duration"] = int(asset.duration_seconds)


class RichMediaAssetResolver:
    """Resolve editor asset IDs to transport-safe media references within one channel."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve(
        self,
        document: PostDocument,
        *,
        channel_id: int,
    ) -> PostDocument:
        asset_ids = _referenced_asset_ids(document)
        if not asset_ids:
            return PostDocument.from_dict(document.to_dict())

        rows = list(
            (
                await self.session.execute(
                    select(MediaAsset).where(
                        MediaAsset.channel_id == int(channel_id),
                        MediaAsset.id.in_(sorted(asset_ids)),
                    )
                )
            ).scalars().all()
        )
        assets = {int(row.id): row for row in rows}
        if set(assets) != asset_ids:
            # Do not reveal whether an omitted ID exists under another channel.
            raise RichMediaAssetError("media asset not found")

        raw = document.to_dict()
        for block in raw.get("blocks", []):
            for node in _walk_mappings(block):
                if not isinstance(node, dict):
                    continue
                asset_value = None
                for field in _ASSET_ID_FIELDS:
                    if node.get(field) is not None:
                        asset_value = node[field]
                        break
                if asset_value is None:
                    continue
                asset = assets[_asset_id(asset_value)]
                _apply_asset(node, asset)

        return PostDocument.from_dict(raw)
