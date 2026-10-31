"""Настройки приложения из переменных окружения (см. .env.example)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMMode(StrEnum):
    """Режим работы с LLM.

    live   — обычные запросы к провайдеру (ответы тоже кэшируются);
    record — то же, что live, но явно для записи «кассет» демо и тестов;
    replay — только из кэша, без сети: CI-тесты и демо без ключа.
    """

    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


DEFAULT_JWT_SECRET = "change-me-in-production-to-a-long-random-string"  # noqa: S105 — заглушка, проверяется


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Веб
    app_port: int = 8080
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_ttl_minutes: int = Field(720, ge=5)
    # демо-учётки (специалист, методолог, администратор): для жюри и стенда; в пилоте — false
    demo_users: bool = True
    demo_users_password: str = "demo"  # noqa: S105 — пароль демо-пользователей, задаётся в .env
    # описание API (/api/docs, /api/openapi.json) без входа: удобно жюри; вне демонстрации — false
    api_docs: bool = True

    # LLM
    llm_mode: LLMMode = LLMMode.LIVE
    llm_base_url: str = ""
    llm_api_key: str = ""
    # проект или каталог провайдера (заголовок OpenAI-Project), если провайдер его требует
    llm_project: str = ""
    llm_model_main: str = ""
    llm_model_fast: str = ""
    llm_supports_json_schema: bool = False
    llm_max_concurrency: int = Field(8, ge=1, le=128)
    llm_rpm: int = Field(60, ge=1)
    llm_tpm: int = Field(200_000, ge=1)
    llm_timeout_s: float = Field(120.0, gt=0)
    # запас токенов на рассуждения для моделей, которые считают их в лимите ответа
    llm_reasoning_tokens: int = Field(4000, ge=0)
    # выключить, если провайдер не понимает параметр thinking (отключение рассуждений)
    llm_thinking_toggle: bool = True
    # длина контекста модели в токенах: из неё — сколько текста документа класть в промпт
    llm_context_tokens: int = Field(32768, ge=4096)
    # второе мнение модели по красным нарушениям (ещё один короткий вызов на группу замечаний)
    llm_second_opinion: bool = True
    # поштучная проверка пунктов по правилам «критично» перед зелёным; выключена, пока её
    # точность не измерена на настоящей модели (make eval-boundary)
    green_sweep: bool = False
    llm_log_bodies: bool = False

    # Эмбеддинги
    embed_url: str = "http://embedder:80"
    # e5-small: на CPU вдвое быстрее и легче bge-m3 при том же качестве поиска (замер 31.10.2026);
    # фрагмент читается до 512 токенов. Смена модели — EMBED_MODEL/EMBED_DIM и make reembed
    embed_model: str = "intfloat/multilingual-e5-small"
    embed_dim: int = Field(384, ge=8)

    # Лимиты обработки
    max_upload_mb: int = Field(100, ge=1)
    # одновременных проверок у одного пользователя: каждая — вызовы платной модели
    # (OWASP LLM10, неограниченное потребление); проверки старше суток не считаются — зависшие
    max_active_checks_per_user: int = Field(5, ge=1)
    # загрузка закупки из ЕИС по номеру; false — без обращений к ЕИС
    eis_fetch: bool = True
    max_unpacked_mb: int = Field(1024, ge=1)
    max_files: int = Field(200, ge=1)
    max_archive_depth: int = Field(2, ge=0, le=5)
    max_compression_ratio: int = Field(100, ge=1)
    max_pages_per_doc: int = Field(1000, ge=1)
    ocr_min_chars: int = Field(50, ge=0)
    ocr_jobs: int = Field(4, ge=1)
    ocr_languages: str = "rus+eng"
    # распаковка RAR/7z/tar: bsdtar из libarchive (в Windows подходит встроенный tar.exe)
    bsdtar: str = "bsdtar"
    quote_min_score: int = Field(92, ge=50, le=100)
    chunk_max_tokens: int = Field(700, ge=100)
    legal_stale_days: int = Field(180, ge=1)

    # Инфраструктура
    database_url: str = "postgresql+psycopg://svetofor:svetofor@postgres:5432/svetofor"
    redis_url: str = "redis://redis:6379/0"
    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    log_json: bool = True
    rules_dir: Path = Path("/app/rules")  # наборы правил YAML для первой загрузки
    default_ruleset: str = "44fz-supplier-base"
    demo_package: Path = Path("/app/samples/demo_44fz_postavka.zip")
    # записанные ответы модели для демо-пакета: «Попробовать на примере» без ключа
    # (запись: python -m svetofor.eval cassette, нужен ключ модели)
    demo_cassette: Path = Path("/app/samples/demo_llm_cassette.jsonl")
    eval_dir: Path = Path("/app/eval")  # наборы для оценки и результаты прогонов
    metrics_port: int = Field(9100, ge=0)  # эндпоинт метрик воркера Celery; 0 — не поднимать
    celery_eager: bool = False  # задачи выполняются сразу в процессе (тесты, отладка без Redis)

    @property
    def llm_configured(self) -> bool:
        """Указан ли провайдер. Ключ не обязателен: локальный vLLM/Ollama работают без него."""
        return bool(self.llm_base_url and self.llm_model_main)

    @property
    def effective_llm_mode(self) -> LLMMode:
        """Без настроенного провайдера приложение работает только по записанным ответам (демо)."""
        return self.llm_mode if self.llm_configured else LLMMode.REPLAY

    @property
    def llm_model_fast_or_main(self) -> str:
        return self.llm_model_fast or self.llm_model_main

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_unpacked_bytes(self) -> int:
        return self.max_unpacked_mb * 1024 * 1024

    @property
    def packages_dir(self) -> Path:
        return self.data_dir / "packages"


@lru_cache
def get_settings() -> Settings:
    return Settings()
