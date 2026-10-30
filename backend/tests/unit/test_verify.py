"""Проверка цитат: настоящие (с OCR и переносами) принимаем, выдуманные и искажённые — нет."""

from __future__ import annotations

import pytest

from svetofor.rules.verify import QuoteVerifier, TextSource, normalize, normalize_with_map

CONTRACT = (
    "2.6. Оплата производится Заказчиком в течение 30 (тридцати) рабочих дней с даты подписания\n"
    "Заказчиком документа о приёмке.\n"
    "7.3. Заказчик вправе принять решение об одностороннем отказе от исполнения контракта "
    "в соответствии с гражданским законодательством.\n"
    "7.4. Поставщик не вправе передавать свои обязательства третьим лицам.\n"
    "8.1. Обеспе-\nчение исполнения контракта устанавливается в размере 5 % от цены контракта.\n"
)

verifier = QuoteVerifier(min_score=92)
SOURCES = [TextSource("c1", CONTRACT)]


def verify(quote: str, sources: list[TextSource] | None = None, preferred: str | None = None):
    return verifier.verify(quote, sources or SOURCES, preferred_id=preferred)


class TestNormalize:
    def test_rules(self) -> None:
        assert normalize("  «Ёлка» — ТЕСТ \n\n ") == '"елка" - тест'

    def test_hyphenation_joined(self) -> None:
        assert normalize("обеспе-\nчение") == "обеспечение"
        assert normalize("пункт 5-\n6") == "пункт 5- 6"  # дефис перед цифрой — не перенос

    def test_soft_hyphen_and_zero_width_removed(self) -> None:
        assert normalize("обес­печение​") == "обеспечение"

    def test_index_map_points_to_original(self) -> None:
        norm = normalize_with_map("A  b\nC")
        assert norm.text == "a b c"
        start, end = norm.original_span(2, 5)
        assert norm.original[start:end] == "b\nC"


class TestAccepts:
    def test_exact_with_different_whitespace(self) -> None:
        match = verify(
            "в течение 30 (тридцати) рабочих дней с даты подписания Заказчиком документа"
        )
        assert match is not None and match.exact
        assert match.text == (
            "в течение 30 (тридцати) рабочих дней с даты подписания\nЗаказчиком документа"
        )  # исходный текст с переводом строки

    def test_yo_quotes_dashes(self) -> None:
        assert verify("Заказчиком документа о приемке") is not None

    def test_hyphenated_word_across_lines(self) -> None:
        match = verify("Обеспечение исполнения контракта устанавливается в размере 5 %")
        assert match is not None
        assert match.text.startswith("Обеспе-\nчение")

    def test_ocr_typo_tolerated(self) -> None:
        # латинская «a» вместо кириллической и пропущенная буква — типичный OCR
        match = verify(
            "Зaказчик вправе принять решение об одностороннем отказе от исполнения контрата"
        )
        assert match is not None and not match.exact
        assert match.score >= 92
        assert "Заказчик вправе" in match.text

    def test_ellipsis_segments_in_order(self) -> None:
        match = verify(
            "Заказчик вправе принять решение … в соответствии с гражданским законодательством"
        )
        assert match is not None
        assert match.text.startswith("Заказчик вправе") and match.text.endswith("законодательством")

    def test_preferred_source_checked_first(self) -> None:
        other = TextSource(
            "c0", "Заказчик вправе принять решение об одностороннем отказе от исполнения"
        )
        match = verify(
            "Заказчик вправе принять решение об одностороннем отказе", [other, SOURCES[0]], "c1"
        )
        assert match is not None and match.source_id == "c1"

    def test_falls_back_to_other_sources(self) -> None:
        empty = TextSource("x", "Совсем другой текст про поставку")
        match = verify(
            "Поставщик не вправе передавать свои обязательства", [empty, SOURCES[0]], "x"
        )
        assert match is not None and match.source_id == "c1"


class TestRejects:
    def test_fabricated(self) -> None:
        assert verify("Заказчик вправе удержать оплату при любой задержке поставки") is None

    @pytest.mark.parametrize(
        "quote",
        [
            "в течение 3 (тридцати) рабочих дней с даты подписания",  # число изменено
            "в течение 30 (тридцати) рабочих дней с даты подписания и 10 дней",  # добавлено число
            "в размере 15 % от цены контракта",
        ],
    )
    def test_changed_numbers(self, quote: str) -> None:
        assert verify(quote) is None

    def test_lost_negation(self) -> None:
        assert verify("Поставщик вправе передавать свои обязательства третьим лицам") is None

    def test_added_negation(self) -> None:
        assert verify("Заказчик не вправе принять решение об одностороннем отказе") is None

    def test_quote_longer_than_source_not_accepted(self) -> None:
        short = TextSource("s", "Заказчик вправе принять решение")
        long_quote = (
            "Заказчик вправе принять решение об одностороннем отказе без каких-либо оснований"
        )
        assert verify(long_quote, [short]) is None

    def test_ellipsis_wrong_order(self) -> None:
        assert verify("гражданским законодательством … Заказчик вправе принять решение") is None

    @pytest.mark.parametrize("quote", [None, "", "оплата", "   …   "])
    def test_too_short_or_empty(self, quote: str | None) -> None:
        assert verify(quote) is None  # type: ignore[arg-type]


