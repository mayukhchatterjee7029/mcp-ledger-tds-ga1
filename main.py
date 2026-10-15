import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="The Ledger Agent", version="1.0.0")

QUESTION_DATA_URL = "https://exam.sanand.workers.dev/questionData?email=24f3003132%40ds.study.iitm.ac.in&quizSign=ckOc7kBsruLGryWtCv2nkuBFHbT5XqsGZAUm5HuLW5dEQWxAoAXkkI9gTkK2q8julkieVaySMrHLPE7fmtUPH9uOGRxG1%2FowRf0VxsNz9w31QUVo6rlsUa60u8OCWNToumMuF%2FtCuCdIg9aO4rrRqHRvFYi3w5m4raF28abwuu9%2Fzw8kYK3QZIc9RTEiX12bfpfHq0jDmx0s8s6elEf325St2jIIPLz37KIkltjmEjTT%2B%2F2k5nh97gFpc2o9jnfX%2FxVo2nKkdvW%2FgArySsUwQxwzc6RmIlCVfSx5fVm22dDTx7Rwk069FdeW8OGx7GAMAed%2Fmo%2BNyfqWo7cbYBvUPQ%3D%3D&questionId=q-ledger-agent-server&path=/"

AIPIPE_TOKEN = os.getenv("AIPIPE_TOKEN", "").strip()
AIPIPE_MODEL = os.getenv("AIPIPE_MODEL", "openai/gpt-4o-mini").strip()
AIPIPE_URL = os.getenv(
    "AIPIPE_URL", "https://aipipe.org/openrouter/v1/chat/completions"
).strip()
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))

_session = requests.Session()
_cache: Dict[str, Any] = {"at": 0.0, "records": None, "schema": None, "raw": None}


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def _get_json(url: str, params: Optional[dict] = None) -> Any:
    response = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text[:5000]}


