from __future__ import annotations

from services.context_server.inference import (
    DeterministicFakeModelClient,
    JsonSchemaContract,
    ProbeStatus,
    build_strict_json_prompt,
)


def test_fake_model_is_scripted_and_deterministic() -> None:
    contract = JsonSchemaContract(name="x", schema={"type": "object"})
    prompt = build_strict_json_prompt("Return an object.", contract)
    first = DeterministicFakeModelClient([{"b": 2, "a": 1}], model_name="fake")
    second = DeterministicFakeModelClient([{"b": 2, "a": 1}], model_name="fake")

    first_response = first.complete(prompt, request_id="job")
    second_response = second.complete(prompt, request_id="job")

    assert first_response.text == '{"a":1,"b":2}'
    assert first_response.response_id == second_response.response_id == "fake-response-0001"
    assert first.calls == second.calls
    assert first.health_probe().status is ProbeStatus.HEALTHY
    assert first.capability_probe().capabilities.supports_json_schema is True
