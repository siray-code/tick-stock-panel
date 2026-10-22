"""新浪公开行情客户端：请求、限频及响应结构校验。

HTTP 层用 httpx(后端默认依赖), 不引入 requests/akshare/baostock。

设计约束 (docs/plugin-development.md):
  - 每个数据集只有**一个**主源, 不做运行期静默回退; 换源改常量 + 重启。
  - client 层只负责"取原始字段", 单位/代码格式/复权口径的适配全部在 provider 层,
    便于用单测锁定 (股→手, 百分数→小数, 北京墙钟, 单事件 ex_factor)。
  - 失败抛 FreeCnError, 由 provider 决定软返回还是上抛 (realtime 软返回, 其余上抛)。

实测口径 (2026-10, 不可凭字段名推断):
  - 新浪日线 getKLineData: 字段 day/open/high/low/close/volume; volume 为**股**;
    datalen 请求上限 6000 根，不承诺所有标的的完整历史，无成交额字段。
  - 新浪除权 hfq.js: 明文 JSON `{"total":N,"data":[{"d":"YYYY-MM-DD","f":"累积因子"}]}`,
    按日期倒序; 因子为**累积后复权因子**, 单事件比值 = f_i / f_{i-1}。
  - 新浪分钟(移动端 quotes.sina.cn, scale=1): 240 根/日, datalen 上限 1023(约 4 个交易日),
    字段同 day/open/high/low/close/volume, volume 为**股**, 无成交额。
  - 新浪盘口 hq.sinajs.cn: GBK 逗号分隔, 顺序
    name,open,prev_close,price,high,low,bid1,ask1,volume(股),amount(元),
    然后 5×(买量,买价)、5×(卖量,卖价), 末尾 date,time; 量单位**股**, 需 /100 转手。
  - Market_Center 仅用于枚举股票及获取换手率；实时价格与时间取 hq.sinajs.cn。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

SINA_KLINE = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
)
SINA_KLINE_MOBILE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
SINA_HFQ = "https://finance.sina.com.cn/realstock/company/{sym}/hfq.js"
SINA_QUOTE = "https://hq.sinajs.cn/list={codes}"
SINA_MARKET = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
    "json_v2.php/Market_Center.getHQNodeData"
)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
_SINA_HEADERS = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"}

DAILY_MAX_BARS = 6000  # 前收盘配价使用的单次请求上限
MINUTE_MAX_BARS = 1023  # 实测上限, 约 4 个交易日
QUOTE_BATCH = 100  # hq.sinajs.cn 单请求实测 100 只稳定
SINA_MARKET_PAGE = 100  # Market_Center 单页实测上限 100

# 限频: 同一上游进程内串行排队, 避免被限流/封路径。实时与五档共用 hq 节流器。
THROTTLE_MARKET = 0.12  # 全市场快照翻页间隔(秒): 约 60 页 ≈ 8s
THROTTLE_QUOTE = 0.08  # hq.sinajs.cn 批量(五档/指数/实时报价)
THROTTLE_KLINE = 0.20  # 分钟/日线单标的(全市场分钟轮需 20min 量级, 有意为之)
_TIMEOUT = 20.0
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BEIJING = timezone(timedelta(hours=8))


class FreeCnError(RuntimeError):
    """上游取数失败(网络/结构变化)。provider 层决定软返回或上抛。"""


def to_sina_symbol(symbol: str) -> str:
    """600519.SH → sh600519; 接受裸代码, 缺后缀按首位推断交易所。"""
    code, _, mkt = symbol.strip().upper().partition(".")
    code = code.strip()
    if not mkt:
        # 北交所: 4/8 开头与 920 号段(新代码); 沪市 6/9(含 900 B 股); 其余深市
        if code.startswith(("4", "8")) or code.startswith("92"):
            mkt = "BJ"
        elif code.startswith(("6", "9")):
            mkt = "SH"
        else:
            mkt = "SZ"
    return f"{mkt.lower()}{code}"


def from_sina_code(code: str) -> str:
    """sh600519 → 600519.SH。"""
    code = code.strip()
    mkt = code[:2].upper()
    return f"{code[2:]}.{mkt}"


_THROTTLE_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}


def _throttle(key: str, interval: float) -> None:
    """同一上游的最小调用间隔(进程内全局, 多线程共享同一令牌)。"""
    if interval <= 0:
        return
    with _THROTTLE_LOCK:
        wait = interval - (time.monotonic() - _LAST_CALL.get(key, 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[key] = time.monotonic()


def _json_rows(response: httpx.Response, label: str, required: tuple[str, ...] = ()) -> list[dict]:
    if response.status_code != 200:
        raise FreeCnError(f"{label}: HTTP {response.status_code}")
    try:
        rows = response.json()
    except ValueError as e:
        raise FreeCnError(f"{label}: JSON 解析失败") from e
    if rows is None:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise FreeCnError(f"{label}: 响应结构应为记录列表")
    if any(any(key not in row for key in required) for row in rows):
        raise FreeCnError(f"{label}: 必需字段缺失")
    return rows


class SinaClient:
    """新浪: 日线(明文 JSON) / 除权事件(hfq.js) / 1 分钟(移动端) / 五档批量(hq.sinajs.cn)."""

    def __init__(self) -> None:
        self._lock = threading.BoundedSemaphore(4)
        self._http = httpx.Client(
            timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
        )

    def close(self) -> None:
        self._http.close()

    def _get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        throttle: tuple[str, float] | None = None,
    ) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(3):
            try:
                if throttle:
                    _throttle(*throttle)
                return self._http.get(url, params=params, headers=headers or _SINA_HEADERS)
            except httpx.HTTPError as e:
                last = e
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
        raise FreeCnError(f"GET {url} failed: {last}") from last

    # ---- 日线 ----
    def daily(self, symbol: str, bars: int = DAILY_MAX_BARS) -> list[dict]:
        sym = to_sina_symbol(symbol)
        with self._lock:
            r = self._get(
                SINA_KLINE,
                params={
                    "symbol": sym,
                    "scale": "240",
                    "ma": "no",
                    "datalen": str(min(bars, DAILY_MAX_BARS)),
                },
                throttle=("sina_kline", THROTTLE_KLINE),
            )
        return _json_rows(r, f"新浪日线({sym})", ("day", "open", "high", "low", "close"))

    # ---- 除权事件 ----
    def hfq_events(self, symbol: str) -> list[tuple[date, float]]:
        """返回按日期升序的 (除权日, 累积后复权因子)。hfq.js 明文, 倒序给。"""
        sym = to_sina_symbol(symbol)
        r = self._get(SINA_HFQ.format(sym=sym), throttle=("sina_kline", THROTTLE_KLINE))
        text = r.text.strip()
        start, end = text.find("{"), text.rfind("}")
        if r.status_code != 200 or start < 0 or end <= start:
            raise FreeCnError(f"新浪除权返回异常({sym}): HTTP {r.status_code}")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise FreeCnError(f"新浪除权 JSON 解析失败({sym})") from e
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise FreeCnError(f"新浪除权响应结构变化({sym})")
        out: list[tuple[date, float]] = []
        for item in payload["data"]:
            try:
                event_date = date.fromisoformat(str(item["d"])[:10])
                factor = float(item["f"])
                if not math.isfinite(factor) or factor <= 0:
                    raise ValueError("invalid factor")
                out.append((event_date, factor))
            except (KeyError, TypeError, ValueError):
                raise FreeCnError(f"新浪除权事件字段变化({sym})") from None
        out.sort(key=lambda x: x[0])
        return out

    # ---- 1 分钟 ----
    def minute(self, symbol: str, bars: int = 240) -> list[dict]:
        sym = to_sina_symbol(symbol)
        n = max(1, min(int(bars), MINUTE_MAX_BARS))
        with self._lock:
            r = self._get(
                SINA_KLINE_MOBILE,
                params={"symbol": sym, "scale": "1", "ma": "no", "datalen": str(n)},
                headers={"User-Agent": _UA, "Referer": "https://finance.sina.cn"},
                throttle=("sina_kline", THROTTLE_KLINE),
            )
        return _json_rows(r, f"新浪分钟({sym})", ("day", "open", "high", "low", "close"))

    # ---- 全市场快照 (realtime 主源) ----
    def market_snapshot(self, max_pages: int = 80) -> list[dict]:
        """全市场 A 股(沪深北)快照: Market_Center.getHQNodeData 分页, 100 只/页。

        返回上游原始 dict 行(changepercent/turnoverratio 为百分数, volume 为股)。
        翻页间隔由 THROTTLE_MARKET 控制; 读到空页或不足一页即停。
        """
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            r = self._get(
                SINA_MARKET,
                params={
                    "page": page,
                    "num": SINA_MARKET_PAGE,
                    "sort": "symbol",
                    "asc": "1",
                    "node": "hs_a",
                    "symbol": "",
                    "_s_r_a": "page",
                },
                throttle=("sina_market", THROTTLE_MARKET),
            )
            items = _json_rows(r, "新浪全市场快照")
            if not items:
                break
            if any(not re.fullmatch(r"\d{6}", str(item.get("code", ""))) for item in items):
                raise FreeCnError("新浪全市场快照代码字段变化")
            rows.extend(items)
            if len(items) < SINA_MARKET_PAGE:
                break
        else:
            raise FreeCnError(f"新浪全市场快照超过页数上限 {max_pages}")
        return rows

    # ---- 五档 / 指数快照 ----
    def quote_batch(self, symbols: Iterable[str]) -> dict[str, list[str]]:
        """批量取 hq.sinajs.cn，返回 {带交易所后缀代码: 字段列表}。"""
        out: dict[str, list[str]] = {}
        codes = [to_sina_symbol(s) for s in symbols]
        for i in range(0, len(codes), QUOTE_BATCH):
            chunk = codes[i : i + QUOTE_BATCH]
            with self._lock:
                r = self._get(
                    SINA_QUOTE.format(codes=",".join(chunk)),
                    headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"},
                    throttle=("sina_hq", THROTTLE_QUOTE),
                )
            if r.status_code != 200:
                raise FreeCnError(f"新浪盘口返回异常: HTTP {r.status_code}")
            seen: set[str] = set()
            try:
                lines = r.content.decode("gbk", errors="strict").splitlines()
            except UnicodeError as e:
                raise FreeCnError("新浪报价编码异常") from e
            for line in lines:
                match = re.fullmatch(
                    r'\s*var\s+hq_str_((?:sh|sz|bj)\d{6})="([^"\r\n]*)";?\s*', line
                )
                if not match or match[1] not in chunk:
                    raise FreeCnError("新浪报价响应结构变化")
                raw_code, body = match.groups()
                seen.add(raw_code)
                if not body:
                    continue  # 明确的成功空报价，与错误页/缺少响应区分。
                fields = body.split(",")
                if len(fields) < 32 or not fields[0]:
                    raise FreeCnError(f"新浪报价字段变化({raw_code})")
                if parse_quote_ts(fields) is None:
                    raise FreeCnError(f"新浪报价时间字段变化({raw_code})")
                out[from_sina_code(raw_code)] = fields
            if seen != set(chunk):
                raise FreeCnError("新浪报价响应缺少请求标的")
        return out


def parse_quote_ts(fields: list[str]) -> int | None:
    """hq.sinajs.cn 末尾 date,time → 毫秒时间戳(北京时间)。"""
    if len(fields) < 32:
        return None
    # 新浪尾部字段个数已从 33 扩到 34(盘后固定价格交易等), 位置不可靠 → 按模式定位
    d_idx = next((i for i, f in enumerate(fields) if _DATE_RE.match(f)), None)
    if d_idx is None or d_idx + 1 >= len(fields):
        return None
    try:
        dt = datetime.strptime(f"{fields[d_idx]} {fields[d_idx + 1]}", "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return int(dt.replace(tzinfo=_BEIJING).timestamp() * 1000)
