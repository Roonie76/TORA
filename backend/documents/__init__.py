"""Phase 11: reading the user's financial documents (Form 16, salary slips, bank statements, AIS)."""

from .reader import (
    DocumentError,
    MAX_BYTES,
    ParsedDocument,
    detect_type,
    extract_text,
    mask_identifiers,
    parse_document,
)

__all__ = ["DocumentError", "MAX_BYTES", "ParsedDocument", "detect_type", "extract_text", "mask_identifiers",
           "parse_document"]
