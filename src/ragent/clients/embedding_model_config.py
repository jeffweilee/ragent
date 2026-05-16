"""Embedding model identity carried end-to-end (B50, T-EM.4).

Settings persist these as JSON; pipeline reads them via `ActiveModelRegistry`;
admin router accepts them on `/embedding/v1/promote`.

ES limit `dense_vector.dims ∈ [1, 4096]` is enforced here so a bad value
fails at boot, not at first ingest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIM_MIN = 1
_DIM_MAX = 4096
_NON_ALNUM = re.compile(r"[^a-z0-9]")


class InvalidEmbeddingModelConfig(ValueError):
    """Raised when name normalizes to empty or dim is outside ES limits."""


def _normalize(name: str) -> str:
    return _NON_ALNUM.sub("", name.lower())


@dataclass(frozen=True)
class EmbeddingModelConfig:
    name: str
    dim: int
    api_url: str
    model_arg: str

    def __post_init__(self) -> None:
        if not (_DIM_MIN <= self.dim <= _DIM_MAX):
            raise InvalidEmbeddingModelConfig(
                f"dim {self.dim} outside ES dense_vector range [{_DIM_MIN}, {_DIM_MAX}]"
            )
        if not _normalize(self.name):
            raise InvalidEmbeddingModelConfig(
                f"name {self.name!r} normalizes to empty (must contain at least one alphanumeric)"
            )

    @property
    def field(self) -> str:
        return f"embedding_{_normalize(self.name)}_{self.dim}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "dim": self.dim,
            "api_url": self.api_url,
            "model_arg": self.model_arg,
            "field": self.field,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> EmbeddingModelConfig:
        return cls(
            name=payload["name"],
            dim=int(payload["dim"]),
            api_url=payload["api_url"],
            model_arg=payload["model_arg"],
        )
