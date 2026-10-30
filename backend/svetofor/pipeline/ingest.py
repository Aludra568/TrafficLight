"""Приём пакета и подготовка документов.

Сервисы работают короткими транзакциями: тяжёлое (конвертация, OCR, разбор) — вне транзакции,
результат пишется одним коммитом. Ошибка одного документа не роняет проверку: документ
получает статус «не удалось обработать» с понятной причиной.
"""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from svetofor.core.config import Settings
from svetofor.core.ids import uuid7
from svetofor.core.logging import get_logger
from svetofor.db.enums import DocType, DocTypeSource, DocumentStatus, Stage
from svetofor.db.models import Check, Chunk, Document, Package, Page, Section
from svetofor.llm.errors import LLMError
from svetofor.llm.gateway import LLMGateway
from svetofor.llm.prompting import render
from svetofor.llm.schemas import DocTypeAnswer
from svetofor.pipeline.chunking import ChunkDraft, make_chunks
from svetofor.pipeline.convert import ConvertError, convert_to_pdf, docx_heading_hints
from svetofor.pipeline.doctype import DocTypeGuess, choose_doc_type, detect_doc_type
from svetofor.pipeline.extract import ExtractedDocument, extract_document
from svetofor.pipeline.ocr import OcrOutcome, ocr_if_needed, ocr_text_quality
from svetofor.pipeline.progress import StageTracker, init_stages
from svetofor.pipeline.revisions import accept_docx_revisions
from svetofor.pipeline.storage import PackagePaths, package_paths, safe_filename
from svetofor.pipeline.structure import DocumentStructure, build_structure
from svetofor.pipeline.textlayer import is_garbage
from svetofor.pipeline.unpack import UnpackError, UnpackLimits, find_bsdtar, unpack_package

log = get_logger(__name__)
SessionFactory = Callable[[], Session]
# (имя файла, начало текста) → тип от модели; None — модель недоступна
DocTypeClassifier = Callable[[str, str], DocTypeGuess | None]


class CheckFailedError(Exception):
    """Проверку нельзя продолжить (битый архив, zip-бомба). Сообщение — для пользователя."""


