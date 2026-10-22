from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.data_providers.fund_flow import (
    FundFlowBatch,
    FundFlowCapabilities,
    FundFlowQuery,
    FundFlowWindow,
    validate_batch,
)
from app.market_time import CN_TZ
from app.services.fund_flow import FundFlowService


def capabilities(**kwargs):
    return FundFlowCapabilities(
        taxonomy="test.industry", metric_basis="test.public_funds",
        subject_types=("industry",), windows=(FundFlowWindow(id="today", label="当天"),),
        **kwargs,
    )


def batch(query=None, rows=None, **kwargs):
    query = query or FundFlowQuery(subject_type="industry", window_id="today")
    return FundFlowBatch(
        source="test", capabilities=capabilities(), query=query,
        fetched_at=datetime(2026, 10, 22, 10, tzinfo=CN_TZ),
        frame=pl.DataFrame(rows if rows is not None else [{
            "source_subject_key": "bank", "subject_id": "881155", "name": "银行",
            "inflow_amount": 120.0, "outflow_amount": 80.0, "net_amount": 40.0,
        }]), **kwargs,
    )


class Provider:
    def __init__(self):
        self.fund_flow_capabilities = capabilities()
        self.calls = 0
        self.fail = False

    def get_fund_flow(self, query):
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream failed")
        return batch(query)


def test_contract_retains_null_and_derives_only_missing_net():
    rows = validate_batch(batch(rows=[
        {"source_subject_key": "a", "name": "A", "inflow_amount": None,
         "outflow_amount": None, "net_amount": -4.0},
        {"source_subject_key": "b", "name": "B", "inflow_amount": 8.0,
         "outflow_amount": 3.0, "net_amount": None},
    ])).frame.to_dicts()
    assert rows[0]["inflow_amount"] is None
    assert rows[0]["net_amount"] == -4
    assert rows[1]["net_amount"] == 5


@pytest.mark.parametrize("change", [
    {"inflow_amount": float("nan")}, {"outflow_amount": -2.0},
    {"net_amount": 999.0}, {"name": ""},
])
def test_contract_rejects_bad_financial_values(change):
    row = batch().frame.to_dicts()[0] | change
    with pytest.raises(ValueError):
        validate_batch(batch(rows=[row]))


def test_contract_precision_identity_completeness_and_version():
    b = batch(rows=[{"source_subject_key": "x", "name": "X", "inflow_amount": 15_870_000,
                     "outflow_amount": 16_220_000, "net_amount": -360_000}], amount_precision=10_000)
    assert validate_batch(b).frame["net_amount"][0] == -360_000
    for invalid in [batch(complete=False), batch(expected_rows=9), batch(api_version=2),
                    batch(rows=batch().frame.to_dicts() * 2)]:
        with pytest.raises(ValueError):
            validate_batch(invalid)


def test_cache_failure_keeps_snapshot_and_isolates_source_day_and_query():
    p = Provider()
    clock = [0.0]
    today = [datetime(2026, 10, 22, 10, tzinfo=CN_TZ)]
    svc = FundFlowService(clock=lambda: clock[0], now=lambda: today[0])
    q = FundFlowQuery(subject_type="industry", window_id="today")
    assert svc.get("test", p, q)["status"] == "ok"
    assert svc.get("test", p, q)["status"] == "ok"
    assert p.calls == 1
    clock[0] = 61
    p.fail = True
    stale = svc.get("test", p, q)
    assert stale["status"] == "stale" and len(stale["rows"]) == 1
    assert svc.get("other", p, q)["status"] == "error"
    today[0] += timedelta(days=1)
    assert svc.get("test", p, q)["status"] == "error"


def test_unsupported_query_never_returns_previous_context():
    p = Provider()
    svc = FundFlowService()
    svc.get("test", p, FundFlowQuery(subject_type="industry", window_id="today"))
    result = svc.get("test", p, FundFlowQuery(subject_type="concept", window_id="today"))
    assert result["status"] == "unsupported" and result["rows"] == []
    assert p.calls == 1


def test_concurrent_consumers_share_one_fetch():
    p = Provider()
    entered, release = Event(), Event()
    original = p.get_fund_flow

    def slow(query):
        entered.set()
        assert release.wait(3)
        return original(query)

    p.get_fund_flow = slow
    svc = FundFlowService()
    q = FundFlowQuery(subject_type="industry", window_id="today")
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(svc.get, "test", p, q)
        assert entered.wait(3)
        second = pool.submit(svc.get, "test", p, q)
        release.set()
        assert first.result()["rows"] == second.result()["rows"]
    assert p.calls == 1


