"""FastAPI 入口：网页 + 对照批次 API + 样表补记 API。"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from plans import structure_lines
from runner import BatchRunner
from sqlcheck import validate_select_sql
from store import Batch, BatchStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/settlement"
)

store = BatchStore(capacity=50)
runner = BatchRunner(DATABASE_URL, store)
pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    last_exc: Exception | None = None
    for _ in range(60):  # 等数据库就绪（compose 里还有 healthcheck 兜底）
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
            break
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(1)
    if pool is None:
        raise RuntimeError(f"无法连接数据库: {last_exc}")
    yield
    await pool.close()


app = FastAPI(title="结算 SQL 计划对照台", lifespan=lifespan)


class BatchIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


@app.post("/api/batches")
async def create_batch(body: BatchIn):
    try:
        sql = validate_select_sql(body.sql)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    batch = Batch(sql=sql)
    if not runner.try_start(batch):
        return JSONResponse(
            status_code=409,
            content={"detail": "已有对照批次在运行，当前繁忙，请稍后再试", "busy": True},
        )
    store.add(batch)
    return JSONResponse(status_code=201, content=batch.to_dict())


@app.get("/api/batches")
async def list_batches():
    return {
        "busy": runner.busy,
        "current_batch_id": runner.current_batch_id,
        "batches": [b.to_dict() for b in store.list()],
    }


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str):
    batch = store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    data = batch.to_dict(include_plans=True)
    for ex, ex_dict in zip(batch.executions, data["executions"]):
        if ex.plan_structure is not None:
            ex_dict["structure_lines"] = structure_lines(ex.plan_structure)
    return data


@app.post("/api/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    batch = store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    found, pid = runner.request_cancel(batch_id)
    if not found:
        return {"ok": False, "detail": "批次未在运行，无法取消"}
    if pid is not None and pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT pg_cancel_backend($1)", pid)
        except Exception:
            pass  # 取消请求失败不阻断，worker 侧还有标记位和兜底超时
    return {"ok": True}


@app.post("/api/sample-row")
async def add_sample_row():
    """补记一行结算样表数据；对照运行期间也可写入（对照事务用同一快照，看不到新行）。"""
    if pool is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH m AS (
                SELECT 1 + (random() * 4999)::int              AS merchant_id,
                       round((1 + random() * 499)::numeric, 2) AS amount
            )
            INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
            SELECT 'SO-M-' || upper(substr(md5(random()::text), 1, 10)),
                   merchant_id, amount, round(amount * 0.006, 2), 'SETTLED', CURRENT_DATE
            FROM m
            RETURNING order_id, order_no, merchant_id, amount, fee, status, settle_date
            """
        )
    return {"ok": True, "row": _jsonable(dict(row))}


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    return value


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
