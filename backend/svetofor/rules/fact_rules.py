"""Детерминированный канал карточки закупки: грамматики сроков, процентов, сумм и номеров.

Работает без модели и не может «выдумать» число: значение и цитата берутся из одного и того же
совпадения в тексте документа. Второй канал — модель (`facts.py`); каналы сверяются
(`merge_channels`): совпали — «подтверждено двумя способами», разошлись — обе цитаты
показываются и специалист проверяет. Без ключа модели карточка и пороговые правила работают.

У поля бывает несколько значений: сроки оплаты по этапам, срок подписания акта рядом со сроком
оплаты, цифры и пропись, расходящиеся из-за опечатки или ошибки распознавания («3 (тридцати)»).
Поэтому грамматика собирает **всех кандидатов**: главным
остаётся первое значение по документу, остальные идут в `candidates`, и пороговое правило
проверяет худшее из них. Срок другого действия («акт, который подписывается в течение 5 дней»)
в кандидаты не допускается.

Выражения применяются к нормализованному тексту (регистр, ё→е, единые кавычки, тире и пробелы,
склеенные переносы, без невидимых символов) и возвращают точные смещения в исходном тексте.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from svetofor.db.enums import DocType
from svetofor.rules.facts import DocumentFacts, FactValue, SourceChunk, facts_for, numbers_in
from svetofor.rules.facts_catalog import FACTS, FactKind
from svetofor.rules.numerals import NUMERAL, parse_numeral
from svetofor.rules.types.pattern import sentence_span
from svetofor.rules.verify import NormalizedText, normalize_with_map

__all__ = [
    "PATTERNS",
    "UnreadableTerm",
    "extract_rule_facts",
    "merge_channels",
    "parse_numeral",
    "same_value",
    "unparsed_terms",
    "unreadable_terms",
]

_NUM = r"(?P<num>\d{1,4}(?:[.,]\d{1,2})?)"
_WORDS = r"(?:\s*\([^)]{1,60}\))?"  # «15 (пятнадцати)»
# «в течение», «не позднее (чем через)», «не более (чем)», «в срок, не превышающий», «в пределах»
_TERM = (
    r"(?:в\s+течение|не\s+позднее(?:\s+чем)?(?:\s+через)?|не\s+более(?:\s+чем)?"
    r"|не\s+превышающ\w*|в\s+пределах|в\s+срок(?:\s+не\s+более|\s+до)?)\s+"
)
# «45 рабочих дней», «45-ти рабочих дней», «45 раб. дн.», «10 календарных дней»
_DAYS = (
    r"(?:\s*-\s*(?:ти|и|х|ми))?\s*(?P<unit>рабоч\w*|раб\.?|календарн\w*|кал\.?)?\s*"
    r"дн(?:ей|я|ь|\.|(?![а-я]))"
)
# число, испорченное распознаванием: «4S», «З0», «4б», «4 5» — цифры вперемешку с похожими буквами
_LOOKALIKE_DIGIT = r"[oоsзбlіi|]"
_BROKEN = (
    rf"(?P<broken>\d\s\d|\d+{_LOOKALIKE_DIGIT}+\d*|{_LOOKALIKE_DIGIT}+\d+{_LOOKALIKE_DIGIT}*)"
    r"(?![0-9a-zа-я])"
)
# «десяти», «двадцати пяти», «десяти (10)» — прописью, возможно с цифрами в скобках
_SPELLED = (
    rf"(?P<spelled>(?:{NUMERAL})(?:\s+(?:{NUMERAL})){{0,2}})(?![а-я])"
    r"(?:\s*\(\s*(?P<spelled_digits>\d{1,4})\s*\))?"
)
# «(7) семи» — цифры в скобках перед прописью (пакет ЕИС 0362100026126000023, 21.10.2026)
_LEAD_DIGITS = (
    rf"\(\s*(?P<lead_digits>\d{{1,4}})\s*\)\s*"
    rf"(?P<lead_words>(?:{NUMERAL})(?:\s+(?:{NUMERAL})){{0,2}})(?![а-я])"
)
_AMOUNT = rf"(?:{_NUM}{_WORDS}|{_SPELLED}|{_LEAD_DIGITS})"
_PAREN_WORDS = re.compile(r"\s*\(\s*([а-я\s]{3,60}?)\s*\)")
MAX_CANDIDATES = 8


def _spelled_after(match: re.Match[str]) -> int | None:
    """Пропись в скобках сразу после цифр: «30 (тридцати)» → 30. Нет или не число — None."""
    if match.group("lead_digits"):  # «(7) семи» — пропись после скобок
        return parse_numeral(" ".join(match.group("lead_words").split()))
    if not match.group("num"):
        return None
    paren = _PAREN_WORDS.match(match.string, match.end("num"))
    return parse_numeral(" ".join(paren.group(1).split())) if paren else None


def _amount(match: re.Match[str]) -> float | None:
    """Число из совпадения `_AMOUNT`: цифрами, цифрами в скобках после прописи или прописью."""
    if match.group("num"):
        found = numbers_in(match.group("num"))
        return found[0] if found else None
    if match.group("spelled_digits"):
        return float(match.group("spelled_digits"))
    if match.group("lead_digits"):
        return float(match.group("lead_digits"))
    value = parse_numeral(match.group("spelled"))
    return float(value) if value is not None else None


_PERCENT = r"\s*(?:%|процент)"


def _gap(exclude: str, limit: int) -> str:
    """Любые символы внутри предложения, кроме слова-исключения («оплата аванса» — не оплата)."""
    return rf"(?:(?!{exclude})[^.;]){{0,{limit}}}?"


Converted = tuple[float | bool | str, str | None]


@dataclass(frozen=True)
class _Pattern:
    key: str
    regex: re.Pattern[str]
    convert: Callable[[re.Match[str]], Converted | None]
    # не засчитывать, если раньше в том же предложении есть это (условие, исключение)
    unless_before: re.Pattern[str] | None = None


def _days(match: re.Match[str]) -> Converted | None:
    value = _amount(match)
    unit_word = match.group("unit") or ""
    unit = "working" if unit_word.startswith("раб") else "calendar" if unit_word else None
    return (value, unit) if value is not None else None


def _number(match: re.Match[str]) -> Converted | None:
    value = numbers_in(match.group(1))
    return (value[0], None) if value else None


def _constant(value: float | bool | str) -> Callable[[re.Match[str]], Converted]:
    return lambda _match: (value, None)


def _months(match: re.Match[str]) -> Converted | None:
    value = numbers_in(match.group(1))
    if not value:
        return None
    years = match.group(2).startswith(("год", "лет"))
    return (value[0] * 12 if years else value[0], None)


def _quantity(match: re.Match[str]) -> Converted | None:
    value = numbers_in(match.group(1))
    return (value[0], match.group(2)) if value else None


def _deadline(match: re.Match[str]) -> Converted:
    time = (match.group(2) or "").replace(".", ":")
    return (f"{match.group(1)} {time}".strip(), None)


def _law(match: re.Match[str]) -> Converted:
    return ("44-ФЗ" if match.group(1).startswith("44") else "223-ФЗ", None)


def _regime(match: re.Match[str]) -> Converted | None:
    sentence = match.group(0)
    if re.search(r"не\s+установлен", sentence):
        return ("none", None)
    for word, value in (
        ("запрет", "ban"),
        ("ограничени", "restriction"),
        ("преимуществ", "advantage"),
    ):
        if word in sentence:
            return (value, None)
    return None


def _p(
    key: str,
    pattern: str,
    convert: Callable[[re.Match[str]], Any],
    unless_before: str | None = None,
) -> _Pattern:
    return _Pattern(
        key, re.compile(pattern), convert, re.compile(unless_before) if unless_before else None
    )


# ─────────────────────────── сроки: якорь + все сроки предложения ───────────────────────────

_TERM_AMOUNT = re.compile(rf"{_TERM}{_AMOUNT}{_DAYS}")
_TERM_BROKEN = re.compile(rf"{_TERM}{_BROKEN}{_DAYS}")
# срок оборван: «в течение 45 рабочих» и дальше не «дней» — строка порвана вёрсткой (две колонки,
# ячейка таблицы) или распознаванием: число есть, но к чему оно — не доказано
_TERM_CUT = re.compile(
    rf"{_TERM}(?P<broken>\d{{1,4}}\s*(?:рабоч\w*|календарн\w*))(?!\s*(?:-\s*)?дн)(?![а-я])"
)
# конец предложения: точка или «;», но не «раб.», «дн.» и не десятичная точка
_SENTENCE_STOP = re.compile(r"(?<!раб)(?<!кал)(?<!дн)(?:(?<!\d)[.;]|[.;](?!\d))")
_CLAUSE_STOP = re.compile(r"[,;:(]")
# срок другого действия внутри предложения о сроке оплаты/приёмки: «акт, который
# подписывается в течение 5 дней» — это срок подписания, а не оплаты
_OTHER_ACTION = re.compile(
    r"подпис\w*|рассмотр\w*|направ\w*|составл\w*|оформл\w*|уведомл\w*|устран\w*|замен\w*"
)


# поставщик — подлежащее перед якорем в том же предложении, заказчика между ними нет
_SUPPLIER_SUBJECT = re.compile(
    r"(?<![а-я])(?:поставщик|исполнитель|подрядчик)(?![а-я])(?:(?!заказчик)[^.;]){0,60}$"
)


@dataclass(frozen=True)
class _TermPattern:
    """Срок в днях: слово-якорь («оплата») и все сроки после него в том же предложении."""

    key: str
    anchor: re.Pattern[str]
    exclude: re.Pattern[str]  # между якорем и сроком — уже о другом («аванс», «заявка»)
    window: int
    other_action: bool  # отсеивать сроки чужих действий в придаточных
    # срок без «в течение»: «Срок поставки — 10 рабочих дней», «Срок оплаты | 45 рабочих дней»
    # (строка таблицы); применяется, если в фрагменте нет срока в полной форме
    bare: re.Pattern[str] | None = None
    # поле, чьи сроки важнее: «оплата после подписания документа о приемке, который
    # подписывается в течение 5 дней, в течение 45 дней» — 45 принадлежит оплате
    yields_to: str | None = None
    # якорь не наш, если действует поставщик: «Поставщик за свой счет оплачивает доставку и
    # осуществляет поставку в течение 15 дней» — это не срок оплаты по контракту
    supplier_acts: bool = False

    def windows(self, text: str) -> Iterator[tuple[int, int]]:
        """Отрезки после якоря поля до конца предложения (не дальше `window`), где ищется срок."""
        for anchor in self.anchor.finditer(text):
            stop = _SENTENCE_STOP.search(text, anchor.end())
            yield (
                anchor.end(),
                min(stop.start() if stop else len(text), anchor.end() + self.window + 80),
            )

    def matches(self, text: str, terms: re.Pattern[str] = _TERM_AMOUNT) -> Iterator[re.Match[str]]:
        for anchor in self.anchor.finditer(text):
            if self.supplier_acts and _SUPPLIER_SUBJECT.search(
                text, max(0, anchor.start() - 80), anchor.start()
            ):
                continue
            stop = _SENTENCE_STOP.search(text, anchor.end())
            # окно — до конца предложения; срок должен начаться не дальше `window` от якоря
            end = min(stop.start() if stop else len(text), anchor.end() + self.window + 80)
            for term in terms.finditer(text, anchor.end(), end):
                if term.start() - anchor.end() > self.window:
                    break
                if self.exclude.search(text, anchor.end(), term.start()):
                    break
                if self.other_action and self._belongs_elsewhere(text, anchor.end(), term.start()):
                    continue
                yield term

    @staticmethod
    def _belongs_elsewhere(text: str, anchor_end: int, term_start: int) -> bool:
        """Срок — в придаточном про другое действие: от запятой до срока есть его глагол."""
        clause_start = anchor_end
        for stop in _CLAUSE_STOP.finditer(text, anchor_end, term_start):
            clause_start = stop.end()
        return bool(_OTHER_ACTION.search(text, clause_start, term_start))


_DELIVERY_ANCHOR = (
    r"срок\w*\s+поставки|поставк\w*\s+товара\s+осуществляется|товар\s+поставляется"
    r"|(?:осуществля|производ)\w*\s+поставк\w*"  # «осуществляет поставку в течение 15 дней»
)
TERM_PATTERNS: tuple[_TermPattern, ...] = (
    _TermPattern(
        "payment_term_working_days",
        re.compile(r"оплат\w*|оплач\w*"),  # «оплата», «оплачивает», «оплачивается»
        re.compile(r"аванс"),
        250,
        True,
        re.compile(rf"срок\w*\s+(?:оплаты|расчет\w*){_gap('аванс', 40)}{_AMOUNT}{_DAYS}"),
        supplier_acts=True,
    ),
    _TermPattern(
        # для приёмки «подписывается» — само действие приёмки, отсев не нужен
        "acceptance_term_working_days",
        re.compile(r"приемк\w*"),
        re.compile(r"оплат|оплач"),
        200,
        False,
        re.compile(rf"срок\w*\s+приемки{_gap('оплат', 40)}{_AMOUNT}{_DAYS}"),
        yields_to="payment_term_working_days",
    ),
    _TermPattern(
        "delivery_term_days",
        re.compile(_DELIVERY_ANCHOR),
        re.compile(r"заявк"),
        150,
        False,
        re.compile(rf"(?:{_DELIVERY_ANCHOR}){_gap('заявк', 150)}{_AMOUNT}{_DAYS}"),
    ),
)

# Порядок внутри ключа важен: первым идёт более специфичный вариант.
PATTERNS: tuple[_Pattern, ...] = (
    _p(
        "advance_percent",
        r"(?:аванс\w*|авансировани\w*)\s+не\s+(?:предусмотрен|выплачивается)|без\s+(?:выплаты\s+)?аванс",
        _constant(0.0),
    ),
    _p(
        "advance_percent",
        rf"аванс\w*{_gap('обеспечени', 80)}в\s+размере\s+{_NUM}{_WORDS}{_PERCENT}",
        _number,
    ),
    _p(
        # «Обеспечение исполнения контракта не требуется» — 0 %: иначе противоречие с контрактом,
        # где обеспечение есть, не видно
        "contract_security_percent",
        r"обеспечени\w*\s+исполнения\s+(?:контракта|договора)[\s:—-]*не\s+"
        r"(?:требуется|устанавливается|установлено|предусмотрено|предусматривается)",
        _constant(0.0),
        # «В случае, если участник — казённое учреждение, обеспечение … не требуется» (ч. 8
        # ст. 96 44-ФЗ) — исключение для части участников, а не «обеспечение 0 %»: иначе ложное
        # противоречие с контрактом (закупка ЕИС 0120100008026000013)
        unless_before=r"если|в\s+случае|при\s+условии|казенн",
    ),
    _p(
        "contract_security_percent",
        rf"обеспечени\w*\s+исполнения\s+(?:контракта|договора)"
        rf"{_gap('гарантийн', 150)}{_NUM}{_WORDS}{_PERCENT}",
        _number,
    ),
    _p(
        "bid_security_percent",
        rf"обеспечени\w*\s+заявк\w*{_gap('контракт', 150)}{_NUM}{_WORDS}{_PERCENT}",
        _number,
    ),
    _p(
        "warranty_security_percent",
        r"обеспечени\w*\s+гарантийн\w*\s+обязательств\w*\s+не\s+(?:требуется|установлен|предусмотрен)",
        _constant(0.0),
    ),
    _p(
        "warranty_security_percent",
        rf"обеспечени\w*\s+гарантийн\w*\s+обязательств\w*[^.;]{{0,120}}?{_NUM}{_WORDS}{_PERCENT}",
        _number,
    ),
    _p(
        "warranty_months",
        rf"гарантийн\w*\s+срок\w*[^.;]{{0,100}}?{_NUM}{_WORDS}\s*(месяц\w*|мес\b|год\w*|лет)",
        _months,
    ),
    _p(
        "nmck_rub",
        r"начальн\w*\s*\(?\s*максимальн\w*\s*\)?\s+цен\w*\s+(?:контракта|договора)[^.;]{0,60}?"
        r"(\d{1,3}(?:\s\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)\s*(?:руб|р\.|₽)",
        _number,
    ),
    _p(
        "procurement_number",
        r"(?:извещени\w*|закупк\w*|аукцион\w*)[^.;]{0,40}?№\s*(0\d{18})(?!\d)",
        lambda m: (m.group(1), None),
    ),
    _p(
        "application_deadline",
        # «Дата и время окончания срока подачи заявок: 15.10.2026 10:00 (МСК)» — строка ЕИС
        r"окончани\w*\s+срок\w*\s+подачи\s+заявок[^;]{0,60}?(\d{2}\.\d{2}\.\d{4})"
        r"(?:\s*(?:г\.?)?\s*(?:в\s*)?(\d{1,2}[:.]\d{2})(?!\.\d{4}))?",
        _deadline,
    ),
    _p("law", r"(44-фз|223-фз)", _law),
    _p(
        "national_regime",
        r"[^.;]*(?:запрет\w*|ограничени\w*|преимуществ\w*)[^.;]*(?:иностранн|происходящ)[^.;]*",
        _regime,
    ),
    _p("smp_only", r"только\s+субъект\w*\s+малого\s+предпринимательства", _constant(True)),
    _p(
        "quantity_total",
        r"(?:в\s+количестве|количеств\w*(?:\s+товара)?\s*[:\-]?)\s*(\d{1,7}(?:[.,]\d+)?)"
        r"(?:\s*\([^)]{1,60}\))?\s*(шт\w*|штук\w*|пач\w*|упаков\w*|комплект\w*|ед\w*|кг|л|м|рул\w*)",
        _quantity,
    ),
)


def _fact(
    key: str,
    value: float | bool | str,
    unit: str | None,
    chunk: SourceChunk,
    normalized: NormalizedText,
    match: re.Match[str],
) -> FactValue:
    start, end = normalized.original_span(match.start(), match.end())
    left, right = sentence_span(chunk.text, start, end)
    return FactValue(
        key=key,
        value=value,
        unit=unit,
        raw=chunk.text[start:end],
        quote=chunk.text[left:right],
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        doc_type=DocType.OTHER,  # проставляется ниже — тип известен документу
        page_from=chunk.page_from,
        char_start=chunk.char_start + left,
        char_end=chunk.char_start + right,
        score=100.0,
        source="rules",
    )


def _without_foreign_terms(
    pattern: _TermPattern, text: str, matches: list[re.Match[str]]
) -> list[re.Match[str]]:
    """Срок, который уже взяло более важное поле, — не наш: «Оплата производится после приемки
    в течение 45 дней» — 45 дней оплаты, а не приёмки. Общий срок «Приемка и оплата — в течение
    45 дней» у приёмки отсекает её исключение «оплат» ещё раньше."""
    owner = next((p for p in TERM_PATTERNS if p.key == pattern.yields_to), None)
    if owner is None:
        return matches
    foreign = {m.span() for m in owner.matches(text)}
    return [m for m in matches if m.span() not in foreign]


def _term_facts(pattern: _TermPattern, chunk: SourceChunk) -> Iterator[FactValue]:
    """Все сроки поля в фрагменте; при расхождении цифр и прописи — оба значения."""
    normalized = normalize_with_map(chunk.text)
    matches = _without_foreign_terms(
        pattern, normalized.text, list(pattern.matches(normalized.text))
    )
    if not matches and pattern.bare is not None:
        matches = list(pattern.bare.finditer(normalized.text))
    for match in matches:
        converted = _days(match)
        if converted is None:
            continue
        value, unit = converted
        yield _fact(pattern.key, value, unit, chunk, normalized, match)
        spelled = _spelled_after(match)
        if spelled is not None and isinstance(value, float) and abs(spelled - value) > 1e-6:
            # «3 (тридцати)»: цифры и пропись разошлись — ошибка распознавания или опечатка
            yield replace(
                _fact(pattern.key, float(spelled), unit, chunk, normalized, match),
                conflict={"reason": "digits_words", "digits": value, "words": spelled},
            )


def _pattern_facts(pattern: _Pattern, chunk: SourceChunk) -> Iterator[FactValue]:
    normalized = normalize_with_map(chunk.text)
    text = normalized.text
    for match in pattern.regex.finditer(text):
        if pattern.unless_before is not None:
            sentence_start = max(text.rfind(mark, 0, match.start()) for mark in ".;!?\n") + 1
            if pattern.unless_before.search(text, sentence_start, match.start()):
                continue
        converted = pattern.convert(match)
        if converted is not None:
            value, unit = converted
            yield _fact(pattern.key, value, unit, chunk, normalized, match)


def _candidate(fact: FactValue) -> dict[str, Any]:
    """Другое значение того же поля в документе — для порогового правила и отчёта."""
    return {
        "value": fact.value,
        "unit": fact.unit,
        "quote": fact.quote,
        "chunk_id": fact.chunk_id,
        "char_start": fact.char_start,
        "char_end": fact.char_end,
        "page": fact.page_from,
        **({"note": "digits_words"} if fact.conflict else {}),
    }


def _extremes(facts: list[FactValue]) -> list[FactValue]:
    """Наименьшее и наибольшее значение каждой единицы: порог «больше» или «меньше» всегда
    решается на краю, поэтому края не должны выпасть при обрезке списка."""
    numeric = [f for f in facts if isinstance(f.value, float)]
    keep: list[FactValue] = []
    for unit in dict.fromkeys(f.unit for f in numeric):
        same = [f for f in numeric if f.unit == unit]
        for fact in (max(same, key=lambda f: f.value), min(same, key=lambda f: f.value)):
            if fact not in keep:
                keep.append(fact)
    return keep


def _with_candidates(found: list[FactValue]) -> FactValue:
    """Первое значение по документу — главное; другие значения — кандидаты (без повторов).

    Кандидатов не больше `MAX_CANDIDATES`, но крайние значения сохраняются всегда: срок
    последнего из 15 этапов не должен пропасть из-за порядка в документе.
    """
    primary = replace(found[0], conflict=None)
    seen = {(primary.value, primary.unit)}
    unique: list[FactValue] = []
    for fact in found[1:]:
        if (fact.value, fact.unit) in seen:
            continue
        seen.add((fact.value, fact.unit))
        unique.append(fact)
    if len(unique) > MAX_CANDIDATES:
        edges = _extremes(unique)
        rest = [f for f in unique if f not in edges][: max(0, MAX_CANDIDATES - len(edges))]
        unique = [f for f in unique if f in edges or f in rest]
    return replace(primary, candidates=tuple(_candidate(f) for f in unique))


@dataclass(frozen=True)
class UnreadableTerm:
    """Срок поля, число в котором испорчено («4S», «4 5»): значения нет, но место известно."""

    chunk: SourceChunk
    start: int  # смещения в тексте фрагмента — предложение со сроком
    end: int
    raw: str  # испорченное число как в документе


def unreadable_terms(key: str, chunks: Sequence[SourceChunk]) -> list[UnreadableTerm]:
    """Сроки поля, которые не прочитать числом: «в течение 4S рабочих дней» рядом с якорем.

    Испорченное число нельзя ни сравнить с порогом, ни молча пропустить: рядом может стоять
    нормальный срок другого этапа, и «в порядке» по нему скрыло бы риск.
    """
    pattern = next((p for p in TERM_PATTERNS if p.key == key), None)
    if pattern is None:
        return []
    found: list[UnreadableTerm] = []
    for chunk in chunks:
        normalized = normalize_with_map(chunk.text)
        broken = [*pattern.matches(normalized.text, _TERM_BROKEN)]
        broken += pattern.matches(normalized.text, _TERM_CUT)
        for match in broken:
            start, end = normalized.original_span(match.start(), match.end())
            left, right = sentence_span(chunk.text, start, end)
            raw_start, raw_end = normalized.original_span(*match.span("broken"))
            found.append(UnreadableTerm(chunk, left, right, chunk.text[raw_start:raw_end]))
    return found


# ───────────── покрытие значений: все сроки и доли в предложениях о поле разобраны ─────────────
# Срок или доля в предложении о поле, которых не взяло ни одно выражение карточки, — значение, о
# котором система ничего не знает: «Расчёт … по истечении 45 рабочих дней», «на 46-й рабочий день»,
# «два месяца», «в размере половины цены». Рядом может стоять нормальный срок того же поля, и «в
# порядке» по нему скрыло бы риск (безопасность зелёного, шаг 2; набор eval/boundary: 9 из 12
# рисков со значениями, 0 ложных тревог на 51 чистом документе).

_QTY = (
    rf"(?:\d{{1,4}}(?:[.,]\d+)?(?:\s*-\s*[а-я]{{1,3}})?|(?:{NUMERAL})(?:\s+(?:{NUMERAL})){{0,2}}"
    r"|половин\w*|трет\w*|четверт\w*)"
)
_DURATION = re.compile(
    rf"(?<![\w-])(?:{_QTY})\s*(?:\([^)]{{1,40}}\)\s*)?(?:(?:рабоч|календарн|банковск)\w*\s+)?"
    # «час(а|ов)», но не «части» («пунктом 1 части 13 статьи 94»)
    r"(?:дн(?:ей|я|ь)|день|сут(?:ок|ки)|недел\w*|месяц\w*|час(?:а|ов|ы)?|год\w*|лет)(?![а-я])"
)
_SHARE = re.compile(
    rf"(?<![\w-])(?:{_QTY})\s*(?:%|процент\w*)|половин\w*\s+(?:цены|стоимости|суммы)"
)
# слово темы после «с даты», «после», «просрочке» — точка отсчёта, а не тема предложения
_NOT_SUBJECT = (
    r"(?<!даты\s)(?<!после\s)(?<!от\s)(?<!момента\s)(?<!дня\s)"
    r"(?<!просрочке\s)(?<!просрочки\s)(?<!просрочку\s)(?<!просрочка\s)"
)
_VALUE_TOPICS = {
    "payment_term_working_days": (
        r"оплат|оплач|расчет|перечисл\w*\s+(?:поставщик|денежн|средств|сумм)|выплачива"
    ),
    "acceptance_term_working_days": r"приемк|экспертиз|провер\w*\s+соответств",
    "delivery_term_days": r"поставк|поставля|отгруз",
    "contract_security_percent": r"обеспечени\w*\s+исполнени",
}
# предложение о другом действии, где тема поля — только точка отсчёта или упоминание (пакеты ЕИС,
# 21.10.2026): «Акт сверки расчетов — в течение 15 дней», «обеспечение возвращается в течение
# 15 дней» (слова «оплата» нет), «гарантийное обслуживание 24 месяца с даты … о приемке»,
# «за 2 рабочих дня до поставки направляет уведомление»
_OTHER_SUBJECT = {
    "payment_term_working_days": (
        r"сверк\w*\s+расчет|^(?![^.;]*(?:оплат|оплач))[^.;]*обеспечени\w*\s+исполнени"
    ),
    "acceptance_term_working_days": (
        r"гарантийн\w*\s+(?:обслуживани|срок)\w*[^.;]{0,80}?\d+\s*(?:\([^)]*\)\s*)?(?:месяц|год|лет)"
    ),
    "delivery_term_days": (
        r"\bза\s+[^.;]{0,40}?(?:дн|день|дня|час)\w*[^.;]{0,40}?\bдо\b[^.;]*уведом"
        r"|уведом[^.;]*\bза\s+[^.;]{0,40}?(?:дн|день|дня|час)\w*[^.;]{0,40}?\bдо\b"
    ),
}
# точка между цифрами — номер части или пункта («частями 7, 7.1 и 7.2»), а не конец предложения
_SENTENCE_PIECE = re.compile(r"(?:[^.;!?]|(?<=\d)\.(?=\d))+(?:[.;!?]|$)")
# год календарной даты («не позднее 15 октября 2026 года», «в 2026 году») — не срок «N лет»
_CALENDAR_YEAR = re.compile(r"(?:19|20)\d\d\s*год")
_DATE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}")  # «31.12.2026 года» — дата, не срок


def _claimed(text: str) -> list[tuple[int, int]]:
    """Отрезки нормализованного текста, которые грамматика поняла: срок после якоря любого поля
    (даже если отнесла его к другому полю — аванс, поставка по заявке, «подписывается в течение»),
    краткая форма, испорченное или оборванное число, другие факты. «В течение N дней» без якоря —
    ничей срок («Экспертиза проводится в течение сорока рабочих дней»)."""
    spans: list[tuple[int, int]] = []
    for term in TERM_PATTERNS:
        for start, end in term.windows(text):
            for regex in (_TERM_AMOUNT, _TERM_BROKEN, _TERM_CUT):
                spans += [m.span() for m in regex.finditer(text, start, end)]
        if term.bare is not None:
            spans += [m.span() for m in term.bare.finditer(text)]
    for pattern in PATTERNS:
        spans += [m.span() for m in pattern.regex.finditer(text)]
    return spans


def unparsed_terms(key: str, chunks: Sequence[SourceChunk]) -> list[UnreadableTerm]:
    """Сроки (для полей-сроков) или доли (для обеспечения) в предложениях о поле, которые не
    взяло ни одно выражение карточки. Пусто — все значения поля в тексте разобраны."""
    topic = _VALUE_TOPICS.get(key)
    if topic is None:
        return []
    values = _SHARE if key == "contract_security_percent" else _DURATION
    subject = re.compile(rf"(?:^|[^а-я]){_NOT_SUBJECT}(?:{topic})")
    found: list[UnreadableTerm] = []
    for chunk in chunks:
        normalized = normalize_with_map(chunk.text)
        text = normalized.text
        claimed = _claimed(text)
        for sentence in _SENTENCE_PIECE.finditer(text):
            piece = sentence.group(0)
            about = subject.search(piece)
            if not about or (key.startswith("payment") and "аванс" in piece):
                continue
            if key.startswith("payment") and _SUPPLIER_SUBJECT.search(
                piece, max(0, about.start() - 80), about.start() + 1
            ):
                continue  # «Поставщик за свой счет оплачивает доставку» — не оплата по контракту
            if _OTHER_SUBJECT.get(key) and re.search(_OTHER_SUBJECT[key], piece):
                continue  # срок другого действия: акт сверки, возврат обеспечения, гарантия
            dates = [m.span() for m in _DATE.finditer(piece)]
            for value in values.finditer(piece):
                start = sentence.start() + value.start()
                if any(a <= value.start() < b for a, b in dates):
                    continue
                if any(a <= start < b for a, b in claimed) or _CALENDAR_YEAR.match(value.group(0)):
                    continue
                raw_start, raw_end = normalized.original_span(start, sentence.start() + value.end())
                left, right = sentence_span(chunk.text, raw_start, raw_end)
                found.append(UnreadableTerm(chunk, left, right, chunk.text[raw_start:raw_end]))
    return found


def extract_rule_facts(
    document_id: str, doc_type: DocType, chunks: Sequence[SourceChunk]
) -> DocumentFacts:
    """Факты документа по грамматикам: первое значение по документу и все остальные — кандидаты."""
    wanted = {spec.key for spec in facts_for(doc_type)}
    found: dict[str, list[FactValue]] = {}
    for term in TERM_PATTERNS:
        if term.key in wanted:
            values = [f for chunk in chunks for f in _term_facts(term, chunk)]
            if values:
                found[term.key] = values
    for pattern in PATTERNS:
        if pattern.key not in wanted or pattern.key in found:
            continue  # у ключа несколько выражений: первое нашедшее — главное
        values = [f for chunk in chunks for f in _pattern_facts(pattern, chunk)]
        if values:
            found[pattern.key] = values
    facts: dict[str, FactValue] = {}
    for key, values in found.items():
        fact = replace(_with_candidates(values), doc_type=doc_type)
        if key.endswith("_working_days") and fact.unit is None:  # «в рабочих днях» без единицы
            fact = replace(fact, unit="working")
        facts[key] = fact
    return DocumentFacts(document_id, doc_type, facts)


def same_value(a: FactValue, b: FactValue) -> bool:
    if (
        FACTS[a.key].kind is FactKind.NUMBER
        and isinstance(a.value, float)
        and isinstance(b.value, float)
    ):
        units_differ = a.key.endswith("_days") and a.unit and b.unit and a.unit != b.unit
        return not units_differ and abs(a.value - b.value) <= 1e-6 * max(1.0, abs(a.value))
    return a.value == b.value


def merge_channels(rules: DocumentFacts, llm: DocumentFacts | None) -> DocumentFacts:
    """Сверка каналов. Расхождение не прячется: значение модели + цитата грамматики рядом.

    Кандидаты грамматики (другие значения поля в документе) сохраняются в любом случае.
    """
    model_facts = llm.facts if llm else {}
    merged: dict[str, FactValue] = {}
    for key in [k for k in FACTS if k in rules.facts or k in model_facts]:
        by_rules, by_model = rules.facts.get(key), model_facts.get(key)
        candidates = by_rules.candidates if by_rules else ()
        if by_rules and by_model:
            if same_value(by_rules, by_model):
                merged[key] = replace(by_rules, source="both", raw=by_model.raw or by_rules.raw)
            else:
                merged[key] = replace(
                    by_model,
                    source="llm",
                    candidates=candidates,
                    conflict={
                        "value": by_rules.value,
                        "unit": by_rules.unit,
                        "quote": by_rules.quote,
                        "chunk_id": by_rules.chunk_id,
                        "char_start": by_rules.char_start,
                        "char_end": by_rules.char_end,
                        "page": by_rules.page_from,
                    },
                )
        else:
            fact = by_model or by_rules
            assert fact is not None
            merged[key] = fact
    return DocumentFacts(
        rules.document_id,
        rules.doc_type,
        merged,
        list(llm.rejected) if llm else [],
        llm.error if llm else None,
        llm.model if llm else None,
        llm.prompt_version if llm else None,
    )
