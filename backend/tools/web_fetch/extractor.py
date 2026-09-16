import re
import html
import logging
from typing import Tuple
from bs4 import BeautifulSoup, Comment

logger = logging.getLogger("tora.web_fetch.extractor")

DEFAULT_MAX_CHARS: int = 3000
HARD_MAX_CHARS: int = 10000

# Non-content tags to strip completely
STRIP_TAGS = {
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "svg",
    "form",
    "aside",
    "iframe",
    "meta",
    "link",
    "button",
    "input",
    "select",
    "textarea",
    "canvas",
    "audio",
    "video",
}


class ContentExtractor:
    """
    Extracts clean, readable, bounded plain text from raw HTML content.
    Removes boilerplate (scripts, styles, navigation, forms, footers) and
    formats headings, paragraphs, lists, and tables cleanly.
    """

    @classmethod
    def extract(cls, raw_html: str, max_chars: int = DEFAULT_MAX_CHARS) -> Tuple[str, str, bool]:
        """
        Extract title, readable content, and truncation status from raw HTML.

        :param raw_html: Raw HTML string.
        :param max_chars: Upper character limit for extracted text.
        :return: Tuple of (title, clean_text, truncated).
        """
        if not raw_html or not raw_html.strip():
            return "", "", False

        effective_limit = max(100, min(max_chars, HARD_MAX_CHARS))

        try:
            soup = BeautifulSoup(raw_html, "html.parser")
        except Exception as e:
            logger.warning("BeautifulSoup failed to parse HTML: %s. Falling back to basic regex.", e)
            return cls._fallback_extract(raw_html, effective_limit)

        # 1. Extract title
        title = ""
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            title = cls._clean_text(title_tag.string)

        # If no <title>, check <h1>
        if not title:
            h1_tag = soup.find("h1")
            if h1_tag:
                title = cls._clean_text(h1_tag.get_text())

        # 2. Remove comments
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # 3. Strip non-content tags
        for tag_name in STRIP_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 4. Extract meaningful text elements preserving structure
        lines = []

        # Find the main content container if present (article, main, body)
        content_root = soup.find("article") or soup.find("main") or soup.body or soup

        for element in content_root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "blockquote"]):
            text = element.get_text(separator=" ", strip=True)
            if not text:
                continue

            clean = cls._clean_text(text)
            if not clean:
                continue

            tag_name = element.name.lower()
            if tag_name in ("h1", "h2", "h3"):
                lines.append(f"\n### {clean}")
            elif tag_name in ("h4", "h5", "h6"):
                lines.append(f"\n#### {clean}")
            elif tag_name == "li":
                lines.append(f"- {clean}")
            elif tag_name == "blockquote":
                lines.append(f"> {clean}")
            elif tag_name == "tr":
                # For table rows, separate cells with |
                cells = [cls._clean_text(c.get_text()) for c in element.find_all(["td", "th"])]
                valid_cells = [c for c in cells if c]
                if valid_cells:
                    lines.append(" | ".join(valid_cells))
            else:
                lines.append(clean)

        # If element-based traversal produced nothing, fall back to whole-body text
        if not lines:
            body_text = content_root.get_text(separator="\n", strip=True)
            raw_lines = [cls._clean_text(line) for line in body_text.splitlines()]
            lines = [line for line in raw_lines if line]

        full_text = "\n".join(lines).strip()
        # Collapse multiple empty newlines
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)

        # 5. Enforce bounding
        truncated = False
        if len(full_text) > effective_limit:
            # Cut cleanly at boundary
            full_text = full_text[:effective_limit].rstrip()
            truncated = True

        return title, full_text, truncated

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """Clean whitespace and unescape HTML entities in a string."""
        if not text:
            return ""
        unescaped = html.unescape(text)
        # Collapse multiple inline whitespaces into single space
        return re.sub(r"[ \t]+", " ", unescaped).strip()

    @classmethod
    def _fallback_extract(cls, raw_html: str, max_chars: int) -> Tuple[str, str, bool]:
        """Regex-based fallback extraction if HTML parser fails."""
        # Extract title
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", raw_html, re.IGNORECASE)
        title = cls._clean_text(title_match.group(1)) if title_match else ""

        # Remove scripts, styles, and tags
        clean_html = re.sub(r"<(script|style|noscript|nav|header|footer)[^>]*>[\s\S]*?</\1>", "", raw_html, flags=re.IGNORECASE)
        plain_text = re.sub(r"<[^>]+>", " ", clean_html)
        clean_text = cls._clean_text(plain_text)

        truncated = False
        if len(clean_text) > max_chars:
            clean_text = clean_text[:max_chars].rstrip()
            truncated = True

        return title, clean_text, truncated
