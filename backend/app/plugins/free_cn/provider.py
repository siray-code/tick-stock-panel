"""新浪免费数据源：在边界统一代码、单位、时区和失败语义。

实现数据集: adj_factor / realtime / minute / depth5 / full_minute
未声明 daily: 新浪日线接口不提供成交额字段, 而 daily 契约要求 amount 必填 —
按插件规范「只声明真实提供的数据集」, 日线由用户独立选择的数据源提供。

单位与口径 (CONTRIBUTING §3, 2026-10 实测, 见 client.py 顶部):
  - 新浪日线/分钟/实时 volume 为股 → floor(/100) 转手。
  - Market_Center 换手率百分数 → /100；涨跌幅按批量报价昨收推导。
  - 秒/分钟 K 的 datetime 为北京墙钟 naive (契约 §分钟K)。
  - 除权因子取新浪 hfq.js 累积因子差分 → 单事件比值(非累积), 累积链由
    indicators.pipeline._apply_adj_factor 构建; prev_close 由新浪原始日线收盘对齐。
  - 分钟成交额上游未提供 → null, 不伪造(契约: 缺失返 null, 前端停画均价线)。
  - 五档量为股 → /100 转手，保留零股；timestamp 用快照里的 date+time 组装。

覆盖率声明 (真实边界, 不夸大):
  - minute: 单标的最多 1023 根，声明 4 个交易日；不承诺补齐超出窗口的历史。
  - full_minute: 只实现修复轮 get_intraday_batch(当日窗口); 上游无全市场 1m 增量端点,
    不实现 get_intraday_latest → 服务按规范降级为「仅修复轮」, 节奏下限 60s。
  - depth5: 仅实时快照, 无历史; 单请求 100 只, 分片由服务层按 capability 控制。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from typing import Any

import polars as pl

from .client import (
    DAILY_MAX_BARS,
    MINUTE_MAX_BARS,
    FreeCnError,
    SinaClient,
    parse_quote_ts,
)

logger = logging.getLogger(__name__)

_DATASETS = ("adj_factor", "realtime", "minute", "depth5", "full_minute")
_BEIJING = timezone(timedelta(hours=8))

_ADJ_SCHEMA = {
    "symbol": pl.String,
    "trade_date": pl.Date,
    "ex_factor": pl.Float64,
    "prev_close": pl.Float64,
}
_MINUTE_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}


def availability() -> tuple[bool, str]:
    """loader 启动自检，仅需后端已有的 httpx。"""
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False, "缺少 httpx(后端默认依赖, 请检查环境)"
    return True, "ok"


@dataclass
class _FreeCnConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "free_cn"
    display_name: str = "免费开源源 (新浪)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _with_suffix(code: str) -> str:
    """6 位裸代码 → 带交易所后缀。北交所 4/8 开头与 920 号段; 沪市 6/9; 其余深市。"""
    if code.startswith(("4", "8")) or code.startswith("92"):
        return f"{code}.BJ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _to_float(value: Any) -> float | None:
    if value in (None, "", "-", "—"):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _lots(shares: Any) -> int | None:
    """股 → 手 (契约: volume 单位为手)。"""
    v = _to_float(shares)
    if v is None:
        return None
    return math.floor(v / 100.0)


class FreeCnProvider:
    """全部走新浪(日线/除权/分钟/盘口/全市场快照)。单一主源, 不跨源静默回退。"""

    name = "free_cn"
    builtin = True
    minute_history_days = 4

    def __init__(self) -> None:
        self.config = _FreeCnConfig()
        self._sina = SinaClient()

    def close(self) -> None:  # loader.load_all 重建注册表时调用
        self._sina.close()

    # ---------------------------------------------------------------- adj_factor
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        rows: list[dict] = []
        total = len(symbols)
        for idx, symbol in enumerate(symbols, 1):
            try:
                events = self._sina.hfq_events(symbol)
            except FreeCnError as e:
                logger.warning("free_cn 除权事件获取失败 %s: %s", symbol, e)
                if on_chunk_done:
                    on_chunk_done(idx, total)
                continue
            if len(events) < 2:
                if on_chunk_done:
                    on_chunk_done(idx, total)
                continue
            # 前收盘：用新浪未复权日线对齐，不依赖成交额。
            closes: dict[date, float] = {}
            try:
                for item in self._sina.daily(symbol, bars=DAILY_MAX_BARS):
                    try:
                        closes[date.fromisoformat(str(item["day"])[:10])] = float(item["close"])
                    except (KeyError, TypeError, ValueError):
                        continue
            except FreeCnError as e:
                logger.warning("free_cn 日线(前收盘)获取失败 %s: %s", symbol, e)
            ordered_dates = sorted(closes)

            for prev, cur in pairwise(events):
                ex_date, cur_factor = cur
                prev_factor = prev[1]
                if prev_factor <= 0:
                    continue
                if start_time and ex_date < _as_date(start_time):
                    continue
                if end_time and ex_date > _as_date(end_time):
                    continue
                prev_close = None
                for d in reversed(ordered_dates):
                    if d < ex_date:
                        prev_close = closes[d]
                        break
                rows.append(
                    {
                        "symbol": symbol,
                        "trade_date": ex_date,
                        "ex_factor": round(cur_factor / prev_factor, 10),
                        "prev_close": prev_close,
                    }
                )
            if on_chunk_done:
                on_chunk_done(idx, total)

        if not rows:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        return pl.DataFrame(rows, schema_overrides=_ADJ_SCHEMA).sort(["symbol", "trade_date"])

    # ---------------------------------------------------------------- realtime
    def get_realtime(self) -> list[dict]:
        """Market_Center 枚举股票，hq 报价提供同一时点的 OHLCV 和真实行情时间。"""
        try:
            items = self._sina.market_snapshot()
            by_symbol = {_with_suffix(str(item["code"])): item for item in items}
            quotes = self._sina.quote_batch(by_symbol)
        except FreeCnError as e:
            logger.warning("free_cn 实时快照失败: %s", e)
            return []
        out: list[dict] = []
        for symbol, fields in quotes.items():
            rec = _map_sina_index(symbol, fields)
            if rec is not None:
                turnover = _to_float(by_symbol[symbol].get("turnoverratio"))
                rec.update(
                    volume=_lots(fields[8]),
                    amount=_to_float(fields[9]),
                    turnover_rate=turnover / 100 if turnover is not None else None,
                )
                out.append(rec)
        if not out:
            logger.warning(
                "free_cn 实时快照解析后为空 (上游 %d 行), 请检查字段是否变化", len(items)
            )
        return out

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数快照(新浪批量)。行字段与 get_realtime 一致; 失败返回 None。"""
        if not symbols:
            return []
        try:
            quotes = self._sina.quote_batch(symbols)
        except FreeCnError as e:
            logger.warning("free_cn 指数快照失败: %s", e)
            return None
        out: list[dict] = []
        for symbol in symbols:
            fields = quotes.get(symbol)
            if not fields:
                continue
            rec = _map_sina_index(symbol, fields)
            if rec is None:
                return None  # 字段异常不是成功空响应，不得清空有效指数缓存。
            out.append(rec)
        return out

    # ---------------------------------------------------------------- depth5
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """五档盘口: {symbol: {bid_prices, bid_volumes, ask_prices, ask_volumes, timestamp}}."""
        if not symbols:
            return {}
        try:
            quotes = self._sina.quote_batch(symbols)
        except FreeCnError as e:
            logger.warning("free_cn 五档获取失败: %s", e)
            return {}
        out: dict[str, dict] = {}
        for symbol in symbols:
            fields = quotes.get(symbol)
            if not fields:
                continue
            book = _parse_depth(symbol, fields)
            if book is not None:
                out[symbol] = book
        return out

    # ---------------------------------------------------------------- minute
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        if freq != "1m":
            logger.warning("free_cn 仅提供 1m 分钟K, 收到 freq=%s, 返回空帧", freq)
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        start = _as_beijing_wallclock(start_time) if start_time else None
        end = _as_beijing_wallclock(end_time) if end_time else None
        rows: list[dict] = []
        total = len(symbols)
        truncated = 0
        for idx, symbol in enumerate(symbols, 1):
            try:
                items = self._sina.minute(symbol, bars=MINUTE_MAX_BARS)
            except FreeCnError as e:
                logger.warning("free_cn 分钟K获取失败 %s: %s", symbol, e)
                if on_chunk_done:
                    on_chunk_done(idx, total)
                continue
            if start and items:
                earliest = _parse_sina_dt(items[0].get("day"))
                if earliest is not None and earliest.date() > start.date():
                    truncated += 1
            for item in items:
                dt = _parse_sina_dt(item.get("day"))
                if dt is None:
                    continue
                if start and dt < start:
                    continue
                if end and dt > end:
                    continue
                row = _minute_row(symbol, dt, item)
                if row is not None:
                    rows.append(row)
            if on_chunk_done:
                on_chunk_done(idx, total)
        if truncated:
            logger.warning(
                "free_cn 分钟K覆盖不足: %d/%d 只标的的最早一根晚于请求起始日 "
                "(上游上限 %d 根 ≈ 4 个交易日)",
                truncated,
                total,
                MINUTE_MAX_BARS,
            )
        if not rows:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        df = pl.DataFrame(rows, schema_overrides=_MINUTE_SCHEMA)
        return df.sort(["symbol", "datetime"])

    def get_intraday_batch(
        self,
        symbols: list[str],
        count: int = 300,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """full_minute 修复轮: 给定标的当日全部 1 分钟K (最多 240 根)。"""
        bars = max(240, min(int(count), MINUTE_MAX_BARS))
        today = datetime.now(_BEIJING).date()
        rows: list[dict] = []
        for symbol in symbols:
            try:
                items = self._sina.minute(symbol, bars=bars)
            except FreeCnError as e:
                logger.warning("free_cn 当日分钟获取失败 %s: %s", symbol, e)
                continue
            for item in items:
                dt = _parse_sina_dt(item.get("day"))
                if dt is None or dt.date() != today:
                    continue
                row = _minute_row(symbol, dt, item)
                if row is not None:
                    rows.append(row)
        if not rows:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        return pl.DataFrame(rows, schema_overrides=_MINUTE_SCHEMA).sort(["symbol", "datetime"])

    # 说明: 不实现 get_intraday_latest —— 上游无全市场 1m 增量端点。
    # 按 docs/plugin-development.md, 服务层会自动降级为「仅修复轮」(60s 下限)。

    # ---------------------------------------------------------------- 设置页试拉
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = [s for s in (symbols or [])][:3] or ["600519.SH", "000001.SZ"]
        try:
            if dataset == "adj_factor":
                df = self.get_adj_factors(
                    syms, datetime.now(_BEIJING) - timedelta(days=3650), datetime.now(_BEIJING)
                )
                head = [_iso_row(r) for r in df.head(5).to_dicts()]
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": df.height,
                    "columns": df.columns,
                    "preview": head,
                }
            if dataset == "minute":
                now = datetime.now(_BEIJING)
                df = self.get_minute(syms, now - timedelta(days=4), now)
                head = [_iso_row(r) for r in df.head(5).to_dicts()]
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": df.height,
                    "columns": df.columns,
                    "preview": head,
                }
            if dataset == "full_minute":
                df = self.get_intraday_batch(syms, count=240)
                head = [_iso_row(r) for r in df.head(5).to_dicts()]
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": df.height,
                    "columns": df.columns,
                    "preview": head,
                }
            if dataset == "realtime":
                rows = self.get_realtime()
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": len(rows),
                    "columns": list(rows[0].keys()) if rows else [],
                    "preview": [{k: v for k, v in r.items() if k != "timestamp"} for r in rows[:5]],
                }
            if dataset == "depth5":
                books = self.get_depth_batch(syms)
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": len(books),
                    "columns": [
                        "bid_prices",
                        "bid_volumes",
                        "ask_prices",
                        "ask_volumes",
                        "timestamp",
                    ],
                    "preview": [
                        {"symbol": k, **{kk: vv for kk, vv in v.items() if kk != "timestamp"}}
                        for k, v in list(books.items())[:3]
                    ],
                }
            if dataset == "realtime_indices":
                rows = self.get_realtime_indices(syms)
                if rows is None:
                    return {
                        "provider": self.name,
                        "dataset": dataset,
                        "rows": 0,
                        "error": "指数快照请求失败，请查看日志",
                    }
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": len(rows),
                    "columns": list(rows[0].keys()) if rows else [],
                    "preview": rows[:3],
                }
        except FreeCnError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": 0,
            "error": f"未支持的数据集: {dataset}(本插件只声明 {', '.join(_DATASETS)})",
        }


