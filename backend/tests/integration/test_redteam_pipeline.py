"""Враждебный набор, часть 2: сквозной конвейер и API на PostgreSQL.

Как и в `tests/unit/test_redteam.py`: тест, который сейчас падает, помечен `xfail(strict=True)`
с причиной — после исправления строгий xfail напомнит снять пометку.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import CheckStatus, DocumentStatus, Light, RuleOutcome
from svetofor.db.models import Check, Chunk, Document, RuleResult, Ruleset, RulesetVersion
from svetofor.pipeline import ingest
from svetofor.pipeline.finalize import finalize_check, start_check
from svetofor.pipeline.ingest import IngestConfig, create_check, prepare_package, process_document
from svetofor.pipeline.ocr import OcrOutcome
from svetofor.pipeline.orchestrator import (
    check_rules,
    embed_chunks,
    evaluate_rules,
    extract_document_facts,
    rule_batches,
)
from svetofor.pipeline.sweep import sweep_rules
from svetofor.pipeline.unpack import UnpackError, UnpackLimits, unpack_package
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.model import RulesetFile, RulesetMeta
from svetofor.rules.store import import_ruleset
from tests.integration.conftest import auth
from tests.integration.test_analysis import CONTRACT, FACTS, RULES_FILE, deps
from tests.integration.test_api_checks import upload
from tests.support.fake_models import RoutingBackend, StemEmbedder
from tests.support.pdfgen import PdfSpec, build_pdf, pages_from_markup

pytestmark = pytest.mark.pg


def _package(tmp: Path, files: dict[str, PdfSpec | bytes]) -> Path:
    archive = tmp / "redteam.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i, (name, content) in enumerate(files.items()):
            if isinstance(content, bytes):
                zf.writestr(name, content)
            else:
                zf.write(build_pdf(content, tmp / f"{i}.pdf"), name)
    return archive


def _run(
    factory: sessionmaker[Session],
    tmp: Path,
    archive: Path,
    analysis: Any,
    classify: bool = False,
    sweep: bool = False,
) -> Any:
    config = IngestConfig(packages_dir=tmp / "packages")
    with factory() as db:
        version = import_ruleset(db, load_ruleset_file(RULES_FILE))
        check = create_check(
            db, upload=archive, original_name="x.zip", config=config, ruleset_version_id=version.id
        )
        db.commit()
    start_check(factory, check.id)
    ids = prepare_package(factory, check.id, config)
    classifier = (
        ingest.classify_with_llm(analysis.gateway)
        if classify and analysis.gateway is not None
        else None
    )
    for document_id in ids:
        process_document(factory, document_id, config, classifier)
    if analysis.embedder is not None:
        embed_chunks(factory, check.id, analysis.embedder)
    for document_id in ids:
        extract_document_facts(factory, check.id, document_id, analysis)
    with factory() as db:
        rules = [r for _, r in check_rules(db, db.get(Check, check.id))]  # type: ignore[arg-type]
    for codes in rule_batches(rules):
        evaluate_rules(factory, check.id, analysis, codes)
    if sweep:  # поштучная проверка пунктов (GREEN_SWEEP) — как задача checks.sweep
        sweep_rules(factory, check.id, analysis)
    finalize_check(factory, check.id)
    return check.id


def _outcomes(factory: sessionmaker[Session], check_id: Any) -> dict[str, RuleResult]:
    with factory() as db:
        return {
            r.rule_code: r
            for r in db.scalars(select(RuleResult).where(RuleResult.check_id == check_id))
        }


# ═════════════════════ нечитаемые документы и страницы ═════════════════════


def test_rt28_unrecognised_page_blocks_absence_based_ok(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: контракт, страница 2 — скан, распознавание не удалось. Ожидается: правила, которые
    говорят «в порядке» по отсутствию формулировки (VOL-01, TRM-01), — «не проверено»."""

    def failed_ocr(pdf: Path, dest: Path, **_: Any) -> OcrOutcome:
        return OcrOutcome(pdf, (2,), "Не удалось распознать страницы 2")

    monkeypatch.setattr(ingest, "ocr_if_needed", failed_ocr)
    pages = pages_from_markup(CONTRACT)
    spec = PdfSpec(pages=[pages[0], [], *pages[1:]], scanned_pages={2})
    archive = _package(tmp_path, {"Проект контракта.pdf": spec})
    check_id = _run(session_factory, tmp_path, archive, deps(RoutingBackend(FACTS, {})))
    results = _outcomes(session_factory, check_id)
    assert results["TRM-01"].outcome is RuleOutcome.NOT_CHECKED
    assert results["SEC-05"].outcome is RuleOutcome.NOT_CHECKED


