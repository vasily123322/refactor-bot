from __future__ import annotations


def convert_message_entities_to_markdown(text, entities):
    """Convert only 'text_link' entities into Markdown anchors. Leave other parts as-is.

    This enables users to input clickable text (without brackets) when they attach a link to a phrase in Telegram UI.
    """
    try:
        if not text or not entities:
            return text or ""
        links = [
            e
            for e in entities
            if getattr(e, "type", None) == "text_link" and getattr(e, "url", None)
        ]
        if not links:
            return text
        links.sort(key=lambda e: int(getattr(e, "offset", 0)))
        result_parts = []
        pos = 0
        for e in links:
            start = int(getattr(e, "offset", 0))
            length = int(getattr(e, "length", 0))
            end = start + max(0, length)
            if start < pos:
                continue
            result_parts.append(text[pos:start])
            pos = end
        result_parts.append(text[pos:])
        return "".join(result_parts)
    except Exception:
        return text


def convert_html_links_to_markdown(html_text: str) -> str:
    """Convert <a href="url">label</a> anchors to Markdown [label](url)."""
    try:
        import re

        if not html_text:
            return ""
        pattern = re.compile(r"<a\s+href=\"([^\"]+)\">(.*?)</a>", re.IGNORECASE)
        return pattern.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", html_text)
    except Exception:
        return html_text or ""
