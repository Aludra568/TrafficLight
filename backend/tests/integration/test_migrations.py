"""Миграции на настоящем PostgreSQL: применение, откат, совпадение с моделями, ограничения."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from svetofor.core.ids import uuid7
from svetofor.db.enums import CheckStatus
from svetofor.db.models import Base, Check, Chunk, Document, Package
from tests.conftest import BACKEND_DIR

pytestmark = pytest.mark.pg


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_downgrade_and_upgrade_roundtrip(pg_url: str, migrated_engine: Engine) -> None:
    cfg = alembic_cfg(pg_url)
    command.downgrade(cfg, "base")
    with migrated_engine.connect() as conn:
        tables = conn.execute(
            text(
                "select count(*) from pg_tables where schemaname='public' "
                "and tablename <> 'alembic_version'"
            )
        ).scalar()
    assert tables == 0
    command.upgrade(cfg, "head")


def test_database_schema_matches_models(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"Схема БД расходится с моделями — нужна миграция: {diff}"


def _package(db: Session) -> Package:
    pkg = Package(
        title="Тест", source_filename="t.zip", size_bytes=1, sha256="0" * 64, storage_path="/tmp"
    )
    db.add(pkg)
    db.flush()
    return pkg


def test_check_constraint_rejects_unknown_status(db: Session) -> None:
    pkg = _package(db)
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "insert into checks (id, package_id, status, prompt_versions, timings, counters) "
                "values (:id, :pkg, 'bogus', '{}', '{}', '{}')"
            ),
            {"id": uuid7(), "pkg": pkg.id},
        )
        db.flush()


def test_package_delete_cascades(db: Session) -> None:
    pkg = _package(db)
    doc = Document(package_id=pkg.id, filename="a.pdf", relative_path="a.pdf", sha256="1" * 64)
    db.add_all([doc, Check(package_id=pkg.id, status=CheckStatus.QUEUED)])
    db.flush()
    db.execute(text("delete from packages where id = :id"), {"id": pkg.id})
    left = db.execute(
        text(
            "select (select count(*) from documents where package_id=:id) + "
            "(select count(*) from checks where package_id=:id)"
        ),
        {"id": pkg.id},
    ).scalar()
    assert left == 0


def test_russian_fulltext_and_vector_search(db: Session) -> None:
    pkg = _package(db)
    doc = Document(package_id=pkg.id, filename="c.docx", relative_path="c.docx", sha256="2" * 64)
    db.add(doc)
    db.flush()
    texts = [
        "Обеспечение исполнения контракта составляет 5 процентов",
        "Поставка товара осуществляется в течение 10 рабочих дней",
    ]
    for i, t in enumerate(texts):
        vec = [0.0] * 1024
        vec[i] = 1.0
        db.add(
            Chunk(
                document_id=doc.id,
                order_index=i,
                text=t,
                text_norm=t.lower(),
                page_from=1,
                page_to=1,
                char_start=0,
                char_end=len(t),
                embedding=vec,
            )
        )
    db.flush()

    fts = (
        db.execute(
            text(
                "select text from chunks where document_id=:d and "
                "tsv @@ websearch_to_tsquery('russian', 'обеспечения контрактов')"
            ),
            {"d": doc.id},
        )
        .scalars()
        .all()
    )
    assert fts == [texts[0]]  # морфология: «обеспечения контрактов» нашло «обеспечение … контракта»

    query = "[" + ",".join(["0"] + ["1"] + ["0"] * 1022) + "]"
    nearest = db.execute(
        text(
            "select text from chunks where document_id=:d "
            "order by embedding <=> cast(:q as vector) limit 1"
        ),
        {"d": doc.id, "q": query},
    ).scalar()
    assert nearest == texts[1]


def test_migrations_apply_on_empty_database(pg_url: str) -> None:
    """Отдельная БД с нуля: так же, как при первом `docker compose up`."""
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("drop database if exists svetofor_fresh"))
        conn.execute(text("create database svetofor_fresh"))
    fresh_url = pg_url.rsplit("/", 1)[0] + "/svetofor_fresh"
    try:
        command.upgrade(alembic_cfg(fresh_url), "head")
        engine = create_engine(fresh_url)
        with engine.connect() as conn:
            head = conn.execute(text("select version_num from alembic_version")).scalar()
            assert head == "0006"
            light = conn.execute(
                text("select pg_get_constraintdef(oid) from pg_constraint where conname = :n"),
                {"n": "ck_checks_light"},
            ).scalar()
            assert "gray" in light
        engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text("drop database if exists svetofor_fresh with (force)"))
        admin.dispose()
