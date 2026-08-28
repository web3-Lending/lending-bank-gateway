"""MONEY_WRITE typed 字段映射（API 规范 v2.2 §8.2）单测。

两层断言，缺一不可：
1. **逐状态钉死映射**（`_EXPECTED`）——防有人「顺手」把某个状态改判成更乐观的档；
2. **合法组合表校验**（`_is_legal_combination`）——直接把 v2.2 §8.2 那张表编码进测试，
   任何新增状态/新分支只要落到非法组合就红。第 2 层是防「改了映射也改了期望值」
   （两处一起改的自欺）的兜底：期望值可以改，规范表不行。
"""

import pytest

from app.domain.money_write import (
    MoneyWriteOperationStatus,
    MoneyWriteOutcome,
    MoneyWriteRetryPolicy,
    money_write_fields,
    money_write_reject_fields,
    money_write_unresolved_fields,
    operation_status_url,
)
from app.domain.states import OrderStatus

_BIZ = "CLT-20260828-0001234567890"


def _fields(
    status: OrderStatus,
    *,
    evidence: bool = False,
    repayment: bool = False,
    ack_trusted: bool = True,
):
    return money_write_fields(
        status,
        no_effect_evidence=evidence,
        biz_seq_no=_BIZ,
        repayment=repayment,
        ack_trusted=ack_trusted,
    )


# ── 第 1 层：逐状态钉死 ────────────────────────────────────────────────────────

# (order_status, no_effect_evidence) → (outcome | None, operationStatus, retryPolicy)
_EXPECTED = {
    (OrderStatus.SUCCEEDED, False): (
        None,
        MoneyWriteOperationStatus.SUCCEEDED,
        MoneyWriteRetryPolicy.NEVER,
    ),
    (OrderStatus.SUCCEEDED, True): (
        None,
        MoneyWriteOperationStatus.SUCCEEDED,
        MoneyWriteRetryPolicy.NEVER,
    ),
    (OrderStatus.ACCEPTED, False): (
        MoneyWriteOutcome.PENDING,
        MoneyWriteOperationStatus.PENDING,
        MoneyWriteRetryPolicy.POLL_STATUS,
    ),
    (OrderStatus.ACCEPTED, True): (
        MoneyWriteOutcome.PENDING,
        MoneyWriteOperationStatus.PENDING,
        MoneyWriteRetryPolicy.POLL_STATUS,
    ),
    (OrderStatus.SUBMITTED, False): (
        MoneyWriteOutcome.ACCEPTED,
        MoneyWriteOperationStatus.PENDING,
        MoneyWriteRetryPolicy.POLL_STATUS,
    ),
    (OrderStatus.SUBMITTED, True): (
        MoneyWriteOutcome.ACCEPTED,
        MoneyWriteOperationStatus.PENDING,
        MoneyWriteRetryPolicy.POLL_STATUS,
    ),
    (OrderStatus.PROCESSING, False): (
        MoneyWriteOutcome.ACCEPTED,
        MoneyWriteOperationStatus.PENDING,
        MoneyWriteRetryPolicy.POLL_STATUS,
    ),
    (OrderStatus.PROCESSING, True): (
        MoneyWriteOutcome.ACCEPTED,
        MoneyWriteOperationStatus.PENDING,
        MoneyWriteRetryPolicy.POLL_STATUS,
    ),
    # 有证据才 NOT_APPLIED；无证据的 FAILED 退 UNKNOWN（本模块的资金安全核心）
    (OrderStatus.FAILED, True): (
        MoneyWriteOutcome.NOT_APPLIED,
        MoneyWriteOperationStatus.REJECTED,
        MoneyWriteRetryPolicy.CORRECT_AND_NEW_INTENT,
    ),
    (OrderStatus.FAILED, False): (
        MoneyWriteOutcome.UNKNOWN,
        MoneyWriteOperationStatus.RECONCILING,
        MoneyWriteRetryPolicy.POLL_STATUS,
    ),
}
# RESULT_UNKNOWN / REVERSED / PARTIALLY_REVERSED / EXPIRED / CANCELLED：证据与否都 UNKNOWN
# （证据标志只对 FAILED 有意义——它是「拒绝是否确证零变动」，其余状态压根不是拒绝）
_CONSERVATIVE = (
    MoneyWriteOutcome.UNKNOWN,
    MoneyWriteOperationStatus.RECONCILING,
    MoneyWriteRetryPolicy.POLL_STATUS,
)
for _st in (
    OrderStatus.RESULT_UNKNOWN,
    OrderStatus.REVERSED,
    OrderStatus.PARTIALLY_REVERSED,
    OrderStatus.EXPIRED,
    OrderStatus.CANCELLED,
):
    _EXPECTED[(_st, False)] = _CONSERVATIVE
    _EXPECTED[(_st, True)] = _CONSERVATIVE


@pytest.mark.parametrize(("key", "expected"), sorted(_EXPECTED.items()))
def test_mapping_pinned_per_status(key, expected) -> None:
    status, evidence = key
    outcome, op_status, retry = expected
    got = _fields(status, evidence=evidence)
    assert got.get("outcome") == outcome
    assert got["operationStatus"] == op_status
    assert got["retryPolicy"] == retry