# ---------------------------------------------------------------- 映射工具
def _as_date(value: datetime | date) -> date:
    return value if type(value) is date else _as_beijing_wallclock(value).date()


def _as_beijing_wallclock(value: datetime) -> datetime:
    return value.astimezone(_BEIJING).replace(tzinfo=None) if value.tzinfo else value


def _parse_sina_dt(raw: Any) -> datetime | None:
    """'2026-10-20 09:31:00' → naive 北京时间(契约要求墙钟, 不带时区)。"""
    if not raw:
        return None
    text = str(raw)
    try:
        return _as_beijing_wallclock(datetime.fromisoformat(text))
    except ValueError:
        return None


def _minute_row(symbol: str, dt: datetime, item: dict) -> dict | None:
    try:
        o, h, low, c = (
            float(item["open"]),
            float(item["high"]),
            float(item["low"]),
            float(item["close"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    vol = _lots(item.get("volume"))
    if not all(math.isfinite(value) and value > 0 for value in (o, h, low, c)):
        return None
    return {
        "symbol": symbol,
        "datetime": dt,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": float(vol) if vol is not None else None,
        "amount": None,
    }  # 上游无分钟成交额: 契约要求返 null 不伪造


def _map_sina_index(symbol: str, fields: list[str]) -> dict | None:
    if len(fields) < 32:
        return None
    last = _to_float(fields[3])
    prev = _to_float(fields[2])
    timestamp = parse_quote_ts(fields)
    if last is None or last <= 0 or timestamp is None:
        logger.warning("free_cn 报价缺少有效价格或行情时间: %s", symbol)
        return None
    change_amount = last - prev if (last is not None and prev is not None) else None
    return {
        "symbol": symbol,
        "name": fields[0],
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(fields[1]),
        "high": _to_float(fields[4]),
        "low": _to_float(fields[5]),
        # 指数成交量/额的单位各家口径不一(手/股、元/万元), 不猜 → null
        "volume": None,
        "amount": None,
        "change_pct": (change_amount / prev) if (change_amount is not None and prev) else None,
        "change_amount": change_amount,
        "amplitude": None,
        "turnover_rate": None,
        "timestamp": timestamp,
        "session": None,
    }


def _parse_depth(symbol: str, fields: list[str]) -> dict | None:
    """保留五档位置和零股，区分有效空档(0)与缺失数量(None)。"""
    if len(fields) < 32:
        return None
    timestamp = parse_quote_ts(fields)
    if timestamp is None:
        return None

    def side(offset: int) -> tuple[list, list]:
        prices, volumes = [], []
        for i in range(5):
            price = _to_float(fields[offset + i * 2 + 1])
            shares = _to_float(fields[offset + i * 2])
            prices.append(price)
            volumes.append(shares / 100 if shares is not None and shares >= 0 else None)
        return prices, volumes

    bid_prices, bid_volumes = side(10)
    ask_prices, ask_volumes = side(20)
    return {
        "bid_prices": bid_prices,
        "bid_volumes": bid_volumes,
        "ask_prices": ask_prices,
        "ask_volumes": ask_volumes,
        "timestamp": timestamp,
    }


def _iso_row(row: dict) -> dict:
    for k, v in list(row.items()):
        if isinstance(v, (date, datetime)):
            row[k] = v.isoformat()
    return row
