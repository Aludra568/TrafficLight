"""Гибридный поиск на настоящем PostgreSQL + pgvector: русская морфология, фильтры, RRF, соседи."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.orm import Session

from svetofor.db.enums import DocType
from svetofor.search.embeddings import EmbeddingError
from svetofor.search.hybrid import HybridSearch, Query
from tests.support.factories import basis, make_chunk, make_document, make_package, make_section

pytestmark = pytest.mark.pg


class FakeEmbedder:
    """Запрос → заранее заданный вектор (близость к фрагментам известна точно)."""

    def __init__(self, queries: dict[str, list[float]] | None = None, fail: bool = False) -> None:
        self.queries = queries or {}
        self.fail = fail

    def embed_query(self, text: str) -> list[float]:
        if self.fail:
            raise EmbeddingError("недоступен")
        return self.queries[text]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


@dataclass
class Corpus:
    package_id: object
    ids: dict[str, object]


@pytest.fixture
def corpus(db: Session) -> Corpus:
    package = make_package(db)
    contract = make_document(db, package, DocType.CONTRACT, "Проект контракта.pdf")
    spec = make_document(db, package, DocType.SPEC, "ТЗ.pdf")
    subject = make_section(db, contract, "Проект контракта › 1. Предмет", "1")
    liability = make_section(db, contract, "Проект контракта › 9. Ответственность", "9")
    termination = make_section(db, contract, "Проект контракта › 11. Расторжение", "11")
    ids = {
        "subject": make_chunk(
            db,
            contract,
            "1.1. Поставщик обязуется поставить товар.",
            0,
            section=subject,
            embedding=basis(0),
        ).id,
        "penalty": make_chunk(
            db,
            contract,
            "9.1. За просрочку начисляется неустойка (пеня) в размере 1/300 ключевой ставки.",
            1,
            section=liability,
            embedding=basis(1),
        ).id,
        "fine": make_chunk(
            db,
            contract,
            "9.2. Штраф за неисполнение обязательств — 10 %.",
            2,
            section=liability,
            embedding=basis(2),
        ).id,
        "refusal": make_chunk(
            db,
            contract,
            "11.1. Заказчик вправе принять решение "
            "об одностороннем отказе от исполнения контракта.",
            3,
            section=termination,
            embedding=basis(3),
        ).id,
        "spec": make_chunk(
            db, spec, "Неустойка за упаковку не применяется.", 0, embedding=basis(1)
        ).id,
    }
    other = make_document(db, make_package(db, "Чужой пакет"), DocType.CONTRACT)
    ids["foreign"] = make_chunk(
        db, other, "Неустойка 1/300 в чужом пакете.", 0, embedding=basis(1)
    ).id
    return Corpus(package.id, ids)


def search(db: Session, corpus: Corpus, embedder: FakeEmbedder | None, **kw: object):
    query = str(kw.pop("query", "q"))
    hints = tuple(kw.pop("hints", ()))  # type: ignore[call-overload]
    params = {
        "package_id": corpus.package_id,
        "doc_types": [DocType.CONTRACT],
        "queries": [Query(query, hints)],
        **kw,
    }
    return HybridSearch(db, embedder).search(**params)  # type: ignore[arg-type]


def test_each_rule_query_gets_its_own_top(db: Session, corpus: Corpus) -> None:
    """Частое правило (неустойки — два фрагмента) не вытесняет редкое (отказ — один)."""
    result = HybridSearch(db, None).search(
        package_id=corpus.package_id,
        doc_types=[DocType.CONTRACT],
        queries=[
            Query("", ("неустойка", "штраф")),
            Query("", ("односторонний отказ",)),
        ],
        limit=2,
    )
    assert {h.chunk_id for h in result.hits} == {corpus.ids["penalty"], corpus.ids["refusal"]}


def test_full_text_understands_russian_word_forms(db: Session, corpus: Corpus) -> None:
    result = search(db, corpus, None, hints=["неустойки"])  # в тексте — «неустойка»
    assert [h.chunk_id for h in result.hits] == [corpus.ids["penalty"]]
    assert not result.vector_used


def test_phrase_hint(db: Session, corpus: Corpus) -> None:
    result = search(db, corpus, None, hints=["односторонний отказ"])
    assert [h.chunk_id for h in result.hits] == [corpus.ids["refusal"]]


def test_vector_finds_paraphrase(db: Session, corpus: Corpus) -> None:
    embedder = FakeEmbedder({"можно ли расторгнуть договор": basis(3)})
    result = search(db, corpus, embedder, query="можно ли расторгнуть договор")
    assert result.vector_used and result.hits[0].chunk_id == corpus.ids["refusal"]


def test_hit_found_by_both_methods_ranks_first(db: Session, corpus: Corpus) -> None:
    embedder = FakeEmbedder({"q": basis(1)})
    result = search(db, corpus, embedder, hints=["пеня", "штраф"])
    assert result.hits[0].chunk_id == corpus.ids["penalty"]
    assert set(result.hits[0].ranks) == {"vector:0", "text:0"}  # оба списка запроса 0


def test_filters_by_package_and_document_type(db: Session, corpus: Corpus) -> None:
    embedder = FakeEmbedder({"q": basis(1)})
    found = {h.chunk_id for h in search(db, corpus, embedder, hints=["неустойка"]).hits}
    assert corpus.ids["spec"] not in found and corpus.ids["foreign"] not in found
    with_spec = search(
        db, corpus, embedder, hints=["неустойка"], doc_types=[DocType.CONTRACT, DocType.SPEC]
    )
    assert corpus.ids["spec"] in {h.chunk_id for h in with_spec.hits}


def test_embedder_failure_falls_back_to_text(db: Session, corpus: Corpus) -> None:
    result = search(db, corpus, FakeEmbedder(fail=True), hints=["штраф"])
    assert not result.vector_used and [h.chunk_id for h in result.hits] == [corpus.ids["fine"]]


def test_limit(db: Session, corpus: Corpus) -> None:
    embedder = FakeEmbedder({"q": basis(0)})
    assert len(search(db, corpus, embedder, limit=2).hits) == 2


def test_expand_adds_neighbours_of_same_section(db: Session, corpus: Corpus) -> None:
    searcher = HybridSearch(db, None)
    hits = searcher.search(
        package_id=corpus.package_id, doc_types=[DocType.CONTRACT], queries=[Query("", ("пеня",))]
    ).hits
    chunks = searcher.expand(hits, budget_tokens=6000)
    assert [c.id for c in chunks] == [corpus.ids["penalty"], corpus.ids["fine"]]
    # «1.1» — соседний по порядку, но из другого раздела; «11.1» — тоже


def test_expand_respects_budget(db: Session, corpus: Corpus) -> None:
    searcher = HybridSearch(db, None)
    hits = searcher.search(
        package_id=corpus.package_id, doc_types=[DocType.CONTRACT], queries=[Query("", ("пеня",))]
    ).hits
    assert [c.id for c in searcher.expand(hits, budget_tokens=25)] == [corpus.ids["penalty"]]
    assert searcher.expand([], budget_tokens=100) == []


def test_vector_search_finds_package_chunks_among_many_packages(db: Session) -> None:
    """Разведка 3, Р1. Если бы поиск шёл по HNSW-индексу всей таблицы, индекс отдал бы ~40
    ближайших фрагментов из ВСЕХ пакетов, а фильтр по пакету — после: при многих проверках и
    более близких чужих фрагментах свой пакет остался бы без результатов. Сейчас запрос
    сортирует по двум ключам (расстояние, порядок в документе) — индекс такую сортировку не
    обслуживает, и PostgreSQL ищет точно среди фрагментов пакета. Тест держит это свойство:
    полный перебор запрещён, как на большой таблице."""
    from sqlalchemy import text

    for n in range(60):  # чужие пакеты: фрагменты ровно по запросу
        other = make_document(db, make_package(db, f"Чужой {n}"), DocType.CONTRACT, "К.pdf")
        for i in range(10):
            make_chunk(db, other, f"Чужой пункт {n}.{i}", i, embedding=basis(0))
    package = make_package(db, "Наш пакет")
    contract = make_document(db, package, DocType.CONTRACT, "Проект контракта.pdf")
    near = [0.3] + [0.0] * (len(basis(0)) - 1)
    near[1] = 1.0
    ours = {make_chunk(db, contract, f"Наш пункт {i}", i, embedding=near).id for i in range(5)}
    db.flush()
    db.execute(text("SET LOCAL enable_seqscan = off"))
    search = HybridSearch(db, FakeEmbedder({"оплата": basis(0)}))
    result = search.search(
        package_id=package.id, doc_types=[DocType.CONTRACT], queries=[Query("оплата", ())]
    )
    assert {h.chunk_id for h in result.hits} == ours
