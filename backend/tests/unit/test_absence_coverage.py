"""Смысловой поиск не доказывает полного просмотра применимого текста."""

import pytest

from svetofor.db.enums import RuleOutcome
from svetofor.rules.engine import guard
from svetofor.rules.results import RuleOutcomeDraft
from tests.unit.test_engine import RULES, context


@pytest.mark.parametrize("outcome", [RuleOutcome.OK, RuleOutcome.ABSENT])
@pytest.mark.parametrize("whole", [None, False, True])
def test_absence_requires_explicit_whole_text_even_with_semantic_search(outcome, whole):
    draft = RuleOutcomeDraft(
        "LIA-01",
        outcome,
        details={"basis": "absence", "whole_text": whole, "semantic": True},
    )
    result = guard(RULES["LIA-01"], draft, context(semantic_search=True))
    assert result.outcome is (outcome if whole else RuleOutcome.NOT_CHECKED)
