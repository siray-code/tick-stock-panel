import subprocess
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from app.data_providers.fund_flow import FundFlowQuery, validate_batch
from app.market_time import CN_TZ
from app.plugins.akshare_ths import provider as adapter
from app.plugins.akshare_ths.worker import PageEvidence, normalize_catalog, normalize_rows


def test_units_percentages_nulls_and_unmapped_identity():
    rows = normalize_rows([
        {"行业": "银行", "流入资金": "1.23", "流出资金": 1.24, "净额": -0.01,
         "行业-涨跌幅": "0.50%", "公司家数": 40},
        {"行业": "新概念", "流入资金": "-", "流出资金": None, "净额": None,
         "阶段涨跌幅": "-2%", "公司家数": None},
    ], {"银行": "881155"})
    assert rows[0]["inflow_amount"] == 123_000_000
    assert rows[0]["net_amount"] == -1_000_000
    assert rows[0]["change_pct"] == 0.005
    assert rows[1]["inflow_amount"] is None
    assert rows[1]["subject_id"] is None and rows[1]["source_subject_key"] == "name:新概念"
    assert rows[1]["change_pct"] == -0.02


def test_normalization_rejects_missing_column_and_nonfinite():
    with pytest.raises(ValueError):
        normalize_rows([{"行业": "银行"}], {})
    with pytest.raises(ValueError):
        normalize_rows([{"行业": "银行", "流入资金": "inf", "流出资金": 1,
                         "净额": 0, "公司家数": 1}], {})


def test_catalog_does_not_guess_ambiguous_or_invalid_identifiers():
    assert normalize_catalog([
        {"name": "重复", "code": "881001"}, {"name": "重复", "code": "881002"},
        {"name": "错误", "code": "nan"}, {"name": "银行", "code": "881155"},
    ]) == {"银行": "881155"}


def page(n=1, total=2, count=2, unit="亿"):
    return f'<span class="page_info">{n}/{total}</span><table><tr>' + ''.join(
        f'<th>{h}({unit})</th>' for h in ("流入资金", "流出资金", "净额")
    ) + '</tr>' + '<tr><td>A</td></tr>' * count + '</table>'


def test_paging_fails_closed_on_missing_wrong_or_changed_pages():
    pytest.importorskip("bs4")
    evidence = PageEvidence()
    evidence.observe("https://data.10jqka.com.cn/funds/hyzjl/", page())
    with pytest.raises(ValueError, match="incomplete"):
        evidence.validate(2)
    with pytest.raises(ValueError, match="wrong page"):
        evidence.observe("https://data.10jqka.com.cn/funds/hyzjl/page/2/", page(1))
    evidence.observe("https://data.10jqka.com.cn/funds/hyzjl/page/2/", page(2, count=1))
    evidence.validate(3)
    with pytest.raises(ValueError, match="record count"):
        evidence.validate(4)
    for invalid in ["<html>challenge</html>", page(unit="万"), page(2, total=3)]:
        with pytest.raises(ValueError):
            evidence.observe("https://data.10jqka.com.cn/funds/hyzjl/", invalid)


def test_missing_dependency_or_wrong_version_does_not_import_akshare(monkeypatch):
    monkeypatch.setattr(adapter.sys, "frozen", False, raising=False)

    def missing(_):
        raise PackageNotFoundError

    monkeypatch.setattr(adapter, "version", missing)
    assert not adapter.availability()[0]
    monkeypatch.setattr(adapter, "version", lambda _: "0.0")
    assert not adapter.availability()[0]
    monkeypatch.setattr(adapter, "version", lambda _: "1.18.84")
    monkeypatch.setattr(adapter, "find_spec", lambda _: object())
    assert adapter.availability() == (True, "ok")
    monkeypatch.setattr(adapter, "find_spec", lambda _: None)
    assert not adapter.availability()[0]


def test_process_timeout_is_killed_and_reaped(monkeypatch):
    class Process:
        returncode = None
        killed = False
        calls = 0

        def communicate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert kwargs["timeout"] == 45
                raise subprocess.TimeoutExpired("worker", 45)
            return "", ""

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **kw: process)
    provider = adapter.AkshareThsProvider()
    with pytest.raises(RuntimeError, match="超时"):
        provider._run({})
    assert process.killed and process.calls == 2 and provider._process is None


def test_adapter_uses_standard_batch_and_cached_catalog(monkeypatch):
    provider = adapter.AkshareThsProvider()
    requests = []

    def run(request):
        requests.append(request)
        return {"rows": [{"source_subject_key": "a", "name": "A", "net_amount": None}],
                "catalog": {"A": "881001"}, "expected_rows": 1,
                "fetched_at": datetime(2026, 10, 22, 10, tzinfo=CN_TZ).isoformat()}

    monkeypatch.setattr(provider, "_run", run)
    q = FundFlowQuery(subject_type="industry", window_id="today")
    assert validate_batch(provider.get_fund_flow(q)).frame.height == 1
    provider.get_fund_flow(q)
    assert requests[0]["catalog"] is None
    assert requests[1]["catalog"] == {"A": "881001"}
    provider.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        # Closed plugins cannot start new child processes.
        adapter.AkshareThsProvider._run(provider, {})


def test_matrix_only_lists_providers_implementing_funds_contract(monkeypatch):
    from app.data_providers import capabilities as matrix
    provider = adapter.AkshareThsProvider()
    monkeypatch.setattr(matrix.custom_sources, "list_plugins", lambda: [
        {"name": "akshare_ths", "datasets": ["fund_flow"], "available": True},
        {"name": "broken", "datasets": ["fund_flow"], "available": True},
    ])
    monkeypatch.setattr(matrix.custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(matrix.custom_sources, "get_provider", lambda name: provider if name == "akshare_ths" else SimpleNamespace())
    monkeypatch.setattr(matrix.custom_sources, "provider_has_dataset", lambda *args: True)
    cap = next(c for c in matrix.build_capability_matrix({}, "expert")["capabilities"] if c["id"] == "fund_flow")
    assert cap["usable"] and not cap["tf_available"] and cap["tf_tier"] is None
    assert [c["name"] for c in cap["candidates"]] == ["akshare_ths"]
    assert cap["pending"][0]["name"] == "broken"
    assert cap["details"]["taxonomy"] == "ths"
    assert cap["details"]["windows"][0]["id"] == "today"
