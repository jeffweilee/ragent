"""T-EM.3 — EmbeddingModelConfig dataclass (B50).

Carries one embedding model's identity end-to-end:
- `name` — model name passed in the embedding-API request body (`bge-m3`, etc.)
- `dim` — vector dimension; must agree with the live ES mapping for `field`
- `api_url` — embedding service endpoint
- `model_arg` — value sent in the JSON request `model` field (often == name)
- `field` — derived ES field name `embedding_<normalized(name)>_<dim>`

Normalization: lowercase + strip non-alphanumeric. So `bge-m3` → `bgem3`,
`text-embedding-3-large` → `textembedding3large`. The normalized form is
ES-index-name safe (only `[a-z0-9]`).
"""

import pytest


def test_field_is_derived_from_name_and_dim() -> None:
    from ragent.clients.embedding_model_config import EmbeddingModelConfig

    cfg = EmbeddingModelConfig(
        name="bge-m3", dim=1024, api_url="http://e/text_embedding", model_arg="bge-m3"
    )
    assert cfg.field == "embedding_bgem3_1024"


def test_field_normalization_strips_non_alphanumeric_and_lowercases() -> None:
    from ragent.clients.embedding_model_config import EmbeddingModelConfig

    cfg = EmbeddingModelConfig(
        name="text-embedding-3-large",
        dim=3072,
        api_url="http://e",
        model_arg="text-embedding-3-large",
    )
    assert cfg.field == "embedding_textembedding3large_3072"


def test_field_normalization_handles_mixed_case() -> None:
    from ragent.clients.embedding_model_config import EmbeddingModelConfig

    cfg = EmbeddingModelConfig(name="BGE-M3-v2", dim=768, api_url="http://e", model_arg="BGE-M3-v2")
    assert cfg.field == "embedding_bgem3v2_768"


@pytest.mark.parametrize("bad_dim", [0, -1, 4097, 10_000])
def test_dim_out_of_range_rejected(bad_dim: int) -> None:
    from ragent.clients.embedding_model_config import (
        EmbeddingModelConfig,
        InvalidEmbeddingModelConfig,
    )

    with pytest.raises(InvalidEmbeddingModelConfig):
        EmbeddingModelConfig(name="bge-m3", dim=bad_dim, api_url="http://e", model_arg="bge-m3")


@pytest.mark.parametrize("ok_dim", [1, 768, 1024, 1536, 4096])
def test_dim_in_range_accepted(ok_dim: int) -> None:
    from ragent.clients.embedding_model_config import EmbeddingModelConfig

    cfg = EmbeddingModelConfig(name="m", dim=ok_dim, api_url="http://e", model_arg="m")
    assert cfg.dim == ok_dim


def test_empty_name_after_normalization_rejected() -> None:
    from ragent.clients.embedding_model_config import (
        EmbeddingModelConfig,
        InvalidEmbeddingModelConfig,
    )

    with pytest.raises(InvalidEmbeddingModelConfig):
        EmbeddingModelConfig(name="!!!", dim=1024, api_url="http://e", model_arg="x")


def test_roundtrip_to_from_dict() -> None:
    from ragent.clients.embedding_model_config import EmbeddingModelConfig

    src = EmbeddingModelConfig(name="bge-m3", dim=1024, api_url="http://e", model_arg="bge-m3")
    payload = src.to_dict()
    restored = EmbeddingModelConfig.from_dict(payload)
    assert restored == src
    assert payload["name"] == "bge-m3"
    assert payload["dim"] == 1024
    assert payload["field"] == "embedding_bgem3_1024"
