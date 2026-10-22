"""instrument_sync 容灾测试: 单交易所失败不得覆盖旧维表(2026-10-12 SZ 瞬断教训)。"""

from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest

from app.services import instrument_sync as ims


class _FakeTickflow:
    """按交易所可控行为的假 client: ok_exchanges 之外的恒抛错。"""

    def __init__(self, ok_exchanges: set[str], outcomes: dict[str, list] | None = None):
        self.ok = ok_exchanges
        self.calls: dict[str, int] = {}
        self.outcomes = outcomes or {}

    @property
    def exchanges(self):
        return self

    def get_instruments(self, ex, instrument_type="stock"):
        self.calls[ex] = self.calls.get(ex, 0) + 1
        if self.outcomes.get(ex):
            outcome = self.outcomes[ex].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if ex not in self.ok:
            raise RuntimeError(f"boom {ex}")
        return [
            {"symbol": {"SH": "600000.SH", "SZ": "000001.SZ", "BJ": "430001.BJ"}[ex],
             "name": f"{ex}股", "code": "1", "exchange": ex,
             "region": "CN", "type": "stock", "ext": {}},
        ]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ims.time, "sleep", lambda s: None)


def _existing(data_dir):
    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["旧"]}).write_parquet(out)
    return out


def test_partial_failure_keeps_existing_table(tmp_path, monkeypatch):
    """SZ 失败: 任务报错且已有维表逐字节保留。"""
    out = _existing(tmp_path)
    before = out.read_bytes()
    fake = _FakeTickflow(ok_exchanges={"SH", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    with pytest.raises(ims.InstrumentSyncIncompleteError, match="SZ"):
        ims.sync_instruments(tmp_path)
    assert fake.calls.get("SZ") == ims._EXCHANGE_RETRIES + 1  # 带重试
    assert out.read_bytes() == before


def test_partial_failure_first_build_does_not_publish_subset(tmp_path, monkeypatch):
    """首次同步也不得把残表发布为全市场维表。"""
    fake = _FakeTickflow(ok_exchanges={"SH", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    with pytest.raises(ims.InstrumentSyncIncompleteError, match="SZ"):
        ims.sync_instruments(tmp_path)
    assert not (tmp_path / "instruments" / "instruments.parquet").exists()


def test_all_exchanges_ok_overwrites(tmp_path, monkeypatch):
    """三交易所齐全: 正常覆盖写。"""
    out = _existing(tmp_path)
    fake = _FakeTickflow(ok_exchanges={"SH", "SZ", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    assert ims.sync_instruments(tmp_path) == 3
    assert pl.read_parquet(out).height == 3


def test_transient_exchange_failure_recovers_before_publishing(tmp_path, monkeypatch):
    fake = _FakeTickflow({"SH", "SZ", "BJ"}, outcomes={"SZ": [RuntimeError("temporary")]})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    assert ims.sync_instruments(tmp_path) == 3
    assert fake.calls == {"SH": 1, "SZ": 2, "BJ": 1}


def test_empty_exchange_response_is_retried(tmp_path, monkeypatch):
    fake = _FakeTickflow({"SH", "SZ", "BJ"}, outcomes={"SZ": [[]]})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    assert ims.sync_instruments(tmp_path) == 3
    assert fake.calls["SZ"] == 2


@pytest.mark.parametrize("existing", [False, True])
def test_permanently_empty_exchange_fails_closed(tmp_path, monkeypatch, existing):
    out = _existing(tmp_path) if existing else tmp_path / "instruments" / "instruments.parquet"
    before = out.read_bytes() if existing else None
    fake = _FakeTickflow({"SH", "SZ", "BJ"}, outcomes={"SZ": [[]] * 3})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    with pytest.raises(ims.InstrumentSyncIncompleteError, match="SZ"):
        ims.sync_instruments(tmp_path)
    assert fake.calls["SZ"] == 3
    assert (out.read_bytes() if out.exists() else None) == before


@pytest.mark.parametrize("existing", [False, True])
def test_all_exchanges_failing_does_not_publish(tmp_path, monkeypatch, existing):
    out = _existing(tmp_path) if existing else tmp_path / "instruments" / "instruments.parquet"
    before = out.read_bytes() if existing else None
    fake = _FakeTickflow(set())
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    with pytest.raises(ims.InstrumentSyncIncompleteError, match="SH,SZ,BJ"):
        ims.sync_instruments(tmp_path)
    assert set(fake.calls.values()) == {3}
    assert (out.read_bytes() if out.exists() else None) == before


def test_tickflow_refresh_preserves_previous_share_fields(tmp_path, monkeypatch):
    out = _existing(tmp_path)
    pl.DataFrame({"symbol": ["600000.SH"], "float_shares": [1e9], "total_shares": [2e9]}).write_parquet(out)
    fake = _FakeTickflow({"SH", "SZ", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    assert ims.sync_instruments(tmp_path) == 3
    row = pl.read_parquet(out).filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert row["float_shares"] == 1e9
    assert row["total_shares"] == 2e9


@pytest.mark.parametrize("entrypoint", ["premarket", "pipeline"])
def test_incomplete_sync_is_not_reported_as_success(tmp_path, monkeypatch, entrypoint):
    from app.jobs import daily_pipeline

    fake = _FakeTickflow({"SH", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with pytest.raises(ims.InstrumentSyncIncompleteError, match="SZ"):
        if entrypoint == "premarket":
            daily_pipeline.run_instruments_sync(repo)
        else:
            daily_pipeline.run_now(repo, None)
