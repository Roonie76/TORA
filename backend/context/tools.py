from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolResult:
    """
    Represents the output from an external tool execution (calculator, search, etc.).
    """
    tool_name: str
    call_id: str
    output: Any
    is_error: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    """
    Architectural boundary for information returned by tools.
    Prevents tool execution payloads from being mixed into user/assistant conversation history.
    In Phase 1, holds zero tool results by default.
    """
    results: List[ToolResult] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Return True if no tool results are present."""
        return len(self.results) == 0

    def add_result(
        self,
        tool_name: str,
        call_id: str,
        output: Any,
        is_error: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ToolContext":
        """Return a new ToolContext with the appended tool result (immutable)."""
        new_results = list(self.results)
        new_results.append(
            ToolResult(
                tool_name=tool_name,
                call_id=call_id,
                output=output,
                is_error=is_error,
                metadata=metadata or {},
            )
        )
        return ToolContext(results=new_results)