def test_second_net_only_provider_uses_same_consumer():
    p = Provider()
    p.fund_flow_capabilities = capabilities(amount_fields=("net_amount",))

    def get(query):
        b = batch(query, rows=[{"source_subject_key": "bank", "name": "银行", "net_amount": -9.0}])
        b.capabilities = p.fund_flow_capabilities
        return b

    p.get_fund_flow = get
    result = FundFlowService().get("test", p, FundFlowQuery(subject_type="industry", window_id="today"))
    assert result["rows"][0]["inflow_amount"] is None
    assert result["rows"][0]["net_amount"] == -9


def test_api_validation_unavailable_success_and_error(monkeypatch):
    from app.api import fund_flow
    from app.services import fund_flow as funds_service
    p = Provider()
    monkeypatch.setattr(fund_flow, "service", FundFlowService())
    monkeypatch.setattr(funds_service.preferences, "get_fund_flow_provider", lambda: "test")
    monkeypatch.setattr(funds_service, "capability_gate", lambda _: (True, "test"))
    monkeypatch.setattr(funds_service.custom_sources, "get_provider", lambda _: p)
    app = FastAPI()
    app.include_router(fund_flow.router)
    client = TestClient(app)
    assert client.get("/api/fund-flow?subject_type=bad").status_code == 422
    assert client.get("/api/fund-flow?subject_ids=%20").status_code == 422
    assert client.get("/api/fund-flow").json()["rows"][0]["net_amount"] == 40
    assert client.get("/api/fund-flow?subject_type=concept").json()["status"] == "unsupported"
    monkeypatch.setattr(fund_flow, "service", FundFlowService())
    p.fail = True
    assert client.get("/api/fund-flow").json()["status"] == "error"
    monkeypatch.setattr(fund_flow, "service", FundFlowService())
    monkeypatch.setattr(p, "get_fund_flow", lambda query: batch(query, rows=[]))
    assert client.get("/api/fund-flow").json()["status"] == "no_data"
    monkeypatch.setattr(funds_service, "capability_gate", lambda _: (False, "missing"))
    assert client.get("/api/fund-flow").json()["status"] == "source_unavailable"


def test_subsets_share_snapshot_without_name_based_joins():
    p = Provider()
    svc = FundFlowService()
    q = FundFlowQuery(subject_type="industry", window_id="today", subject_ids=("881155",))
    assert len(svc.get("test", p, q)["rows"]) == 1
    assert svc.get("test", p, q.model_copy(update={"subject_ids": ("银行",)}))["rows"] == []
    assert p.calls == 1


def test_extension_shutdown_closes_used_provider(monkeypatch):
    from app.custom.fund_flow import setup
    from app.extensions.registry import BackendExtensionRegistrar
    from app.services import fund_flow as funds_service

    p = Provider()
    closed = []
    p.close = lambda: closed.append(True)
    svc = FundFlowService()
    svc.get("test", p, FundFlowQuery(subject_type="industry", window_id="today"))
    monkeypatch.setattr(funds_service, "service", svc)
    registrar = BackendExtensionRegistrar("official.fund-flow", api_version=1)
    setup(registrar)
    app = FastAPI()
    for router in registrar.routers:
        app.include_router(router)
    with TestClient(app):
        pass
    assert closed == [True]


def test_old_and_invalid_preferences_keep_independent_funds_default(monkeypatch):
    from app.services import preferences

    state = {"daily_data_provider": "old_source"}
    monkeypatch.setattr(preferences, "load", lambda: state)
    monkeypatch.setattr(preferences, "_allowed_data_providers", lambda: {"tickflow", "old_source", "second"})
    assert preferences.get_fund_flow_provider() == "akshare_ths"
    assert preferences.get_daily_data_provider() == "old_source"
    state["fund_flow_data_provider"] = "unknown"
    assert preferences.get_fund_flow_provider() == "akshare_ths"
    state["fund_flow_data_provider"] = "second"
    assert preferences.get_fund_flow_provider() == "second"


def test_settings_route_saves_only_the_requested_funds_preference(monkeypatch):
    from app.api import settings as settings_api
    from app.services import preferences

    state = {}
    saved = []

    def save(updates):
        saved.append(updates)
        state.update(updates)
        return state

    monkeypatch.setattr(preferences, "load", lambda: state)
    monkeypatch.setattr(preferences, "save", save)
    monkeypatch.setattr(preferences, "_allowed_data_providers", lambda: {"tickflow", "second"})
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: None)
    app = FastAPI()
    app.include_router(settings_api.router)
    response = TestClient(app).put("/api/settings/preferences/data-providers", json={"fund_flow_data_provider": "second"})
    assert response.status_code == 200
    assert saved == [{"fund_flow_data_provider": "second"}]
    assert response.json()["fund_flow_data_provider"] == "second"
    assert response.json()["daily_data_provider"] == "tickflow"
