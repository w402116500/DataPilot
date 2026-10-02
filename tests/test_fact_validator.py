from __future__ import annotations

from agent_runtime.contracts import AnalysisClaimValue
from agent_runtime.fact_validator import validate_final_answer_facts


def _fact(
    *,
    name: str,
    value: int | float | str,
    fact_key: str,
    dimensions: dict[str, str] | None = None,
    unit: str | None = None,
) -> AnalysisClaimValue:
    return AnalysisClaimValue(
        name=name,
        value=value,
        unit=unit,
        fact_key=fact_key,
        dimensions=dimensions or {},
    )


def test_grouped_markdown_values_are_checked_by_fact_key_and_dimension() -> None:
    fact = _fact(
        name="not_fully_paid_count",
        value=387,
        fact_key="not_fully_paid_count|purpose=all_other",
        dimensions={"purpose": "all_other"},
    )

    result = validate_final_answer_facts(
        "| purpose | not_fully_paid_count |\n| --- | --- |\n| all_other | 387 |\n",
        [fact],
    )

    assert result.status == "passed"
    assert result.checked_count == 1


def test_grouped_markdown_value_mismatch_is_rejected() -> None:
    fact = _fact(
        name="not_fully_paid_count",
        value=387,
        fact_key="not_fully_paid_count|purpose=all_other",
        dimensions={"purpose": "all_other"},
    )

    result = validate_final_answer_facts(
        "| purpose | not_fully_paid_count |\n| --- | --- |\n| all_other | 384 |\n",
        [fact],
    )

    assert result.status == "mismatch"
    assert result.mismatches == ("not_fully_paid_count|purpose=all_other",)


def test_labeled_ratio_checks_unit_without_guessing_other_numbers() -> None:
    fact = _fact(name="rate", value=65.7, fact_key="rate", unit="%")

    result = validate_final_answer_facts("rate: 65.7%\n", [fact])

    assert result.status == "passed"


def test_unbound_markdown_numbers_remain_unchecked() -> None:
    fact = _fact(name="total", value=42, fact_key="total")

    result = validate_final_answer_facts("这是一个没有事实标签的说明：2026。\n", [fact])

    assert result.status == "unchecked"


def test_decimal_string_accepts_thousands_separator() -> None:
    fact = _fact(name="订单金额合计", value="6747.00", fact_key="订单金额合计")

    result = validate_final_answer_facts(
        "| 订单金额合计 |\n| --- |\n| 6,747.00 |\n",
        [fact],
    )

    assert result.status == "passed"
    assert result.checked_count == 1


def test_iso_date_labelled_line_is_not_year_only() -> None:
    fact = _fact(name="最早订单日期", value="2025-01-02", fact_key="最早订单日期")

    result = validate_final_answer_facts("最早订单日期：2025-01-02\n", [fact])

    assert result.status == "passed"
    assert result.checked_count == 1


def test_display_places_half_up_matches_money_average() -> None:
    fact = _fact(name="订单金额平均值", value="281.125000", fact_key="订单金额平均值")

    result = validate_final_answer_facts("订单金额平均值：281.13\n", [fact])

    assert result.status == "passed"


def test_bankers_rounding_is_not_used_for_half_up() -> None:
    fact = _fact(name="订单金额平均值", value="281.125", fact_key="订单金额平均值")

    result = validate_final_answer_facts("订单金额平均值：281.12\n", [fact])

    assert result.status == "mismatch"
    assert result.mismatches == ("订单金额平均值",)


def test_rewritten_money_string_is_rejected() -> None:
    fact = _fact(name="订单金额合计", value="6747.00", fact_key="订单金额合计")

    result = validate_final_answer_facts(
        "| 订单金额合计 |\n| --- |\n| 6,748.00 |\n",
        [fact],
    )

    assert result.status == "mismatch"
    assert result.mismatches == ("订单金额合计",)


def test_rewritten_iso_date_is_rejected() -> None:
    fact = _fact(name="最早订单日期", value="2025-01-02", fact_key="最早订单日期")

    result = validate_final_answer_facts("最早订单日期：2025-01-03\n", [fact])

    assert result.status == "mismatch"
    assert result.mismatches == ("最早订单日期",)


def test_dimensionless_total_ignores_grouping_value_column() -> None:
    fact = _fact(name="订单金额合计", value="6747.00", fact_key="total_amount_sum")
    markdown = (
        "| 指标 | 数值 |\n"
        "| --- | --- |\n"
        "| 订单总笔数 | 24 |\n"
        "| 订单金额合计 | 6747.00 |\n"
        "\n"
        "| 渠道 | 订单金额合计 |\n"
        "| --- | --- |\n"
        "| mobile | 2964.00 |\n"
        "| store | 2334.00 |\n"
        "| web | 1449.00 |\n"
    )

    result = validate_final_answer_facts(markdown, [fact])

    assert result.status == "passed"
    assert result.checked_count == 1


def test_dimensionless_total_grouping_table_alone_stays_unchecked() -> None:
    fact = _fact(name="订单金额合计", value="6747.00", fact_key="total_amount_sum")

    result = validate_final_answer_facts(
        "| 渠道 | 订单金额合计 |\n| --- | --- |\n| mobile | 2964.00 |\n",
        [fact],
    )

    assert result.status == "unchecked"
    assert result.checked_count == 0


def test_dimensionless_total_label_rewrite_still_rejected_with_grouping_table() -> None:
    fact = _fact(name="订单金额合计", value="6747.00", fact_key="total_amount_sum")
    markdown = (
        "| 指标 | 数值 |\n"
        "| --- | --- |\n"
        "| 订单金额合计 | 6748.00 |\n"
        "\n"
        "| 渠道 | 订单金额合计 |\n"
        "| --- | --- |\n"
        "| mobile | 2964.00 |\n"
    )

    result = validate_final_answer_facts(markdown, [fact])

    assert result.status == "mismatch"
    assert result.mismatches == ("total_amount_sum",)
