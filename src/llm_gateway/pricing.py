import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

TOKENS_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal


@dataclass(frozen=True)
class PriceList:
    price_list_id: str
    models: dict[str, ModelPrice]


def _parse_price(value: object) -> Decimal:

    if not isinstance(value, str):
        raise TypeError(f"Expected string for price, got {type(value).__name__}")

    try:
        price = Decimal(value)
    except InvalidOperation as e:
        raise ValueError(f"Invalid decimal value: {value}") from e

    if not price.is_finite() or price < 0:
        raise ValueError(f"Price must be a non-negative finite decimal, got: {value}")

    return price


def load_price_list(path: str | Path) -> PriceList:
    """Load a price list from a TOML file."""

    with Path(path).open("rb") as f:
        data = tomllib.load(f)

    metadata = data.get("price_list")

    if not isinstance(metadata, dict):
        raise TypeError("Missing or invalid 'price_list' section in TOML file.")

    price_list_id = metadata.get("id")
    if not isinstance(price_list_id, str) or not price_list_id.strip():
        raise ValueError("price_list.id must be a non-empty string.")

    raw_models = data.get("models")

    if not isinstance(raw_models, dict):
        raise TypeError("Missing or invalid 'models' section in TOML file.")

    models = {}

    for model_name, values in raw_models.items():
        if (
            not isinstance(model_name, str)
            or not model_name.strip()
            or not isinstance(values, dict)
        ):
            raise TypeError(
                f"Invalid model entry: {model_name}: {values}. "
                "Expected a non-empty string key and a dictionary value."
            )

        models[model_name] = ModelPrice(
            input_per_million_usd=_parse_price(values.get("input_per_million_usd")),
            output_per_million_usd=_parse_price(values.get("output_per_million_usd")),
        )

    return PriceList(price_list_id=price_list_id, models=models)


def calculate_cost_usd(
    tokens_in: int | None,
    tokens_out: int | None,
    price: ModelPrice,
) -> Decimal | None:
    """Calculate the cost in USD based on token usage and model pricing."""

    if price.input_per_million_usd == 0 and price.output_per_million_usd == 0:
        return Decimal(0)

    if tokens_in is None or tokens_out is None:
        return None

    if (
        type(tokens_in) is not int
        or type(tokens_out) is not int
        or tokens_in < 0
        or tokens_out < 0
    ):
        raise ValueError(
            f"Token counts must be non-negative integers, got: "
            f"tokens_in={tokens_in}, tokens_out={tokens_out}"
        )

    cost_in = (Decimal(tokens_in) * price.input_per_million_usd) / TOKENS_PER_MILLION
    cost_out = (Decimal(tokens_out) * price.output_per_million_usd) / TOKENS_PER_MILLION
    return cost_in + cost_out
