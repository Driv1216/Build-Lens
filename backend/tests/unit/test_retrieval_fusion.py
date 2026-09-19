from fractions import Fraction
from uuid import UUID

from buildlens.application.retrieval import RetrievalService
from buildlens.domain.embeddings import ChunkEmbedding
from buildlens.domain.retrieval import (
    CandidatePage,
    ExactMatch,
    LexicalCandidate,
    RetrievalFilters,
    SemanticCandidate,
)


def embedding(identifier: int) -> ChunkEmbedding:
    chunk_id = UUID(int=identifier)
    return ChunkEmbedding(
        UUID(int=100 + identifier),
        UUID(int=200),
        UUID(int=201),
        UUID(int=202),
        UUID(int=203),
        UUID(int=204),
        chunk_id,
        f"key-{identifier}",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        2,
        1,
        "generated",
    )


def test_rrf_is_deduplicated_exact_first_and_uses_missing_rank_zero() -> None:
    first, second, third = UUID(int=1), UUID(int=2), UUID(int=3)
    lexical = CandidatePage(
        (
            LexicalCandidate(first, "z", 0.9, {"content": 0.9}, ()),
            LexicalCandidate(second, "b", 0.8, {"path": 0.8}, ()),
            LexicalCandidate(first, "z", 0.1, {"metadata": 0.1}, ()),
        ),
        False,
    )
    semantic = CandidatePage(
        (
            SemanticCandidate(third, "a", 0.99, embedding(3)),
            SemanticCandidate(first, "z", 0.7, embedding(1)),
            SemanticCandidate(first, "z", 0.6, embedding(1)),
        ),
        False,
    )
    ranked = RetrievalService._rank_hybrid(
        lexical,
        semantic,
        {second: (ExactMatch("symbol_name", "Chosen"),)},
    )
    assert [item.chunk_id for item in ranked] == [second, first, third]
    assert ranked[0].exact_rank == 1
    assert ranked[0].semantic is None
    assert ranked[0].score is not None
    assert float(ranked[0].score) == 1 / 62
    assert ranked[1].lexical is not None and ranked[1].lexical.rank == 1
    assert ranked[1].semantic is not None and ranked[1].semantic.rank == 2
    assert ranked[1].score == Fraction(1, 61) + Fraction(1, 62)


def test_query_validation_preserves_exact_whitespace_only_for_comparison() -> None:
    filters = RetrievalFilters()
    assert RetrievalService._validate("exact", " path.py ", 10, filters) == " path.py "
    assert RetrievalService._validate("lexical", "  getUser  ", 10, filters) == "getUser"
