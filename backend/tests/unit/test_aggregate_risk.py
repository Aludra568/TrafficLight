import math

import pytest

from svetofor.db.enums import Light, Severity
from svetofor.rules.aggregate import light_from_severities, package_light, package_verdict
from svetofor.rules.risk_index import (
    Recommendation,
    RiskModel,
    RiskModelError,
    ahp_weights,
    category_scores,
    combine_weights,
    entropy_weights,
)

R, Y = Severity.RED, Severity.YELLOW


class TestLights:
    @pytest.mark.parametrize(
        ("severities", "expected"),
        [
            ([], Light.GREEN),
            ([Y], Light.YELLOW),
            ([Y, Y], Light.YELLOW),
            ([Y, R], Light.RED),
            ([R], Light.RED),
        ],
    )
    def test_document(self, severities: list[Severity], expected: Light) -> None:
        assert light_from_severities(severities) is expected

    def test_package_takes_worst_including_cross_document(self) -> None:
        assert package_light([Light.GREEN, Light.YELLOW]) is Light.YELLOW
        assert package_light([Light.GREEN, Light.GREEN], [R]) is Light.RED
        assert package_light([]) is Light.GREEN

    @pytest.mark.parametrize(
        ("lights", "assessed", "total", "expected"),
        [
            ([Light.GREEN], 28, 28, Light.GREEN),
            ([Light.GREEN], 14, 28, Light.GREEN),  # половина — достаточно
            ([Light.GREEN], 13, 28, Light.GRAY),
            ([Light.GREEN], 0, 28, Light.GRAY),
            ([], 0, 0, Light.GRAY),  # ни один документ не открылся
            ([Light.RED], 1, 28, Light.RED),  # найденный риск — риск при любой полноте
            ([Light.YELLOW], 0, 28, Light.YELLOW),
        ],
    )
    def test_verdict_needs_coverage_for_green(
        self, lights: list[Light], assessed: int, total: int, expected: Light
    ) -> None:
        assert package_verdict(lights, [], assessed, total) is expected

    def test_unchecked_red_capable_rule_blocks_green(self) -> None:
        """Проверено 27 из 28, но не проверен из-за сбоя «односторонний отказ» — не зелёный."""
        assert package_verdict([Light.GREEN], [], 27, 28, critical_gaps=1) is Light.GRAY
        assert package_verdict([Light.YELLOW], [], 27, 28, critical_gaps=1) is Light.YELLOW

    def test_red_capable_and_failure(self) -> None:
        from svetofor.rules.aggregate import can_be_red, is_failure
        from tests.support.rulesets import STARTER_RULES

        by_id = {r.id: r for r in STARTER_RULES}
        assert can_be_red(by_id["TRM-01"])  # смысловое, красное
        assert can_be_red(by_id["PAY-01"])  # порог с красным условием
        assert can_be_red(by_id["LIA-02"])  # «условие не найдено» — красное
        assert not can_be_red(by_id["PAY-02"])  # только жёлтое
        assert is_failure({"error": "unavailable"}) and is_failure({"blocked": "unreadable"})
        assert not is_failure(None) and not is_failure({"context": []})


class TestAhp:
    def test_consistent_matrix(self) -> None:
        # Идеально согласованная: веса 4:2:1
        m = [[1, 2, 4], [0.5, 1, 2], [0.25, 0.5, 1]]
        result = ahp_weights(m)
        assert [round(w, 4) for w in result.weights] == [
            round(4 / 7, 4),
            round(2 / 7, 4),
            round(1 / 7, 4),
        ]
        assert math.isclose(result.lambda_max, 3.0, abs_tol=1e-9)
        assert result.consistency_ratio == pytest.approx(0.0, abs=1e-9)
        assert result.consistent

    def test_saaty_textbook_example(self) -> None:
        # Пересчитано вручную (средние геометрические): w ≈ (0,157; 0,594; 0,249),
        # λmax ≈ 3,054, CI = (λmax − 3) / 2 ≈ 0,027, CR = CI / 0,58 ≈ 0,046 < 0,1

        m = [[1, 1 / 3, 1 / 2], [3, 1, 3], [2, 1 / 3, 1]]
        result = ahp_weights(m)
        assert result.lambda_max == pytest.approx(3.0536, abs=1e-3)
        assert result.consistency_ratio == pytest.approx(0.046, abs=2e-3)
        assert result.consistent
        assert max(result.weights) == result.weights[1]

    def test_inconsistent_flagged(self) -> None:
        # A > B, B > C, но C ≫ A — противоречие
        m = [[1, 5, 1 / 7], [1 / 5, 1, 5], [7, 1 / 5, 1]]
        assert not ahp_weights(m).consistent

    def test_non_reciprocal_rejected(self) -> None:
        with pytest.raises(RiskModelError, match="обратно-симметрична"):
            ahp_weights([[1, 2], [2, 1]])

    def test_non_square_rejected(self) -> None:
        with pytest.raises(RiskModelError, match="квадратной"):
            ahp_weights([[1, 2, 3], [0.5, 1, 1]])


class TestEntropy:
    def test_constant_column_gets_zero_weight(self) -> None:
        scores = [[1.0, 0.0], [1.0, 1.0], [1.0, 0.5]]
        w = entropy_weights(scores)
        assert w[0] == pytest.approx(0.0, abs=1e-9)
        assert w[1] == pytest.approx(1.0)

    def test_all_zero_uniform(self) -> None:
        assert entropy_weights([[0, 0], [0, 0]]) == (0.5, 0.5)

    def test_single_package_uniform(self) -> None:
        assert entropy_weights([[1, 0, 0.5]]) == pytest.approx((1 / 3, 1 / 3, 1 / 3))

    def test_combine(self) -> None:
        assert combine_weights([0.8, 0.2], [0.2, 0.8], alpha=0.5) == pytest.approx((0.5, 0.5))
        with pytest.raises(RiskModelError):
            combine_weights([1.0], [0.5, 0.5])


class TestRiskModel:
    def test_index_and_recommendation(self) -> None:
        model = RiskModel(weights={"payment": 0.5, "liability": 0.3, "terms": 0.2})
        scores = category_scores([("payment", Y), ("payment", R), ("terms", Y)])
        assert scores == {"payment": 1.0, "terms": 0.5}
        idx = model.index(scores)
        assert idx == pytest.approx(60.0)
        assert model.recommend(idx) is Recommendation.HIGH_RISK
        assert model.recommend(29.9) is Recommendation.PARTICIPATE
        assert model.recommend(30) is Recommendation.CAUTION
        # критичный риск при низком индексе: не «можно участвовать» рядом с красным светофором
        assert model.recommend(8.3, critical=1) is Recommendation.CAUTION
        assert model.recommend(75, critical=1) is Recommendation.HIGH_RISK
        assert model.contributions(scores) == {"payment": 50.0, "liability": 0.0, "terms": 10.0}

    def test_uniform(self) -> None:
        model = RiskModel.uniform(["a", "b", "c", "d"])
        assert model.index({"a": 1.0}) == 25.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"weights": {}},
            {"weights": {"a": 0.7}},
            {"weights": {"a": 1.0}, "caution_from": 70, "high_from": 60},
        ],
    )
    def test_validation(self, kwargs: dict) -> None:
        with pytest.raises(RiskModelError):
            RiskModel(**kwargs)