def test_rt29_textless_contract_is_never_green(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Вход: «Проект контракта.pdf» — скан одной страницы без текста, модель работает.
    Ожидается: правила по контракту «не проверено», пакет не зелёный."""
    archive = _package(tmp_path, {"Проект контракта.pdf": PdfSpec(pages=[[]], scanned_pages={1})})
    check_id = _run(session_factory, tmp_path, archive, deps(RoutingBackend({}, {})))
    results = _outcomes(session_factory, check_id)
    assert results["TRM-01"].outcome is RuleOutcome.NOT_CHECKED
    with session_factory() as db:
        assert db.get(Check, check_id).light is not Light.GREEN  # type: ignore[union-attr]


def test_rt30_fake_pdf_fails_alone(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    """Вход: «ТЗ.pdf» — на самом деле текст, рядом нормальный контракт.
    Ожидается: ТЗ «не удалось» с понятной причиной, контракт проверен, проверка завершена."""
    archive = _package(
        tmp_path,
        {
            "Проект контракта.pdf": PdfSpec(pages=pages_from_markup(CONTRACT)),
            "ТЗ.pdf": b"this is not a pdf at all",
        },
    )
    check_id = _run(session_factory, tmp_path, archive, deps(RoutingBackend(FACTS, {})))
    with session_factory() as db:
        documents = {d.filename: d for d in db.scalars(select(Document))}
        check = db.get(Check, check_id)
        assert documents["ТЗ.pdf"].status is DocumentStatus.FAILED
        assert "не PDF" in (documents["ТЗ.pdf"].error or "")
        assert documents["Проект контракта.pdf"].status is DocumentStatus.READY
        assert check is not None and check.status is CheckStatus.DONE_WITH_WARNINGS


def test_rt31_vector_search_outage_is_recorded(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    archive = _package(
        tmp_path, {"Проект контракта.pdf": PdfSpec(pages=pages_from_markup(CONTRACT))}
    )
    analysis = deps(RoutingBackend(FACTS, {}), StemEmbedder(fail=True))
    check_id = _run(session_factory, tmp_path, archive, analysis)
    with session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None and check.counters.get("vector_search") == "unavailable"


# ═════════════════════ повторы и идемпотентность ═════════════════════


def test_rt32_repeated_steps_do_not_duplicate(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Вход: обработка документа и правила запускаются повторно (повтор задачи после сбоя
    воркера). Ожидается: ни фрагментов, ни исходов правил не становится больше."""
    archive = _package(
        tmp_path, {"Проект контракта.pdf": PdfSpec(pages=pages_from_markup(CONTRACT))}
    )
    analysis = deps(RoutingBackend(FACTS, {}))
    check_id = _run(session_factory, tmp_path, archive, analysis)
    with session_factory() as db:
        chunks = db.scalar(select(func.count()).select_from(Chunk))
        results = db.scalar(select(func.count()).select_from(RuleResult))
        document_id = db.scalar(select(Document.id))
        db.execute(  # как будто документ взят в работу повторно
            Document.__table__.update().values(status=DocumentStatus.PENDING)
        )
        db.commit()
    process_document(session_factory, document_id, IngestConfig(packages_dir=tmp_path / "packages"))
    evaluate_rules(session_factory, check_id, analysis)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Chunk)) == chunks
        assert db.scalar(select(func.count()).select_from(RuleResult)) == results


# ═════════════════════ архивы и файлы ═════════════════════


