"""损坏插件声明不得逃出分钟源解析边界或触发跨源请求。"""
from types import SimpleNamespace

import pytest

from app.services import kline_sync


@pytest.mark.parametrize("resolver", [
    kline_sync._resolve_minute_provider,
    kline_sync._resolve_full_minute_provider,
], ids=["minute", "full_minute"])
@pytest.mark.parametrize("datasets", [None, 42])
def test_invalid_dataset_declaration_isolated(monkeypatch, resolver, datasets):
    provider = SimpleNamespace(config=SimpleNamespace(datasets=datasets))
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)

    resolved, use_tickflow, error = resolver("broken_src")

    assert resolved is None
    assert use_tickflow is False
    assert error
