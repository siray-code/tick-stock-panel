"""Short-lived, killable AKShare worker. Not imported by the web process.

AKShare 1.18.84 does not expose HTTP timeouts or paging evidence. A scoped
requests wrapper in this isolated process supplies both; never patch the host.
"""
from __future__ import annotations

import contextlib
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def normalize_rows(rows: list[dict], catalog: dict[str, str]) -> list[dict]:
    """Convert explicit upstream units: 亿元 → 元, 百分数 → 小数."""
    result = []
    required = {"行业", "流入资金", "流出资金", "净额", "公司家数"}
    for raw in rows:
        if not required.issubset(raw):
            raise ValueError("同花顺资金流字段缺失")
        name = str(raw["行业"]).strip()
        subject_id = catalog.get(name)

        def number(value):
            if value is None or str(value).strip() in {"", "--", "-", "nan"}:
                return None
            n = float(str(value).replace(",", "").rstrip("%"))
            if not math.isfinite(n):
                raise ValueError("non-finite funds value")
            return n

        pct = number(raw.get("行业-涨跌幅", raw.get("阶段涨跌幅")))
        count = number(raw["公司家数"])
        if count is not None and not count.is_integer():
            raise ValueError("invalid member count")
        result.append({
            "source_subject_key": subject_id or f"name:{name}", "subject_id": subject_id, "name": name,
            **{field: None if (n := number(raw[label])) is None else n * 100_000_000
               for field, label in (("inflow_amount", "流入资金"), ("outflow_amount", "流出资金"), ("net_amount", "净额"))},
            "change_pct": None if pct is None else pct / 100,
            "member_count": None if count is None else int(count),
        })
    return result


def normalize_catalog(rows: list[dict]) -> dict[str, str]:
    catalog: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in rows:
        name, code = str(row["name"]).strip(), str(row["code"]).strip()
        if not name or not re.fullmatch(r"\d{6}", code):
            continue
        if name in catalog and catalog[name] != code:
            ambiguous.add(name)
        catalog[name] = code
    return {name: code for name, code in catalog.items() if name not in ambiguous}


class PageEvidence:
    def __init__(self):
        self.pages: dict[int, int] = {}
        self.total: int | None = None

    def observe(self, url: str, body: str) -> None:
        if "/funds/" not in url:
            return
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body, "lxml")
        info = soup.select_one(".page_info")
        table = soup.find("table")
        if info is None or table is None:
            raise ValueError("missing funds paging evidence")
        header = "".join(th.get_text(strip=True) for th in table.find_all("th"))
        for label in ("流入资金", "流出资金", "净额"):
            if not re.search(label + r"\s*[（(]\s*亿(?:元)?\s*[）)]", header):
                raise ValueError("funds amount unit or columns changed")
        match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", info.get_text())
        if match is None:
            raise ValueError("invalid funds paging evidence")
        page, total = map(int, match.groups())
        requested = re.search(r"/page/(\d+)/", url)
        if requested and page != int(requested[1]):
            raise ValueError("upstream returned wrong page")
        if total < 1 or total > 100 or not 1 <= page <= total:
            raise ValueError("invalid funds page range")
        if self.total is not None and self.total != total:
            raise ValueError("funds page count changed during fetch")
        self.total = total
        # The initial discovery page may be repeated by AKShare; do not count it twice.
        count = len([tr for tr in table.find_all("tr") if tr.find("td") is not None])
        if count == 0:
            raise ValueError("empty funds page")
        self.pages[page] = count

    def validate(self, row_count: int) -> None:
        if self.total is None or set(self.pages) != set(range(1, self.total + 1)):
            raise ValueError("incomplete funds pagination")
        if sum(self.pages.values()) != row_count:
            raise ValueError("funds record count mismatch")


def fetch(request: dict) -> dict:
    import akshare as ak
    import requests

    query = request["query"]
    kind = query["subject_type"]
    window = query["window_id"]
    labels = {"today": "即时", **{f"{n}d": f"{n}日排行" for n in (3, 5, 10, 20)}}
    if kind not in {"industry", "concept"} or window not in labels:
        raise ValueError("unsupported query")
    original_get = requests.get
    evidence = PageEvidence()

    def guarded_get(url, *args, **kwargs):
        kwargs["timeout"] = (5, 12)
        response = original_get(url, *args, **kwargs)
        response.raise_for_status()
        evidence.observe(str(url), response.text)
        return response

    fn = ak.stock_fund_flow_industry if kind == "industry" else ak.stock_fund_flow_concept
    with patch("requests.get", guarded_get):
        frame = fn(symbol=labels[window])
    evidence.validate(len(frame))
    catalog = request.get("catalog")
    if catalog is None:
        catalog_fn = ak.stock_board_industry_name_ths if kind == "industry" else ak.stock_board_concept_name_ths
        # Catalog failure leaves rows explicitly unlinked; it must not hide the funds data.
        try:
            with patch("requests.get", lambda *a, **kw: original_get(*a, **(kw | {"timeout": (5, 12)}))):
                names = catalog_fn().to_dict("records")
            catalog = normalize_catalog(names)
        except Exception:  # noqa: BLE001 — canonical identity is optional, amounts are not
            catalog = {}
    return {
        "rows": normalize_rows(frame.to_dict("records"), catalog), "catalog": catalog,
        "expected_rows": len(frame),
        "fetched_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    }


def main() -> None:
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(sys.stderr):
        result = fetch(request)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
