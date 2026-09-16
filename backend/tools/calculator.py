import ast
import math
from typing import Union, Dict, Any
from pydantic import BaseModel, Field, field_validator

from .base import BaseTool, ToolMetadata


MAX_EXPRESSION_LENGTH = 500
MAX_AST_DEPTH = 30
MAX_NUMBER_MAGNITUDE = 1e100
MAX_EXPONENT = 1000


class CalculatorInput(BaseModel):
    """Input model for the deterministic CalculatorTool."""
    expression: str = Field(
        ...,
        description="Mathematical expression to evaluate (e.g., '60000 * 0.20', '(2 + 3) * 4', '200000 * 0.36 / 12').",
        min_length=1,
        max_length=MAX_EXPRESSION_LENGTH,
    )

    @field_validator("expression")
    @classmethod
    def validate_expression_content(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("Expression must be a string.")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Expression cannot be empty or whitespace-only.")
        if len(cleaned) > MAX_EXPRESSION_LENGTH:
            raise ValueError(f"Expression length exceeds limit of {MAX_EXPRESSION_LENGTH} characters.")
        return cleaned


def _evaluate_ast_node(node: ast.AST, depth: int = 0) -> Union[int, float]:
    """
    Recursively and safely evaluates an AST node using an explicit allow-list.
    Rejects all function calls, imports, variables, attributes, comprehensions, etc.
    """
    if depth > MAX_AST_DEPTH:
        raise ValueError(f"Expression exceeds maximum allowed depth/complexity of {MAX_AST_DEPTH}.")

    if isinstance(node, ast.Expression):
        return _evaluate_ast_node(node.body, depth + 1)

    # 1. Numeric Literals
    if isinstance(node, ast.Constant):
        # In Python, bool is a subclass of int, so explicitly reject booleans
        if isinstance(node.value, bool):
            raise ValueError("Boolean literals are not allowed in mathematical expressions.")
        if not isinstance(node.value, (int, float)):
            raise ValueError(f"Literal of type '{type(node.value).__name__}' is not allowed in mathematical expressions.")
        if math.isnan(node.value) or math.isinf(node.value):
            raise ValueError("NaN and Infinity literals are not allowed.")
        if abs(node.value) > MAX_NUMBER_MAGNITUDE:
            raise ValueError(f"Numeric literal exceeds maximum allowed magnitude of {MAX_NUMBER_MAGNITUDE}.")
        return node.value

    # 2. Unary Operators (+, -)
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_ast_node(node.operand, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return +operand
        elif isinstance(node.op, ast.USub):
            return -operand
        else:
            raise ValueError(f"Unsupported unary operator: {node.op.__class__.__name__}")

    # 3. Binary Operators (+, -, *, /, //, %, **)
    if isinstance(node, ast.BinOp):
        left = _evaluate_ast_node(node.left, depth + 1)
        right = _evaluate_ast_node(node.right, depth + 1)

        if isinstance(node.op, ast.Add):
            res = left + right
        elif isinstance(node.op, ast.Sub):
            res = left - right
        elif isinstance(node.op, ast.Mult):
            res = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError("Division by zero is undefined.")
            res = left / right
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ZeroDivisionError("Integer division by zero is undefined.")
            res = left // right
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise ZeroDivisionError("Modulo by zero is undefined.")
            res = left % right
        elif isinstance(node.op, ast.Pow):
            if abs(right) > MAX_EXPONENT:
                raise ValueError(f"Exponent ({right}) exceeds maximum allowed safety limit of {MAX_EXPONENT}.")
            if abs(left) > 1e6 and abs(right) > 20:
                raise ValueError("Power calculation exceeds numerical safety limit.")
            if left == 0 and right < 0:
                raise ZeroDivisionError("Zero cannot be raised to a negative power.")
            try:
                res = left ** right
            except OverflowError as e:
                raise ValueError(f"Power calculation overflow: {e}") from e
        else:
            raise ValueError(f"Unsupported binary operator: {node.op.__class__.__name__}")

        if isinstance(res, complex):
            raise ValueError("Complex/imaginary results are not supported.")
        # Magnitude first: math.isnan() raises OverflowError for very large ints.
        if abs(res) > MAX_NUMBER_MAGNITUDE:
            raise ValueError(f"Result exceeds maximum allowed magnitude of {MAX_NUMBER_MAGNITUDE}.")
        if isinstance(res, float) and (math.isnan(res) or math.isinf(res)):
            raise ValueError("Calculation resulted in NaN or Infinity.")
        return res

    # 4. Strict Rejection of all other AST nodes
    raise ValueError(f"Unsupported syntax or operation: '{node.__class__.__name__}' is forbidden.")


def safe_eval(expression: str) -> Union[int, float]:
    """
    Safely parse and evaluate a mathematical expression using an AST allow-list.
    Returns int if the result is an exact integer, otherwise float.
    """
    if not isinstance(expression, str):
        raise ValueError("Expression must be a string.")
    
    cleaned = expression.strip()
    if not cleaned:
        raise ValueError("Expression cannot be empty.")
    
    if len(cleaned) > MAX_EXPRESSION_LENGTH:
        raise ValueError(f"Expression length ({len(cleaned)}) exceeds limit of {MAX_EXPRESSION_LENGTH} characters.")

    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid mathematical expression syntax: {e.msg}") from e

    result = _evaluate_ast_node(tree)

    # Clean integer representations (e.g. 12000.0 -> 12000)
    if isinstance(result, float) and result.is_integer():
        return int(result)
    return result


class CalculatorTool(BaseTool):
    """
    Deterministic mathematical calculation tool for TORA.
    Evaluates arithmetic expressions independently of the LLM.
    """
    name = "calculator"
    description = (
        "Perform deterministic mathematical calculations. "
        "Supports +, -, *, /, %, **, unary +/-, and parentheses. "
        "Use for exact financial and mathematical arithmetic (e.g. '60000 * 0.20', '(2 + 3) * 4', '200000 * 0.36 / 12')."
    )
    args_schema = CalculatorInput
    metadata = ToolMetadata(
        name="calculator",
        description="Deterministic mathematical expression evaluator.",
        version="1.0.0",
        tags=["math", "calculator", "arithmetic", "finance"],
        is_deterministic=True,
        requires_auth=False,
    )

    async def execute(self, expression: str) -> Dict[str, Any]:
        """
        Execute safe calculation on the provided mathematical expression.
        """
        result = safe_eval(expression)
        return {
            "expression": expression.strip(),
            "result": result,
        }
