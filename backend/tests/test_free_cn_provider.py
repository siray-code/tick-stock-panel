"""free_cn 的数据边界及真实消费链契约，不依赖网络。"""

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import polars as pl
import pytest
import yaml

from app.indicators.pipeline import _apply_adj_factor, filter_halt_days
from app.plugins.free_cn import client as c
from app.plugins.free_cn import provider as p
from app.services.depth_service import DepthService
from app.services.quote_service import QuoteService

CN = timezone(timedelta(hours=8))
DAY = date(2026, 10, 22)
SYMBOL = "600000.SH"


def quote_fields(*, price="11", ask_volume="0", ask_price="0", quote_date="2026-10-22"):
    return (
        ["浦发银行", "10", "10", price, "11", "10", "11", "0", "12345", "135795"]
        + ["100", "11"] * 5
        + [ask_volume, ask_price] * 5
        + [quote_date, "14:57:00", "00"]
    )


def quote_response(symbols):
    return httpx.Response(
        200,
        content="\n".join(
            f'var hq_str_{symbol}="{",".join(fields)}";' for symbol, fields in symbols
        ).encode("gbk"),
    )


@pytest.fixture
def provider():
    instance = p.FreeCnProvider()
    yield instance
    instance.close()


def seal(book):
    service = DepthService()
    service._notify_depth_updated = Mock()
    service._seal_locked({SYMBOL: book}, [SYMBOL], [], DAY, False)
    return service._sealed_cache[SYMBOL]


def test_zero_depth_levels_preserve_sealed_state():
    book = p._parse_depth(SYMBOL, quote_fields())
    assert book["ask_prices"] == [0.0] * 5
    assert book["ask_volumes"] == [0.0] * 5
    assert seal(book)["sealed_up"] is True


def test_fractional_lot_does_not_become_empty_sell_pressure():
    book = p._parse_depth(SYMBOL, quote_fields(ask_volume="99", ask_price="11"))
    assert book["ask_volumes"][0] == pytest.approx(0.99)
    assert seal(book)["sealed_up"] is False


def test_fractional_depth_survives_persistence_and_restart(tmp_path):
    fields = quote_fields(ask_volume="99", ask_price="11")
    fields[10] = "12345"
    book = p._parse_depth(SYMBOL, fields)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    service = DepthService()
    service.set_repo(repo)
    service._notify_depth_updated = Mock()
    service._seal_locked({SYMBOL: book}, [SYMBOL], [], DAY, True)
    historical = DepthService()
    historical.set_repo(repo)
    assert historical.get_sealed_map(DAY, False)[SYMBOL]["vol"] == pytest.approx(123.45)
    assert historical.get_sealed_map(DAY, True)[SYMBOL]["vol"] == pytest.approx(0.99)
    historical._restore_from_parquet(DAY)
    assert historical.get_sealed_map(DAY, False)[SYMBOL]["sealed"] is False
    assert historical.get_sealed_map(DAY, True)[SYMBOL]["vol"] == pytest.approx(0.99)