def test_rt33_zip_slip_and_bomb(tmp_path: Path) -> None:
    """Вход: путь «../../evil.pdf» и файл с коэффициентом сжатия ≫ 100."""
    slip = tmp_path / "slip.zip"
    with zipfile.ZipFile(slip, "w") as zf:
        zf.writestr("../../evil.pdf", b"%PDF-1.4")
        zf.writestr("ok.pdf", b"%PDF-1.4 ok")
    files = unpack_package(slip, "slip.zip", tmp_path / "out", UnpackLimits())
    assert {f.skipped_reason for f in files if f.relative_path.endswith("evil.pdf")} == {
        "небезопасный путь внутри архива"
    }
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.pdf", b"0" * (20 * 1024 * 1024))
    with pytest.raises(UnpackError, match="zip-бомб"):
        unpack_package(bomb, "bomb.zip", tmp_path / "out2", UnpackLimits())


def test_rt34_same_file_twice_is_checked_once(tmp_path: Path) -> None:
    archive = tmp_path / "dup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a/Контракт.pdf", b"%PDF-1.4 same")
        zf.writestr("b/Контракт копия.pdf", b"%PDF-1.4 same")
    files = unpack_package(archive, "dup.zip", tmp_path / "out", UnpackLimits())
    assert sum(1 for f in files if not f.skipped) == 1


# ═════════════════════ API: тихие ошибки и безопасность ═════════════════════


def test_rt35_rerun_keeps_the_ruleset(api: dict[str, Any]) -> None:
    factory = api["factory"]
    with factory() as db:
        base = load_ruleset_file(RULES_FILE)
        custom = RulesetFile(
            ruleset=RulesetMeta(code="custom-redteam", title="Особый набор", law="44-ФЗ"),
            rules=base.rules[:5],
        )
        import_ruleset(db, custom)
        db.commit()
    archive = _package(
        api["tmp"], {"Проект контракта.pdf": PdfSpec(pages=pages_from_markup(CONTRACT))}
    )
    with archive.open("rb") as stream:
        created = (
            api["client"]
            .post(
                "/api/checks",
                headers=auth(api),
                files={"file": ("p.zip", stream, "application/zip")},
                data={"ruleset_code": "custom-redteam"},
            )
            .json()
        )
    rerun = api["client"].post(f"/api/checks/{created['check_id']}/rerun", headers=auth(api)).json()
    with factory() as db:
        check = db.get(Check, rerun["check_id"])
        version = db.get(RulesetVersion, check.ruleset_version_id)  # type: ignore[union-attr]
        assert db.get(Ruleset, version.ruleset_id).code == "custom-redteam"  # type: ignore[union-attr]


def test_rt36_package_in_progress_cannot_be_deleted(api: dict[str, Any]) -> None:
    created = upload(api)
    with api["factory"]() as db:
        check = db.get(Check, created["check_id"])
        assert check is not None
        check.status = CheckStatus.RUNNING
        db.commit()
    response = api["client"].delete(f"/api/packages/{created['package_id']}", headers=auth(api))
    assert response.status_code == 409


def test_rt37_login_bruteforce_is_throttled(api: dict[str, Any]) -> None:
    codes = [
        api["client"]
        .post("/api/auth/login", json={"email": "specialist@demo.ru", "password": f"x{i}"})
        .status_code
        for i in range(25)
    ]
    assert 429 in codes


def test_rt38_search_query_is_literal(api: dict[str, Any]) -> None:
    """Вход: q с кавычками, «%» и попыткой SQL. Ожидается: 200 и пустой результат."""
    upload(api)
    response = api["client"].get(
        "/api/checks", headers=auth(api), params={"q": "x'; drop table checks; -- %_"}
    )
    assert response.status_code == 200 and response.json()["total"] == 0


def test_rt39_other_users_check_is_visible_by_design(api: dict[str, Any]) -> None:
    """Модель доступа — одна организация: специалист видит проверки коллег.
    Тест фиксирует решение: при переходе к нескольким организациям он должен начать падать."""
    created = upload(api)
    response = api["client"].get(
        f"/api/checks/{created['check_id']}/report", headers=auth(api, "methodologist")
    )
    assert response.status_code == 200
