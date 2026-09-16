import re
import logging
from typing import Optional, List, Dict, Any, Set, Tuple

from .models import (
    FetchedPage,
    ExtractedClaim,
    ResearchEvidence,
    SourceMetadata,
    current_utc_timestamp,
    extract_domain_from_url,
)
from .normalization import clean_text

logger = logging.getLogger("tora.research.evidence")

# Common English stop words to ignore during passage relevance matching
STOP_WORDS: Set[str] = {
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "and", "or", "but", "if", "then", "else", "when",
    "where", "why", "how", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same", "so",
    "than", "too", "very", "can", "will", "just", "should", "now", "what", "which",
    "i", "want", "know", "tell", "me", "find", "get", "check",
}

# Regex patterns for exact financial entity preservation
PERCENTAGE_PATTERN = re.compile(
    r"(?:(?:\d+(?:\.\d+)?)\s*%\s*(?:p\.?a\.?|per\s+annum|APR|p\.?m\.?)?)",
    re.IGNORECASE,
)
CURRENCY_AMOUNT_PATTERN = re.compile(
    r"(?:(?:₹|Rs\.?|INR|\$)\s*\d+(?:,\d+)*(?:\.\d+)?(?:\s*(?:lakh|crore|k|m|b|million|billion|trillion))?)",
    re.IGNORECASE,
)
QUALIFIER_PATTERN = re.compile(
    r"\b(?:start(?:ing)?\s+from|start(?:ing)?\s+at|onwards|up\s+to|as\s+of|subject\s+to|effective\s+from|valid\s+until|minimum|maximum|promotional|indicative|approximate(?:ly)?|base\s+rate|spread)\b",
    re.IGNORECASE,
)
FINANCIAL_KEYWORD_PATTERN = re.compile(
    r"\b(?:rate|interest|loan|emi|deposit|fd|repo|reverse\s+repo|tenure|benchmark|margin|processing\s+fee|concession|cibil|apr)\b",
    re.IGNORECASE,
)


def extract_financial_entities(text: str) -> Dict[str, Any]:
    """
    Extract exact numerical rates, amounts, currencies, and qualifiers from text.
    Preserves exact decimal precision, units, and semantic modifiers without rounding or altering.
    """
    if not text:
        return {}

    entities: Dict[str, Any] = {}

    # 1. Percentages and Interest Rates
    rates = [m.group(0).strip() for m in PERCENTAGE_PATTERN.finditer(text)]
    if rates:
        entities["rates"] = rates

    # 2. Currency Amounts
    amounts = [m.group(0).strip() for m in CURRENCY_AMOUNT_PATTERN.finditer(text)]
    if amounts:
        entities["amounts"] = amounts

    # 3. Qualifiers (e.g. "starting from", "up to")
    qualifiers = [m.group(0).strip().lower() for m in QUALIFIER_PATTERN.finditer(text)]
    if qualifiers:
        entities["qualifiers"] = list(set(qualifiers))

    # 4. Financial domain keywords
    keywords = [m.group(0).strip().lower() for m in FINANCIAL_KEYWORD_PATTERN.finditer(text)]
    if keywords:
        entities["keywords"] = list(set(keywords))

    return entities


