"""Version 1 funds dataset. Amounts are CNY; ratios are decimals, never percentages.

This is an optional protocol: existing market providers need not implement it.
Provider-native windows and measurement bases must survive every consumer boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

import polars as pl
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

SubjectType = Literal["stock", "industry", "concept"]
AmountField = Literal["inflow_amount", "outflow_amount", "net_amount"]


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class FundFlowWindow(ContractModel):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=100)
    basis: Literal["source_defined", "trading_days", "calendar_days"] = "source_defined"
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered_bounds(self) -> FundFlowWindow:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("资金流窗口起止时间无效")
        return self


class FundFlowQuery(ContractModel):
    subject_type: SubjectType
    window_id: str = Field(min_length=1, max_length=80)
    subject_ids: tuple[str, ...] = Field(default=(), max_length=200)
    granularity: Literal["snapshot", "1d", "1m"] = "snapshot"

    @field_validator("subject_ids")
    @classmethod
    def canonical_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not s.strip() or len(s) > 100 for s in value):
            raise ValueError("invalid subject ID")
        return tuple(sorted({s.strip() for s in value}))


class FundFlowCapabilities(ContractModel):
    api_version: Literal[1] = 1
    taxonomy: str = Field(min_length=1, max_length=100)
    metric_basis: str = Field(min_length=1, max_length=100)
    metric_label: str = "来源资金口径"
    subject_types: tuple[SubjectType, ...] = Field(min_length=1)
    windows: tuple[FundFlowWindow, ...] = Field(min_length=1)
    granularities: tuple[Literal["snapshot", "1d", "1m"], ...] = Field(default=("snapshot",), min_length=1)
    amount_fields: tuple[AmountField, ...] = Field(default=("inflow_amount", "outflow_amount", "net_amount"), min_length=1)
    refresh_interval_s: int = Field(default=60, ge=60, le=86400)

    @field_validator("windows")
    @classmethod
    def unique_windows(cls, windows: tuple[FundFlowWindow, ...]) -> tuple[FundFlowWindow, ...]:
        if len({w.id for w in windows}) != len(windows):
            raise ValueError("duplicate fund-flow window")
        return windows

    def supports(self, query: FundFlowQuery) -> bool:
        return (query.subject_type in self.subject_types
                and query.window_id in {w.id for w in self.windows}
                and query.granularity in self.granularities)


class FundFlowRow(ContractModel):
    source_subject_key: str = Field(min_length=1, max_length=300)
    subject_id: str | None = Field(default=None, min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=150)
    inflow_amount: float | None = Field(default=None, ge=0, strict=True)
    outflow_amount: float | None = Field(default=None, ge=0, strict=True)
    net_amount: float | None = Field(default=None, strict=True)
    change_pct: float | None = Field(default=None, strict=True)
    member_count: int | None = Field(default=None, ge=0)


@dataclass
class FundFlowBatch:
    source: str
    capabilities: FundFlowCapabilities
    query: FundFlowQuery
    frame: pl.DataFrame
    fetched_at: datetime
    source_updated_at: datetime | None = None
    amount_precision: float = 0.01
    complete: bool = True
    expected_rows: int | None = None
    api_version: int = 1


class FundFlowProvider(Protocol):
    fund_flow_capabilities: FundFlowCapabilities

    def get_fund_flow(self, query: FundFlowQuery) -> FundFlowBatch:
        """Return a complete standard batch, or raise. Never silently change source."""


class FundFlowResponse(ContractModel):
    api_version: Literal[1] = 1
    status: Literal["ok", "no_data", "stale", "error", "source_unavailable", "unsupported"]
    source: str
    query: FundFlowQuery
    capabilities: FundFlowCapabilities | None = None
    window: FundFlowWindow | None = None
    fetched_at: AwareDatetime | None = None
    source_updated_at: AwareDatetime | None = None
    amount_precision: float | None = Field(default=None, gt=0)
    currency: Literal["CNY"] = "CNY"
    complete: bool = False
    rows: tuple[FundFlowRow, ...] = ()
    message: str | None = None


def capabilities_of(provider: object) -> FundFlowCapabilities:
    caps = getattr(provider, "fund_flow_capabilities", None)
    if not isinstance(caps, FundFlowCapabilities) or not callable(getattr(provider, "get_fund_flow", None)):
        raise ValueError("数据源未实现资金流契约 v1")
    return caps


def validate_batch(batch: FundFlowBatch) -> FundFlowBatch:
    """Validate before caching. Nulls remain null; names never become canonical IDs."""
    import math

    if batch.api_version != 1 or batch.complete is not True:
        raise ValueError("资金流契约版本不兼容或批次不完整")
    if not batch.capabilities.supports(batch.query):
        raise ValueError("资金流查询不受支持")
    if not math.isfinite(batch.amount_precision) or batch.amount_precision <= 0:
        raise ValueError("资金流金额精度无效")
    if not isinstance(batch.fetched_at, datetime):
        raise ValueError("资金流必须记录抓取时刻")
    for stamp in (batch.fetched_at, batch.source_updated_at):
        if stamp is not None and (stamp.tzinfo is None or stamp.utcoffset() is None):
            raise ValueError("资金流时间必须携带时区")
    if batch.expected_rows is not None and batch.expected_rows != batch.frame.height:
        raise ValueError("资金流分页记录不完整")
    keys: set[str] = set()
    ids: set[str] = set()
    records = []
    fields = set(batch.capabilities.amount_fields)
    for raw in batch.frame.to_dicts():
        row = FundFlowRow.model_validate(raw)
        if row.source_subject_key in keys or (row.subject_id is not None and row.subject_id in ids):
            raise ValueError("资金流标的重复")
        keys.add(row.source_subject_key)
        if row.subject_id is not None:
            ids.add(row.subject_id)
        for field in ("inflow_amount", "outflow_amount", "net_amount"):
            if getattr(row, field) is not None and field not in fields:
                raise ValueError("资金流返回未声明的金额字段")
        if row.inflow_amount is not None and row.outflow_amount is not None:
            calculated = row.inflow_amount - row.outflow_amount
            if row.net_amount is None and "net_amount" in fields:
                row = row.model_copy(update={"net_amount": calculated})
            elif row.net_amount is not None and abs(row.net_amount - calculated) > batch.amount_precision * 1.5 + 1e-6:
                raise ValueError("资金流净额与流入流出不一致")
        records.append(row.model_dump())
    schema = {name: pl.String if name in {"source_subject_key", "subject_id", "name"}
              else pl.Int64 if name == "member_count" else pl.Float64
              for name in FundFlowRow.model_fields}
    return replace(batch, frame=pl.DataFrame(records, schema=schema))