@dataclass(frozen=True)
class IngestConfig:
    packages_dir: Path
    limits: UnpackLimits = field(default_factory=UnpackLimits)
    max_pages: int = 1000
    ocr_min_chars: int = 50
    ocr_languages: str = "rus+eng"
    ocr_jobs: int = 4
    ocr_timeout_s: int = 900
    convert_timeout_s: int = 120
    chunk_max_tokens: int = 700
    soffice: str = "soffice"
    bsdtar: str | None = None  # RAR/7z/tar; None — такие архивы не поддерживаются

    @classmethod
    def from_settings(cls, settings: Settings) -> IngestConfig:
        return cls(
            packages_dir=settings.packages_dir,
            limits=UnpackLimits(
                max_files=settings.max_files,
                max_unpacked_bytes=settings.max_unpacked_bytes,
                max_depth=settings.max_archive_depth,
                max_ratio=settings.max_compression_ratio,
            ),
            max_pages=settings.max_pages_per_doc,
            ocr_min_chars=settings.ocr_min_chars,
            ocr_languages=settings.ocr_languages,
            ocr_jobs=settings.ocr_jobs,
            chunk_max_tokens=settings.chunk_max_tokens,
            bsdtar=find_bsdtar(settings.bsdtar),
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ─────────────────────────── Приём ───────────────────────────


def create_check(
    db: Session,
    *,
    upload: Path,
    original_name: str,
    config: IngestConfig,
    ruleset_version_id: uuid.UUID | None,
    owner_id: uuid.UUID | None = None,
    is_demo: bool = False,
) -> Check:
    """Пакет из загруженного файла (файл переносится в хранилище) и проверка в очереди."""
    package_id = uuid7()
    paths = package_paths(config.packages_dir, package_id).ensure()
    target = paths.original / safe_filename(original_name)
    shutil.move(upload, target)
    package = Package(
        id=package_id,
        owner_id=owner_id,
        title=PurePosixPath(safe_filename(original_name)).stem[:500] or "Пакет документов",
        source_filename=original_name[:500],
        size_bytes=target.stat().st_size,
        sha256=file_sha256(target),
        storage_path=str(paths.root),
        is_demo=is_demo,
    )
    db.add(package)
    db.flush()
    return new_check(db, package, ruleset_version_id)


def new_check(db: Session, package: Package, ruleset_version_id: uuid.UUID | None) -> Check:
    check = Check(package_id=package.id, ruleset_version_id=ruleset_version_id)
    db.add(check)
    db.flush()
    init_stages(db, check.id)
    return check


# ─────────────────────────── Распаковка ───────────────────────────


def prepare_package(
    session_factory: SessionFactory, check_id: uuid.UUID, config: IngestConfig
) -> list[uuid.UUID]:
    """Документы пакета для обработки. При перепроверке пакет повторно не распаковывается."""
    tracker = StageTracker(session_factory, check_id)
    with session_factory() as db:
        check = db.get(Check, check_id)
        if check is None:
            raise CheckFailedError("Проверка не найдена")
        package = check.package
        existing = list(db.scalars(select(Document).where(Document.package_id == package.id)))
        paths = PackagePaths(Path(package.storage_path)).ensure()
        source = paths.original / safe_filename(package.source_filename)
        original_name = package.source_filename
        package_id = package.id
    if existing:
        tracker.skip(Stage.UNPACK, "Файлы уже подготовлены при прошлой проверке")
        return [d.id for d in existing if d.status is not DocumentStatus.SKIPPED]

    tracker.start(Stage.UNPACK)
    workdir = paths.tmp / "unpack"
    try:
        files = unpack_package(source, original_name, workdir, config.limits, config.bsdtar)
    except UnpackError as exc:
        tracker.fail(Stage.UNPACK, str(exc))
        raise CheckFailedError(str(exc)) from exc
    except BaseException:  # таймаут задачи, сбой диска: распакованное наполовину не копим
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    document_ids: list[uuid.UUID] = []
    with session_factory() as db:
        for item in files:
            document = Document(
                id=uuid7(),
                package_id=package_id,
                filename=item.filename[:500],
                relative_path=item.relative_path,
                mime=mimetypes.guess_type(item.filename)[0],
                sha256=item.sha256 or "",
                size_bytes=item.size,
                status=DocumentStatus.SKIPPED if item.skipped else DocumentStatus.PENDING,
                error=item.skipped_reason,
            )
            db.add(document)
            if item.path is not None:
                shutil.move(item.path, paths.source_for(document.id, item.filename))
                document_ids.append(document.id)
        db.commit()
    shutil.rmtree(workdir, ignore_errors=True)
    skipped = len(files) - len(document_ids)
    detail = f"Документов: {len(document_ids)}" + (f", пропущено: {skipped}" if skipped else "")
    tracker.done(Stage.UNPACK, detail)
    return document_ids


# ─────────────────────────── Обработка документа ───────────────────────────


@dataclass
class _Prepared:
    page_count: int
    ocr: OcrOutcome
    extracted: ExtractedDocument
    structure: DocumentStructure
    chunks: list[ChunkDraft]
    doc_type: DocTypeGuess
    doc_type_source: DocTypeSource
    warnings: list[str]
    signals: dict[str, Any]


def hidden_spans(extracted: ExtractedDocument) -> list[dict[str, Any]]:
    """Скрытые строки, идущие подряд на одной странице, — один отрезок: абзац целиком.

    Скрытый абзац переносится на несколько строк; цитата из первой строки оборвала бы смысл
    («Указание для проверки: условия контракта…» без «…рисков нет»).
    """
    spans: list[dict[str, Any]] = []
    for line in extracted.hidden_lines:
        last = spans[-1] if spans else None
        if last and last["page"] == line.page and line.char_start - last["char_end"] <= 1:
            last["char_end"] = line.char_end
            last["text"] = extracted.text[last["char_start"] : line.char_end]
            continue
        spans.append(
            {
                "page": line.page,
                "char_start": line.char_start,
                "char_end": line.char_end,
                "text": line.text,
            }
        )
    return spans


def unreadable_pages(
    extracted: ExtractedDocument, ocr: OcrOutcome, page_count: int, config: IngestConfig
) -> list[int]:
    """Страницы без надёжного текста.

    Не распознан скан (сбой или таймаут OCR), распознанный текст — мусор, страница за лимитом
    обработки. Пустая страница, которую OCR распознал успешно, — читаемая (это пустой лист).
    """
    texts = {page.number: page.text for page in extracted.pages}
    pages: set[int] = set()
    for number in ocr.pages:
        text = texts.get(number, "")
        if ocr.error:
            if number in ocr.garbage or len(text.strip()) < config.ocr_min_chars:
                pages.add(number)
        elif is_garbage(text):
            pages.add(number)
    pages.update(range(config.max_pages + 1, page_count + 1))
    return sorted(pages)


def _page_list(pages: list[int]) -> str:
    shown = ", ".join(str(p) for p in pages[:10])
    return shown + (f" и ещё {len(pages) - 10}" if len(pages) > 10 else "")


def classify_with_llm(gateway: LLMGateway) -> DocTypeClassifier:
    """Запасной путь определения типа: короткий вызов быстрой модели по началу текста."""

    def classify(filename: str, text: str) -> DocTypeGuess | None:
        prompt = render("doc_type.v1", filename=filename, text=text[:1500])
        try:
            result = gateway.complete_json(
                task="doc_type",
                model_role="fast",
                system=prompt.system,
                user=prompt.user,
                schema=DocTypeAnswer,
                prompt_version=prompt.version,
                max_tokens=100,
            )
        except LLMError:
            return None
        answer = result.value
        return DocTypeGuess(answer.doc_type, round(answer.confidence, 2), "по ответу модели")

    return classify


def _prepare(
    source: Path,
    filename: str,
    document_id: uuid.UUID,
    paths: PackagePaths,
    config: IngestConfig,
    classify: DocTypeClassifier | None,
) -> _Prepared:
    extension = PurePosixPath(filename).suffix.lower()
    pdf = paths.pdf_for(document_id)
    warnings: list[str] = []
    signals: dict[str, Any] = {}
    if extension == ".docx":
        accepted = paths.tmp / f"{document_id}.accepted.docx"
        revisions = accept_docx_revisions(source, accepted)
        signals["revisions"] = revisions
        if revisions:
            source = accepted
            warnings.append(
                f"В документе были неподтверждённые исправления ({revisions}): проверена редакция "
                "с принятыми исправлениями, удалённый текст не учитывался"
            )
    page_count = convert_to_pdf(
        source,
        extension,
        pdf,
        soffice=config.soffice,
        timeout=config.convert_timeout_s,
        work_dir=paths.tmp,
    )
    hints = docx_heading_hints(source) if extension == ".docx" else {}
    ocr = ocr_if_needed(
        pdf,
        paths.tmp / f"{document_id}.ocr.pdf",
        min_chars=config.ocr_min_chars,
        languages=config.ocr_languages,
        jobs=config.ocr_jobs,
        timeout=config.ocr_timeout_s,
    )
    if ocr.pdf_path != pdf:
        ocr.pdf_path.replace(pdf)
    if ocr.error:
        warnings.append(ocr.error)
    if ocr.garbage and not ocr.error:
        pages = ", ".join(str(n) for n in ocr.garbage)
        warnings.append(f"Страницы {pages}: текстовый слой был нечитаемым — текст распознан заново")
    extracted = extract_document(pdf, config.max_pages)
    if page_count > config.max_pages:
        warnings.append(f"Обработаны первые {config.max_pages} страниц из {page_count}")
    if not extracted.text.strip():
        warnings.append("В документе не найден текст")
    structure = build_structure(extracted, PurePosixPath(filename).stem, hints)
    first_page = extracted.pages[0].text if extracted.pages else ""
    guess, source_kind = detect_doc_type(filename, structure.title, first_page), DocTypeSource.RULE
    if not guess.certain and classify is not None and first_page.strip():
        llm_guess = classify(filename, extracted.text)
        guess, by_model = choose_doc_type(guess, llm_guess)
        if by_model:
            source_kind = DocTypeSource.LLM
        elif llm_guess is not None and llm_guess.doc_type is not guess.doc_type:
            # модель не согласна с признаками: тип остаётся по признакам, мнение — для справки
            signals["doc_type_model"] = {
                "doc_type": llm_guess.doc_type.value,
                "confidence": llm_guess.confidence,
            }
    chunks = make_chunks(extracted, structure, config.chunk_max_tokens)
    hidden = hidden_spans(extracted)
    if hidden:
        signals["hidden_text"] = hidden[:MAX_HIDDEN_SIGNALS]
        signals["hidden_total"] = len(hidden)
    unreadable = unreadable_pages(extracted, ocr, page_count, config)
    if unreadable:
        signals["unreadable_pages"] = unreadable
        if not ocr.error:  # о сбое распознавания предупреждение уже есть
            warnings.append(
                f"Страницы {_page_list(unreadable)} прочитаны ненадёжно — по этому документу "
                "«в порядке» не ставится, где условие могло быть на них"
            )
    return _Prepared(
        page_count, ocr, extracted, structure, chunks, guess, source_kind, warnings, signals
    )


MAX_DOCUMENT_ATTEMPTS = 2
MAX_HIDDEN_SIGNALS = 20  # строк скрытого текста в сигналах документа
CRASHED_MESSAGE = (
    "Не удалось обработать документ: обработка аварийно прерывалась несколько раз "
    "(файл слишком большой или повреждён)"
)
INTERNAL_MESSAGE = "Не удалось обработать документ: внутренняя ошибка"


def register_attempt(
    session_factory: SessionFactory, document_id: uuid.UUID, limit: int = MAX_DOCUMENT_ATTEMPTS
) -> bool:
    """Отметить попытку обработки. False — попытки исчерпаны, документ помечен «не удалось».

    Если воркер убит посреди документа (память, зависший разбор PDF), задача возвращается
    в очередь (`acks_late`) — без счётчика она убивала бы воркер снова и снова.
    Счётчик обнуляется, когда обработка завершилась (успешно или с понятной ошибкой).
    """
    with session_factory() as db:
        document = db.get(Document, document_id, with_for_update=True)
        if document is None:
            return False
        if document.status in (DocumentStatus.READY, DocumentStatus.SKIPPED):
            return True  # готов — process_document просто вернёт статус
        document.attempts += 1
        if document.attempts > limit:
            document.status = DocumentStatus.FAILED
            document.error, document.attempts = CRASHED_MESSAGE, 0
            db.commit()
            log.warning("document_attempts_exhausted", document_id=str(document_id))
            return False
        db.commit()
        return True


def _set_status(
    session_factory: SessionFactory,
    document_id: uuid.UUID,
    status: DocumentStatus,
    error: str | None,
) -> None:
    with session_factory() as db:
        document = db.get(Document, document_id)
        if document is not None:
            document.status, document.error, document.attempts = status, error, 0
            if status is DocumentStatus.FAILED:
                _type_by_name(document)
            db.commit()


def _type_by_name(document: Document) -> None:
    """Документ не открылся — тип хотя бы по имени файла («Проект контракта.docx»): иначе
    в отчёте «Похоже на „Другой документ“» у файла, который никто не читал."""
    if document.doc_type_source is DocTypeSource.USER:
        return
    guess = detect_doc_type(document.filename, None, "")
    if guess.doc_type is not DocType.OTHER:
        document.doc_type = guess.doc_type
        document.doc_type_source = DocTypeSource.RULE
        document.doc_type_confidence = guess.confidence


def mark_document_failed(
    session_factory: SessionFactory, document_id: uuid.UUID, error: str = INTERNAL_MESSAGE
) -> None:
    _set_status(session_factory, document_id, DocumentStatus.FAILED, error)


def process_document(
    session_factory: SessionFactory,
    document_id: uuid.UUID,
    config: IngestConfig,
    classify: DocTypeClassifier | None = None,
) -> DocumentStatus:
    """Конвертация → OCR → текст → структура → тип → фрагменты. Повторный вызов безопасен."""
    with session_factory() as db:
        document = db.get(Document, document_id)
        if document is None or document.status in (DocumentStatus.READY, DocumentStatus.SKIPPED):
            return document.status if document else DocumentStatus.FAILED
        document.status, document.error = DocumentStatus.PROCESSING, None
        filename = document.filename
        paths = PackagePaths(Path(document.package.storage_path)).ensure()
        db.commit()
    try:
        prepared = _prepare(
            paths.source_for(document_id, filename), filename, document_id, paths, config, classify
        )
    except ConvertError as exc:
        _set_status(session_factory, document_id, DocumentStatus.FAILED, str(exc))
        return DocumentStatus.FAILED
    except Exception:
        log.exception("document_processing_failed", document_id=str(document_id))
        mark_document_failed(session_factory, document_id)
        return DocumentStatus.FAILED
    try:
        with session_factory() as db:
            document = db.get(Document, document_id)
            if document is None:
                return DocumentStatus.FAILED
            _save(db, document, prepared, paths)
            document.attempts = 0
            db.commit()
    except Exception:  # ошибка записи не должна оборвать проверку пакета
        log.exception("document_save_failed", document_id=str(document_id))
        mark_document_failed(session_factory, document_id)
        return DocumentStatus.FAILED
    return DocumentStatus.READY


def _save(db: Session, document: Document, prepared: _Prepared, paths: PackagePaths) -> None:
    for model in (Chunk, Section, Page):  # повторная обработка — без дублей
        db.execute(delete(model).where(model.document_id == document.id))
    ocr_pages = set(prepared.ocr.pages) if prepared.ocr.error is None else set()
    for page in prepared.extracted.pages:
        is_ocr = page.number in ocr_pages
        db.add(
            Page(
                document_id=document.id,
                number=page.number,
                text=page.text,
                char_offset_start=page.char_start,
                is_ocr=is_ocr,
                ocr_confidence=ocr_text_quality(page.text) if is_ocr else None,
                width=page.width,
                height=page.height,
            )
        )
    section_ids = [uuid7() for _ in prepared.structure.nodes]
    for node, section_id in zip(prepared.structure.nodes, section_ids, strict=True):
        db.add(
            Section(
                id=section_id,
                document_id=document.id,
                parent_id=section_ids[node.parent] if node.parent is not None else None,
                number=node.number,
                title=node.title,
                path=node.path,
                level=node.level,
                order_index=node.order,
                page_from=node.page_from,
                page_to=node.page_to,
                char_start=node.char_start,
                char_end=node.char_end,
            )
        )
    db.flush()  # разделы раньше фрагментов: внешний ключ
    for chunk in prepared.chunks:
        db.add(
            Chunk(
                document_id=document.id,
                section_id=section_ids[chunk.section_order]
                if chunk.section_order is not None
                else None,
                order_index=chunk.order,
                section_path=chunk.section_path,
                text=chunk.text,
                text_norm=chunk.text_norm,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_count=chunk.token_count,
            )
        )
    document.pdf_path = str(paths.pdf_for(document.id))
    document.page_count = prepared.page_count
    document.ocr_pages = len(ocr_pages)
    if document.doc_type_source is not DocTypeSource.USER:  # ручное исправление не затираем
        document.doc_type = prepared.doc_type.doc_type
        document.doc_type_source = prepared.doc_type_source
        document.doc_type_confidence = prepared.doc_type.confidence
    document.status = DocumentStatus.READY
    document.error = "; ".join(prepared.warnings) or None  # у готового документа — предупреждения
    document.signals = prepared.signals or None


def documents_of(
    db: Session, package_id: uuid.UUID, doc_type: DocType | None = None
) -> list[Document]:
    statement = select(Document).where(Document.package_id == package_id)
    if doc_type is not None:
        statement = statement.where(Document.doc_type == doc_type)
    return list(db.scalars(statement.order_by(Document.relative_path)))
