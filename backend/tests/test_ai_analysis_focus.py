"""AI 分析关注重点的统一 Prompt 契约测试。"""

import pytest

from app.custom.assistant.prompt import build_system_prompt
from app.services.ai_provider import build_focus_instruction, sanitize_focus
from app.services.concept_rotation_analyzer import (
    _build_system_prompt as build_rotation_system_prompt,
)
from app.services.concept_rotation_analyzer import (
    _build_user_prompt as build_rotation_user_prompt,
)
from app.services.financial_analyzer import (
    _SYSTEM_PROMPT as FINANCIAL_SYSTEM_PROMPT,
)
from app.services.financial_analyzer import (
    _build_user_prompt as build_financial_user_prompt,
)
from app.services.market_recap import (
    _SYSTEM_PROMPT as RECAP_SYSTEM_PROMPT,
)
from app.services.market_recap import (
    _build_user_prompt as build_recap_user_prompt,
)
from app.services.stock_analyzer import (
    _SYSTEM_PROMPT as STOCK_SYSTEM_PROMPT,
)
from app.services.stock_analyzer import (
    _build_user_prompt as build_stock_user_prompt,
)


def test_focus_whitespace_is_normalized() -> None:
    assert sanitize_focus("  支撑位\n  多少  ") == "支撑位 多少"


@pytest.mark.parametrize("focus", [
    "现在能买吗,目标价多少",
    "给出仓位、止损止盈和交易计划",
    "Should I buy or sell? Give an entry price and stop loss.",
])
def test_trade_focus_is_passed_without_reframing(focus: str) -> None:
    instruction = build_focus_instruction(focus, report_name="个股分析报告")

    assert f"用户关注: {focus}" in instruction
    assert "关注重点回应" in instruction
    assert "用 2-4 条带具体数据的结论直接回应" in instruction
    assert "不得给出相应操作结论" not in instruction
    assert "请将其转换" not in instruction


@pytest.mark.parametrize("prompt", [
    STOCK_SYSTEM_PROMPT,
    FINANCIAL_SYSTEM_PROMPT,
    RECAP_SYSTEM_PROMPT,
    build_rotation_system_prompt("concept"),
    build_rotation_system_prompt("industry"),
    build_system_prompt({"page": "自选", "symbol": "600000.SH"}),
], ids=["stock", "financial", "recap", "concept", "industry", "assistant"])
def test_system_prompts_allow_advice_with_data_and_risk_context(prompt: str) -> None:
    assert "可以基于数据给出交易建议" in prompt
    assert "风险" in prompt
    assert "数据不足" in prompt or "数据缺失" in prompt
    for restriction in (
        "不提供买卖指令", "不输出操作指令", "不输出投资建议",
        "不下操作结论", "不下\"该买/该卖\"结论", "财经记者",
        "不构成投资建议", "不包含任何买卖或操作建议",
        "不涉及仓位与买卖方向", "不与投资行为挂钩",
    ):
        assert restriction not in prompt


def test_assistant_keeps_data_and_action_confirmation_contracts() -> None:
    prompt = build_system_prompt({"page": "自选", "symbol": "600000.SH"})

    assert "自选" in prompt and "600000.SH" in prompt
    assert "不编造数字" in prompt
    assert "用户点「确认」后才会执行" in prompt
    assert "拒绝或超时意味着未执行" in prompt
    assert "未经确认通过前, 不要声称操作已完成" in prompt


def test_safe_focus_requires_a_direct_answer_without_extra_warning() -> None:
    instruction = build_focus_instruction("支撑位多少", report_name="个股分析报告")

    assert "用户关注: 支撑位多少" in instruction
    assert "用 2-4 条带具体数据的结论直接回应" in instruction
    assert "不得给出相应操作结论" not in instruction


def test_all_focus_enabled_analyzers_share_the_priority_contract() -> None:
    focus = "支撑位多少"
    prompts = [
        build_stock_user_prompt([], {}, {}, 10.0, "600000.SH", focus),
        build_financial_user_prompt({}, "600000.SH", focus),
        build_recap_user_prompt({}, [], focus),
        build_rotation_user_prompt({}, {}, 12, [], focus),
    ]

    for prompt in prompts:
        assert "## 用户关注重点(必须优先回应)" in prompt
        assert f"用户关注: {focus}" in prompt
        assert "### 0. 🔎 关注重点回应" in prompt


def test_empty_focus_does_not_add_focus_section() -> None:
    prompts = [
        build_stock_user_prompt([], {}, {}, 10.0, "600000.SH", ""),
        build_financial_user_prompt({}, "600000.SH", ""),
        build_recap_user_prompt({}, [], ""),
        build_rotation_user_prompt({}, {}, 12, [], ""),
    ]

    assert all("用户关注重点" not in prompt for prompt in prompts)
