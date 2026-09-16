from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KnowledgeItem:
    """
    Represents a single verified knowledge snippet (tax regulation, scheme detail, etc.).
    """
    title: str
    content: str
    source: Optional[str] = None
    source_type: str = "document"  # e.g., "tax_code", "faq", "official_circular"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeContext:
    """
    Architectural boundary for retrieved knowledge (RAG, tax regulations, verified guidelines).
    In Phase 1, holds zero documents by default and has no vector DB / embedding dependencies.
    """
    items: List[KnowledgeItem] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Return True if no knowledge items are present."""
        return len(self.items) == 0

    def add_item(self, title: str, content: str, source: Optional[str] = None, source_type: str = "document") -> "KnowledgeContext":
        """Return a new KnowledgeContext with the appended item (immutable)."""
        new_items = list(self.items)
        new_items.append(KnowledgeItem(title=title, content=content, source=source, source_type=source_type))
        return KnowledgeContext(items=new_items)
