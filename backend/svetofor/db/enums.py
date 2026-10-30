"""Перечисления, общие для БД, API и движка правил. Значения хранятся в БД строками."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SPECIALIST = "specialist"  # проверки, отчёты, отметки
    METHODOLOGIST = "methodologist"  # + правила
    ADMIN = "admin"


class DocType(StrEnum):
    NOTICE = "notice"  # извещение
    SPEC = "spec"  # описание объекта закупки / ТЗ
    CONTRACT = "contract"  # проект контракта
    NMCK = "nmck"  # обоснование НМЦК
    BID_REQUIREMENTS = "bid_requirements"  # требования к содержанию заявки
    OTHER = "other"


DOC_TYPE_TITLES: dict[DocType, str] = {
    DocType.NOTICE: "Извещение",
    DocType.SPEC: "Описание объекта закупки",
    DocType.CONTRACT: "Проект контракта",
    DocType.NMCK: "Обоснование НМЦК",
    DocType.BID_REQUIREMENTS: "Требования к заявке",
    DocType.OTHER: "Другой документ",
}


class DocTypeSource(StrEnum):
    RULE = "rule"
    LLM = "llm"
    USER = "user"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    SKIPPED = "skipped"
    FAILED = "failed"


class CheckStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DONE_WITH_WARNINGS = "done_with_warnings"
    FAILED = "failed"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Light(StrEnum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"
    GRAY = "gray"  # только у пакета: замечаний нет, но проверена меньшая часть правил


class Severity(StrEnum):
    RED = "red"
    YELLOW = "yellow"


class FindingKind(StrEnum):
    VIOLATION = "violation"  # условие найдено и это риск
    ABSENT = "absent"  # нужное условие не найдено
    CONTRADICTION = "contradiction"  # документы противоречат друг другу


class FindingStatus(StrEnum):
    SHOWN = "shown"
    REJECTED_UNVERIFIED = "rejected_unverified"  # цитата модели не нашлась в документе


class RuleOutcome(StrEnum):
    VIOLATION = "violation"
    OK = "ok"
    ABSENT = "absent"
    NOT_CHECKED = "not_checked"


class RuleStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    DEPRECATED = "deprecated"


class FeedbackVerdict(StrEnum):
    CORRECT = "correct"
    FALSE_POSITIVE = "false_positive"
    MISSED = "missed"


class Stage(StrEnum):
    """Этапы проверки, видимые пользователю (порядок важен)."""

    UNPACK = "unpack"
    CONVERT = "convert"
    OCR = "ocr"
    STRUCTURE = "structure"
    FACTS = "facts"
    RULES = "rules"
    FINALIZE = "finalize"


STAGE_TITLES: dict[Stage, str] = {
    Stage.UNPACK: "Распаковка архива",
    Stage.CONVERT: "Подготовка документов",
    Stage.OCR: "Распознавание сканов",
    Stage.STRUCTURE: "Разбор структуры",
    Stage.FACTS: "Карточка закупки",
    Stage.RULES: "Проверка по правилам",
    Stage.FINALIZE: "Сверка цитат и итог",
}
