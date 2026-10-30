"""Служебные команды: загрузка набора правил из YAML и пересчёт эмбеддингов."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.core.config import get_settings
from svetofor.db.enums import DocType
from svetofor.db.models import Chunk
from svetofor.search.reembed import column_dimension, ensure_dimension, reembed
from tests.support.factories import make_chunk, make_document, make_package
from tests.support.fake_models import StemEmbedder
from tests.support.rulesets import RULE_COUNT

pytestmark = pytest.mark.pg
RULES_FILE = Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"


def test_import_rules_command(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import svetofor.seed.import_rules as command

    monkeypatch.setattr(command, "session_scope", _scope(session_factory))
    # CLI настраивает логирование на текущий stdout — в тесте это поток pytest, его закроют
    monkeypatch.setattr(command, "configure_logging", lambda *_: None)
    assert command.main([str(RULES_FILE)]) == 0
    assert f"версия 1, правил {RULE_COUNT}" in capsys.readouterr().out
    assert command.main([str(RULES_FILE)]) == 0  # повторно — та же версия
    assert "версия 1" in capsys.readouterr().out
    assert command.main(["нет-такого-файла.yaml"]) == 1
    assert command.main([]) == 2


def _scope(factory: sessionmaker[Session]):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        with factory() as db:
            yield db
            db.commit()

    return scope


def test_reembed_recomputes_all_vectors(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        document = make_document(db, make_package(db), DocType.CONTRACT)
        for i in range(3):
            make_chunk(db, document, f"Пункт {i}: оплата в течение {i + 7} рабочих дней.", i)
        db.commit()
        assert column_dimension(db) == get_settings().embed_dim
        assert (
            ensure_dimension(db, get_settings().embed_dim) is False
        )  # та же модель — без изменений
    embedder = StemEmbedder()
    assert reembed(session_factory, embedder) == 3  # type: ignore[arg-type]
    with session_factory() as db:
        vectors = list(db.scalars(select(Chunk.embedding)))
        assert len(vectors) == 3 and all(v is not None and len(v) == embedder.dim for v in vectors)
