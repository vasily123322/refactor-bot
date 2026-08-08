import pytest

from app.bot.keyboards.pagination import page_nav_row, paginate


def test_paginate_clamps_page_and_preserves_order() -> None:
    items = list(range(20))
    first, page, pages = paginate(items, -5, page_size=8)
    assert page == 0
    assert pages == 3
    assert first == list(range(8))

    last, page, pages = paginate(items, 99, page_size=8)
    assert page == 2
    assert pages == 3
    assert last == [16, 17, 18, 19]


def test_paginate_empty_collection_has_one_logical_page() -> None:
    page_items, page, pages = paginate([], 7, page_size=8)
    assert page_items == []
    assert page == 0
    assert pages == 1


def test_paginate_rejects_invalid_page_size() -> None:
    with pytest.raises(ValueError):
        paginate([1], 0, page_size=0)


def test_page_navigation_has_prev_position_next() -> None:
    row = page_nav_row(
        prefix="cp_page",
        page=1,
        total_pages=3,
        noop_callback="noop",
    )
    assert row is not None
    assert [button.text for button in row] == ["‹", "2 / 3", "›"]
    assert [str(button.callback_data) for button in row] == [
        "cp_page:0",
        "noop",
        "cp_page:2",
    ]


def test_single_page_has_no_navigation_row() -> None:
    assert (
        page_nav_row(
            prefix="cp_page",
            page=0,
            total_pages=1,
            noop_callback="noop",
        )
        is None
    )