def test_lookalike_latin_letters_folded_only_in_mixed_words() -> None:
    from svetofor.rules.verify import fold_lookalikes

    assert fold_lookalikes("Срок пoставки") == "Срок поставки"  # латинская «o»
    assert fold_lookalikes("ГОСТ R 50, Wi-Fi, iPhone") == "ГОСТ R 50, Wi-Fi, iPhone"
    text = "Оплaта прoизводится"
    norm = normalize_with_map(text)
    assert norm.text == "оплата производится" and len(norm.index_map) == len(text)


def test_quote_found_despite_lookalikes_in_document() -> None:
    match = QuoteVerifier().verify(
        "Оплата производится в течение 7 рабочих дней",
        [TextSource("c", "2.1. Оплaта прoизводится в течение 7 рабочих дней.")],
    )
    assert match is not None and match.exact and match.text.startswith("Оплaта")


@pytest.mark.parametrize(
    ("ocr", "printed"),
    [("No 1042", "№ 1042"), ("No1042", "№1042"), ("N° 1042", "№ 1042"), ("Nо 1042", "№ 1042")],
)
def test_ocr_number_sign_reads_as_numero(ocr: str, printed: str) -> None:
    """Tesseract распознаёт «№» как «No» (замер OCR): цитата и правило с «№» всё равно сходятся."""
    text = f"постановлением Правительства РФ {ocr}"
    assert normalize(text) == normalize(f"постановлением Правительства РФ {printed}")
    mapped = normalize_with_map(text)
    start = mapped.text.index("№")
    assert text[mapped.original_span(start, start + 1)[0]] in "NН"


@pytest.mark.parametrize("text", ["Nokia 5310", "Notebook 15", "Ноутбук 15"])
def test_words_starting_with_no_are_untouched(text: str) -> None:
    assert "№" not in normalize(text)


DUTY_TEXT = (
    "6.4. Заказчик вправе произвести оплату по контракту в течение 30 рабочих дней с даты "
    "подписания документа о приёмке, если иное не установлено законодательством."
)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "modal",
    ["обязан", "должен", "обязуется"],
)
def test_changed_modality_is_rejected(modal: str, strict: bool) -> None:
    """Разведка 3, Р2: «обязан» вместо «вправе» проходило с оценкой 95,5 — и в «в порядке»."""
    quote = (
        f"Заказчик {modal} произвести оплату по контракту в течение 30 рабочих дней "
        "с даты подписания документа о приёмке"
    )
    match = QuoteVerifier().verify(quote, [TextSource("c", DUTY_TEXT)], strict=strict)
    assert match is None


def test_modality_kept_with_small_typo_still_verified() -> None:
    quote = (
        "Заказчик вправе произвести оплату по контракту в течении 30 рабочих дней "
        "с даты подписания документа о приемке"
    )
    match = QuoteVerifier().verify(quote, [TextSource("c", DUTY_TEXT)])
    assert match is not None and "вправе" in match.text


@pytest.mark.parametrize(
    ("document", "quote"),
    [
        (
            "Поставщик имеет право привлекать соисполнителей по согласованию с заказчиком.",
            "Поставщик обязан привлекать соисполнителей по согласованию с заказчиком.",
        ),
        (
            "Привлечение соисполнителей запрещается без согласия заказчика в письменной форме.",
            "Привлечение соисполнителей допускается без согласия заказчика в письменной форме.",
        ),
    ],
)
def test_right_duty_and_ban_are_different_meanings(document: str, quote: str) -> None:
    assert QuoteVerifier().verify(quote, [TextSource("c", document)]) is None


def test_quote_cut_right_after_modal_word_is_rejected() -> None:
    """Код-ревью 2026-10-08: цитата сразу после «вправе» — точная подстрока без модальности,
    сверка модальности её не видела, а смысл («оплата не обязательна») терялся."""
    quote = "произвести оплату по контракту в течение 30 рабочих дней с даты подписания документа"
    assert QuoteVerifier().verify(quote, [TextSource("c", DUTY_TEXT)], strict=True) is None


def test_quote_not_adjacent_to_modal_word_is_fine() -> None:
    quote = "оплату по контракту в течение 30 рабочих дней с даты подписания документа о приёмке"
    assert QuoteVerifier().verify(quote, [TextSource("c", DUTY_TEXT)]) is not None


def test_quote_starting_with_the_modal_word_is_fine() -> None:
    quote = "вправе произвести оплату по контракту в течение 30 рабочих дней"
    assert QuoteVerifier().verify(quote, [TextSource("c", DUTY_TEXT)], strict=True) is not None
