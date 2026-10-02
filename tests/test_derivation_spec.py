"""任务 09-28-opening-protocol-derivation 第 1 步规格测试。

这些测试描述重构后的期望行为，来自 60 runs 执行错误盘点
（.trellis/tasks/09-28-opening-protocol-derivation/research/execution-errors-inventory.md）
与六个离线反例。大切换前它们应当失败（红）；大切换后转绿。
断言只依赖大切换后依然存在的接缝：计划物化、结果验证、结果列派生和观察投影。
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_runtime.analysis_planning import materialize_analysis_plan
from agent_runtime.query_protocol import authoritative_result_columns
from agent_runtime.result_verifier import verify_analysis_result
from contracts.datasources import (
    SchemaColumnRead,
    SchemaSummaryRead,
    SchemaTableRead,
)


def _shops_schema() -> SchemaSummaryRead:
    return SchemaSummaryRead(
        datasource_id="datasource_derivation_spec",
        dialect="mysql",
        tables=[
            SchemaTableRead(
                name="shops",
                columns=[
                    SchemaColumnRead(name="shop_id", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="shop_name", type="VARCHAR", nullable=False),
                    SchemaColumnRead(name="city", type="VARCHAR", nullable=False),
                    SchemaColumnRead(name="rating", type="DECIMAL", nullable=True),
                ],
            )
        ],
    )


# ---------------------------------------------------------------------------
# A. 合法明细作为证据（q18 反例：列出上海店铺评分被判“series 缺少聚合”）
# ---------------------------------------------------------------------------


def _detail_assertion() -> dict[str, Any]:
    return {
        "description": "列出上海店铺的名称和评分",
        "source_tables": ["shops"],
        "result_columns": ["shop_name", "rating"],
        "claim_extractions": [
            {
                "mode": "series",
                "name": "上海店铺评分",
                "value_field": "rating",
                "dimension_fields": ["shop_name"],
                "required": True,
            }
        ],
    }


def test_detail_series_without_aggregate_is_accepted() -> None:
    """无聚合的明细 series 是合法证据，不再被迫改成 context_only。"""

    plan = {
        "mode": "ready",
        "requirements": [
            {
                "description": "查看上海店铺评分",
                "acceptance_criteria": ["每家店铺都有已核验的评分"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [_detail_assertion()],
                },
            }
        ],
    }
    from agent_runtime.contracts import AnalysisPlanningDraft

    result = materialize_analysis_plan(AnalysisPlanningDraft.model_validate(plan), _shops_schema())

    assert not isinstance(result, AgentFailure), (
        f"合法明细计划不应被拒绝: {getattr(result, 'validation_issues', None)}"
    )


def test_detail_series_repair_hint_does_not_recommend_context_only() -> None:
    """即使仍存在其他拒绝理由，修复建议不得教模型把明细目标改成 context_only。"""

    from agent_runtime.contracts import AnalysisPlanningDraft

    # 留一个非法字段触发 shape 失败，但断言 future 行为：不再出现
    # “series 缺少 aggregate 或 group_by” 的 finding。
    plan = {
        "mode": "ready",
        "requirements": [
            {
                "description": "查看上海店铺评分",
                "acceptance_criteria": ["每家店铺都有已核验的评分"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "列出上海店铺的名称和评分",
                            "source_tables": ["shops"],
                            "dimensions": ["shop_name"],
                            "result_columns": ["shop_name", "rating"],
                            "claim_extractions": [
                                {
                                    "mode": "series",
                                    "name": "上海店铺评分",
                                    "value_field": "rating",
                                    "dimension_fields": ["shop_name"],
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    }
    result = materialize_analysis_plan(AnalysisPlanningDraft.model_validate(plan), _shops_schema())

    findings = getattr(result, "validation_issues", []) if isinstance(result, AgentFailure) else []
    for issue in findings:
        assert "series 缺少 aggregate" not in str(getattr(issue, "actual", ""))
        assert "context_only" not in str(getattr(issue, "action", ""))


# ---------------------------------------------------------------------------
# B. 物理字段与投影别名分离（q11/q18/q19 的 7 次误拒）
# ---------------------------------------------------------------------------


def test_expected_columns_stay_projection_aliases_not_physical_references() -> None:
    """Claim 提取引用结果别名（region），而不是物理全名（customers.region）。

    q11 死因：authoritative_result_columns 把 claim 字段当结果列，而 claim 字段
    若是物理引用（customers.region）会以全名形式进入 expected_columns，结果验证
    拿全名去找投影别名（region）找不到 → 7/11 次误拒。期望：物化时 claim 字段
    被视为投影别名并去掉物理限定，expected_columns 输出别名。
    """

    from agent_runtime.contracts import (
        AnalysisAssertion,
        AnalysisSeriesClaimExtraction,
    )

    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="各大区平均订单金额",
        source_tables=["orders", "customers"],
        result_columns=["region", "avg_order_amount"],
        claim_extractions=[
            AnalysisSeriesClaimExtraction(
                mode="series",
                name="大区平均订单金额",
                value_field="region",
                dimension_fields=["avg_order_amount"],
                required=True,
            )
        ],
    )

    expected = authoritative_result_columns([assertion])

    assert "customers.region" not in expected
    assert "region" in expected


def test_result_verification_matches_projection_alias_not_qualified_name() -> None:
    """结果验证用投影别名核对；物理全名（shops.shop_id）不应被要求出现在结果列中。

    q18 死因：expected_columns 含物理全名 shops.shop_id，结果列是 shop_id，
    RESULT_EXPECTED_COLUMN_MISSING 误拒。
    """

    from contracts.datasources import TableDataRead

    result = TableDataRead(
        columns=["shop_id", "shop_name", "city", "rating"],
        rows=[
            [1, "一号店", "上海", 4.6],
            [2, "二号店", "上海", 4.2],
        ],
        row_count=2,
    )

    from agent_runtime.contracts import (
        AnalysisAssertion,
        AnalysisSeriesClaimExtraction,
    )

    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="上海店铺评分明细",
        source_tables=["shops"],
        result_columns=["shop_id", "shop_name", "city", "rating"],
        claim_extractions=[
            AnalysisSeriesClaimExtraction(
                mode="series",
                name="上海店铺",
                value_field="rating",
                dimension_fields=["shop_name"],
                required=True,
            )
        ],
    )

    verification = verify_analysis_result(
        result,
        [assertion],
        expected_columns=["shop_id", "shop_name", "city", "rating"],
    )

    assert verification.valid, [f.message for f in verification.findings]
    assert verification.verified_values, "明细行应展开为可提交事实"


# ---------------------------------------------------------------------------
# C. 预言式结果检查不再判死（q17 死局 / q13 行数 / q14 空结果 / q08 NULL）
# ---------------------------------------------------------------------------


def _scalar_assertion_with() -> Any:
    """构造带 required scalar 提取的 assertion。

    大切换删除了 AnalysisResultCheck 整族类型，原 ``check`` 参数（comparison/
    non_empty/not_null 等预言检查对象）随之消失；预言检查不再存在本身就是
    本组规格的行为。断言语句保持不变。
    """

    from agent_runtime.contracts import (
        AnalysisAssertion,
        AnalysisScalarClaimExtraction,
    )

    return AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="会员平均评分",
        source_tables=["reviews"],
        result_columns=["avg_rating"],
        claim_extractions=[
            AnalysisScalarClaimExtraction(
                mode="scalar",
                name="平均评分",
                field="avg_rating",
                required=True,
            )
        ],
    )


def test_comparison_prediction_check_no_longer_fails_query() -> None:
    """q17 死局：真实平均值落在模型预言范围之外不应判失败。

    期望行为：comparison 预言检查被移除，验证结果 valid，真实值 4.6 进入
    verified_values。
    """

    from contracts.datasources import TableDataRead

    result = TableDataRead(
        columns=["avg_rating"],
        rows=[[4.6]],
        row_count=1,
    )
    assertion = _scalar_assertion_with()

    verification = verify_analysis_result(result, [assertion])

    assert verification.valid, [f.message for f in verification.findings]
    assert verification.verified_values[0].value == pytest.approx(4.6)


def test_non_empty_prediction_check_no_longer_fails_empty_result() -> None:
    """q14：过滤条件无匹配行时，空结果是如实数据，不是失败。"""

    from contracts.datasources import TableDataRead

    result = TableDataRead(
        columns=["status", "total"],
        rows=[],
        row_count=0,
    )
    assertion = _scalar_assertion_with()

    verification = verify_analysis_result(result, [assertion])

    assert verification.valid, [f.message for f in verification.findings]


def test_not_null_prediction_check_no_longer_fails_null_sum() -> None:
    """q08：SUM 在零匹配行返回 NULL，验证不判死（空结果按无行处理）。"""

    from contracts.datasources import TableDataRead

    result = TableDataRead(
        columns=["cancelled_total_amount"],
        rows=[[None]],
        row_count=1,
    )
    assertion = _scalar_assertion_with()

    verification = verify_analysis_result(result, [assertion])

    assert verification.valid, [f.message for f in verification.findings]


# ---------------------------------------------------------------------------
# D. 观察值直通（q20：模型看到了 4 行却不知道值是“浏览/加购/下单/退款”）
# ---------------------------------------------------------------------------


def test_discovery_observation_projection_carries_safe_sample_values() -> None:
    """观察投影应携带有限的安全样例值，不再只传列名和行数。

    q20 死因：模型已执行 DISTINCT event_type，后续定稿只看到
    “event_type 列有 4 行”。期望：投影含脱敏、限量的实际值。
    """

    from agent_runtime.contracts import DiscoveryObservation
    from agent_runtime.conversation_context import discovery_observation_projection

    observation = DiscoveryObservation(
        tool_call_id="tc_1",
        audit_log_id="audit_1",
        columns=["event_type"],
        row_count=4,
        rows_truncated=False,
        sample_values={"event_type": ["浏览", "加购", "下单", "退款"]},
    )

    projection = discovery_observation_projection([observation])

    assert projection, "观察投影不应为空"
    payload = projection[0].model_dump(mode="json")
    serialized = str(payload)
    assert "浏览" in serialized, "低基数字符列的实际值必须进入定稿上下文"


def test_observation_sampling_skips_fully_masked_columns() -> None:
    """整列脱敏为占位值的掩码列不应占用观察样例预算，未脱敏列照常采样。

    mask_fields 联动：Gateway 已把掩码列的值替换为 "***"，采样器识别并跳过
    只有占位值的列；部分脱敏的列保留未脱敏值。
    """

    from agent_runtime.conversation_context import sample_observation_values
    from contracts.datasources import TableDataRead

    result = TableDataRead(
        columns=["phone", "event_type"],
        rows=[
            ["***", "浏览"],
            ["***", "加购"],
            ["***", "下单"],
        ],
        row_count=3,
    )

    samples = sample_observation_values(result)

    assert "phone" not in samples, "整列脱敏占位的掩码列不应被采样"
    assert samples.get("event_type") == ["浏览", "加购", "下单"]


def test_observation_sampling_keeps_unmasked_values_in_partially_masked_column() -> None:
    """部分脱敏的列：未脱敏值照常进入样例，占位值去重后最多保留一份。"""

    from agent_runtime.conversation_context import sample_observation_values
    from contracts.datasources import TableDataRead

    result = TableDataRead(
        columns=["note"],
        rows=[["***"], ["正常备注"], ["***"]],
        row_count=3,
    )

    samples = sample_observation_values(result)

    assert "正常备注" in samples.get("note", []), "未脱敏值必须保留"


# ---------------------------------------------------------------------------
# E. 修复建议不诱导更错的 SQL（q18：反馈教模型写 `shops.shop_id` 字面别名）
# ---------------------------------------------------------------------------


def test_missing_result_column_hint_names_the_actual_projection() -> None:
    """结果列缺失提示必须指向实际投影别名，不要求模型补物理全名列。"""

    from agent_runtime.contracts import (
        AnalysisAssertion,
        AnalysisScalarClaimExtraction,
    )
    from contracts.datasources import TableDataRead

    result = TableDataRead(
        columns=["region", "avg_amount"],
        rows=[["华东", 310.5]],
        row_count=1,
    )
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="各大区平均订单金额",
        source_tables=["orders", "customers"],
        result_columns=["region", "avg_order_amount"],
        claim_extractions=[
            AnalysisScalarClaimExtraction(
                mode="scalar",
                name="平均订单金额",
                field="avg_order_amount",
                required=True,
            )
        ],
    )

    verification = verify_analysis_result(
        result,
        [assertion],
        expected_columns=["region", "avg_order_amount"],
    )

    assert not verification.valid, "缺失列仍应被发现"
    messages = " ".join(f.message for f in verification.findings)
    assert "customers" not in messages, "提示不应要求物理全名列"
    assert "avg_order_amount" in messages, "提示应点名缺失的投影别名"


# ---------------------------------------------------------------------------
# F. 聚合匹配器不再按声明判死 SQL（q17 CAST / q02 子查询 / q08 DISTINCT）
#    大切换前的接口边界：这些断言在 preflight_sql_contract 上的期望为
#    “不再产生 AGGREGATE_MISMATCH”，但 preflight 的移除属于第 3 步；
#    这里先用 authoritative_result_columns 的别名语义锁住方向。
# ---------------------------------------------------------------------------


def test_aggregate_constraint_alias_is_not_required_for_result_columns() -> None:
    """聚合约束的 alias 不再是结果列的唯一合法来源（CAST/子查询场景）。

    q02 死因之一：契约声明 COUNT(...) AS category_count，SQL 子查询投影读出
    _col_0。期望：result_columns 声明的别名直接是权威结果列，不依赖约束别名
    的字符串匹配。大切换删除了 AnalysisSqlConstraint 整族类型，原
    sql_constraints=[AnalysisAggregateConstraint(...)] 构造随之移除；
    断言语句保持不变。
    """

    from agent_runtime.contracts import AnalysisAssertion

    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计商品类目总数",
        source_tables=["categories"],
        result_columns=["category_count"],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "商品类目总数",
                "field": "category_count",
                "required": True,
            }
        ],
    )

    expected = authoritative_result_columns([assertion])

    assert expected == ["category_count"]


# 需要在大切换中保持导入安全的失败类型
from agent_runtime.contracts import AgentFailure  # noqa: E402
