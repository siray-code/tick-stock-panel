"""Optional dependency boundary. No AKShare/pandas imports in the host process."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.data_providers.fund_flow import (
    FundFlowBatch,
    FundFlowCapabilities,
    FundFlowQuery,
    FundFlowRow,
    FundFlowWindow,
)

CAPABILITIES = FundFlowCapabilities(
    taxonomy="ths", metric_basis="ths.public_funds", metric_label="同花顺公开资金口径",
    subject_types=("industry", "concept"),
    windows=(FundFlowWindow(id="today", label="当天（源快照）"), *(
        FundFlowWindow(id=f"{days}d", label=f"近{days}日（同花顺口径）") for days in (3, 5, 10, 20)
    )),
)


def availability() -> tuple[bool, str]:
    if getattr(sys, "frozen", False):
        return False, "此插件需要源码或 Docker Python 运行环境"
    try:
        installed = version("akshare")
    except PackageNotFoundError:
        return False, "未安装 AKShare 1.18.84，请安装插件依赖"
    if installed != "1.18.84":
        return False, f"需要已验证的 AKShare 1.18.84，当前为 {installed}"
    if any(find_spec(module) is None for module in ("akshare", "bs4", "lxml", "py_mini_racer")):
        return False, "AKShare 依赖不完整，请重新安装插件依赖"
    return True, "ok"


class AkshareThsProvider:
    name = "akshare_ths"
    fund_flow_capabilities = CAPABILITIES
    # All plugin instances share one upstream slot (including during reload).
    _upstream = threading.BoundedSemaphore(1)

    def __init__(self):
        self.config = SimpleNamespace(datasets={"fund_flow": True}, display_name="同花顺资金流 (AKShare)")
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._closed = False
        self._catalogs: dict[str, tuple[float, dict]] = {}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def get_fund_flow(self, query: FundFlowQuery) -> FundFlowBatch:
        if not CAPABILITIES.supports(query):
            raise ValueError("不支持的同花顺资金流查询")
        if not self._upstream.acquire(timeout=2):
            raise RuntimeError("资金流数据源正在处理其他窗口，请稍后重试")
        try:
            cached = self._catalogs.get(query.subject_type)
            catalog = cached[1] if cached and time.monotonic() - cached[0] < 86400 else None
            payload = self._run({"query": query.model_dump(), "catalog": catalog})
            self._catalogs[query.subject_type] = (time.monotonic(), payload["catalog"])
            rows = [FundFlowRow.model_validate(row).model_dump() for row in payload["rows"]]
            schema = {k: pl.String if k in {"source_subject_key", "subject_id", "name"}
                      else pl.Int64 if k == "member_count" else pl.Float64 for k in FundFlowRow.model_fields}
            return FundFlowBatch(
                source=self.name, capabilities=CAPABILITIES, query=query,
                frame=pl.DataFrame(rows, schema=schema),
                fetched_at=datetime.fromisoformat(payload["fetched_at"]),
                amount_precision=1_000_000.0, complete=True, expected_rows=payload["expected_rows"],
            )
        finally:
            self._upstream.release()

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "fund_flow":
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": "此插件只提供资金流"}
        from app.services.fund_flow import service

        result = service.get(self.name, self, FundFlowQuery(
            subject_type="industry", window_id="today", subject_ids=tuple(symbols or ()),
        ))
        return {"provider": self.name, "dataset": dataset, "rows": len(result["rows"]),
                "columns": list(FundFlowRow.model_fields), "preview": result["rows"][:5],
                "error": result["message"] if result["status"] not in {"ok", "no_data"} else None}

    def _run(self, request: dict) -> dict:
        with self._lock:
            if self._closed:
                raise RuntimeError("资金流插件已关闭")
            process = subprocess.Popen(
                [sys.executable, "-m", "app.plugins.akshare_ths.worker"],
                cwd=Path(__file__).resolve().parents[3],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8",
            )
            self._process = process
        try:
            stdout, _ = process.communicate(json.dumps(request, ensure_ascii=False), timeout=45)
            if process.returncode != 0:
                raise RuntimeError("同花顺资金流获取失败（访问校验、限流或结构变化）")
            return json.loads(stdout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise RuntimeError("同花顺资金流获取超时") from exc
        finally:
            with self._lock:
                self._process = None

