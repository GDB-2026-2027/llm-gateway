import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class InvalidLLMOutput(ValueError):
    """Raised when the LLM output does not conform to the expected schema."""


def validate_schema(schema: dict[str, Any]) -> None:
    """Validate that the provided schema is a valid JSON Schema."""
    Draft202012Validator.check_schema(schema)


def _reject_constant(value: str) -> None:
    """Reject non-standard JSON values like "NaN" and "Infinity"."""
    raise ValueError(f"Invalid JSON constant: {value}")


def parse_and_validate_output(text: str, schema: dict[str, Any]) -> Any:
    """Parse the LLM output as JSON and validate it against the provided schema."""

    try:
        parsed = json.loads(
            text,
            parse_constant=_reject_constant,
        )

    except (json.JSONDecodeError, ValueError) as e:
        raise InvalidLLMOutput(f"Failed to parse LLM output as JSON: {e}") from e

    validator = Draft202012Validator(schema)

    try:
        validator.validate(parsed)
    except ValidationError as e:
        raise InvalidLLMOutput(
            f"LLM output does not conform to the expected schema: {e.message}"
        ) from e

    return parsed


def build_fallback(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic fallback for supported semantic outputs"""

    if schema.get("type") != "object":
        raise ValueError("Only object schemas are supported for fallback generation.")

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError(  # noqa: TRY004
            "This schema is not valid for fallback generation."
        )

    # A failed Boolean result defaults to false.
    if (
        set(properties) == {"answer"}
        and set(required) == {"answer"}
        and properties["answer"].get("type") == "boolean"
    ):
        return {"answer": False}

    # Classification uses a sentinel that does not collide with valid labels.
    if (
        set(properties) == {"label"}
        and set(required) == {"label"}
        and properties["label"].get("type") == "string"
    ):
        labels = properties["label"].get("enum")

        if isinstance(labels, list) and all(isinstance(label, str) for label in labels):
            reserve_label = "__PARSE_FAILURE__"

            while reserve_label in labels:
                reserve_label = f"_{reserve_label}"

            return {"label": reserve_label}

    raise ValueError("This schema is not valid for fallback generation.")
