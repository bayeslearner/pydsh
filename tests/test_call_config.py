"""Call-config merge precedence — Requirement 2, Property 1 (invariant I2)."""

from __future__ import annotations

import itertools

import pytest

from pydsh.llm import (
    GenerateOptions,
    LlmCallConfig,
    call_config_equals,
    call_config_from_options,
    call_config_to_dict,
    merge_call_config,
)


def test_empty_merge_is_all_defaults():
    assert merge_call_config() == LlmCallConfig()


def test_request_beats_header_beats_defaults():
    merged = merge_call_config(
        {"provider": "p0", "model": "m0", "temperature": 0.1},
        {"model": "m1", "temperature": 0.2},
        {"temperature": 0.3},
    )
    assert (merged.provider, merged.model, merged.temperature) == ("p0", "m1", 0.3)


def test_none_never_overrides_a_lower_layer():
    merged = merge_call_config(
        {"model": "from-defaults"}, {"model": None}, {"model": None}
    )
    assert merged.model == "from-defaults"


@pytest.mark.parametrize(
    "field,values",
    [
        ("model", ("m0", "m1", "m2")),
        ("temperature", (0.1, 0.2, 0.3)),
        ("max_tokens", (10, 20, 30)),
        ("reasoning_effort", ("off", "high", "max")),
        ("provider", ("p0", "p1", "p2")),
    ],
)
def test_precedence_is_total_over_every_set_combination(field, values):
    """Property 1: the winner is the highest layer that set the field."""
    for mask in itertools.product([False, True], repeat=3):
        layers = [
            {field: value} if present else {}
            for present, value in zip(mask, values)
        ]
        merged = merge_call_config(*layers)
        winners = [v for present, v in zip(mask, values) if present]
        expected = winners[-1] if winners else getattr(LlmCallConfig(), field)
        assert getattr(merged, field) == expected, f"mask={mask} field={field}"


def test_stop_normalizes_list_and_tuple_to_tuple():
    assert merge_call_config({"stop": ["a"]}, {"stop": ("b", "c")}).stop == ("b", "c")


def test_stop_rejects_a_non_sequence():
    with pytest.raises(TypeError, match="sequence"):
        merge_call_config({"stop": 5})


def test_equality_compares_stop_element_wise():
    a = LlmCallConfig(provider="p", model="m", stop=("x", "y"))
    b = LlmCallConfig(provider="p", model="m", stop=("x", "y"))
    c = LlmCallConfig(provider="p", model="m", stop=("x", "z"))
    assert call_config_equals(a, b)
    assert not call_config_equals(a, c)


def test_equality_distinguishes_unset_from_empty_stop():
    assert not call_config_equals(
        LlmCallConfig(stop=None), LlmCallConfig(stop=())
    )


def test_to_dict_omits_unset_optionals():
    assert call_config_to_dict(LlmCallConfig(provider="p", model="m")) == {
        "provider": "p",
        "model": "m",
    }


def test_to_dict_includes_every_set_field():
    config = LlmCallConfig(
        provider="p",
        model="m",
        reasoning_effort="high",
        temperature=0.5,
        max_tokens=99,
        stop=("x",),
    )
    assert call_config_to_dict(config) == {
        "provider": "p",
        "model": "m",
        "reasoning_effort": "high",
        "temperature": 0.5,
        "max_tokens": 99,
        "stop": ["x"],
    }


def test_from_options_treats_empty_identifiers_as_unset():
    """A blank request must not erase a route's default model."""
    assert call_config_from_options(GenerateOptions("", "", [])) == {}


def test_from_options_extracts_what_was_set():
    options = GenerateOptions("p", "m", [], temperature=0.2, stop=["z"])
    assert call_config_from_options(options) == {
        "provider": "p",
        "model": "m",
        "temperature": 0.2,
        "stop": ["z"],
    }


def test_round_trip_through_dict_preserves_the_config():
    config = LlmCallConfig(provider="p", model="m", temperature=0.5, stop=("x",))
    assert call_config_equals(merge_call_config(call_config_to_dict(config)), config)
