from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Type, Union
from dataclasses import dataclass, field
from pydantic import BaseModel, ValidationError


class ToolError(Exception):
    """Base exception for all tool-related errors."""
    pass


class ToolNotFoundError(ToolError):
    """Raised when attempting to access a tool that is not registered."""
    pass


class ToolAlreadyRegisteredError(ToolError):
    """Raised when registering a tool with a name that already exists."""
    pass


class ToolValidationError(ToolError):
    """Raised when tool input arguments fail schema validation."""
    def __init__(self, message: str, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.errors = errors or []


class ToolExecutionError(ToolError):
    """Raised when an unhandled error occurs during tool execution."""
    pass


@dataclass(frozen=True)
class ToolMetadata:
    """
    Metadata describing a tool's identity, capabilities, and constraints.
    """
    name: str
    description: str
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)
    is_deterministic: bool = True
    requires_auth: bool = False

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("Tool name must be a non-empty string.")
        if not self.description or not self.description.strip():
            raise ValueError("Tool description must be a non-empty string.")


@dataclass(frozen=True)
class ToolResult:
    """
    Standardized, structured output from any tool execution.
    Provider and model independent.
    """
    tool_name: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to generic dictionary."""
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "call_id": self.call_id,
            "metadata": self.metadata,
        }


class BaseTool(ABC):
    """
    Abstract Base Class for all TORA external tools.
    Encapsulates tool metadata, Pydantic argument validation,
    OpenAI/generic JSON Schema export, and async execution.
    """

    name: str
    description: str
    args_schema: Optional[Type[BaseModel]] = None
    metadata: Optional[ToolMetadata] = None

    def __init__(self):
        if not hasattr(self, "name") or not self.name:
            raise ValueError(f"Tool {self.__class__.__name__} must define a 'name' attribute.")
        if not hasattr(self, "description") or not self.description:
            raise ValueError(f"Tool {self.__class__.__name__} must define a 'description' attribute.")

        if self.metadata is None:
            self.metadata = ToolMetadata(
                name=self.name,
                description=self.description,
            )

    def get_schema(self) -> Dict[str, Any]:
        """
        Generate standardized function-calling JSON Schema for LLMs.
        """
        parameters: Dict[str, Any]
        if self.args_schema is not None:
            schema = self.args_schema.model_json_schema()
            # Clean up Pydantic metadata not needed by LLMs
            parameters = {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            }
        else:
            parameters = {
                "type": "object",
                "properties": {},
            }

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def validate_args(self, raw_args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Validate incoming arguments against the tool's Pydantic args_schema.
        Returns validated dictionary or raises ToolValidationError.
        """
        args_dict = raw_args or {}

        if self.args_schema is None:
            return args_dict

        try:
            validated = self.args_schema.model_validate(args_dict)
            return validated.model_dump()
        except ValidationError as e:
            raise ToolValidationError(
                message=f"Validation failed for tool '{self.name}': {e.errors()}",
                errors=e.errors(),
            ) from e

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """
        Concrete tool logic to be implemented by subclasses.
        Must be asynchronous and accept validated keyword arguments.
        """
        pass

    async def run(
        self,
        args: Optional[Dict[str, Any]] = None,
        call_id: Optional[str] = None,
    ) -> ToolResult:
        """
        Execute tool with end-to-end argument validation and structured error containment.
        Never raises uncaught exceptions — always returns a structured ToolResult.
        """
        raw_args = args or {}
        try:
            validated_args = self.validate_args(raw_args)
            output = await self.execute(**validated_args)
            return ToolResult(
                tool_name=self.name,
                success=True,
                data=output,
                call_id=call_id,
            )
        except ToolValidationError as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(e),
                call_id=call_id,
                metadata={"validation_errors": e.errors},
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Error executing tool '{self.name}': {str(e)}",
                call_id=call_id,
            )
