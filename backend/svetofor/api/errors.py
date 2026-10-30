"""Единый формат ошибок API: {"error": {"code": "...", "message": "по-русски для пользователя"}}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from svetofor.core.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    """Ожидаемая ошибка с понятным пользователю сообщением."""

    def __init__(self, code: str, message: str, status: int = 400, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def not_found(what: str = "Объект") -> AppError:
    return AppError("not_found", f"{what} не найден", 404)


def forbidden(message: str = "Недостаточно прав для этого действия") -> AppError:
    return AppError("forbidden", message, 403)


def unauthorized(message: str = "Требуется вход в систему") -> AppError:
    return AppError("unauthorized", message, 401)


_HTTP_MESSAGES = {
    401: ("unauthorized", "Требуется вход в систему"),
    403: ("forbidden", "Недостаточно прав для этого действия"),
    404: ("not_found", "Не найдено"),
    405: ("method_not_allowed", "Метод не поддерживается"),
    413: ("too_large", "Файл слишком большой"),
}


# разбор multipart в Starlette отвечает по-английски — пользователю нужна понятная причина
_MULTIPART_MESSAGES = (
    ("too many files", "Слишком много файлов в одной загрузке — не больше 1000"),
    ("too many fields", "Слишком много полей в форме"),
    ("exceeded maximum size", "Поле формы слишком большое"),
    ("boundary", "Файл передан не полностью или в неверном формате. Повторите загрузку"),
)


def _http_message(status: int, detail: Any) -> tuple[str, str]:
    if status in _HTTP_MESSAGES:
        return _HTTP_MESSAGES[status]
    text = str(detail).lower()
    for marker, message in _MULTIPART_MESSAGES:
        if marker in text:
            return "bad_upload", message
    if status == 400:
        return "bad_request", "Некорректный запрос"
    return "http_error", "Ошибка запроса"


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _http_message(exc.status_code, exc.detail)
        return JSONResponse(_body(code, message), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "problem": err.get("msg"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            _body("validation_error", "Проверьте заполнение полей", fields), status_code=422
        )

    @app.exception_handler(DataError)
    async def _data_error(_: Request, exc: DataError) -> JSONResponse:
        # значение, которое PostgreSQL не принимает: символ NUL в строке, число вне диапазона —
        # это ошибка во входных данных, а не «внутренняя ошибка сервиса»
        log.warning("invalid_value", error=str(exc.orig).splitlines()[0] if exc.orig else "")
        return JSONResponse(
            _body("invalid_value", "Недопустимое значение в запросе: проверьте введённые данные"),
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            _body("internal_error", "Внутренняя ошибка сервиса. Повторите попытку позже"),
            status_code=500,
        )
