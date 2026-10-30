"""Документы и обратная связь по замечаниям."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from svetofor.api.access import check_for, document_for, finding_for
from svetofor.api.deps import CurrentUser, DbSession
from svetofor.api.errors import AppError
from svetofor.db.enums import DOC_TYPE_TITLES, DocTypeSource, DocumentStatus, FeedbackVerdict
from svetofor.db.models import AuditLog, Feedback
from svetofor.schemas.checks import DocumentPatch, FeedbackIn, MissedIn

router = APIRouter(prefix="/api", tags=["Документы и отметки"])


@router.get("/documents/{document_id}/pdf", summary="PDF документа для просмотрщика")
def document_pdf(document_id: uuid.UUID, user: CurrentUser, db: DbSession) -> FileResponse:
    document = document_for(db, document_id, user)
    path = Path(document.pdf_path) if document.pdf_path else None
    if document.status is not DocumentStatus.READY or path is None or not path.is_file():
        raise AppError("pdf_missing", "Для этого документа нет PDF: он не был обработан", 404)
    return FileResponse(
        path, media_type="application/pdf", filename=f"{Path(document.filename).stem}.pdf"
    )


@router.patch("/documents/{document_id}", summary="Исправить тип документа")
def patch_document(
    document_id: uuid.UUID, body: DocumentPatch, user: CurrentUser, db: DbSession
) -> dict[str, str]:
    document = document_for(db, document_id, user)
    old = document.doc_type
    document.doc_type, document.doc_type_source = body.doc_type, DocTypeSource.USER
    document.doc_type_confidence = 1.0
    db.add(
        AuditLog(
            user_id=user.id,
            action="doc_type",
            entity="document",
            entity_id=str(document_id),
            payload={"from": old.value, "to": body.doc_type.value},
        )
    )
    db.commit()
    return {
        "doc_type": body.doc_type.value,
        "doc_type_title": DOC_TYPE_TITLES[body.doc_type],
        "hint": "Запустите перепроверку, чтобы правила учли новый тип документа",
    }


@router.post(
    "/findings/{finding_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отметка: замечание верное или ложное",
)
def feedback(finding_id: uuid.UUID, body: FeedbackIn, user: CurrentUser, db: DbSession) -> None:
    finding = finding_for(db, finding_id, user)
    if body.verdict is FeedbackVerdict.MISSED:
        raise AppError("bad_verdict", "Для пропущенного риска — отдельная кнопка", 400)
    row = db.scalar(
        select(Feedback).where(Feedback.finding_id == finding_id, Feedback.user_id == user.id)
    )
    if row is None:
        row = Feedback(finding_id=finding_id, check_id=finding.check_id, user_id=user.id)
        db.add(row)
    row.verdict, row.rule_code, row.comment = body.verdict, finding.rule_code, body.comment
    db.commit()


@router.post(
    "/checks/{check_id}/missed",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Система пропустила риск",
)
def missed(check_id: uuid.UUID, body: MissedIn, user: CurrentUser, db: DbSession) -> None:
    check_for(db, check_id, user)
    db.add(
        Feedback(
            check_id=check_id,
            user_id=user.id,
            verdict=FeedbackVerdict.MISSED,
            rule_code=body.rule_code,
            comment=body.comment,
        )
    )
    db.commit()
