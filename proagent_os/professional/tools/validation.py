"""Lightweight JSON Schema checks for tool arguments (stdlib only).

Aligned with OpenAI FunctionTool / Agents SDK (validate args before run) and
Anthropic tool_use (reject inputs that do not match the declared schema into
the observation channel). This is intentionally a practical subset — not a
full Draft-07 engine and not a new tool DSL.

additionalProperties policy
---------------------------
JSON Schema defaults ``additionalProperties`` to true when omitted. We follow
that: extras are allowed unless the schema sets ``additionalProperties: false``.
Built-in tools that want a closed object should set the flag explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


JSONTypeName = str  # "string" | "number" | "integer" | "boolean" | "object" | "array" | "null"


@dataclass
class ValidationIssue:
    path: str
    message: str
    code: str = "invalid"

    def as_dict(self) -> Dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass
class ValidationResult:
    ok: bool
    issues: List[ValidationIssue] = field(default_factory=list)

    def error_message(self) -> str:
        if self.ok:
            return ""
        parts = [f"{i.path or '$'}: {i.message}" for i in self.issues]
        return "tool argument validation failed: " + "; ".join(parts)

    def observation(self, tool_name: str) -> Dict[str, Any]:
        """Structured payload the specialist can read without executing the tool."""
        return {
            "error": "schema_validation_failed",
            "tool": tool_name,
            "issues": [i.as_dict() for i in self.issues],
            "executed": False,
        }


def _path_join(parent: str, key: Union[str, int]) -> str:
    if parent == "":
        return str(key) if not isinstance(key, int) else f"[{key}]"
    if isinstance(key, int):
        return f"{parent}[{key}]"
    return f"{parent}.{key}"


def _type_names(schema: Dict[str, Any]) -> List[str]:
    raw = schema.get("type")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return [str(raw)]


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        # bool is a subclass of int in Python — reject it here.
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        if isinstance(value, bool):
            return False
        return isinstance(value, (int, float))
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "null":
        return value is None
    # Unknown type keyword: do not fail closed on exotic schemas.
    return True


def _validate_value(
    value: Any,
    schema: Any,
    path: str,
    issues: List[ValidationIssue],
) -> None:
    if not isinstance(schema, dict):
        return

    type_names = _type_names(schema)
    if type_names and not any(_matches_type(value, t) for t in type_names):
        issues.append(
            ValidationIssue(
                path=path or "$",
                code="type",
                message=f"expected {'|'.join(type_names)}, got {type(value).__name__}",
            )
        )
        return

    if isinstance(value, dict) and (type_names == [] or "object" in type_names):
        props = schema.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        required = schema.get("required") or []
        if isinstance(required, Sequence):
            for key in required:
                if key not in value:
                    issues.append(
                        ValidationIssue(
                            path=_path_join(path, str(key)),
                            code="required",
                            message="required property missing",
                        )
                    )
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in props:
                _validate_value(child, props[key], _path_join(path, str(key)), issues)
            elif additional is False:
                issues.append(
                    ValidationIssue(
                        path=_path_join(path, str(key)),
                        code="additionalProperties",
                        message="additional property not allowed",
                    )
                )
            elif isinstance(additional, dict):
                _validate_value(child, additional, _path_join(path, str(key)), issues)

    if isinstance(value, list) and (type_names == [] or "array" in type_names):
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, child in enumerate(value):
                _validate_value(child, items, _path_join(path, idx), issues)

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        issues.append(
            ValidationIssue(
                path=path or "$",
                code="enum",
                message=f"value not in enum ({len(enum)} options)",
            )
        )


def validate_arguments(
    arguments: Any,
    schema: Optional[Dict[str, Any]],
    *,
    strict: bool = True,
) -> ValidationResult:
    """Validate ``arguments`` against a JSON Schema object (tool parameters).

    When ``strict`` is False, always returns ok (no checks). Missing / empty
    schemas are treated as ``{"type": "object", "properties": {}}`` with
    additionalProperties allowed.
    """
    if not strict:
        return ValidationResult(ok=True)

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return ValidationResult(
            ok=False,
            issues=[
                ValidationIssue(
                    path="$",
                    code="type",
                    message=f"arguments must be an object, got {type(arguments).__name__}",
                )
            ],
        )

    effective: Dict[str, Any]
    if not schema or not isinstance(schema, dict):
        effective = {"type": "object", "properties": {}, "additionalProperties": True}
    else:
        effective = schema

    # Root must be an object for OpenAI/Anthropic function args.
    root_types = _type_names(effective)
    if root_types and "object" not in root_types:
        # Still validate as object-shaped args against properties if present.
        pass

    issues: List[ValidationIssue] = []
    # Ensure we check object rules even if schema omitted type.
    object_schema = dict(effective)
    if "type" not in object_schema:
        object_schema["type"] = "object"
    _validate_value(arguments, object_schema, "", issues)
    return ValidationResult(ok=len(issues) == 0, issues=issues)


def validate_tool_call(
    tool_name: str,
    arguments: Any,
    schema: Optional[Dict[str, Any]],
    *,
    strict: bool = True,
) -> Tuple[ValidationResult, Optional[Dict[str, Any]]]:
    """Convenience: result plus observation dict on failure."""
    result = validate_arguments(arguments, schema, strict=strict)
    if result.ok:
        return result, None
    return result, result.observation(tool_name)
