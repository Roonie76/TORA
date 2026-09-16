from typing import Optional, List, Dict, Any, Type, Callable
from .base import (
    BaseTool,
    ToolResult,
    ToolNotFoundError,
    ToolAlreadyRegisteredError,
)


class ToolRegistry:
    """
    Central registry for managing, discovering, and executing TORA external tools.
    Decoupled from specific tool implementations and LLM providers.
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool, overwrite: bool = False) -> None:
        """
        Register a tool instance in the registry.

        :param tool: An instance of a BaseTool subclass.
        :param overwrite: If True, allow replacing an existing tool with the same name.
        :raises TypeError: If tool is not an instance of BaseTool.
        :raises ToolAlreadyRegisteredError: If tool name exists and overwrite is False.
        """
        if not isinstance(tool, BaseTool):
            raise TypeError(f"Expected BaseTool instance, got {type(tool).__name__}")

        name = tool.name.strip()
        if not name:
            raise ValueError("Tool name cannot be empty.")

        if name in self._tools and not overwrite:
            raise ToolAlreadyRegisteredError(
                f"Tool '{name}' is already registered. Use overwrite=True to replace it."
            )

        self._tools[name] = tool

    def unregister(self, name: str) -> Optional[BaseTool]:
        """
        Remove a tool from the registry by name.
        Returns the removed tool if found, or None.
        """
        return self._tools.pop(name.strip(), None)

    def get(self, name: str) -> Optional[BaseTool]:
        """
        Retrieve a tool by name. Returns None if not found.
        """
        return self._tools.get(name.strip())

    def get_or_raise(self, name: str) -> BaseTool:
        """
        Retrieve a tool by name or raise ToolNotFoundError.
        """
        tool = self.get(name)
        if tool is None:
            raise ToolNotFoundError(f"Tool '{name}' is not registered.")
        return tool

    def has(self, name: str) -> bool:
        """
        Check if a tool is registered by name.
        """
        return name.strip() in self._tools

    def list_tools(self) -> List[BaseTool]:
        """
        Return a list of all registered tool instances.
        """
        return list(self._tools.values())

    def list_names(self) -> List[str]:
        """
        Return a list of names of all registered tools.
        """
        return list(self._tools.keys())

    def get_schemas(self) -> List[Dict[str, Any]]:
        """
        Return JSON Schema definitions for all registered tools
        in standard function-calling format for LLMs.
        """
        return [tool.get_schema() for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        call_id: Optional[str] = None,
    ) -> ToolResult:
        """
        Lookup and execute a tool by name with argument validation and error containment.
        If tool is not found, returns a failed ToolResult rather than raising uncaught error.
        """
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                tool_name=name,
                success=False,
                error=f"Tool '{name}' not found in registry. Available tools: {self.list_names()}",
                call_id=call_id,
            )

        return await tool.run(args=args, call_id=call_id)

    def clear(self) -> None:
        """
        Clear all registered tools.
        """
        self._tools.clear()

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)
