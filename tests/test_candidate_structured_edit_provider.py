from __future__ import annotations

import asyncio

from app.domain.content import PostDocument
from app.services.candidate_rewrite import RewriteInput
from app.services.candidate_structured_edit_ai import ChannelAIStructuredEditProvider, canonical_structured_document_json


class _PreparedCompletion:
    model = "model-v1"

    def __init__(self) -> None:
        self.system_prompt = ""
        self.user_prompt = ""

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return '{"schema_version":1,"mode":"rich","blocks":[{"id":"h","type":"heading","level":2,"content":"Edited heading"},{"id":"p","type":"paragraph","content":"Edited body with preserved facts."}],"telegram":{},"metadata":{}}'


def test_provider_edits_full_canonical_document_without_markdown_round_trip() -> None:
    async def run() -> None:
        document = PostDocument.from_dict(
            {
                "schema_version": 1,
                "mode": "rich",
                "blocks": [
                    {"id": "h", "type": "heading", "level": 2, "content": "Current heading"},
                    {"id": "p", "type": "paragraph", "content": "Current body"},
                ],
                "telegram": {},
                "metadata": {},
            }
        )
        prepared = _PreparedCompletion()
        provider = ChannelAIStructuredEditProvider(
            prepared,  # type: ignore[arg-type]
            parent_run_id=42,
            document=document,
            operation="add_headings",
        )
        output = await provider.rewrite(
            RewriteInput(
                candidate_id=1,
                source_document_id=2,
                title="Source",
                source_url=None,
                text="Untrusted source facts.",
                reuse_policy="rewrite_with_attribution",
            )
        )
        canonical = canonical_structured_document_json(document)
        assert canonical in prepared.user_prompt
        assert '"blocks"' in prepared.user_prompt
        assert "```" not in prepared.user_prompt
        assert "Return the complete edited document, not a patch and not Markdown" in prepared.system_prompt
        assert output.metadata is not None
        assert output.metadata["parent_rewrite_run_id"] == 42
        assert output.metadata["structured_edit_operation"] == "add_headings"

    asyncio.run(run())