def test_legacy_integer_depth_file_still_loads(tmp_path):
    path = tmp_path / "depth5" / f"date={DAY}" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": [SYMBOL], "sealed_up": [True], "sealed_down": [False],
                  "ask1_vol": [0], "bid1_vol": [123], "fetched_at": [1.0]}).write_parquet(path)
    service = DepthService()
    service.set_repo(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    service._restore_from_parquet(DAY)
    assert service.get_sealed_map(DAY, False)[SYMBOL]["vol"] == 123
    assert service.get_sealed_map(DAY, False)[SYMBOL]["sealed"] is True


def test_missing_depth_volume_is_unknown_not_zero():
    fields = quote_fields(ask_volume="-", ask_price="11")
    book = p._parse_depth(SYMBOL, fields)
    assert book["ask_volumes"][0] is None
    assert seal(book)["sealed_up"] is None


def test_quote_identity_includes_exchange(monkeypatch, provider):
    response = quote_response(
        [
            ("sh000001", quote_fields(price="3000")),
            ("sz000001", quote_fields(price="10")),
        ]
    )
    monkeypatch.setattr(provider._sina, "_get", lambda *a, **kw: response)
    records = provider.get_realtime_indices(["000001.SH", "000001.SZ"])
    assert [(r["symbol"], r["last_price"]) for r in records] == [
        ("000001.SH", 3000.0),
        ("000001.SZ", 10.0),
    ]


def test_realtime_uses_quote_time_and_consistent_quote_prices(monkeypatch, provider):
    monkeypatch.setattr(
        provider._sina,
        "market_snapshot",
        lambda: [
            {"symbol": "sh600000", "code": "600000", "trade": "9", "turnoverratio": "5"},
        ],
    )
    monkeypatch.setattr(provider._sina, "quote_batch", lambda symbols: {SYMBOL: quote_fields()})
    records = provider.get_realtime()
    assert len(records) == 1
    record = records[0]
    assert record["timestamp"] == int(datetime(2026, 10, 22, 14, 57, tzinfo=CN).timestamp() * 1000)
    assert record["last_price"] == 11
    assert record["change_pct"] == pytest.approx(0.1)
    assert record["turnover_rate"] == pytest.approx(0.05)
    assert record["volume"] == 123
    assert record["amount"] == 135795


def test_realtime_does_not_fabricate_missing_quote_time(monkeypatch, provider, caplog):
    monkeypatch.setattr(provider._sina, "market_snapshot", lambda: [{"code": "600000"}])
    fields = quote_fields(quote_date="bad-date")
    monkeypatch.setattr(provider._sina, "quote_batch", lambda symbols: {SYMBOL: fields})
    assert provider.get_realtime() == []
    assert caplog.records


@pytest.mark.parametrize("body", [{"error": "changed"}, ["unexpected row"], [{"unexpected": 1}]])
def test_realtime_schema_failure_is_soft(monkeypatch, provider, body, caplog):
    monkeypatch.setattr(provider._sina, "_get", lambda *a, **kw: httpx.Response(200, json=body))
    assert provider.get_realtime() == []
    assert caplog.records


@pytest.mark.parametrize("body", ["<html>error</html>", 'var hq_str_sh000001="broken";'])
def test_index_invalid_response_is_failure_not_empty(monkeypatch, provider, body, caplog):
    monkeypatch.setattr(provider._sina, "_get", lambda *a, **kw: httpx.Response(200, text=body))
    assert provider.get_realtime_indices(["000001.SH"]) is None
    assert caplog.records


def test_index_successful_empty_response(monkeypatch, provider):
    monkeypatch.setattr(
        provider._sina,
        "_get",
        lambda *a, **kw: httpx.Response(
            200,
            text='var hq_str_sh000001="";',
        ),
    )
    assert provider.get_realtime_indices(["000001.SH"]) == []


@pytest.mark.parametrize("fields", [quote_fields(price="nan"), quote_fields(quote_date="invalid")])
def test_index_invalid_quote_fields_preserve_cache(monkeypatch, provider, fields):
    monkeypatch.setattr(provider._sina, "quote_batch", lambda symbols: {"000001.SH": fields})
    assert provider.get_realtime_indices(["000001.SH"]) is None


def test_old_quote_cannot_confirm_final_snapshot(monkeypatch, provider):
    from app.services import quote_service as qs

    monkeypatch.setattr(provider._sina, "market_snapshot", lambda: [{"code": "600000"}])
    monkeypatch.setattr(provider._sina, "quote_batch", lambda symbols: {SYMBOL: quote_fields()})
    service = QuoteService()
    monkeypatch.setattr(qs, "_persist_last_fetch", Mock())
    monkeypatch.setattr(service, "_broadcast_quote_updated", Mock())
    process_daily = Mock()
    monkeypatch.setattr(service, "_build_daily", process_daily)
    boundary = int(datetime(2026, 10, 22, 15, tzinfo=CN).timestamp() * 1000)
    result = service._process_full_market_records(
        provider.get_realtime(),
        t0=0,
        now_ts=0,
        final_boundary_ms=boundary,
    )
    assert result is None
    assert service._last_final_confirmed is False
    process_daily.assert_not_called()


def test_minute_field_changes_are_logged_and_progress_finishes(monkeypatch, provider, caplog):
    monkeypatch.setattr(
        provider._sina,
        "_get",
        lambda *a, **kw: httpx.Response(
            200,
            json=[{"renamed": "2026-10-22 10:00:00"}],
        ),
    )
    progress = Mock()
    assert provider.get_minute([SYMBOL], on_chunk_done=progress).is_empty()
    assert "必需字段缺失" in caplog.text
    progress.assert_called_once_with(1, 1)


def test_index_nullable_volume_amount_survive_daily_normalization(monkeypatch):
    monkeypatch.setattr("app.services.quote_service.cn_today", lambda: DAY)
    record = p._map_sina_index("000001.SH", quote_fields())
    assert record["volume"] is None and record["amount"] is None
    daily = QuoteService._build_daily([record])
    assert daily.height == 1
    assert daily["close"][0] == 11


def test_null_trade_totals_do_not_hide_definite_halts():
    raw = pl.DataFrame(
        {
            "symbol": ["valid", "halt", "zero", "partial"],
            "open": [10.0, 0.0, 10.0, 10.0],
            "high": [11.0, 0.0, 11.0, 11.0],
            "volume": [None, None, 0.0, 0.0],
            "amount": [None, None, 0.0, None],
        }
    )
    assert filter_halt_days(raw)["symbol"].to_list() == ["valid", "partial"]


def minute_item(time="10:00:00", day="2026-10-22"):
    return {
        "day": f"{day} {time}",
        "open": "10",
        "high": "11",
        "low": "9",
        "close": "10",
        "volume": "12345",
    }


def test_minute_respects_exact_beijing_window_and_progress(monkeypatch, provider):
    monkeypatch.setattr(
        provider._sina,
        "minute",
        lambda *a, **kw: [
            minute_item("09:31:00"),
            minute_item(),
            minute_item("14:59:00"),
        ],
    )
    progress = Mock()
    # UTC 输入也必须先转换为北京墙钟再过滤。
    boundary = datetime(2026, 10, 22, 2, tzinfo=UTC)
    frame = provider.get_minute([SYMBOL], boundary, boundary, on_chunk_done=progress)
    assert frame["datetime"].to_list() == [datetime(2026, 10, 22, 10)]
    assert frame["amount"].to_list() == [None]
    assert frame["volume"].to_list() == [123.0]
    progress.assert_called_once_with(1, 1)
    assert provider.minute_history_days == 4


def test_full_minute_only_returns_today(monkeypatch, provider):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 22, 10, tzinfo=tz)

    monkeypatch.setattr(p, "datetime", Clock)
    monkeypatch.setattr(
        provider._sina,
        "minute",
        lambda *a, **kw: [
            minute_item(day="2026-10-21"),
            minute_item(),
        ],
    )
    frame = provider.get_intraday_batch([SYMBOL])
    assert frame.height == 1
    assert frame["datetime"][0] == datetime(2026, 10, 22, 10)
    assert frame.columns == list(p._MINUTE_SCHEMA)
    assert not hasattr(provider, "get_intraday_latest")


