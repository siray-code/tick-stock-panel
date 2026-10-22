"""Shared funds routing, validation and snapshots; no supplier imports here."""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future

from app.data_providers import custom as custom_sources
from app.data_providers.capabilities import capability_gate
from app.data_providers.fund_flow import (
    FundFlowQuery,
    FundFlowResponse,
    capabilities_of,
    validate_batch,
)
from app.market_time import cn_now
from app.services import preferences

logger = logging.getLogger(__name__)


class _ProviderIdentity:
    """Keep a bounded strong reference so a reloaded provider cannot reuse an old id."""
    def __init__(self, provider: object):
        self.provider = provider

    def __hash__(self):
        return id(self.provider)

    def __eq__(self, other):
        return isinstance(other, _ProviderIdentity) and self.provider is other.provider


class FundFlowService:
    def __init__(self, *, clock: Callable = time.monotonic, now: Callable = cn_now):
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, tuple[float, FundFlowResponse]] = OrderedDict()
        self._inflight: dict[tuple, Future] = {}

    def close(self) -> None:
        """Router shutdown terminates this dataset's active optional workers."""
        with self._lock:
            providers = {key[1] for key in (*self._cache, *self._inflight)}
            self._cache.clear()
        for identity in providers:
            close = getattr(identity.provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 — continue releasing other optional workers
                    logger.warning("fund-flow close failed: kind=%s", type(exc).__name__)

    def get_current(self, query: FundFlowQuery) -> dict:
        """Public business entry: resolve the same independent route for every consumer."""
        source = preferences.get_fund_flow_provider()
        usable, _ = capability_gate("fund_flow")
        if usable:
            try:
                provider = custom_sources.get_provider(source)
            except (KeyError, ValueError):
                pass
            else:
                return self.get(source, provider, query)
        return FundFlowResponse(
            status="source_unavailable", source=source, query=query,
            message="资金流数据源未就绪，请在数据源设置安装或选择可用插件",
        ).model_dump(mode="json")

    def get(self, source: str, provider: object, query: FundFlowQuery) -> dict:
        """Coalesce readers; publish complete immutable snapshots outside network locks."""
        if query.subject_ids:
            # Share the complete source snapshot across cards/subsets instead of N+1 fetches.
            response = self.get(source, provider, query.model_copy(update={"subject_ids": ()}))
            response["query"] = query.model_dump(mode="json")
            response["rows"] = [r for r in response["rows"] if r["subject_id"] in query.subject_ids]
            if response["status"] == "ok" and not response["rows"]:
                response["status"] = "no_data"
            return response
        try:
            caps = capabilities_of(provider)
        except ValueError as exc:
            return FundFlowResponse(status="source_unavailable", source=source, query=query,
                                    message=str(exc)).model_dump(mode="json")
        if not caps.supports(query):
            return FundFlowResponse(status="unsupported", source=source, query=query, capabilities=caps,
                                    message="当前数据源不支持所选标的、窗口或粒度").model_dump(mode="json")
        key = (source, _ProviderIdentity(provider), caps.model_dump_json(), query.model_dump_json(), self._now().date())
        with self._lock:
            cached = self._cache.get(key)
            if cached and self._clock() - cached[0] < caps.refresh_interval_s:
                return cached[1].model_dump(mode="json")
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future
        if not owner:
            return future.result(timeout=55).model_dump(mode="json")
        try:
            batch = validate_batch(provider.get_fund_flow(query))
            if batch.source != source or batch.query != query or batch.capabilities != caps:
                raise ValueError("资金流批次来源或查询上下文不一致")
            frame = batch.frame
            result = FundFlowResponse(
                status="ok" if frame.height else "no_data", source=source, query=query,
                capabilities=caps, window=next(w for w in caps.windows if w.id == query.window_id),
                fetched_at=batch.fetched_at, source_updated_at=batch.source_updated_at,
                amount_precision=batch.amount_precision, complete=True,
                rows=tuple(frame.to_dicts()),
            )
        except Exception as exc:  # noqa: BLE001 — provider failures must not break other datasets
            # Provider exceptions may contain credential-bearing request URLs.
            logger.warning("fund-flow fetch failed: source=%s kind=%s", source, type(exc).__name__)
            if cached and cached[1].complete:
                result = cached[1].model_copy(update={"status": "stale", "message": "本轮更新失败，保留上次完整数据"})
            else:
                result = FundFlowResponse(status="error", source=source, query=query, capabilities=caps,
                                          message="资金流获取失败，请稍后重试或检查数据源")
        with self._lock:
            self._cache[key] = (self._clock(), result)
            self._cache.move_to_end(key)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
            self._inflight.pop(key, None)
            future.set_result(result)
        return result.model_dump(mode="json")


service = FundFlowService()


def get_fund_flow(query: FundFlowQuery) -> dict:
    """Reusable routed entry for rotation, recap and future API consumers."""
    return service.get_current(query)
