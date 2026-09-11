from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.candidate_drafts import structured_candidate_draft_document
from app.services.candidate_rewrite import RewriteInput
from app.services.candidate_structured_rewrite_ai import (
    STRUCTURED_REWRITE_GENERATION_KIND,
    ChannelAIStructuredRewriteProvider,
    StructuredRewriteDocumentError,
    parse_structured_rewrite_document,
    structured_document_from_run_output,
    structured_rewrite_document_text,
)


def _payload(**overrides):
    value = {
        "schema_version": 1,
        "mode": "rich",
        "blocks": [
            {"id": "h", "type": "heading", "size": 2, "content": "Title"},
            {
                "id": "p",
                "type": "paragraph",
                "content": [{"text": "Body", "marks": ["bold"]}],
            },
            {
                "id": "d",
                "type": "details",
                "summary": "More",
                "blocks": [{"id": "dp", "type": "paragraph", "content": "Nested"}],
            },
        ],
        "telegram": {},
        "metadata": {"model_supplied": "discard"},
    }
    value.update(overrides)
    return value


def test_structured_rewrite_parser_returns_controlled_native_post_document() -> None:
    document = parse_structured_rewrite_document(json.dumps(_payload()))
    assert document.mode == "rich"
    assert [block["type"] for block in document.blocks] == ["heading", "paragraph", "details"]
    assert document.telegram == {}
    assert document.metadata == {"ai_generation_kind": STRUCTURED_REWRITE_GENERATION_KIND}
    assert structured_rewrite_document_text(document) == "Title\n\nBody\n\nMore\nNested"


@pytest.mark.parametrize("block_type", ["table", "cta", "media", "map", "anchor", "math"])
def test_structured_rewrite_rejects_capability_outside_ai_authoring_subset(block_type: str) -> None:
    with pytest.raises(StructuredRewriteDocumentError, match="not authorable"):
        parse_structured_rewrite_document(
            {
                "schema_version": 1,
                "mode": "rich",
                "blocks": [{"id": "x", "type": block_type, "content": "x"}],
                "telegram": {},
                "metadata": {},
            }
        )


def test_structured_rewrite_rejects_transport_authority_and_non_json_wrapper() -> None:
    with pytest.raises(StructuredRewriteDocumentError, match="Telegram delivery options"):
        parse_structured_rewrite_document(
            {
                **_payload(),
                "telegram": {"silent": True},
            }
        )
    with pytest.raises(StructuredRewriteDocumentError, match="invalid JSON"):
        parse_structured_rewrite_document("```json\n{}\n```")


def test_structured_rewrite_rejects_excessive_nested_depth() -> None:
    too_deep = {
        "id": "q0",
        "type": "quote",
        "blocks": [
            {
                "id": "q1",
                "type": "quote",
                "blocks": [
                    {
                        "id": "q2",
                        "type": "details",
                        "summary": "deep",
                        "blocks": [{"id": "p3", "type": "paragraph", "content": "too deep"}],
                    }
                ],
            }
        ],
    }
    with pytest.raises(StructuredRewriteDocumentError, match="nesting exceeds"):
        parse_structured_rewrite_document(
            {
                "schema_version": 1,
                "mode": "rich",
                "blocks": [too_deep],
                "telegram": {},
                "metadata": {},
            }
        )


def test_structured_rewrite_rejects_duplicate_nested_ids_and_ai_links() -> None:
    with pytest.raises(StructuredRewriteDocumentError, match="duplicate block id"):
        parse_structured_rewrite_document(
            {
                "schema_version": 1,
                "mode": "rich",
                "blocks": [
                    {
                        "id": "q",
                        "type": "quote",
                        "blocks": [{"id": "q", "type": "paragraph", "content": "duplicate"}],
                    }
                ],
                "telegram": {},
                "metadata": {},
            }
        )

    with pytest.raises(StructuredRewriteDocumentError, match="mark is not authorable.*link"):
        parse_structured_rewrite_document(
            {
                "schema_version": 1,
                "mode": "rich",
                "blocks": [
                    {
                        "id": "p",
                        "type": "paragraph",
                        "content": [
                            {
                                "text": "invented link",
                                "marks": [{"type": "link", "url": "https://example.com"}],
                            }
                        ],
                    }
                ],
                "telegram": {},
                "metadata": {},
            }
        )


def test_structured_rewrite_must_also_pass_renderer_field_validation() -> None:
    with pytest.raises(StructuredRewriteDocumentError, match="renderer-valid.*rich list requires"):
        parse_structured_rewrite_document(
            {
                "schema_version": 1,
                "mode": "rich",
                "blocks": [{"id": "l", "type": "list", "items": []}],
                "telegram": {},
                "metadata": {},
            }
        )


class _Prepared:
    model = "fake-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        return self.response


def test_structured_provider_persists_document_in_rewrite_output_metadata() -> None:
    async def run() -> None:
        prepared = _Prepared(json.dumps(_payload()))
        provider = ChannelAIStructuredRewriteProvider(prepared)  # type: ignore[arg-type]
        output = await provider.rewrite(
            RewriteInput(
                candidate_id=1,
                source_document_id=2,
                title="Source",
                source_url="https://source.example/item",
                text="Untrusted source body",
                reuse_policy="rewrite_with_attribution",
            )
        )
        assert output.text == "Title\n\nBody\n\nMore\nNested"
        assert output.metadata is not None
        assert output.metadata["generation_kind"] == STRUCTURED_REWRITE_GENERATION_KIND
        document = structured_document_from_run_output(output.metadata)
        assert document is not None
        assert document.mode == "rich"
        assert prepared.calls
        assert "untrusted" in prepared.calls[0]["system_prompt"].lower()
        assert "Return only the JSON object" in prepared.calls[0]["system_prompt"]

    asyncio.run(run())


def test_explicit_draft_apply_adds_attribution_and_authoritative_provenance() -> None:
    generated = parse_structured_rewrite_document(_payload())
    source = SimpleNamespace(
        source_url="https://source.example/item",
        title="Source title",
    )
    result = structured_candidate_draft_document(
        generated,
        source_document=source,  # type: ignore[arg-type]
        metadata={"rewrite_run_id": 42, "rewrite_provider": "channel_ai_structured"},
    )
    assert result.mode == "rich"
    assert result.telegram == {}
    assert result.blocks[:-1] == generated.blocks
    assert result.blocks[-1]["type"] == "paragraph"
    assert result.blocks[-1]["content"] == "Источник: https://source.example/item"
    assert result.metadata["rewrite_run_id"] == 42
    assert result.metadata["rewrite_provider"] == "channel_ai_structured"
    assert result.metadata["ai_generation_kind"] == STRUCTURED_REWRITE_GENERATION_KIND


def test_run_output_reparse_fails_closed_after_tampering() -> None:
    with pytest.raises(StructuredRewriteDocumentError, match="not authorable"):
        structured_document_from_run_output(
            {
                "generation_kind": STRUCTURED_REWRITE_GENERATION_KIND,
                "post_document": {
                    "schema_version": 1,
                    "mode": "rich",
                    "blocks": [{"id": "bad", "type": "table", "rows": []}],
                    "telegram": {},
                    "metadata": {},
                },
            }
        )
