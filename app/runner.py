"""对照批次执行器。

关键语义：
- 同时只运行一批（busy 名额）；满额时新提交直接提示繁忙；
- 四次 EXPLAIN (ANALYZE, FORMAT JSON) 在同一个「可重复读 + 只读」事务里执行，
  共享同一快照，期间其他连接照常写入也不影响本批；
- 每次执行由服务端 statement_timeout=10s 限时，另有客户端兜底超时；
- 取消通过 pg_cancel_backend 让正在执行的 SQL 真正停下来；
- 名额在本批 SQL 实际停止、事务结束、连接关闭之后才释放。
"""
from __future__ import annotations

import asyncio
import json
import time

import asyncpg

from plans import build_conclusion, plan_structure
from store import Batch, BatchStore, Execution

STATEMENT_TIMEOUT_MS = 10_000   # 每次执行限时 10 秒（服务端）
CLIENT_BACKSTOP_S = 15.0        # 客户端兜底超时，到点直接断开连接

# 第 1 轮：默认 -> 关闭 nestloop；第 2 轮颠倒顺序，抵消缓存预热影响
ROUNDS = (
    (1, (("default", "on"), ("nestloop_off", "off"))),
    (2, (("nestloop_off", "off"), ("default", "on"))),
)


class _Cancelled(Exception):
    pass


class _Timeout(Exception):
    pass


class BatchRunner:
    def __init__(self, dsn: str, store: BatchStore):
        self._dsn = dsn
        self._store = store
        self.busy = False
        self.current_batch_id: str | None = None
        self.backend_pid: int | None = None
        self._task: asyncio.Task | None = None

    def try_start(self, batch: Batch) -> bool:
        """占用名额并启动后台任务；已在运行则返回 False（繁忙）。"""
        if self.busy:
            return False
        self.busy = True
        self.current_batch_id = batch.id
        self.backend_pid = None
        self._task = asyncio.create_task(self._run(batch))
        return True

    def request_cancel(self, batch_id: str) -> tuple[bool, int | None]:
        """标记取消并返回后端 pid（供 pg_cancel_backend）；批次不在运行则 found=False。"""
        if self.current_batch_id != batch_id:
            return False, None
        batch = self._store.get(batch_id)
        if batch is not None:
            batch.cancel_requested = True
        return True, self.backend_pid

    async def _run(self, batch: Batch) -> None:
        conn: asyncpg.Connection | None = None
        try:
            batch.status = "running"
            batch.started_at = time.time()
            conn = await asyncpg.connect(self._dsn)
            self.backend_pid = await conn.fetchval("SELECT pg_backend_pid()")

            # 同一可重复读只读事务：四次执行共享同一快照
            await conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")

            seq = 0
            for round_no, steps in ROUNDS:
                for setting, guc in steps:
                    if batch.cancel_requested:
                        raise _Cancelled()
                    seq += 1
                    ex = Execution(round_no=round_no, setting=setting, seq=seq, status="running")
                    batch.executions.append(ex)
                    await conn.execute(f"SET LOCAL enable_nestloop = {guc}")
                    row = await self._execute_one(conn, batch, ex)
                    payload = json.loads(row[0])[0]
                    ex.plan = payload
                    ex.execution_time_ms = payload.get("Execution Time")
                    ex.planning_time_ms = payload.get("Planning Time")
                    ex.plan_structure = plan_structure(payload["Plan"])
                    ex.status = "ok"

            # 只读事务，回滚即结束事务且无任何副作用
            await conn.execute("ROLLBACK")
            batch.conclusion = build_conclusion(batch.executions)
            batch.status = "completed"
        except _Cancelled:
            batch.status = "cancelled"
        except _Timeout:
            batch.status = "timeout"
        except Exception as exc:  # 用户 SQL 报错、连接异常等
            batch.status = "failed"
            batch.error = f"{type(exc).__name__}: {exc}"
        finally:
            # 关键顺序：先等本批 SQL 停止、事务结束、连接关闭，再释放运行名额
            if conn is not None:
                try:
                    if conn.is_in_transaction():
                        await asyncio.wait_for(conn.execute("ROLLBACK"), timeout=5)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(conn.close(), timeout=5)
                except Exception:
                    try:
                        conn.terminate()
                    except Exception:
                        pass
            for ex in batch.executions:
                if ex.status in ("pending", "running"):
                    ex.status = "cancelled" if batch.status == "cancelled" else "error"
                    if ex.error is None:
                        ex.error = "未完成"
            batch.finished_at = time.time()
            self.busy = False
            self.current_batch_id = None
            self.backend_pid = None

    async def _execute_one(self, conn: asyncpg.Connection, batch: Batch, ex: Execution):
        sql_text = f"EXPLAIN (ANALYZE, FORMAT JSON) {batch.sql}"
        t0 = time.monotonic()
        task = asyncio.ensure_future(conn.fetchrow(sql_text))
        done, _ = await asyncio.wait({task}, timeout=CLIENT_BACKSTOP_S)
        if not done:
            # 服务端 10s 限时未生效时的兜底：断开连接，确保 SQL 实际停止
            conn.terminate()
            await asyncio.wait({task}, timeout=2)
            if task.done() and not task.cancelled():
                task.exception()  # 取走异常，避免事件循环告警
            ex.status = "timeout"
            ex.error = f"执行超过 {CLIENT_BACKSTOP_S:.0f} 秒（客户端兜底），连接已终止"
            ex.wall_time_ms = (time.monotonic() - t0) * 1000
            raise _Timeout()
        ex.wall_time_ms = (time.monotonic() - t0) * 1000
        try:
            return task.result()
        except asyncpg.QueryCanceledError as exc:
            msg = str(exc)
            if batch.cancel_requested:
                ex.status = "cancelled"
                ex.error = "已被用户取消"
                raise _Cancelled() from exc
            if "statement timeout" in msg:
                ex.status = "timeout"
                ex.error = f"执行超过 {STATEMENT_TIMEOUT_MS // 1000} 秒限时"
                raise _Timeout() from exc
            ex.status = "error"
            ex.error = msg
            raise
        except asyncpg.PostgresError as exc:
            ex.status = "error"
            ex.error = str(exc).strip()
            raise
