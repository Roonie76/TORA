"""
TORA Research Tool (wires the Phase 2H research pipeline into the live agent).

Runs CompositeResearchProvider.multi_source_research:
search -> normalize -> fetch (SSRF-safe) -> extract evidence -> verify sources
-> corroborate / detect conflicts -> synthesize.

The output is a compact dict that ContextBuilder renders as a
"Multi-Source Research Synthesis" block inside the untrusted external-data message.
"""

import logging
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field, field_validator

from .base import BaseTool, ToolMetadata

logger = logging.getLogger("tora.tools.research")

MAX_RESEARCH_QUERY_CHARS: int = 300
DEFAULT_RESEARCH_SOURCES: int = 3
MAX_RESEARCH_SOURCES: int = 5
MAX_RENDERED_CONCLUSIONS: int = 8
RESEARCH_CHARS_PER_SOURCE: int = 3000


class ResearchInput(BaseModel):
    query: str = Field(
        ...,
        min_length=3,
        max_length=MAX_RESEARCH_QUERY_CHARS,
        description="Research question or comparison topic, e.g. 'SBI vs HDFC home loan interest rates'.",
    )
    max_sources: int = Field(
        default=DEFAULT_RESEARCH_SOURCES,
        ge=1,
        le=MAX_RESEARCH_SOURCES,
        description=f"Number of web sources to read and cross-check (1 to {MAX_RESEARCH_SOURCES}).",
    )

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        cleaned = " ".join(v.split())
        if len(cleaned) < 3:
            raise ValueError("Research query is too short.")
        return cleaned


def _compact_conclusion(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "topic": c.get("topic"),
        "entity": c.get("entity"),
        "product": c.get("product"),
        "synthesized_statement": c.get("synthesized_statement", ""),
        "confidence": c.get("confidence"),
        "agreement_status": c.get("agreement_status"),
        "corroboration_strength": c.get("corroboration_strength"),
        "qualifiers": c.get("qualifiers") or [],
        "effective_date": c.get("effective_date"),
        "caveats": (c.get("caveats") or [])[:2],
        "provenance_urls": (c.get("provenance_urls") or [])[:3],
    }


class ResearchTool(BaseTool):
    name: str = "research"
    description: str = (
        "Multi-source verified research: searches the web, reads several pages, checks source "
        "authority (RBI/SEBI/official bank sites vs aggregators), preserves qualifiers such as "
        "'starting from', detects conflicting figures and returns corroborated conclusions with sources. "
        "Use for comparing current rates/charges across banks or verifying official financial figures."
    )
    args_schema: Type[BaseModel] = ResearchInput

    def __init__(self, provider: Optional[Any] = None):
        self.metadata = ToolMetadata(
            name=self.name,
            description=self.description,
            version="1.0.0",
            tags=["web", "research", "verification", "finance"],
            is_deterministic=False,
            requires_auth=False,
        )
        super().__init__()
        if provider is None:
            # Imported lazily: backend.research imports backend.tools submodules.
            from ..research.provider import CompositeResearchProvider

            provider = CompositeResearchProvider()
        self._provider = provider

    @property
    def provider(self) -> Any:
        return self._provider

    async def execute(self, query: str, max_sources: int = DEFAULT_RESEARCH_SOURCES, **kwargs: Any) -> Dict[str, Any]:
        logger.info("ResearchTool running multi-source research (sources=%d, query_len=%d)", max_sources, len(query))
        synthesis = await self._provider.multi_source_research(
            query=query,
            max_sources=max_sources,
            max_chars_per_source=RESEARCH_CHARS_PER_SOURCE,
        )
        data = synthesis.to_dict()

        conclusions: List[Dict[str, Any]] = [
            _compact_conclusion(c) for c in data.get("conclusions", [])[:MAX_RENDERED_CONCLUSIONS]
        ]
        sources = [
            {
                "domain": s.get("domain"),
                "source_type": s.get("source_type"),
                "authority_level": s.get("authority_level"),
                "is_official": s.get("is_official"),
            }
            for s in data.get("sources_evaluated", [])
        ]
        return {
            "query": data.get("query", query),
            "overall_status": data.get("overall_status"),
            "overall_confidence": data.get("overall_confidence"),
            "synthesis_summary": data.get("synthesis_summary", ""),
            "has_conflicts": data.get("has_conflicts", False),
            "conclusions": conclusions,
            "conflicts": [
                {"reason": cf.get("reason", ""), "divergence_detail": cf.get("divergence_detail", "")}
                for cf in data.get("conflicts", [])
            ],
            "sources": sources,
            "success": data.get("success", True),
            "error": data.get("error"),
        }
