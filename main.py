"""Plain-English query service for the Acme Appliances ledger."""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

DATA_URL = os.getenv(
    "LEDGER_DATA_URL",
    "https://exam.sanand.workers.dev/questionData?email=24f3003132%40ds.study.iitm.ac.in"
    "&quizSign=ckOc7kBsruLGryWtCv2nkuBFHbT5XqsGZAUm5HuLW5dEQWxAoAXkkI9gTkK2q8julkieVaySMrHLPE7fmtUPH9uOGRxG1%2FowRf0VxsNz9w31QUVo6rlsUa60u8OCWNToumMuF%2FtCuCdIg9aO4rrRqHRvFYi3w5m4raF28abwuu9%2Fzw8kYK3QZIc9RTEiX12bfpfHq0jDmx0s8s6elEf325St2jIIPLz37KIkltjmEjTT%2B%2F2k5nh97gFpc2o9jnfXxVo2nKkdvW%2FgArySsUwQxwzc6RmIlCVfSx5fVm22dDTx7Rwk069FdeW8OGx7GAMAed%2Fmo%2BNyfqWo7cbYBvUPQ%3D%3D"
    "&questionId=q-ledger-agent-server&path=/"
)
AI_PIPE_URL = os.getenv(
    "AI_PIPE_URL", "https://aipipe.org/openrouter/v1/chat/completions"
)
TZ = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Acme Appliances Ledger Agent")
orders: list[dict[str, Any]] = []
rates: dict[str, Decimal] = {}


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Plan(BaseModel):
    metric: Literal["revenue", "refunds", "orders", "quantity", "customers"]
    region: str | None = None
    product: str | None = None
    customer: str | None = None
    status: str | None = None
    start: str | None = None
    end: str | None = None
    group_by: Literal["none", "region", "product", "customer", "month"] = "none"
    limit: int = Field(default=10, ge=1, le=100)


def _date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(TZ)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _extract_orders(payload: Any) -> list[dict[str, Any]]:
    """Accept both the documented paginated shape and the export NDJSON shape."""
    if isinstance(payload, dict):
        if isinstance(payload.get("orders"), list):
            return [x for x in payload["orders"] if isinstance(x, dict)]
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, str):
        result = []
        for line in payload.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            result.extend(_extract_orders(item))
        return result
    return []


async def _load_data() -> None:
    global orders, rates
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LedgerAgent/1.0)"},
    ) as client:
        root = (await client.get(DATA_URL)).raise_for_status().json()
        links = root["links"]
        rates_payload = (await client.get(links["rates"])).raise_for_status().json()
        rates = {k: Decimal(str(v)) for k, v in rates_payload["usd_per_unit"].items()}

        # The export endpoint is the fastest path. Fall back to pages for API variants
        # that expose export as an object or do not provide it.
        response = await client.get(links["export"])
        response.raise_for_status()
        raw = response.json() if "json" in response.headers.get("content-type", "") else response.text
        loaded = _extract_orders(raw)
        if not loaded:
            first = (await client.get(links["orders"])).raise_for_status().json()
            loaded = _extract_orders(first)
            next_url = first.get("next")
            while next_url:
                page = (await client.get(next_url)).raise_for_status().json()
                loaded.extend(_extract_orders(page))
                next_url = page.get("next")

    # An order ID can have historical rows. Keep the newest updated_at.
    current: dict[str, dict[str, Any]] = {}
    for order in loaded:
        if not all(k in order for k in ("id", "updated_at")):
            continue
        old = current.get(str(order["id"]))
        if old is None or _date(str(order["updated_at"])) < _date(str(old["updated_at"])):
            current[str(order["id"])] = order
    orders = list(current.values())


