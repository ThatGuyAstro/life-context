from __future__ import annotations

import pytest

from services.context_server.inference import (
    DuplicateJsonKeyError,
    JsonExtractionError,
    JsonSchemaContract,
    SchemaValidationError,
    build_strict_json_prompt,
    extract_strict_json,
)


@pytest.fixture()
def contract() -> JsonSchemaContract:
    return JsonSchemaContract(
        name="daily_summary",
        version="2",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "items"],
        },
    )


def test_prompt_is_deterministic_and_contains_strict_contract(contract: JsonSchemaContract) -> None:
    first = build_strict_json_prompt(
        "Summarize the supplied events.", contract, context={"b": 2, "a": 1}
    )
    second = build_strict_json_prompt(
        "Summarize the supplied events.", contract, context={"a": 1, "b": 2}
    )

    assert first.prompt_hash == second.prompt_hash
    assert first.schema_hash == contract.schema_hash
    assert "Return exactly one JSON value" in first.system_prompt
    assert "additionalProperties" in first.system_prompt
    assert first.messages[0]["role"] == "system"


def test_prompt_payload_round_trip_preserves_hashes(contract: JsonSchemaContract) -> None:
    prompt = build_strict_json_prompt("Persist this prompt.", contract, context={"id": 1})

    restored = prompt.from_payload(prompt.to_payload())

    assert restored.messages == prompt.messages
    assert restored.to_dict() == prompt.to_dict()


def test_strict_extraction_validates_shape_and_rejects_surrounding_text(
    contract: JsonSchemaContract,
) -> None:
    value = extract_strict_json('{"summary":"ok","items":["one"]}', contract)
    assert value == {"summary": "ok", "items": ["one"]}

    with pytest.raises(JsonExtractionError, match="exactly one"):
        extract_strict_json('Here is the JSON: {"summary":"ok","items":[]}', contract)
    with pytest.raises(JsonExtractionError, match="code fences"):
        extract_strict_json('```json\n{"summary":"ok","items":[]}\n```', contract)
    assert extract_strict_json(
        '```json\n{"summary":"ok","items":[]}\n```', contract, allow_code_fence=True
    )


def test_strict_extraction_rejects_duplicate_keys_and_schema_violations(
    contract: JsonSchemaContract,
) -> None:
    with pytest.raises(DuplicateJsonKeyError):
        extract_strict_json('{"summary":"a","summary":"b","items":[]}', contract)
    with pytest.raises(SchemaValidationError, match="not allowed"):
        extract_strict_json('{"summary":"ok","items":[],"extra":true}', contract)
    with pytest.raises(SchemaValidationError, match="required"):
        extract_strict_json('{"summary":"ok"}', contract)
