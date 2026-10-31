"""`llm_check` — смысловая проверка моделью.

Правила одной группы проверяются одним вызовом (до 6 правил), ответ — по схеме. Модель даёт
только вердикт, цитату и пояснение; цвет — из правила. Нарушение без подтверждённой цитаты в отчёт
не попадает: оно сохраняется как «отклонено проверкой цитаты» и честно считается.

Что видит модель (Self-Route):
- документы правила помещаются в контекст — модель читает их **целиком**, и её «не найдено»
  окончательно (поиск не может промахнуться);
- не помещаются — фрагменты поиска, а затем для всех правил с ответом «не найдено» —
  **второй проход** одним вызовом на группу: весь текст или все фрагменты с ключевыми словами
  правил. Пустая выдача поиска — сразу второй проход. Только после него «не найдено» становится
  «в порядке по отсутствию» (или замечанием «условие не найдено»), с записью, где искали.

Раньше второй проход был только у правил, сообщающих об отсутствии условия, и промах поиска по
остальным (в том числе красным) молча давал «в порядке» — модели часто ложно отвечают «такого
пункта нет» (ContractEval, 2025).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from svetofor.core.metrics import EXPLANATIONS_DROPPED, QUOTES_REJECTED
from svetofor.db.enums import DocType, FindingKind, FindingStatus, RuleOutcome
from svetofor.llm.errors import LLMContextTooLongError, LLMError, LLMTruncatedError
from svetofor.llm.prompting import PromptChunk, alias_chunks, render, resolve_alias, squash
from svetofor.llm.schemas import GroupResult, RuleVerdict
from svetofor.rules.facts import SourceChunk, fits_whole, select_chunks
from svetofor.rules.model import AbsentBehavior, LlmCheckRule
from svetofor.rules.results import (
    CheckContext,
    Evidence,
    FindingDraft,
    RuleOutcomeDraft,
    not_checked,
    ok_by_absence,
    ok_with_evidence,
)
from svetofor.rules.review import second_opinion
from svetofor.rules.templates import render as render_message
from svetofor.rules.types import RuleEvaluator, SearchRequest, register
from svetofor.rules.verify import TextSource

CHECK_TASK = "check_group"
# v3: текст документа — в начале промпта, правила — после (общий префикс кэшируется провайдером)
# v4: у правила могут быть пограничные примеры (examples); без них текст как в v3
CHECK_TEMPLATE = "check_group.v4"
# с пограничными примерами модель объясняет подробнее;
# при 400 ответы на группу обрывались (оценка 31.10)
_TOKENS_PER_RULE = 700
_NUMBERS = re.compile(r"\d+(?:[.,]\d+)?")


def batches(rules: Sequence[LlmCheckRule], size: int) -> list[list[LlmCheckRule]]:
    """По группам (в порядке первого появления), каждая — порциями не больше `size`."""
    groups: dict[str, list[LlmCheckRule]] = {}
    for rule in rules:
        groups.setdefault(rule.group, []).append(rule)
    return [group[i : i + size] for group in groups.values() for i in range(0, len(group), size)]


def search_request(rule: LlmCheckRule) -> SearchRequest:
    """Запрос поиска правила: подсказки и вопрос — для вектора, подсказки — для полнотекста.
    Его же берёт замер поиска без модели (`svetofor.eval.retrieval`)."""
    return SearchRequest(
        " ".join([*rule.search_hints, squash(rule.question)]), tuple(rule.search_hints)
    )


def _ids(chunks: Sequence[SourceChunk]) -> list[str]:
    return [c.id for c in chunks]


@dataclass(frozen=True)
class _Scope:
    """Текст второго прохода и подпись «где искали» для замечания и трассировки."""

    chunks: list[SourceChunk]
    text: str
    whole: bool


@register("llm_check")
class LlmCheckEvaluator(RuleEvaluator):
    def evaluate(self, rules: Sequence[LlmCheckRule], ctx: CheckContext) -> list[RuleOutcomeDraft]:
        outcomes: dict[str, RuleOutcomeDraft] = {}
        for batch in batches(rules, self.deps.max_rules_per_call):
            for outcome in self.evaluate_batch(batch, ctx):
                outcomes[outcome.rule_id] = outcome
        ordered = [outcomes[rule.id] for rule in rules]
        if self.deps.second_opinion and self.deps.gateway is not None:
            ordered = second_opinion(ordered, rules, ctx, self.deps.gateway, self.deps.check_id)
        return ordered

    # ─────────────────────────── первый проход ───────────────────────────

    def evaluate_batch(
        self, rules: Sequence[LlmCheckRule], ctx: CheckContext
    ) -> list[RuleOutcomeDraft]:
        gateway, retrieve = self.deps.gateway, self.deps.retrieve
        if gateway is None or retrieve is None:
            return [not_checked(r.id, "нет доступа к модели", "unavailable") for r in rules]
        doc_types = list(dict.fromkeys(t for r in rules for t in r.applies_to))
        candidates = [c for d in ctx.documents_of(doc_types) for c in d.chunks]
        if candidates and fits_whole(candidates, self.deps.whole_budget):
            try:  # документы целиком: «не найдено» по ним — окончательное
                outcomes = self._evaluate_with(rules, candidates, ctx)
                return [o.with_details(context=_ids(candidates), whole_text=True) for o in outcomes]
            except LLMContextTooLongError:
                pass  # оценка токенов ошиблась — дальше обычный путь через поиск
        return self._evaluate_retrieved(rules, ctx, doc_types)

    def _evaluate_retrieved(
        self, rules: Sequence[LlmCheckRule], ctx: CheckContext, doc_types: list[DocType]
    ) -> list[RuleOutcomeDraft]:
        """Документы не помещаются: фрагменты поиска по запросу каждого правила группы."""
        assert self.deps.retrieve is not None
        retrieve = self.deps.retrieve
        requests = [search_request(r) for r in rules]
        chunks = retrieve(doc_types, requests)
        if not chunks:  # поиск ничего не дал — это ещё не «условия нет»
            return list(self._second_pass(rules, ctx, ()).values())
        try:
            outcomes = self._evaluate_with(rules, chunks, ctx)
        except LLMContextTooLongError as exc:
            if len(rules) > 1:  # группа делится пополам — у каждой половины меньше текста
                middle = len(rules) // 2
                return [
                    *self.evaluate_batch(rules[:middle], ctx),
                    *self.evaluate_batch(rules[middle:], ctx),
                ]
            # одно правило: вдвое меньше фрагментов поиска, повтор один раз
            smaller = retrieve(doc_types, requests, max(1, sum(c.token_count for c in chunks) // 2))
            if not smaller or len(smaller) >= len(chunks):
                return [not_checked(rules[0].id, exc.message, exc.kind)]
            chunks = smaller
            try:
                outcomes = self._evaluate_with(rules, chunks, ctx)
            except LLMContextTooLongError as again:
                return [not_checked(rules[0].id, again.message, again.kind)]
        # что видела модель в первом проходе — отличить «не нашёл поиск» от ошибки модели;
        # semantic=False — фрагменты подобраны только по словам (`engine.guard`)
        semantic = getattr(chunks, "semantic", True)
        return [
            o.with_details(context=_ids(chunks), whole_text=False, semantic=semantic)
            for o in outcomes
        ]

    def _evaluate_with(
        self, rules: Sequence[LlmCheckRule], chunks: Sequence[SourceChunk], ctx: CheckContext
    ) -> list[RuleOutcomeDraft]:
        """Исходы по готовому контексту. «Длиннее контекста» — наружу: текст сократит вызывающий."""
        try:
            answer = self._ask(rules, chunks)
        except LLMContextTooLongError:
            raise
        except LLMTruncatedError as exc:
            if len(rules) == 1:
                return [not_checked(rules[0].id, exc.message, exc.kind)]
            # ответ не уместился — группа делится пополам, у каждой половины свой поиск
            middle = len(rules) // 2
            return [
                *self.evaluate_batch(rules[:middle], ctx),
                *self.evaluate_batch(rules[middle:], ctx),
            ]
        except LLMError as exc:
            return [not_checked(r.id, exc.message, exc.kind) for r in rules]
        missing = [
            rule
            for rule in rules
            if (v := answer.verdicts.get(rule.id)) is not None and v.verdict == "not_found"
        ]
        second = self._second_pass(missing, ctx, chunks) if missing else {}
        return [
            second[rule.id]
            if rule.id in second
            else self._apply(rule, answer.verdicts.get(rule.id), chunks, answer.prompt_chunks, ctx)
            for rule in rules
        ]

    def recheck(
        self, rules: Sequence[LlmCheckRule], chunks: Sequence[SourceChunk], ctx: CheckContext
    ) -> list[RuleOutcomeDraft]:
        """Вопрос правил по заданному тексту, без второго прохода (подтверждение отмеченного
        пункта поштучной проверкой). «Не найдено» здесь — «в этом тексте не подтвердилось», а не
        отсутствие условия. Сбой модели — наружу (`LLMError`)."""
        answer = self._ask(rules, chunks)
        result = []
        for rule in rules:
            verdict = answer.verdicts.get(rule.id)
            if verdict is None or verdict.verdict == "not_found":
                result.append(not_checked(rule.id, "в показанном тексте не подтвердилось"))
            else:
                result.append(self._apply(rule, verdict, chunks, answer.prompt_chunks, ctx))
        return result

    def _ask(self, rules: Sequence[LlmCheckRule], chunks: Sequence[SourceChunk]) -> _Answer:
        assert self.deps.gateway is not None
        prompt_chunks = alias_chunks(
            [(c.id, c.section_path, c.page_from, c.page_to, c.text) for c in chunks]
        )
        prompt = render(CHECK_TEMPLATE, rules=rules, chunks=prompt_chunks)
        result = self.deps.gateway.complete_json(
            task=CHECK_TASK,
            model_role="main",
            system=prompt.system,
            user=prompt.user,
            schema=GroupResult,
            prompt_version=prompt.version,
            max_tokens=_TOKENS_PER_RULE * len(rules) + 200,
            check_id=self.deps.check_id,
        )
        verdicts: dict[str, RuleVerdict] = {}
        for verdict in result.value.results:
            verdicts.setdefault(verdict.rule_id.strip(), verdict)
        return _Answer(verdicts, prompt_chunks)

    # ─────────────────────────── второй проход ───────────────────────────

    def _scope(self, rules: Sequence[LlmCheckRule], ctx: CheckContext, budget: int) -> _Scope:
        """Весь текст документов правил, а если не помещается — фрагменты с их ключевыми словами."""
        doc_types = list(dict.fromkeys(t for r in rules for t in r.applies_to))
        documents = ctx.documents_of(doc_types)
        candidates = [c for d in documents for c in d.chunks]
        labels = ", ".join(f"«{d.label}»" for d in documents)
        if fits_whole(candidates, budget):
            return _Scope(candidates, f"весь текст: {labels}", True)
        words = " ".join(w for r in rules for w in [*r.search_hints, r.title, r.question])
        selected = select_chunks(candidates, words, budget)
        text = (
            f"{len(selected)} из {len(candidates)} фрагментов {labels} с ключевыми словами правила"
        )
        return _Scope(selected, text, False)

    def _second_pass(
        self, rules: Sequence[LlmCheckRule], ctx: CheckContext, seen: Sequence[SourceChunk]
    ) -> dict[str, RuleOutcomeDraft]:
        """«Не найдено» в первом проходе → проверка по всему тексту (одним вызовом на группу).

        Только «не найдено» и здесь становится отсутствием условия. Сбой — «не проверено»:
        подтвердить отсутствие не удалось, а не «условия нет».
        """
        scope = self._scope(rules, ctx, self.deps.whole_budget)
        if not scope.chunks or set(_ids(scope.chunks)) <= set(_ids(seen)):
            # нового текста нет: модель уже видела всё, что можно показать
            return {
                r.id: self._absent(r, ctx, scope.text).with_details(context=_ids(seen))
                for r in rules
            }
        try:
            answer = self._ask(rules, scope.chunks)
        except LLMContextTooLongError:
            fewer = self._scope(rules, ctx, self.deps.whole_budget // 2)
            if not fewer.chunks or len(fewer.chunks) >= len(scope.chunks):
                return {r.id: self._second_pass_failed(r, scope.chunks) for r in rules}
            scope = fewer
            try:
                answer = self._ask(rules, scope.chunks)
            except LLMError as exc:
                return {r.id: self._second_pass_failed(r, scope.chunks, exc) for r in rules}
        except LLMTruncatedError as exc:
            if len(rules) == 1:
                return {rules[0].id: self._second_pass_failed(rules[0], scope.chunks, exc)}
            middle = len(rules) // 2
            return {
                **self._second_pass(rules[:middle], ctx, seen),
                **self._second_pass(rules[middle:], ctx, seen),
            }
        except LLMError as exc:
            return {r.id: self._second_pass_failed(r, scope.chunks, exc) for r in rules}
        trace = {"context": _ids(scope.chunks), "second_pass": True, "whole_text": scope.whole}
        result: dict[str, RuleOutcomeDraft] = {}
        for rule in rules:
            verdict = answer.verdicts.get(rule.id)
            if verdict is not None and verdict.verdict == "not_found":
                result[rule.id] = self._absent(rule, ctx, scope.text).with_details(**trace)
            else:
                result[rule.id] = self._apply(
                    rule, verdict, scope.chunks, answer.prompt_chunks, ctx
                ).with_details(**trace)
        return result

    @staticmethod
    def _second_pass_failed(
        rule: LlmCheckRule, seen: Sequence[SourceChunk], exc: LLMError | None = None
    ) -> RuleOutcomeDraft:
        reason = "не удалось подтвердить отсутствие условия: " + (
            exc.message if exc else "текст документа длиннее контекста модели"
        )
        return not_checked(rule.id, reason, exc.kind if exc else "context").with_details(
            context=_ids(seen), second_pass=True
        )

    # ─────────────────────────── исход по ответу ───────────────────────────

    def _apply(
        self,
        rule: LlmCheckRule,
        verdict: RuleVerdict | None,
        chunks: Sequence[SourceChunk],
        prompt_chunks: Sequence[PromptChunk],
        ctx: CheckContext,
    ) -> RuleOutcomeDraft:
        if verdict is None:
            return not_checked(rule.id, "модель не дала ответ по этому правилу")
        if verdict.verdict == "ok":
            # «в порядке» с проверенной цитатой — доказанный исход; без цитаты — неподтверждённый
            # (не засчитывается в полноту зелёного)
            ok_match = self._verified(rule, verdict, chunks, prompt_chunks, ctx)
            if ok_match is None and self._reports_absence(rule):
                # отсутствие условия здесь само риск (нет ответственности заказчика): «в порядке»
                # без найденной цитаты не доказывает, что условие есть
                return not_checked(
                    rule.id,
                    "модель сочла условие нормальным, но её цитата не нашлась в документе",
                    "unverified_ok",
                )
            return ok_with_evidence(rule.id, *([ok_match] if ok_match else []))
        if verdict.verdict == "not_found":  # сюда — только после второго прохода
            return self._absent(rule, ctx, None)
        # нарушение: цитата обязана найтись в документе
        evidence = self._verified(rule, verdict, chunks, prompt_chunks, ctx)
        if evidence is None:
            QUOTES_REJECTED.inc()
            rejected = FindingDraft(
                rule_id=rule.id,
                kind=FindingKind.VIOLATION,
                severity=rule.severity,
                title=rule.title,
                message=render_message(rule.message, quote=""),
                explanation=verdict.explanation or None,
                related={"model_quote": verdict.quote, "model_chunk": verdict.chunk_id},
                status=FindingStatus.REJECTED_UNVERIFIED,
            )
            # модель сообщила о нарушении — опровергнуть его нечем: это пробел проверки, а не
            # исход «по существу», и «критично» по такому правилу делает зелёный невозможным
            return RuleOutcomeDraft(
                rule.id,
                RuleOutcome.NOT_CHECKED,
                "модель указала на нарушение, но её цитата не нашлась в документе — "
                "проверьте вручную",
                (rejected,),
                details={"error": "unverified_violation"},
            )
        finding = FindingDraft(
            rule_id=rule.id,
            kind=FindingKind.VIOLATION,
            severity=rule.severity,
            title=rule.title,
            message=render_message(rule.message, quote=evidence.quote),
            explanation=trusted_explanation(verdict.explanation, evidence.quote, rule),
            evidence=(evidence,),
            document_id=evidence.document_id,
        )
        return RuleOutcomeDraft(rule.id, RuleOutcome.VIOLATION, None, (finding,))

    def _verified(
        self,
        rule: LlmCheckRule,
        verdict: RuleVerdict,
        chunks: Sequence[SourceChunk],
        prompt_chunks: Sequence[PromptChunk],
        ctx: CheckContext,
    ) -> Evidence | None:
        """Цитата модели → точный отрезок документа нужного типа. None — не подтвердилась."""
        allowed = [c for c in chunks if self._doc_type(c, ctx) in rule.applies_to]
        preferred = resolve_alias(verdict.chunk_id, prompt_chunks)
        match = self.deps.verifier.verify(
            verdict.quote,
            [TextSource(c.id, c.text) for c in allowed],
            preferred.source_id if preferred else None,
            strict=verdict.verdict == "ok",  # доказательство «в порядке» — без пропуска чисел
        )
        if match is None:
            return None
        chunk = next(c for c in allowed if c.id == match.source_id)
        return Evidence.from_chunk(chunk, match.start, match.end, match.score)

    @staticmethod
    def _doc_type(chunk: SourceChunk, ctx: CheckContext) -> DocType | None:
        document = ctx.document(chunk.document_id)
        return document.doc_type if document else None

    @staticmethod
    def _reports_absence(rule: LlmCheckRule) -> bool:
        return rule.absent_behavior is AbsentBehavior.REPORT and rule.absent_severity is not None

    def _absent(self, rule: LlmCheckRule, ctx: CheckContext, scope: str | None) -> RuleOutcomeDraft:
        """Условия нет в прочитанном тексте: «в порядке по отсутствию» или замечание правила."""
        if not self._reports_absence(rule) or rule.absent_severity is None:
            return ok_by_absence(rule.id, **({"search_scope": scope} if scope else {}))
        related: dict[str, object] = {
            "checked_documents": [d.label for d in ctx.documents_of(rule.applies_to)]
        }
        if scope:
            related["search_scope"] = scope
        finding = FindingDraft(
            rule_id=rule.id,
            kind=FindingKind.ABSENT,
            severity=rule.absent_severity,
            title=rule.title,
            message=rule.absent_message or rule.message,
            related=related,
        )
        return RuleOutcomeDraft(rule.id, RuleOutcome.ABSENT, None, (finding,))


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUMBERS.findall(text)}


def trusted_explanation(explanation: str, quote: str, rule: LlmCheckRule) -> str | None:
    """Пояснение модели — только если все его числа есть в цитате или в самом правиле.

    Цитата проверена, а пояснение — свободный текст: модель может «досказать» несуществующую
    статью закона или другой срок. Такое пояснение не показываем — остаются цитата и текст
    правила («почему это риск»). Номера статей приходят из правила (`legal_basis`), не от модели.
    """
    text = " ".join(explanation.split())
    if not text:
        return None
    rule_text = " ".join(
        [rule.title, rule.question, rule.violation_if, rule.ok_if, rule.message]
        + [str(v) for v in rule.params.values()]
    )
    allowed = _numbers(quote) | _numbers(rule_text)
    if not _numbers(text) <= allowed:
        EXPLANATIONS_DROPPED.inc()
        return None
    return text


@dataclass(frozen=True)
class _Answer:
    verdicts: dict[str, RuleVerdict]
    prompt_chunks: list[PromptChunk]
