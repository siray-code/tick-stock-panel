"""Funds HTTP mapping; consumers share the same dataset and service."""
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from app.data_providers.fund_flow import FundFlowQuery, FundFlowResponse, SubjectType
from app.services.fund_flow import service

router = APIRouter(prefix="/api/fund-flow", tags=["fund-flow"])


@router.get("", response_model=FundFlowResponse)
def get_fund_flow(
    subject_type: SubjectType = "industry",
    window_id: Annotated[str, Query(min_length=1, max_length=80)] = "today",
    subject_ids: Annotated[list[str] | None, Query(max_length=200)] = None,
    granularity: Literal["snapshot", "1d", "1m"] = "snapshot",
) -> dict:
    try:
        query = FundFlowQuery(subject_type=subject_type, window_id=window_id,
                              subject_ids=tuple(subject_ids or ()), granularity=granularity)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="资金流查询参数无效") from exc
    return service.get_current(query)
