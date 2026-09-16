from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

# Maximum allowed sequential steps in a single ToolPlan
MAX_PLAN_STEPS: int = 3


class ToolPlanStep(BaseModel):
    """
    Represents a single step in a tool execution plan.
    Specifies which registered tool to execute and its parameters.
    """
    tool_name: str = Field(..., description="The registered name of the tool to execute.")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Key-value arguments conforming to the tool's schema.")
    call_id: Optional[str] = Field(default=None, description="Optional tracking identifier for this step.")


class ToolPlan(BaseModel):
    """
    Strongly typed structured plan produced by the Planner.
    Specifies whether tools are needed and lists sequential steps.
    """
    requires_tools: bool = Field(default=False, description="True if one or more tools must be executed to fulfill the request.")
    steps: List[ToolPlanStep] = Field(default_factory=list, description="Ordered list of tool execution steps.")
    thought: Optional[str] = Field(default=None, description="Brief rationale for why tools are or are not required.")