def _walk(obj: Any, path: str = "$") -> List[Tuple[str, Any]]:
    """Yield every path/value pair in a JSON document."""
    found = [(path, obj)]
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(_walk(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for index, value in enumerate(obj[:1000]):
            found.extend(_walk(value, f"{path}[{index}]"))
    return found


def _find_record_lists(obj: Any) -> List[List[dict]]:
    """Find arrays that look like collections of ledger records."""
    candidates: List[List[dict]] = []
    for _, value in _walk(obj):
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            # Prefer object arrays with a consistent shape.
            keysets = [set(x.keys()) for x in value[:20]]
            if keysets and len(set(map(tuple, map(sorted, keysets)))) <= max(2, len(keysets) // 3):
                candidates.append(value)
    candidates.sort(key=len, reverse=True)
    return candidates


def _find_urls(obj: Any, base_url: str) -> List[str]:
    urls: List[str] = []
    for _, value in _walk(obj):
        if isinstance(value, str):
            candidate = value.strip()
            if candidate.startswith(("http://", "https://", "/")):
                absolute = urljoin(base_url, candidate)
                parsed = urlparse(absolute)
                if parsed.scheme in ("http", "https") and absolute not in urls:
                    urls.append(absolute)
    return urls


def _find_next(obj: Any, base_url: str) -> Optional[str]:
    """Follow only explicit next-page links/cursors/URLs returned by the API."""
    if not isinstance(obj, dict):
        return None
    next_keys = {"next", "nexturl", "next_url", "nextpageurl", "next_page_url", "nextcursorurl"}
    for key, value in obj.items():
        normalized = re.sub(r"[^a-z]", "", key.lower())
        if normalized in next_keys and isinstance(value, str) and value.strip():
            return urljoin(base_url, value.strip())
        if normalized in {"links", "pagination", "paging", "pageinfo", "meta"} and isinstance(value, dict):
            result = _find_next(value, base_url)
            if result:
                return result
    # Common explicit link shape: {"links": {"next": "..."}}
    for value in obj.values():
        if isinstance(value, dict):
            result = _find_next(value, base_url)
            if result:
                return result
    return None


def _is_likely_record(record: dict) -> bool:
    keys = {re.sub(r"[^a-z]", "", str(k).lower()) for k in record}
    signals = {
        "orderid", "order_id", "date", "orderdate", "amount", "revenue",
        "product", "productname", "customer", "customername", "refund",
        "region", "transactiontype", "type", "total"
    }
    return bool(keys & signals)


def discover_and_fetch() -> Tuple[List[dict], Any]:
    """
    Load the bootstrap endpoint, collect embedded records, and follow explicit
    next-page links. This is intentionally conservative: it never brute-forces
    guessed endpoints or parameter names.
    """
    if not QUESTION_DATA_URL:
        raise RuntimeError(
            "Set QUESTION_DATA_URL to the complete questionData URL from the assignment."
        )

    now = time.time()
    if _cache["records"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["records"], _cache["schema"]

    current_url = QUESTION_DATA_URL
    seen_urls = set()
    records: List[dict] = []
    bootstrap: Any = None
    pages = 0

    while current_url and current_url not in seen_urls and pages < 100:
        seen_urls.add(current_url)
        payload = _get_json(current_url)
        if bootstrap is None:
            bootstrap = payload

        candidates = _find_record_lists(payload)
        if candidates:
            # Select the largest likely record collection, avoiding arbitrary
            # metadata arrays where possible.
            likely = [arr for arr in candidates if any(_is_likely_record(x) for x in arr[:10])]
            selected = max(likely or candidates, key=len)
            records.extend(selected)

        next_url = _find_next(payload, current_url)
        if next_url and next_url not in seen_urls:
            current_url = next_url
        else:
            current_url = None
        pages += 1

    # De-duplicate repeated records across pages using canonical JSON.
    unique: Dict[str, dict] = {}
    for record in records:
        unique[json.dumps(record, sort_keys=True, default=str)] = record
    records = list(unique.values())

    schema = {
        "bootstrap_type": type(bootstrap).__name__,
        "top_level_keys": list(bootstrap.keys()) if isinstance(bootstrap, dict) else [],
        "record_count": len(records),
        "sample_record": records[0] if records else None,
        "advertised_urls": _find_urls(bootstrap, QUESTION_DATA_URL)[:80],
        "pagination_next_found": pages > 1,
    }
    _cache.update({"at": now, "records": records, "schema": schema, "raw": bootstrap})
    return records, schema


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _get(record: dict, aliases: List[str]) -> Any:
    lookup = {_normalize_key(k): v for k, v in record.items()}
    for alias in aliases:
        key = _normalize_key(alias)
        if key in lookup:
            return lookup[key]
    return None


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "").replace("USD", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Do not guess whether numeric timestamps are seconds or milliseconds.
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%m/%d/%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(text[:26], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _ask_ai(question: str) -> dict:
    if not AIPIPE_TOKEN:
        raise RuntimeError("Set AIPIPE_TOKEN to your AI Pipe token.")
    prompt = """
Translate the user's question into a JSON query plan for an order ledger.
Return JSON only with keys:
metric: one of revenue, refunds, net_revenue, order_count, customer_count,
        product_list, customer_list, record_count, average_order_value
filters: object with region, product, customer, currency, start_date, end_date,
         transaction_type, status; use null for unspecified filters
group_by: null or one of region, product, customer, month, currency
sort: null or asc or desc
limit: integer or null
Use ISO dates. Interpret named months as calendar months. "Revenue" means
sales revenue unless the user's wording explicitly asks for net revenue.
Never calculate a numeric answer; only create the plan. Do not invent filter values.
"""
    response = _session.post(
        AIPIPE_URL,
        headers={
            "Authorization": f"Bearer {AIPIPE_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "model": AIPIPE_MODEL,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, dict):
        return content
    return json.loads(content)


def _record_matches(record: dict, filters: dict) -> bool:
    aliases = {
        "region": ["region", "sales_region", "territory"],
        "product": ["product", "product_name", "item", "sku_name"],
        "customer": ["customer", "customer_name", "client", "buyer"],
        "currency": ["currency", "currency_code"],
        "transaction_type": ["transaction_type", "type", "event_type", "kind"],
        "status": ["status", "order_status", "payment_status"],
    }
    for filter_name, alias_list in aliases.items():
        expected = filters.get(filter_name)
        if expected is None or expected == "":
            continue
        actual = _get(record, alias_list)
        if actual is None or str(actual).strip().casefold() != str(expected).strip().casefold():
            return False

    start = _parse_date(filters.get("start_date"))
    end = _parse_date(filters.get("end_date"))
    if start or end:
        actual_date = _parse_date(_get(record, [
            "date", "order_date", "created_at", "transaction_date", "timestamp"
        ]))
        if actual_date is None:
            return False
        if start and actual_date < start:
            return False
        if end and actual_date > end:
            return False
    return True


def _amount(record: dict) -> Optional[Decimal]:
    return _to_decimal(_get(record, [
        "amount", "total_amount", "order_total", "total", "revenue",
        "sales_amount", "transaction_amount", "refund_amount", "net_amount"
    ]))


def _transaction_kind(record: dict) -> str:
    kind = _get(record, [
        "transaction_type", "type", "event_type", "kind", "status"
    ])
    return str(kind or "").casefold()


def _is_refund(record: dict) -> bool:
    kind = _transaction_kind(record)
    return "refund" in kind or "return" in kind or _get(record, ["refund_amount"]) is not None


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def calculate_answer(records: List[dict], plan: dict) -> Any:
    filters = plan.get("filters") or {}
    selected = [r for r in records if _record_matches(r, filters)]
    metric = str(plan.get("metric", "revenue")).casefold()

    if metric in ("product_list", "customer_list"):
        aliases = ["product", "product_name", "item"] if metric == "product_list" else ["customer", "customer_name", "client"]
        values = sorted({
            str(_get(r, aliases)) for r in selected
            if _get(r, aliases) is not None
        })
        limit = plan.get("limit")
        return values[:int(limit)] if limit else values

    if metric in ("record_count", "order_count", "customer_count"):
        if metric == "customer_count":
            return len({
                str(_get(r, ["customer", "customer_name", "client"]))
                for r in selected
                if _get(r, ["customer", "customer_name", "client"]) is not None
            })
        if metric == "order_count":
            ids = {
                str(_get(r, ["order_id", "orderid", "id"]))
                for r in selected
                if _get(r, ["order_id", "orderid", "id"]) is not None
            }
            return len(ids) if ids else len(selected)
        return len(selected)

    def amount_of(record: dict) -> Decimal:
        amount = _amount(record)
        return amount if amount is not None else Decimal("0")

    sales = sum((amount_of(r) for r in selected if not _is_refund(r)), Decimal("0"))
    refunds = sum((abs(amount_of(r)) for r in selected if _is_refund(r)), Decimal("0"))

    if metric == "refunds":
        result = refunds
    elif metric == "net_revenue":
        result = sales - refunds
    elif metric == "average_order_value":
        order_ids = {
            str(_get(r, ["order_id", "orderid", "id"]))
            for r in selected if not _is_refund(r)
            and _get(r, ["order_id", "orderid", "id"]) is not None
        }
        count = len(order_ids) if order_ids else sum(1 for r in selected if not _is_refund(r))
        result = sales / count if count else Decimal("0")
    else:
        result = sales

    if plan.get("group_by"):
        group_by = str(plan["group_by"]).casefold()
        group_aliases = {
            "region": ["region", "sales_region", "territory"],
            "product": ["product", "product_name", "item"],
            "customer": ["customer", "customer_name", "client"],
            "currency": ["currency", "currency_code"],
        }
        grouped: Dict[str, Decimal] = {}
        for record in selected:
            if metric == "refunds" and not _is_refund(record):
                continue
            if metric in ("revenue", "average_order_value") and _is_refund(record):
                continue
            if group_by == "month":
                d = _parse_date(_get(record, ["date", "order_date", "created_at", "transaction_date"]))
                key = d.strftime("%Y-%m") if d else "unknown"
            else:
                key = str(_get(record, group_aliases.get(group_by, [group_by])) or "unknown")
            value = amount_of(record)
            if metric == "refunds":
                value = abs(value)
            elif metric == "net_revenue" and _is_refund(record):
                value = -abs(value)
            grouped[key] = grouped.get(key, Decimal("0")) + value
        return {k: _money(v) for k, v in sorted(grouped.items())}

    return _money(result)


@app.get("/")
def health():
    return {
        "service": "The Ledger Agent",
        "status": "ok",
        "usage": "POST / with JSON {'question': '...'}",
    }


@app.get("/debug/schema")
def debug_schema():
    """Useful during setup; remove or protect this route before production."""
    try:
        _, schema = discover_and_fetch()
        return schema
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/")
def answer_question(payload: QuestionRequest):
    try:
        records, schema = discover_and_fetch()
        if not records:
            raise RuntimeError(
                "No ledger records found in the bootstrap response or its explicit next-page links. "
                "Inspect GET /debug/schema and adapt discover_and_fetch() to the API's advertised routes."
            )
        plan = _ask_ai(payload.question)
        answer = calculate_answer(records, plan)
        return JSONResponse({"answer": answer})
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="An upstream request timed out.")
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