def test_adj_factor_consumed_by_real_pipeline(monkeypatch, provider):
    monkeypatch.setattr(
        provider._sina,
        "hfq_events",
        lambda symbol: [
            (date(2020, 1, 1), 1.0),
            (date(2026, 10, 21), 2.0),
        ],
    )
    monkeypatch.setattr(
        provider._sina,
        "daily",
        lambda *a, **kw: [
            {"day": "2026-10-20", "close": "10"},
        ],
    )
    progress = Mock()
    factors = provider.get_adj_factors([SYMBOL], on_chunk_done=progress)
    raw = pl.DataFrame(
        {
            "symbol": [SYMBOL] * 2,
            "date": [date(2026, 10, 20), date(2026, 10, 21)],
            "close": [10.0, 5.0],
        }
    )
    assert factors["prev_close"][0] == 10
    assert _apply_adj_factor(raw, factors)["close"].to_list() == [5.0, 5.0]
    progress.assert_called_once_with(1, 1)


def test_daily_history_not_retained_and_client_closed(monkeypatch, provider):
    response = httpx.Response(200, json=[minute_item()])
    monkeypatch.setattr(provider._sina, "_get", lambda *a, **kw: response)
    for symbol in ["600000.SH", "600001.SH", "600002.SH"]:
        assert provider._sina.daily(symbol)[0]["close"] == "10"
    assert not getattr(provider._sina, "_daily_cache", {})
    transport = provider._sina._http
    provider.close()
    assert transport.is_closed


