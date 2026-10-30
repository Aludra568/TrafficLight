"""Качество: прогон оценки, отметки пользователей, работа модели."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from svetofor.api.deps import CurrentUser, DbSession
from svetofor.core.config import get_settings
from svetofor.db.enums import FeedbackVerdict, FindingStatus
from svetofor.db.models import EvalRun, Feedback, Finding, LLMCall
from svetofor.llm.naming import public_model_name
from svetofor.schemas.rules import QualityOut, RuleFeedbackStats

router = APIRouter(prefix="/api", tags=["Качество"])


@router.get("/quality", response_model=QualityOut, summary="Качество проверки")
def quality(_: CurrentUser, db: DbSession) -> QualityOut:
    run = db.scalar(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(1))
    marks = Counter((f.rule_code, f.verdict) for f in db.scalars(select(Feedback)) if f.rule_code)
    codes = sorted({code for code, _ in marks})
    feedback = []
    for code in codes:
        correct = marks[(code, FeedbackVerdict.CORRECT)]
        wrong = marks[(code, FeedbackVerdict.FALSE_POSITIVE)]
        feedback.append(
            RuleFeedbackStats(
                rule_code=code,
                correct=correct,
                false_positive=wrong,
                missed=marks[(code, FeedbackVerdict.MISSED)],
                precision=round(correct / (correct + wrong), 2) if correct + wrong else None,
            )
        )
    since = datetime.now(UTC) - timedelta(days=30)
    calls = list(db.scalars(select(LLMCall).where(LLMCall.created_at >= since)))
    live = [c for c in calls if not c.cache_hit and c.error is None]
    statuses = dict(db.execute(select(Finding.status, func.count()).group_by(Finding.status)).all())
    settings = get_settings()
    return QualityOut(
        eval=(
            {
                "dataset": run.dataset,
                "created_at": run.created_at.isoformat(),
                "model_main": run.model_main,
                "metrics": run.metrics,
            }
            if run
            else None
        ),
        feedback=feedback,
        llm={
            "calls_30d": len(calls),
            "cache_hit_rate": round(sum(c.cache_hit for c in calls) / len(calls), 3)
            if calls
            else None,
            "errors_30d": sum(1 for c in calls if c.error),
            "avg_latency_ms": round(sum(c.latency_ms for c in live) / len(live)) if live else None,
            "tokens_in": sum(c.tokens_in for c in calls),
            "tokens_out": sum(c.tokens_out for c in calls),
        },
        quotes={
            "shown": statuses.get(FindingStatus.SHOWN, 0),
            "rejected_unverified": statuses.get(FindingStatus.REJECTED_UNVERIFIED, 0),
        },
        models={
            "mode": settings.effective_llm_mode.value,
            "configured": settings.llm_configured,
            "main": public_model_name(settings.llm_model_main),
            "fast": public_model_name(settings.llm_model_fast_or_main),
            "embeddings": settings.embed_model,
        },
    )