def _fallback_plan(question: str) -> Plan:
    """Small safety net for temporary AI PIPE failures; calculations remain local."""
    text = question.lower()
    metric: Literal["revenue", "refunds", "orders", "quantity", "customers"] = "revenue"
    if "refund" in text:
        metric = "refunds"
    elif "how many order" in text or "number of order" in text or "count" in text:
        metric = "orders"
    elif "unit" in text or "quantity" in text or "items" in text:
        metric = "quantity"
    elif "customer" in text and ("how many" in text or "number" in text or "count" in text):
        metric = "customers"
    group: Literal["none", "region", "product", "customer", "month"] = "none"
    for word, candidate in (("region", "region"), ("product", "product"), ("customer", "customer"), ("month", "month")):
        if word in text:
            group = candidate  # type: ignore[assignment]
    return Plan(metric=metric, group_by=group)


async def _make_plan(question: str) -> Plan:
    token = os.getenv("AIPIPE_TOKEN") or os.getenv("AI_PIPE_TOKEN")
    if not token:
        return _fallback_plan(question)
    schema = Plan.model_json_schema()
    prompt = (
        "Convert the user's ledger question into JSON matching this schema. "
        "Do not calculate. Dates are calendar dates in Asia/Kolkata; end is exclusive. "
        "Revenue means paid orders and refunds means refunded orders. "
        f"Schema: {json.dumps(schema)}\nQuestion: {question}"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                AI_PIPE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.getenv("AI_PIPE_MODEL", "openai/gpt-4.1-nano"),
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "Return JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return Plan.model_validate_json(content)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return _fallback_plan(question)


def _answer(plan: Plan) -> Any:
    selected = []
    for order in orders:
        if plan.region and order.get("region", "").lower() != plan.region.lower():
            continue
        if plan.product and order.get("product", "").lower() != plan.product.lower():
            continue
        if plan.customer and str(order.get("customer", "")).lower() != plan.customer.lower():
            continue
        if plan.status and order.get("status", "").lower() != plan.status.lower():
            continue
        moment = _date(str(order["created_at"]))
        if plan.start and moment < _date(plan.start):
            continue
        if plan.end and moment >= _date(plan.end):
            continue
        selected.append(order)

    def value(order: dict[str, Any]) -> Decimal:
        if plan.metric in ("orders", "quantity"):
            return Decimal("1") if plan.metric == "orders" else Decimal(str(order["qty"]))
        if plan.metric == "customers":
            return Decimal("1")
        return Decimal(str(order["amount"])) * rates[str(order["currency"])]

    if plan.metric == "revenue":
        selected = [o for o in selected if o.get("status") == "paid"]
    elif plan.metric == "refunds":
        selected = [o for o in selected if o.get("status") == "refunded"]

    if plan.group_by == "none":
        if plan.metric == "customers":
            return len({str(o["customer"]) for o in selected})
        return int(sum((value(o) for o in selected), Decimal("0"))) if plan.metric in ("orders", "quantity") else _money(sum((value(o) for o in selected), Decimal("0")))

    groups: dict[str, Decimal] = {}
    customer_groups: dict[str, set[str]] = {}
    for order in selected:
        if plan.group_by == "month":
            key = _date(str(order["created_at"])).strftime("%Y-%m")
        else:
            key = str(order.get(plan.group_by, ""))
        if plan.metric == "customers":
            customer_groups.setdefault(key, set()).add(str(order["customer"]))
        else:
            groups[key] = groups.get(key, Decimal("0")) + value(order)
    if plan.metric == "customers":
        groups = {key: Decimal(len(customers)) for key, customers in customer_groups.items()}
    ranked = sorted(groups.items(), key=lambda item: (-item[1], item[0]))[: plan.limit]
    return [
        {"group": key, "value": int(amount) if plan.metric in ("orders", "quantity", "customers") else _money(amount)}
        for key, amount in ranked
    ]


@app.on_event("startup")
async def startup() -> None:
    await _load_data()


@app.post("/")
@app.post("/ask")
async def ask(question: Question) -> dict[str, Any]:
    if not orders:
        raise HTTPException(status_code=503, detail="Ledger data is not loaded")
    plan = await _make_plan(question.question)
    return {"answer": _answer(plan)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )
