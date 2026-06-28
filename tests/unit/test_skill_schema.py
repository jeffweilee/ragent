"""T-SK — SkillWriteRequest field-bound validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ragent.schemas.skill import (
    DESCRIPTION_MAX,
    INSTRUCTIONS_MAX,
    NAME_MAX,
    SkillWriteRequest,
)


def test_minimal_valid_request_defaults_enabled_true_and_empty_description():
    req = SkillWriteRequest(name="Translator", instructions="Translate to English.")
    assert req.name == "Translator"
    assert req.instructions == "Translate to English."
    assert req.description == ""
    assert req.enabled is True


def test_name_required_non_empty():
    with pytest.raises(ValidationError):
        SkillWriteRequest(name="", instructions="x")


def test_instructions_required_non_empty():
    with pytest.raises(ValidationError):
        SkillWriteRequest(name="x", instructions="")


def test_name_max_length_enforced():
    with pytest.raises(ValidationError):
        SkillWriteRequest(name="a" * (NAME_MAX + 1), instructions="x")


def test_description_max_length_enforced():
    with pytest.raises(ValidationError):
        SkillWriteRequest(name="x", instructions="y", description="d" * (DESCRIPTION_MAX + 1))


def test_instructions_max_length_enforced():
    with pytest.raises(ValidationError):
        SkillWriteRequest(name="x", instructions="i" * (INSTRUCTIONS_MAX + 1))