def test_every_order_status_is_mapped() -> None:
    """状态机新增状态时本用例先红——防新状态静默落进 else 兜底而无人复核。"""
    assert {status for status, _ in _EXPECTED} == set(OrderStatus)


# ── 第 2 层：v2.2 §8.2 合法组合表 ──────────────────────────────────────────────

_LEGAL = (
    # (allowed outcomes, allowed operationStatus, allowed retryPolicy, allowed resubmitAllowed)
    # 200 同步完成：outcome 省略
    ({None}, {MoneyWriteOperationStatus.SUCCEEDED, None}, {MoneyWriteRetryPolicy.NEVER}, {False}),
    # 已持久化受理 202——规范该行的 retryPolicy 只有 POLL_STATUS，
    # 灾难隔离态 NEVER 只出现在 UNKNOWN / ACCEPTED 两行，不许并行放宽（2026-08-28 复核 MINOR）
    (
        {MoneyWriteOutcome.PENDING},
        {MoneyWriteOperationStatus.PENDING, MoneyWriteOperationStatus.RECONCILING},
        {MoneyWriteRetryPolicy.POLL_STATUS},
        {False},
    ),
    # 上游已受理、本地控制仍在收口
    (
        {MoneyWriteOutcome.ACCEPTED},
        {MoneyWriteOperationStatus.PENDING, MoneyWriteOperationStatus.RECONCILING},
        {MoneyWriteRetryPolicy.POLL_STATUS, MoneyWriteRetryPolicy.NEVER},
        {False},
    ),
    # 有证据确认未产生影响
    (
        {MoneyWriteOutcome.NOT_APPLIED},
        {MoneyWriteOperationStatus.REJECTED, None},
        {
            MoneyWriteRetryPolicy.NEVER,
            MoneyWriteRetryPolicy.RETRY_SAME_KEY_AFTER,
            MoneyWriteRetryPolicy.CORRECT_AND_NEW_INTENT,
        },
        {True, False},  # true 仅在权威策略允许时；本仓无 9000 策略，另有用例钉死 false
    ),
    # 已 dispatch 但当前不能确认终态
    (
        {MoneyWriteOutcome.UNKNOWN},
        {MoneyWriteOperationStatus.RECONCILING},
        {MoneyWriteRetryPolicy.POLL_STATUS, MoneyWriteRetryPolicy.NEVER},
        {False},
    ),
)


def _is_legal_combination(fields) -> bool:
    outcome = fields.get("outcome")
    for outcomes, op_statuses, retries, resubmits in _LEGAL:
        if outcome in outcomes:
            return (
                fields.get("operationStatus") in op_statuses
                and fields.get("retryPolicy") in retries
                and fields.get("resubmitAllowed") in resubmits
            )
    return False


@pytest.mark.parametrize("status", sorted(OrderStatus))
@pytest.mark.parametrize("evidence", [True, False])
@pytest.mark.parametrize("ack_trusted", [True, False])
def test_all_combinations_are_legal_per_spec_table(status, evidence, ack_trusted) -> None:
    assert _is_legal_combination(_fields(status, evidence=evidence, ack_trusted=ack_trusted))


def test_illegal_combination_is_actually_detected() -> None:
    """变异自检：把合法组合改一个字段，_is_legal_combination 必须判非法。

    否则这层校验就是「不可能失败的校验」（写了等于没写）。
    """
    legal = _fields(OrderStatus.RESULT_UNKNOWN)
    assert _is_legal_combination(legal)
    assert not _is_legal_combination({**legal, "resubmitAllowed": True})
    assert not _is_legal_combination(
        {**legal, "operationStatus": MoneyWriteOperationStatus.SUCCEEDED}
    )
    assert not _is_legal_combination({**legal, "outcome": "TOTALLY_MADE_UP"})
    # PENDING + NEVER：规范表里不存在的组合（PENDING 只在「已持久化受理」行，
    # 该行 retryPolicy 只许 POLL_STATUS）——合并两行会让它蒙混过关
    assert not _is_legal_combination(
        {
            **_fields(OrderStatus.ACCEPTED),
            "retryPolicy": MoneyWriteRetryPolicy.NEVER,
        }
    )


# ── 硬性不变量 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", sorted(OrderStatus))
@pytest.mark.parametrize("evidence", [True, False])
def test_resubmit_never_allowed_and_operation_pair_always_present(status, evidence) -> None:
    """resubmitAllowed 恒 false（无 9000 权威放行策略）；operationId/statusUrl 必须成对。"""
    fields = _fields(status, evidence=evidence)
    assert fields["resubmitAllowed"] is False
    assert fields["operationId"] == _BIZ
    assert isinstance(fields["statusUrl"], str) and fields["statusUrl"]


def test_not_applied_requires_evidence() -> None:
    """无证据时任何状态都不得产出 NOT_APPLIED——这是本模块唯一的资金安全断言。"""
    assert all(
        _fields(status, evidence=False).get("outcome") != MoneyWriteOutcome.NOT_APPLIED
        for status in OrderStatus
    )


