"""Точки гибели процесса между транзакцией БД и отправкой в брокер."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from svetofor.db.enums import CheckStatus
from svetofor.db.models import Check
from svetofor.db.portfolio import Portfolio, PortfolioItem
from svetofor.portfolios.queue import claim_items
from svetofor.tasks.checks import run_check
from svetofor.tasks.portfolio_recovery import requeue_unstarted, sweep_portfolios
from svetofor.tasks.portfolios import process_portfolio_item
from tests.integration.test_analysis import build_upload
from tests.integration.test_portfolios import submit

pytestmark = pytest.mark.pg


class ProcessDied(BaseException):
    """Не ошибка приложения: имитация уничтожения процесса без обработки исключения."""


def test_check_committed_before_broker_send_is_recovered(api, monkeypatch) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    created = submit(api, [("Пакет.zip", build_upload(api["tmp"]).read_bytes())])
    identity = uuid.UUID(created.json()["id"])
    with api["factory"]() as db:
        claimed = claim_items(db, identity)
        db.commit()

    def die(*_):
        raise ProcessDied()

    with monkeypatch.context() as patch:
        patch.setattr(run_check, "delay", die)
        with pytest.raises(ProcessDied):
            process_portfolio_item.run(str(claimed[0][0]), claimed[0][1])
    with api["factory"]() as db:
        item = db.get(PortfolioItem, claimed[0][0])
        check_id = item.check_id
        assert db.get(Check, check_id).status == CheckStatus.QUEUED
        item.started_at = datetime.now(UTC) - timedelta(seconds=31)
        db.commit()
    requeue_unstarted(api["factory"], identity)
    requeue_unstarted(api["factory"], identity)
    with api["factory"]() as db:
        assert db.get(Check, check_id).status == CheckStatus.DONE
        assert len(list(db.scalars(select(Check)))) == 1


def test_sweep_recovers_lost_dispatch_without_user_action(api, monkeypatch) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    created = submit(api, [("Пакет.zip", b"data")])
    identity = uuid.UUID(created.json()["id"])
    with api["factory"]() as db:
        portfolio = db.get(Portfolio, identity)
        portfolio.dispatch_token, portfolio.dispatch_until = None, None
        db.commit()
    resumed = []
    monkeypatch.setattr(
        "svetofor.tasks.portfolio_recovery.enqueue", lambda *args: resumed.append(args)
    )
    sweep_portfolios.run()
    assert [row[0] for row in resumed] == [identity]
