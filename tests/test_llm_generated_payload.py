from app.services.llm.generated_payload import apply_generated_text_to_payload, extract_payload_text


def test_apply_generated_text_to_empty_payload_creates_text_post():
    assert apply_generated_text_to_payload({}, " готовый текст ") == {
        "type": "text",
        "text": "готовый текст",
    }


def test_apply_generated_text_to_text_payload_preserves_unrelated_keys():
    payload = {"type": "text", "text": "старый", "buttons": [{"text": "Go"}]}

    updated = apply_generated_text_to_payload(payload, "новый")

    assert updated == {"type": "text", "text": "новый", "buttons": [{"text": "Go"}]}
    assert payload["text"] == "старый"


def test_apply_generated_text_to_media_payload_updates_caption():
    payload = {"type": "photo", "file_id": "abc", "caption": "старый"}

    updated = apply_generated_text_to_payload(payload, "новая подпись")

    assert updated == {"type": "photo", "file_id": "abc", "caption": "новая подпись"}


def test_apply_generated_text_to_unknown_payload_falls_back_to_text_type():
    payload = {"type": "poll", "question": "?"}

    updated = apply_generated_text_to_payload(payload, "пост")

    assert updated == {"type": "text", "question": "?", "text": "пост"}


def test_extract_payload_text_reads_text_and_caption():
    assert extract_payload_text({"type": "text", "text": "пост"}) == "пост"
    assert extract_payload_text({"type": "video", "caption": "подпись"}) == "подпись"
    assert extract_payload_text({"type": "poll", "text": "ignored"}) == ""