def test_sync_success_omits_outcome() -> None:
    """v2.2：同步完成由成功模型表达，不得为「字段齐全」塞 outcome。"""
    assert "outcome" not in _fields(OrderStatus.SUCCEEDED)


def test_enum_values_are_closed_per_spec() -> None:
    """三个枚举的值域必须与 v2.2 §8.2 逐字一致（多一个少一个都算私自扩表）。"""
    assert {e.value for e in MoneyWriteOutcome} == {
        "NOT_APPLIED",
        "PENDING",
        "UNKNOWN",
        "ACCEPTED",
    }
    assert {e.value for e in MoneyWriteOperationStatus} == {
        "PENDING",
        "RECONCILING",
        "SUCCEEDED",
        "REJECTED",
    }
    assert {e.value for e in MoneyWriteRetryPolicy} == {
        "NEVER",
        "RETRY_SAME_KEY_AFTER",
        "POLL_STATUS",
        "REAUTH_AND_REPLAY",
        "CORRECT_AND_NEW_INTENT",
    }


# ── statusUrl ─────────────────────────────────────────────────────────────────


def test_status_url_routes_repayment_to_dedicated_endpoint() -> None:
    """还款必须指向专用查单端点：通用 5.5 查询不返回 debtSettled / steps[]。"""
    assert operation_status_url(_BIZ, repayment=True) == (
        f"/api/v1/loans/p2p-repayments/{_BIZ}/status"
    )
    assert operation_status_url(_BIZ, repayment=False) == (
        f"/api/v1/bank-funds/status?bizSeqNo={_BIZ}"
    )


def test_status_url_percent_encodes_key() -> None:
    """validate_biz_seq_no 当前不放行这些字符，但 statusUrl 不假设别处的不变量永不放宽。"""
    assert operation_status_url("A/B?c=1", repayment=False) == (
        "/api/v1/bank-funds/status?bizSeqNo=A%2FB%3Fc%3D1"
    )
    assert operation_status_url("A/B", repayment=True) == (
        "/api/v1/loans/p2p-repayments/A%2FB/status"
    )


# ── ack 可信度降级（v2.2 §8.2 ACCEPTED = 直接上游已确认受理）──────────────────


@pytest.mark.parametrize("status", [OrderStatus.SUBMITTED, OrderStatus.PROCESSING])
def test_untrusted_ack_downgrades_accepted_to_unknown(status) -> None:
    """毒值/缺失 ack 让台账落 SUBMITTED，但对外只能说 UNKNOWN——不是「上游已确认受理」。"""
    assert _fields(status, ack_trusted=True).get("outcome") == MoneyWriteOutcome.ACCEPTED
    got = _fields(status, ack_trusted=False)
    assert got["outcome"] == MoneyWriteOutcome.UNKNOWN
    assert got["operationStatus"] == MoneyWriteOperationStatus.RECONCILING
    assert got["retryPolicy"] == MoneyWriteRetryPolicy.POLL_STATUS


@pytest.mark.parametrize("status", sorted(OrderStatus))
def test_ack_trust_only_affects_upstream_accepted_row(status) -> None:
    """可信度只作用于 SUBMITTED/PROCESSING 行：其余台账态与 ack 无关，不得被它改写。"""
    if status in (OrderStatus.SUBMITTED, OrderStatus.PROCESSING):
        return
    assert _fields(status, ack_trusted=False) == _fields(status, ack_trusted=True)


# ── 写错误 typed 字段（§8.2「写错误必须」）────────────────────────────────────


def test_reject_fields_are_legal_and_carry_no_operation_pair() -> None:
    """网关侧 dispatch 前拒绝：NOT_APPLIED，且**不许**给 operationId/statusUrl（没建单=死链）。"""
    fields = money_write_reject_fields()
    assert _is_legal_combination(fields)
    assert fields["outcome"] == MoneyWriteOutcome.NOT_APPLIED
    assert fields["retryPolicy"] == MoneyWriteRetryPolicy.CORRECT_AND_NEW_INTENT
    assert fields["resubmitAllowed"] is False
    assert "operationId" not in fields and "statusUrl" not in fields
    # 无 operation 时 operationStatus 留空（规范原文「REJECTED 或无 operation」）
    assert "operationStatus" not in fields


@pytest.mark.parametrize("repayment", [True, False])
def test_unresolved_fields_are_legal_and_pair_is_real(repayment) -> None:
    """409 幂等冲突 / dispatch 后异常：UNKNOWN + 成对查单地址（order 行必然存在）。"""
    fields = money_write_unresolved_fields(_BIZ, repayment=repayment)
    assert _is_legal_combination(fields)
    assert fields["outcome"] == MoneyWriteOutcome.UNKNOWN
    assert fields["operationStatus"] == MoneyWriteOperationStatus.RECONCILING
    assert fields["retryPolicy"] == MoneyWriteRetryPolicy.POLL_STATUS
    assert fields["resubmitAllowed"] is False
    assert fields["operationId"] == _BIZ
    assert fields["statusUrl"] == operation_status_url(_BIZ, repayment=repayment)
