from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequestModel(BaseModel):
    """A base model that forbids extra fields."""

    model_config = ConfigDict(extra="forbid")


class InputMessage(StrictRequestModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, description="The content of the message.")


class JsonSchemaFormat(StrictRequestModel):
    type: Literal["json_schema"]
    name: str = Field(min_length=1, description="The name of the schema.")
    strict: Literal[True]
    schema_definition: dict[str, Any] = Field(
        alias="schema", description="The JSON schema definition."
    )


class TextOptions(StrictRequestModel):
    format: JsonSchemaFormat


class RunMetadata(StrictRequestModel):
    run_id: str = Field(min_length=1, description="The unique identifier for the run.")
    cache_namespace: str = Field(
        min_length=1, description="The namespace for the cache entry."
    )


class ResponsesRequest(StrictRequestModel):
    model: str = Field(min_length=1, description="The model name.")
    instructions: str = Field(
        min_length=1, description="The instructions for the model."
    )
    input: list[InputMessage] = Field(
        min_length=1, description="The input messages for the model"
    )
    text: TextOptions
    metadata: RunMetadata

    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Sampling temperature for the model.",
    )

    top_logprobs: int | None = Field(
        default=None,
        ge=0,
        description="Number of top log probabilities to return.",
    )


class EmbeddingsRequest(StrictRequestModel):
    model: str
    input: str | list[str]
    encoding_format: Literal["float"] = "float"
    metadata: RunMetadata

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Model name must be a non-empty string.")
        return value

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str | list[str]) -> str | list[str]:
        texts = [value] if isinstance(value, str) else value

        if not texts or any(not text.strip() for text in texts):
            raise ValueError(
                "Input must be a non-empty string or list of non-empty strings."
            )

        return value
