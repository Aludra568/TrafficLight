"""Тип документа по имени файла, названию и первой странице.

Сильные признаки — имя файла и название документа: в каждом из них ищется самый специфичный
тип (НМЦК раньше контракта: «Обоснование начальной (максимальной) цены контракта» —
не контракт). Слабые — характерные обороты первой страницы. Если признаки спорят или их нет,
уверенность низкая: тогда тип уточняет модель, а интерфейс спрашивает пользователя
(«Похоже на проект контракта. Верно?»).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from svetofor.db.enums import DocType

CERTAIN = 0.6  # ниже — тип нужно подтвердить (моделью или пользователем)
FIRST_PAGE_CHARS = 2000
_STRONG, _WEAK_CAP = 3, 2

# Порядок важен: от самого специфичного к самому общему.
_NAME_PATTERNS: tuple[tuple[DocType, re.Pattern[str]], ...] = tuple(
    (doc_type, re.compile(pattern))
    for doc_type, pattern in (
        (
            DocType.NMCK,
            r"нмцк|нмц\b|начальн\w* \(?максимальн\w*\)? цен|обосновани\w* цен"
            r"|расчет\w* (?:\w+ ){0,2}цен|nmck|nmtsk",
        ),
        (
            DocType.BID_REQUIREMENTS,
            r"требовани\w* к (?:содержанию|составу|оформлению)\w*.{0,40}заявк|требовани\w* к заявк"
            r"|инструкци\w* по (?:заполнению|подготовке) заявк",
        ),
        (
            DocType.SPEC,
            r"описани\w* объекта закупки|\bооз\b|техническ\w* задани|\bтз\b|техзадани"
            r"|спецификаци|техническ\w* требовани|\btz\b|tehnicheskoe",
        ),
        (
            DocType.NOTICE,
            r"\bизвещение\b|документаци\w* (?:о|об) (?:закупк|аукцион|конкурс)"
            r"|(?:закупочн|конкурсн|аукционн)\w* документаци|izvesh",
        ),
        (
            DocType.CONTRACT,
            # «Проект ГК» — государственный (муниципальный) контракт (пакет ЕИС 0173100012626000097)
            r"проект\w* (?:\w+ )?(?:контракт|договор)|\bконтракт|\bдоговор|kontrakt|dogovor"
            r"|проект\w* (?:гк|мк)\b",
        ),
    )
)

# Обороты, характерные для текста документа (слабый признак, каждый засчитывается один раз).
_BODY_PATTERNS: dict[DocType, tuple[re.Pattern[str], ...]] = {
    doc_type: tuple(re.compile(p) for p in patterns)
    for doc_type, patterns in {
        DocType.CONTRACT: (
            r"именуем\w* в дальнейшем",
            r"предмет (?:контракта|договора)",
            r"заключили настоящий (?:(?:государственн|муниципальн)\w* )?(?:контракт|договор)",
            r"(?:государственн|муниципальн)\w* (?:контракт|договор) №",
        ),
        DocType.SPEC: (
            r"описани\w* объекта закупки",
            r"техническ\w* задани",
            r"требовани\w* к (?:качеству|техническим характеристикам)",
        ),
        DocType.NMCK: (
            r"начальн\w* \(максимальн\w*\) цен",
            r"метод\w* сопоставимых рыночных цен",
            r"коммерческ\w* предложени",
        ),
        DocType.NOTICE: (
            r"извещение (?:о|об) ",
            r"идентификационн\w* код\w* закупки",
            r"окончания срока подачи заявок",
        ),
        DocType.BID_REQUIREMENTS: (
            r"требовани\w* к (?:содержанию|составу)\w*.{0,40}заявк",
            r"инструкци\w* по заполнению",
        ),
    }.items()
}


_CONTRACT_ATTACHMENT = re.compile(
    r"приложени\w*\s*(?:№\s*\d+\s*)?к\s+(?:проекту\s+)?(?:государственн\w*\s+|муниципальн\w*\s+)?"
    r"(?:контракт|договор)"
)


# «Изменения в документацию», «Изменения в проект контракта», «Дополнения к извещению»
_AMENDMENT = re.compile(r"^(?:\d+\s+)?(?:изменени|дополнени)\w*\s+(?:в|к)\s")
_AMENDS_CONTRACT = re.compile(
    r"проект\w*\s+(?:государственн\w*\s+|муниципальн\w*\s+)?(?:контракт|договор)"
    r"|(?:пункт|раздел)\w*\s+[\d.]+\s+(?:проекта\s+)?(?:контракт|договор)"
)


def _amendment_type(stem: str, title: str | None, body: str) -> DocType | None:
    """Изменения: тип — тот документ, который они меняют. «Изменения в документацию о закупке»
    с новой редакцией пункта контракта — это условия контракта: иначе новый срок оплаты не видит
    ни одно правило контракта."""
    names = [_normalize(stem), _normalize(title or "")]
    if not any(_AMENDMENT.search(n) for n in names):
        return None
    return DocType.CONTRACT if _AMENDS_CONTRACT.search(body) else DocType.NOTICE


@dataclass(frozen=True)
class DocTypeGuess:
    doc_type: DocType
    confidence: float  # 0..1
    reason: str  # почему так решили — для журнала и подсказки в интерфейсе

    @property
    def certain(self) -> bool:
        return self.confidence >= CERTAIN


def choose_doc_type(
    by_rules: DocTypeGuess, by_model: DocTypeGuess | None
) -> tuple[DocTypeGuess, bool]:
    """Тип по правилам и по модели → итоговый тип и признак «решила модель».

    Модель уточняет тип, только когда правила не нашли ни одного признака (тип «прочее»): тогда
    её ответ может лишь добавить документ под проверку. Если признаки есть, а модель не согласна,
    тип остаётся по признакам — модель не может сама вывести документ из-под правил (приложение
    «График оплаты» с оплатой через 45 дней становилось «прочим» и пакет — зелёным; безопасность
    зелёного, шаг 1). Уверенность модели — её самооценка, а не доказательство.
    """
    if by_model is None:
        return by_rules, False
    if by_rules.doc_type is DocType.OTHER:
        return by_model, True
    if by_model.doc_type is by_rules.doc_type:
        confidence = max(by_rules.confidence, by_model.confidence)
        return DocTypeGuess(
            by_rules.doc_type, confidence, f"{by_rules.reason}; модель согласна"
        ), True
    return by_rules, False


def _normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[_\s]+", " ", text)
    return text.strip()


def _name_type(text: str) -> DocType | None:
    """Самый специфичный тип, упомянутый в имени файла или названии."""
    normalized = _normalize(text)
    for doc_type, pattern in _NAME_PATTERNS:
        if pattern.search(normalized):
            return doc_type
    return None


def _file_stem(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return re.sub(r"[-.]+", " ", stem)


def detect_doc_type(filename: str, title: str | None, first_page: str) -> DocTypeGuess:
    scores = dict.fromkeys(DocType, 0)
    reasons: dict[DocType, list[str]] = {t: [] for t in DocType}
    body = _normalize(first_page[:FIRST_PAGE_CHARS])
    amended = _amendment_type(_file_stem(filename), title, body)
    if amended is not None:
        what = "проект контракта" if amended is DocType.CONTRACT else "извещение"
        return DocTypeGuess(amended, 0.7, f"изменения: меняют {what}")
    by_name = _name_type(_file_stem(filename))
    by_title = _name_type(title) if title else None
    if by_name is not None:
        scores[by_name] += _STRONG
        reasons[by_name].append("имя файла")
    if by_title is not None:
        scores[by_title] += _STRONG
        reasons[by_title].append("название документа")
    if _CONTRACT_ATTACHMENT.search(body[:300]):
        # «Приложение № 2 к контракту» (график оплаты, ответственность) — часть контракта: иначе
        # его условия не видит ни одно правило контракта. Слабее явного
        # названия: ТЗ, оформленное приложением к контракту, остаётся ТЗ
        scores[DocType.CONTRACT] += _WEAK_CAP
        reasons[DocType.CONTRACT].append("приложение к контракту")
    for doc_type, patterns in _BODY_PATTERNS.items():
        hits = sum(1 for p in patterns if p.search(body))
        if hits:
            scores[doc_type] += min(hits, _WEAK_CAP)
            reasons[doc_type].append("текст первой страницы")

    ranked = sorted(DocType, key=lambda t: (scores[t], t == by_title), reverse=True)
    best, second = ranked[0], ranked[1]
    if scores[best] == 0:
        return DocTypeGuess(DocType.OTHER, 0.0, "нет характерных признаков")
    margin = (scores[best] - scores[second]) / scores[best]
    evidence = min(1.0, scores[best] / 4)
    confidence = round(min(0.95, margin * evidence), 2)
    reason = "по признакам: " + ", ".join(reasons[best])
    if scores[second]:
        reason += f"; есть признаки другого типа ({', '.join(reasons[second])})"
    return DocTypeGuess(best, confidence, reason)
