"""Квоты итогового состава при редактировании."""

from sqlalchemy.orm import Session

from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.db.portfolio import Portfolio
from svetofor.portfolios.admission import ensure_capacity


def capacity(db: Session, portfolio: Portfolio, sources_bytes: int, total_bytes: int) -> None:
    if portfolio.owner_id is None:
        raise AppError("owner_missing", "Владелец портфеля удалён", 409)
    if total_bytes > get_settings().max_portfolio_mb * 1024**2:
        raise AppError("too_large", "Превышен общий размер портфеля", 413)
    ensure_capacity(db, portfolio.owner_id, sources_bytes, exclude_portfolio=portfolio.id)
