"""Strict `{{variable}}` substitution — Requirement 3.

Pure logic with sharp edges. The point of every case here is that a broken
prompt fails at assembly rather than reaching a model half-rendered.
"""

from __future__ import annotations

import pytest

from pydsh.prompt import PromptVariableError, interpolate


def render(text: str, **variables) -> str:
    return interpolate("a-section", text, dict(variables))


# --------------------------------------------------------------------------- #
# Substitution (R3.2)
# --------------------------------------------------------------------------- #
def test_a_reference_is_replaced():
    assert render("hello {{name}}", name="Ada") == "hello Ada"


def test_several_references_including_repeats():
    assert render("{{a}}-{{b}}-{{a}}", a="1", b="2") == "1-2-1"


def test_text_with_no_reference_is_untouched():
    assert render("plain text") == "plain text"


def test_an_empty_value_substitutes_as_empty():
    """Empty is a value; only `None` means "no value this assembly"."""
    assert render("[{{note}}]", note="") == "[]"


def test_a_value_containing_braces_is_not_re_scanned():
    """Substituted text is output, not input — no second interpolation pass."""
    assert render("{{a}}", a="{{b}}") == "{{b}}"


# --------------------------------------------------------------------------- #
# Failures (R3.3–R3.5)
# --------------------------------------------------------------------------- #
def test_an_unknown_variable_raises_and_lists_the_known_ones():
    with pytest.raises(PromptVariableError) as caught:
        render("hi {{who}}", name="Ada", place="here")
    message = str(caught.value)
    assert "who" in message
    assert "a-section" in message
    assert "name, place" in message  # the registered names, to find the typo


def test_an_unknown_variable_with_nothing_registered_says_so():
    with pytest.raises(PromptVariableError, match="none registered"):
        render("hi {{who}}")


def test_a_variable_with_no_value_raises():
    """R3.4 — rendering it as empty would ship a broken prompt silently."""
    with pytest.raises(PromptVariableError, match="no value"):
        render("hi {{who}}", who=None)


def test_a_malformed_reference_that_is_later_closed_raises():
    """R3.5 — `{{` … `}}` with something wrong between them is not prose."""
    with pytest.raises(PromptVariableError, match="malformed"):
        render("{{ {name} }}", name="Ada")


def test_an_illegal_variable_name_raises():
    with pytest.raises(PromptVariableError, match="illegal"):
        render("{{Name}}", Name="Ada")


def test_an_empty_reference_raises():
    with pytest.raises(PromptVariableError, match="illegal"):
        render("{{}}")


# --------------------------------------------------------------------------- #
# Prose (R3.6)
# --------------------------------------------------------------------------- #
def test_a_lone_open_brace_pair_is_literal():
    """A brace in a code sample must not make ordinary text unwritable."""
    assert render("use {{ for a block") == "use {{ for a block"


def test_a_literal_brace_before_a_real_reference_still_raises():
    """The prose exemption is narrow on purpose.

    Once a `}}` appears anywhere after a `{{`, the text is ambiguous between
    prose and a typo — `{{ user_name }}` (spaces, a very common mistake) reads
    exactly like a literal brace followed by a reference. Strictness refuses to
    guess, so this raises rather than silently choosing one reading. The price
    is that a literal `{{` cannot precede a real reference in the same text.
    """
    with pytest.raises(PromptVariableError, match="malformed"):
        render("{{ then {{name}}", name="Ada")


def test_the_kind_appears_in_the_error():
    """Contexts and sections are different registries; the error says which."""
    with pytest.raises(PromptVariableError, match="context 'clock'"):
        interpolate("clock", "{{missing}}", {}, kind="context")