class EvidenceExtractor:
    """
    Deterministic Evidence & Claim Extraction Engine.
    Segments cleaned web content into coherent passages, scores relevance against
    the user's research topic, preserves exact financial semantics, and constructs
    provenance-backed ExtractedClaim objects.
    """

    def __init__(
        self,
        max_claims_per_page: int = 5,
        min_passage_chars: int = 20,
        max_passage_chars: int = 800,
    ):
        self.max_claims_per_page = max_claims_per_page
        self.min_passage_chars = min_passage_chars
        self.max_passage_chars = max_passage_chars

    def _tokenize_query(self, query: str) -> Set[str]:
        """Extract meaningful keyword stems from search query."""
        if not query:
            return set()
        words = re.findall(r"\b[a-zA-Z0-9_\-\.%₹]+\b", query.lower())
        return {w for w in words if w not in STOP_WORDS and len(w) > 1}

    def _segment_text(self, text: str) -> List[str]:
        """
        Segment page text into coherent semantic passages (paragraphs or structural blocks).
        """
        if not text:
            return []

        # Split by double newline or heading marker
        raw_blocks = re.split(r"\n\s*\n|(?<=\n)###\s+", text)
        passages: List[str] = []

        for block in raw_blocks:
            cleaned = clean_text(block)
            if not cleaned or len(cleaned) < self.min_passage_chars:
                continue

            if len(cleaned) > self.max_passage_chars:
                # Break large paragraphs into sentence groups
                sentences = re.split(r"(?<=[.!?])\s+", cleaned)
                current_chunk: List[str] = []
                current_len = 0
                for s in sentences:
                    s_clean = s.strip()
                    if not s_clean:
                        continue
                    if current_len + len(s_clean) > self.max_passage_chars and current_chunk:
                        passages.append(" ".join(current_chunk))
                        current_chunk = [s_clean]
                        current_len = len(s_clean)
                    else:
                        current_chunk.append(s_clean)
                        current_len += len(s_clean)
                if current_chunk:
                    passages.append(" ".join(current_chunk))
            else:
                passages.append(cleaned)

        return passages

    def _score_passage(self, passage: str, query_tokens: Set[str]) -> Tuple[float, Dict[str, Any]]:
        """
        Score passage relevance based on query keyword matches and financial entity density.
        Uses word-boundary matching to prevent false substring positives (e.g. 'rate' in 'corporate').
        """
        passage_lower = passage.lower()
        entities = extract_financial_entities(passage)

        # Base score from query keyword matches using whole-word boundaries
        keyword_hits = 0
        for token in query_tokens:
            if re.search(r"\b" + re.escape(token) + r"\b", passage_lower):
                keyword_hits += 1

        score = float(keyword_hits * 3.0)

        # Only award entity bonuses if query matches OR query is empty
        if keyword_hits > 0 or not query_tokens:
            if entities.get("rates"):
                score += 4.0 * len(entities["rates"])
            if entities.get("amounts"):
                score += 3.0 * len(entities["amounts"])
            if entities.get("qualifiers"):
                score += 1.5 * len(entities["qualifiers"])
            if entities.get("keywords"):
                score += 1.0 * len(entities["keywords"])

        return score, entities

    def extract_evidence(
        self,
        page: FetchedPage,
        query: str = "",
        max_claims: Optional[int] = None,
    ) -> ResearchEvidence:
        """
        Extract structured, provenance-backed evidence from a FetchedPage.
        """
        limit = max_claims if max_claims is not None else self.max_claims_per_page
        limit = max(1, min(limit, 20))

        # Handle failed page fetches cleanly
        if not page.success:
            logger.warning("Cannot extract evidence from failed page fetch: %s", page.error)
            return ResearchEvidence(
                query=query,
                source_url=page.canonical_url or page.url,
                source_title=page.title or "Untitled",
                source_domain=page.domain or extract_domain_from_url(page.url),
                retrieved_at=page.retrieved_at,
                claims=[],
                page_summary=f"Fetch failed: {page.error}",
                raw_content_truncated=page.truncated,
                success=False,
                error=page.error or "Page fetch failed",
            )

        if not page.text_content or not page.text_content.strip():
            return ResearchEvidence(
                query=query,
                source_url=page.canonical_url or page.url,
                source_title=page.title or "Untitled",
                source_domain=page.domain or extract_domain_from_url(page.url),
                retrieved_at=page.retrieved_at,
                claims=[],
                page_summary="No relevant evidence found (empty page content).",
                raw_content_truncated=page.truncated,
                success=True,
                error=None,
            )

        query_tokens = self._tokenize_query(query)
        passages = self._segment_text(page.text_content)

        if not passages:
            return ResearchEvidence(
                query=query,
                source_url=page.canonical_url or page.url,
                source_title=page.title or "Untitled",
                source_domain=page.domain or extract_domain_from_url(page.url),
                retrieved_at=page.retrieved_at,
                claims=[],
                page_summary="No relevant evidence found.",
                raw_content_truncated=page.truncated,
                success=True,
                error=None,
            )

        scored_passages: List[Tuple[float, str, Dict[str, Any]]] = []
        for p in passages:
            score, entities = self._score_passage(p, query_tokens)
            if query_tokens and score > 0:
                scored_passages.append((score, p, entities))
            elif not query_tokens and (entities.get("rates") or entities.get("amounts") or entities.get("keywords")):
                scored_passages.append((score, p, entities))

        # Sort descending by score
        scored_passages.sort(key=lambda item: item[0], reverse=True)

        claims: List[ExtractedClaim] = []
        seen_claim_texts: Set[str] = set()

        for score, passage_text, entities in scored_passages[:limit]:
            # Derive the core claim statement from passage
            sentences = re.split(r"(?<=[.!?])\s+", passage_text)
            best_sentence = passage_text
            for s in sentences:
                s_clean = s.strip()
                if any(r in s_clean for r in entities.get("rates", [])) or any(a in s_clean for a in entities.get("amounts", [])):
                    best_sentence = s_clean
                    break

            if best_sentence in seen_claim_texts:
                continue
            seen_claim_texts.add(best_sentence)

            claim = ExtractedClaim(
                claim_text=best_sentence,
                supporting_text=passage_text,
                source_url=page.canonical_url or page.url,
                source_title=page.title or "Untitled",
                source_domain=page.domain or extract_domain_from_url(page.url),
                retrieved_at=page.retrieved_at,
                confidence=1.0,
                financial_entities=entities,
                extraction_type="deterministic_passage",
            )
            claims.append(claim)

        summary = None
        if claims:
            summary = claims[0].claim_text
        else:
            summary = "No relevant evidence found."

        return ResearchEvidence(
            query=query,
            source_url=page.canonical_url or page.url,
            source_title=page.title or "Untitled",
            source_domain=page.domain or extract_domain_from_url(page.url),
            retrieved_at=page.retrieved_at,
            claims=claims,
            page_summary=summary,
            raw_content_truncated=page.truncated,
            success=True,
            error=None,
        )

    def extract_from_text(
        self,
        text: str,
        source_url: str,
        title: str = "Untitled",
        domain: Optional[str] = None,
        query: str = "",
        max_claims: Optional[int] = None,
        retrieved_at: Optional[str] = None,
    ) -> ResearchEvidence:
        """
        Convenience method to extract evidence directly from plain text and URL.
        """
        clean_url = source_url.strip()
        resolved_domain = domain or extract_domain_from_url(clean_url)
        meta = SourceMetadata(
            url=clean_url,
            title=title,
            domain=resolved_domain,
            retrieved_at=retrieved_at or current_utc_timestamp(),
        )
        page = FetchedPage(
            url=clean_url,
            canonical_url=clean_url,
            title=title,
            domain=resolved_domain,
            text_content=text,
            metadata=meta,
            success=True,
        )
        return self.extract_evidence(page=page, query=query, max_claims=max_claims)
