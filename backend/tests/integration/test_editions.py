"""Прежние редакции той же закупки находятся по номеру извещения из карточки."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import CheckStatus, DocType, Role
from svetofor.db.models import Check, Fact, Package, User
from svetofor.reports.compare import previous_editions
from tests.support.factories import make_chunk, make_document, make_package

pytestmark = pytest.mark.pg
NUMBER = "0373100012326000123"


def _check(db: Session, number: str, minutes_ago: int, status: CheckStatus) -> Check:
    package = make_package(db, f"Закупка {uuid.uuid4().hex[:6]}")
    document = make_document(db, package, DocType.NOTICE)
    chunk = make_chunk(db, document, f"Номер извещения: № {number}.", 0)
    check = Check(
        package_id=package.id,
        status=status,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    db.add(check)
    db.flush()
    db.add(
        Fact(
            check_id=check.id,
            document_id=document.id,
            key="procurement_number",
            value_text=number,
            quote=f"№ {number}",
            chunk_id=chunk.id,
            verified=True,
        )
    )
    return check


def test_previous_editions_by_procurement_number(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        first = _check(db, NUMBER, 120, CheckStatus.DONE)
        _check(db, "0373100012326000999", 90, CheckStatus.DONE)  # другая закупка
        _check(db, NUMBER, 60, CheckStatus.FAILED)  # не завершилась — сравнивать нечего
        latest = _check(db, NUMBER, 1, CheckStatus.DONE)
        db.commit()
        found = previous_editions(db, latest)
        assert [f["check_id"] for f in found] == [str(first.id)]
        assert found[0]["procurement_number"] == NUMBER
        assert previous_editions(db, first) == []  # более ранних нет


def test_previous_editions_only_of_visible_owner(session_factory: sessionmaker[Session]) -> None:
    """Специалисту — только свои прежние редакции: чужие названия пакетов не показываются."""
    with session_factory() as db:
        mine, theirs = (
            User(email=f"{name}@test.ru", full_name=name, role=Role.SPECIALIST, password_hash="x")
            for name in ("mine", "theirs")
        )
        db.add_all([mine, theirs])
        db.flush()
        own_old = _check(db, NUMBER, 120, CheckStatus.DONE)
        foreign_old = _check(db, NUMBER, 90, CheckStatus.DONE)
        latest = _check(db, NUMBER, 1, CheckStatus.DONE)
        for check, owner in ((own_old, mine), (foreign_old, theirs), (latest, mine)):
            package = db.get(Package, check.package_id)
            assert package is not None
            package.owner_id = owner.id
        db.commit()
        visible = previous_editions(db, latest, owner_id=mine.id)
        assert [f["check_id"] for f in visible] == [str(own_old.id)]
        everyone = previous_editions(db, latest)  # методолог и администратор
        assert [f["check_id"] for f in everyone] == [str(foreign_old.id), str(own_old.id)]