def test_market_pagination_and_empty_termination(monkeypatch, provider):
    calls = []

    def get(url, **kwargs):
        page = kwargs["params"]["page"]
        calls.append(page)
        return httpx.Response(
            200, json=[{"code": "600000"}] * c.SINA_MARKET_PAGE if page == 1 else []
        )

    monkeypatch.setattr(provider._sina, "_get", get)
    assert len(provider._sina.market_snapshot()) == c.SINA_MARKET_PAGE
    assert calls == [1, 2]


def test_market_pagination_has_upper_bound(monkeypatch, provider, caplog):
    monkeypatch.setattr(
        provider._sina,
        "_get",
        lambda *a, **kw: httpx.Response(
            200,
            json=[{"code": "600000"}] * c.SINA_MARKET_PAGE,
        ),
    )
    with pytest.raises(c.FreeCnError, match="页数"):
        provider._sina.market_snapshot(max_pages=2)


def test_loader_registration_and_datasets(monkeypatch):
    from app.data_providers.capabilities import CAPABILITY_REGISTRY, build_capability_matrix
    from app.data_providers.custom import loader

    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    manifest = yaml.safe_load((Path(p.__file__).parent / "plugin.yaml").read_text())
    loader._register_one_plugin(manifest)
    registered = loader.get_provider("free_cn")
    try:
        assert loader.list_plugins()[0]["available"] is True
        for dataset in ("adj_factor", "realtime", "minute", "depth5", "full_minute"):
            assert loader.provider_has_dataset("free_cn", dataset)
        assert not loader.provider_has_dataset("free_cn", "daily")
        assert not loader.provider_has_dataset("free_cn", "financial")
        assert registered.test_dataset("daily")["error"]
        selected = {
            cap["field"]: "free_cn"
            for cap in CAPABILITY_REGISTRY
            if cap["id"] in registered.config.datasets
        }
        matrix = build_capability_matrix(selected, tickflow_tier="none")
        active = [row for row in matrix["capabilities"] if row["current"] == "free_cn"]
        assert {row["id"] for row in active} == set(registered.config.datasets)
        assert all(row["usable"] for row in active)
    finally:
        registered.close()


def test_loader_failure_isolated(monkeypatch):
    from app.data_providers.custom import loader

    sentinel = SimpleNamespace()
    monkeypatch.setattr(loader, "_PROVIDERS", {"existing": sentinel})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    loader._register_one_plugin({"name": "broken", "entry": "missing.module:Provider"})
    assert {"existing": sentinel} == loader._PROVIDERS
    assert loader.list_plugins()[0]["available"] is False


def test_settings_api_exposes_plugin_routes_and_shallow_history(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.settings import _minute_history_days, router
    from app.data_providers.custom import loader
    from app.services import preferences

    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    monkeypatch.setattr("app.tickflow.policy.base_tier_name", lambda: "none")
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    manifest = yaml.safe_load((Path(p.__file__).parent / "plugin.yaml").read_text())
    loader._register_one_plugin(manifest)
    selected = dict.fromkeys([
        "adj_factor_provider", "realtime_data_provider", "minute_data_provider",
        "depth5_data_provider", "full_minute_data_provider",
    ], "free_cn")
    preferences.save(selected)
    app = FastAPI()
    app.include_router(router)
    try:
        with TestClient(app) as http:
            listing = http.get("/api/settings/data-sources")
            assert listing.status_code == 200
            assert listing.json()["plugins"][0]["name"] == "free_cn"
            response = http.get("/api/settings/capability-matrix")
            assert response.status_code == 200
            routed = [row for row in response.json()["capabilities"] if row["field"] in selected]
            assert len(routed) == 5
            assert all(row["effective"] == "free_cn" and row["usable"] for row in routed)
            assert _minute_history_days() == 4
    finally:
        loader.get_provider("free_cn").close()
        preferences._invalidate_cache()
