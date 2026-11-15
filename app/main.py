"""FastAPI 入口：网页 + 对照批次 API + 样表补记 API + pg_stat_statements 采样 API。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from plans import structure_lines
from runner import BatchRunner
from sampler import SamplerError, SamplerService
from sqlcheck import validate_select_sql
from store import Batch, BatchStore

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/settlement"
)

store = BatchStore(capacity=50)
runner = BatchRunner(DATABASE_URL, store)
pool: asyncpg.Pool | None = None
sampler: SamplerService | None = None
auto_sampler: "AutoSampler | None" = None

DEFAULT_AUTO_INTERVAL_S = 30.0
MIN_AUTO_INTERVAL_S = 3.0


class AutoSampler:
    """可选的定时采样循环；采集失败只记录错误，不推进增量起点。"""

    def __init__(self, service: SamplerService):
        self._service = service
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self.interval_s = DEFAULT_AUTO_INTERVAL_S
        self.last_error: str | None = None
        self.last_sample_at: str | None = None
        self.sample_count = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, interval_s: float) -> None:
        self.stop()
        self.interval_s = max(MIN_AUTO_INTERVAL_S, float(interval_s))
        self.last_error = None
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(self._stop))

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
        self._task = None
        self._stop = None

    async def astop(self) -> None:
        """等待正在跑的循环真正退出，避免重启时出现两个采样任务。"""
        task = self._task
        self.stop()
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def _run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    result = await self._service.sample()
                    self.last_error = None
                    self.sample_count += 1
                    self.last_sample_at = result["sample"]["sampled_at"]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 目标未选、数据库瞬间不可用等
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("自动采样失败：%s", self.last_error)
        except asyncio.CancelledError:
            pass

    def status(self) -> dict:
        return {
            "running": self.running,
            "interval_s": self.interval_s,
            "last_error": self.last_error,
            "last_sample_at": self.last_sample_at,
            "sample_count": self.sample_count,
        }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, sampler, auto_sampler
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
    sampler = SamplerService(pool)
    try:
        await sampler.ensure_schema()
    except Exception as exc:  # 采样表建不出来不影响计划对照
        logger.warning("初始化采样表失败：%s", exc)
    auto_sampler = AutoSampler(sampler)
    yield
    await auto_sampler.astop()
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


def _require_sampler() -> SamplerService:
    if sampler is None:
        raise HTTPException(status_code=503, detail="采样器未就绪")
    return sampler


def _sampler_http_error(exc: SamplerError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


class TargetIn(BaseModel):
    dbid: int
    userid: int
    # queryid 是 64 位有符号整数，JSON 数字在 JS 里会丢精度，因此优先接受
    # 字符串；直接调用 API 时传数字也兼容。
    queryid: str | int

    @field_validator("queryid")
    @classmethod
    def _normalize_queryid(cls, value):
        text = str(value)
        if not re.fullmatch(r"-?\d{1,20}", text):
            raise ValueError("queryid 必须是整数")
        return text

    @property
    def queryid_int(self) -> int:
        return int(self.queryid)


class AutoIn(BaseModel):
    running: bool
    interval_s: float = Field(default=DEFAULT_AUTO_INTERVAL_S, ge=MIN_AUTO_INTERVAL_S, le=3600)


class TrafficIn(BaseModel):
    times: int = Field(default=5, ge=1, le=50)


@app.get("/api/sampler")
async def sampler_status():
    service = _require_sampler()
    try:
        data = await service.status()
    except SamplerError as exc:
        raise _sampler_http_error(exc)
    data["auto"] = auto_sampler.status() if auto_sampler is not None else {"running": False}
    return data


@app.post("/api/sampler/target")
async def sampler_select_target(body: TargetIn):
    service = _require_sampler()
    try:
        return await service.select_target(body.dbid, body.userid, body.queryid_int)
    except SamplerError as exc:
        raise _sampler_http_error(exc)


@app.delete("/api/sampler/target")
async def sampler_clear_target():
    service = _require_sampler()
    try:
        await service.clear_target()
    except SamplerError as exc:
        raise _sampler_http_error(exc)
    return {"ok": True}


@app.post("/api/sampler/samples")
async def sampler_take_sample():
    service = _require_sampler()
    try:
        return await service.sample()
    except SamplerError as exc:
        raise _sampler_http_error(exc)


@app.post("/api/sampler/traffic")
async def sampler_traffic(body: TrafficIn | None = None):
    service = _require_sampler()
    try:
        return await service.run_traffic((body or TrafficIn()).times)
    except SamplerError as exc:
        raise _sampler_http_error(exc)


@app.post("/api/sampler/auto")
async def sampler_auto(body: AutoIn):
    if auto_sampler is None:
        raise HTTPException(status_code=503, detail="采样器未就绪")
    if body.running:
        try:
            await _require_sampler().sample()
        except SamplerError as exc:
            raise _sampler_http_error(exc)
        auto_sampler.start(body.interval_s)
    else:
        auto_sampler.stop()
    return auto_sampler.status()


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
