from __future__ import annotations

from bs4 import BeautifulSoup


def extract_article_text(html_content: str, *, max_len: int = 8000) -> str:
    """Извлечь плоский текст статьи из HTML.

    - Удаляет script/style/nav/header/footer
    - Возвращает очищенный текст, усечённый до max_len символов (если нужно)
    """
    try:
        soup = BeautifulSoup(html_content, "lxml")
    except Exception:
        # Фоллбэк на встроенный парсер, если lxml недоступен
        soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    article_text = "\n".join(lines)
    if len(article_text) > max_len:
        article_text = article_text[:max_len] + "..."
    return article_text
