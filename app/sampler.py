"""pg_stat_statements 采样器。

跟踪对象是「当前数据库里一条涉及 settlement_orders 的只读 SELECT」，
用 (dbid, userid, queryid) 三元组标识。每次采样读取它的累计 ``calls`` /
``total_exec_time`` 以及全局统计起始时间 ``stats_reset``，与上一条样本
比较后落库。

关键语义：

- **串行化**：每次采样（包括选定目标时写入的基线样本）都在同一个事务里
  先取 ``pg_advisory_xact_lock``，再读统计、算增量、写样本。并发采样请求
  会排队，不会两次使用同一个前置样本，也不会写出时间倒序的区间。
- **增量条件**：只有数据库、用户、queryid、统计起始时间都一致，且累计
  调用数与累计耗时都没有回退时，才计算 ``delta_calls`` / ``avg_exec_ms``。
- **断档**：统计重置、条目消失后重现、累计计数回退时写入带 ``gap_reason``
  的样本，增量置空，绝不跨段相减。
- **失败不推进起点**：采集本身报错时整个事务回滚，不写样本；上一次有效
  样本仍然是下一次增量计算的起点。
- **持久化**：跟踪目标与样本都存在 PostgreSQL 中，服务重启后可继续采样。
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

import asyncpg

logger = logging.getLogger(__name__)

# 事务级 advisory lock：同一数据库内所有采样器共用一把锁。
LOCK_NAMESPACE = 20261115
LOCK_KEY = 1

MAX_CANDIDATES = 50
MAX_SAMPLES = 100          # 网页展示的最近样本数
RETAIN_SAMPLES = 5000      # 每个跟踪目标在库里保留的样本上限，防止长期采样无限增长
MAX_TRAFFIC_TIMES = 50

# 累计耗时是 float8，比较回退时留一点浮点余量。
EPS_TIME_MS = 1e-6

TRAFFIC_MARKER = "settlement-sampler-traffic"

# 网页上「运行结算查询」按钮执行的固定 SELECT。语句文本固定、涉及
# settlement_orders，可反复执行制造可采样流量。末尾的注释只用于在
# pg_stat_statements 里认出这条语句，不影响 queryid 与执行计划。
TRAFFIC_SQL = f"""SELECT count(*)                  AS order_cnt,
       coalesce(sum(o.amount), 0) AS total_amount,
       coalesce(sum(o.fee), 0)    AS total_fee
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE m.merchant_id <= 20
  AND o.status = 'SETTLED'
/* {TRAFFIC_MARKER} */"""

# 「涉及 settlement_orders 的只读 SELECT」候选条件。EXPLAIN 包装的计划
# 对照语句不以 SELECT 开头，天然被排除；采样器自己的查询也被排除。
_CANDIDATE_WHERE = r"""
        s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    AND s.toplevel
    AND s.queryid IS NOT NULL
    AND s.queryid <> 0
    AND s.query ~* '^\s*(select|with)\M'
    AND s.query ~* '\msettlement_orders\M'
    AND s.query !~* 'pg_stat_statements'
    AND s.query !~* '\msampler_target\M'
    AND s.query !~* '\mss_samples\M'
"""

CANDIDATES_SQL = f"""
SELECT s.dbid,
       s.userid,
       pg_get_userbyid(s.userid) AS username,
       s.queryid,
       s.calls,
       s.total_exec_time,
       s.mean_exec_time,
       s.query
FROM pg_stat_statements s
WHERE {_CANDIDATE_WHERE}
ORDER BY s.total_exec_time DESC, s.calls DESC
LIMIT {MAX_CANDIDATES}
"""

# 按 (dbid, userid, queryid) 定点读取跟踪对象当前累计值。
TARGET_STATS_SQL = """
SELECT s.dbid,
       s.userid,
       s.queryid,
       s.calls,
       s.total_exec_time,
       s.query,
       i.stats_reset
FROM pg_stat_statements s
CROSS JOIN pg_stat_statements_info i
WHERE s.dbid = $1
  AND s.userid = $2
  AND s.queryid = $3
  AND s.toplevel
LIMIT 1
"""

# 制造流量后按注释标记定点定位该语句；比「执行前后 top-N 差分」可靠，
# 语句刚出现、累计耗时还排不进前列时也能找到。
TRAFFIC_STATS_SQL = """
SELECT s.dbid,
       s.userid,
       pg_get_userbyid(s.userid) AS username,
       s.queryid,
       s.calls,
       s.query
FROM pg_stat_statements s
WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND s.toplevel
  AND s.query LIKE $1
ORDER BY s.calls DESC
LIMIT 1
"""

# 选定目标时额外校验「确实是一条涉及 settlement_orders 的 SELECT」。
VALIDATE_TARGET_SQL = f"""
SELECT s.dbid, s.userid, s.queryid, s.calls, s.total_exec_time, s.query
FROM pg_stat_statements s
WHERE s.dbid = $1
  AND s.userid = $2
  AND s.queryid = $3
  AND s.toplevel
  AND s.query ~* '^\\s*(select|with)\\M'
  AND s.query ~* '\\msettlement_orders\\M'
LIMIT 1
"""

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sampler_target (
        id          smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        dbid        bigint      NOT NULL,
        userid      bigint      NOT NULL,
        queryid     bigint      NOT NULL,
        query_text  text        NOT NULL,
        selected_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ss_samples (
        id                  bigserial   PRIMARY KEY,
        dbid                bigint      NOT NULL,
        userid              bigint      NOT NULL,
        queryid             bigint      NOT NULL,
        sampled_at          timestamptz NOT NULL,
        stats_reset         timestamptz,
        total_calls         bigint,
        total_exec_time_ms  double precision,
        delta_calls         bigint,
        delta_exec_time_ms  double precision,
        avg_exec_ms         double precision,
        gap_reason          text,
        is_baseline         boolean     NOT NULL DEFAULT false,
        state               text        NOT NULL DEFAULT 'sample',
        prev_sample_id      bigint
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ss_samples_target_id
        ON ss_samples (dbid, userid, queryid, id)
    """,
)

GAP_LABELS = {
    "stats_reset": "统计已重置（stats_reset 变化），断档，不跨段相减",
    "target_reappeared": "条目消失后重现，断档，不跨段相减",
    "counter_regressed": "累计计数回退，断档，不跨段相减",
    "missing": "条目已从当前统计中消失，等待重现",
}


class SamplerError(Exception):
    """采样器可预期错误，带 HTTP 状态码。"""

    status_code = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SamplerUnavailable(SamplerError):
    status_code = 503


class TargetNotSelected(SamplerError):
    status_code = 409


class TargetNotFound(SamplerError):
    status_code = 404


def _iso(value):
    return value.isoformat() if value is not None else None


class SamplerService:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ------------------------------------------------------------------ 基础设施

    async def ensure_schema(self) -> None:
        """建表并尽力安装扩展（未预加载时给出可读错误，由 API 呈现）。"""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
            except asyncpg.PostgresError as exc:
                # 没有预加载时 CREATE EXTENSION 也会失败，保留原因供状态接口展示
                logger.warning("安装 pg_stat_statements 失败：%s", exc)
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)

    async def _lock(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1::int, $2::int)",
            LOCK_NAMESPACE,
            LOCK_KEY,
        )

    async def _stats_reset(self, conn: asyncpg.Connection):
        return await conn.fetchval("SELECT stats_reset FROM pg_stat_statements_info")

    async def _check_stats(self, conn: asyncpg.Connection) -> tuple[bool, str | None]:
        """扩展是否真的可用（已安装且通过 shared_preload_libraries 加载）。"""
        try:
            await self._stats_reset(conn)
            return True, None
        except asyncpg.PostgresError as exc:
            return False, str(exc).strip()

    # ------------------------------------------------------------------ 查询

    async def _get_target(self, conn: asyncpg.Connection):
        return await conn.fetchrow("SELECT * FROM sampler_target WHERE id = 1")

    async def get_target(self) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await self._get_target(conn)
        return self._target_dict(row) if row is not None else None

    async def _list_candidates(self, conn: asyncpg.Connection) -> list[dict]:
        rows = await conn.fetch(CANDIDATES_SQL)
        return [
            {
                "dbid": row["dbid"],
                "userid": row["userid"],
                "username": row["username"],
                "queryid": str(row["queryid"]),
                "calls": row["calls"],
                "total_exec_time_ms": row["total_exec_time"],
                "mean_exec_ms": row["mean_exec_time"],
                "query": row["query"],
            }
            for row in rows
        ]

    async def list_candidates(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            available, reason = await self._check_stats(conn)
            if not available:
                raise SamplerUnavailable(f"pg_stat_statements 不可用：{reason}")
            return await self._list_candidates(conn)

    async def _list_samples(self, conn: asyncpg.Connection, target) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT * FROM (
                SELECT * FROM ss_samples
                WHERE dbid = $1 AND userid = $2 AND queryid = $3
                ORDER BY id DESC
                LIMIT $4
            ) recent
            ORDER BY id ASC
            """,
            target["dbid"],
            target["userid"],
            target["queryid"],
            MAX_SAMPLES,
        )
        return [self._sample_dict(row) for row in rows]

    async def _server_info(self, conn: asyncpg.Connection) -> dict:
        row = await conn.fetchrow(
            """
            SELECT current_database() AS database,
                   (SELECT oid::bigint FROM pg_database
                     WHERE datname = current_database()) AS dbid,
                   current_user AS username,
                   (SELECT oid::bigint FROM pg_roles
                     WHERE rolname = current_user) AS userid
            """
        )
        return dict(row)

    async def status(self) -> dict:
        async with self.pool.acquire() as conn:
            available, reason = await self._check_stats(conn)
            stats_reset = None
            if available:
                stats_reset = await self._stats_reset(conn)
            target = await self._get_target(conn)
            samples = await self._list_samples(conn, target) if target is not None else []
            candidates = await self._list_candidates(conn) if available else []
            server = await self._server_info(conn)
        return {
            "available": available,
            "reason": reason,
            "stats_reset": _iso(stats_reset),
            "server": server,
            "target": self._target_dict(target) if target is not None else None,
            "samples": samples,
            "candidates": candidates,
            "traffic_sql": TRAFFIC_SQL,
            "max_samples": MAX_SAMPLES,
        }

    # ------------------------------------------------------------------ 目标

    async def select_target(self, dbid: int, userid: int, queryid: int) -> dict:
        """选定跟踪目标并立刻写一条基线/续接样本。"""
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await self._lock(conn)
                    row = await conn.fetchrow(
                        VALIDATE_TARGET_SQL, dbid, userid, queryid
                    )
                    if row is None:
                        raise TargetNotFound(
                            "未在 pg_stat_statements 中找到该条涉及 settlement_orders 的 SELECT，"
                            "请先执行一次对应查询"
                        )
                    await conn.execute(
                        """
                        INSERT INTO sampler_target
                            (id, dbid, userid, queryid, query_text, selected_at)
                        VALUES (1, $1, $2, $3, $4, clock_timestamp())
                        ON CONFLICT (id) DO UPDATE
                        SET dbid = EXCLUDED.dbid,
                            userid = EXCLUDED.userid,
                            queryid = EXCLUDED.queryid,
                            query_text = EXCLUDED.query_text,
                            selected_at = EXCLUDED.selected_at
                        """,
                        dbid,
                        userid,
                        queryid,
                        row["query"],
                    )
                    target = await self._get_target(conn)
                    result = await self._sample_locked(conn, target)
        except SamplerError:
            raise
        except asyncpg.PostgresError as exc:
            raise SamplerError(f"选定采样目标失败：{exc}") from exc
        return {
            "target": self._target_dict(target),
            "sample": result["sample"],
            "baseline": result["sample"]["is_baseline"],
        }

    async def clear_target(self) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._lock(conn)
                await conn.execute("DELETE FROM sampler_target WHERE id = 1")

    # ------------------------------------------------------------------ 采样

    async def sample(self) -> dict:
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await self._lock(conn)
                    target = await self._get_target(conn)
                    if target is None:
                        raise TargetNotSelected(
                            "尚未选定采样目标，请先执行结算查询并在候选列表中选择一条"
                        )
                    result = await self._sample_locked(conn, target)
        except SamplerError:
            raise
        except asyncpg.PostgresError as exc:
            # 事务已回滚：不写样本，上一次有效样本仍是下一次增量的起点
            raise SamplerUnavailable(f"采样失败（未推进起点）：{exc}") from exc
        result["target"] = self._target_dict(target)
        return result

    async def _sample_locked(self, conn: asyncpg.Connection, target) -> dict:
        """在 advisory lock 保护下读取统计并写样本。"""
        dbid = target["dbid"]
        userid = target["userid"]
        queryid = target["queryid"]

        # 必须在拿到锁之后取时间：排队等待的前一个采样已经写过更早的时间，
        # 用事务开始时间（now()）会造成区间颠倒。
        sampled_at = await conn.fetchval("SELECT clock_timestamp()")
        prev_any = await conn.fetchrow(
            """
            SELECT * FROM ss_samples
            WHERE dbid = $1 AND userid = $2 AND queryid = $3
            ORDER BY id DESC LIMIT 1
            """,
            dbid,
            userid,
            queryid,
        )
        # 兜底：即使系统时钟回拨，也不写出时间倒序的样本。
        if prev_any is not None and sampled_at <= prev_any["sampled_at"]:
            sampled_at = prev_any["sampled_at"] + timedelta(microseconds=1)

        stats_reset = await self._stats_reset(conn)
        current = await conn.fetchrow(TARGET_STATS_SQL, dbid, userid, queryid)

        if current is None:
            # 条目消失：记一条没有累计值的样本；已连续消失则不重复写。
            if prev_any is not None and prev_any["state"] == "missing":
                return {"sample": self._sample_dict(prev_any), "unchanged": True}
            # 全局重置会让条目直接消失，此时优先标「统计重置」；
            # 目标被定点重置或统计淘汰则标「条目消失」。
            missing_gap = "missing"
            if (
                prev_any is not None
                and prev_any["stats_reset"] is not None
                and prev_any["stats_reset"] != stats_reset
            ):
                missing_gap = "stats_reset"
            row = await conn.fetchrow(
                """
                INSERT INTO ss_samples
                    (dbid, userid, queryid, sampled_at, stats_reset,
                     total_calls, total_exec_time_ms, delta_calls,
                     delta_exec_time_ms, avg_exec_ms, gap_reason,
                     is_baseline, state, prev_sample_id)
                VALUES ($1, $2, $3, $4, $5,
                        NULL, NULL, NULL,
                        NULL, NULL, $6,
                        false, 'missing', $7)
                RETURNING *
                """,
                dbid,
                userid,
                queryid,
                sampled_at,
                stats_reset,
                missing_gap,
                prev_any["id"] if prev_any is not None else None,
            )
            return {"sample": self._sample_dict(row), "unchanged": False}

        prev = await conn.fetchrow(
            """
            SELECT * FROM ss_samples
            WHERE dbid = $1 AND userid = $2 AND queryid = $3 AND state = 'sample'
            ORDER BY id DESC LIMIT 1
            """,
            dbid,
            userid,
            queryid,
        )

        gap_reason: str | None = None
        delta_calls: int | None = None
        delta_time: float | None = None
        avg_exec_ms: float | None = None
        is_baseline = prev is None

        if prev is not None:
            missing_since = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM ss_samples
                    WHERE dbid = $1 AND userid = $2 AND queryid = $3
                      AND state = 'missing' AND id > $4
                )
                """,
                dbid,
                userid,
                queryid,
                prev["id"],
            )
            if prev["stats_reset"] is None or prev["stats_reset"] != stats_reset:
                # 上一条样本没有统计起始时间（例如扩展刚重置）也算断档，
                # 不能拿一段未知的累计值相减。
                gap_reason = "stats_reset"
            elif missing_since:
                gap_reason = "target_reappeared"
            elif (
                current["calls"] < prev["total_calls"]
                or current["total_exec_time"]
                < prev["total_exec_time_ms"] - EPS_TIME_MS
            ):
                gap_reason = "counter_regressed"
            else:
                delta_calls = current["calls"] - prev["total_calls"]
                delta_time = (
                    current["total_exec_time"] - prev["total_exec_time_ms"]
                )
                if delta_calls > 0:
                    avg_exec_ms = delta_time / delta_calls

        row = await conn.fetchrow(
            """
            INSERT INTO ss_samples
                (dbid, userid, queryid, sampled_at, stats_reset,
                 total_calls, total_exec_time_ms, delta_calls,
                 delta_exec_time_ms, avg_exec_ms, gap_reason,
                 is_baseline, state, prev_sample_id)
            VALUES ($1, $2, $3, $4, $5,
                    $6, $7, $8,
                    $9, $10, $11,
                    $12, 'sample', $13)
            RETURNING *
            """,
            dbid,
            userid,
            queryid,
            sampled_at,
            stats_reset,
            current["calls"],
            current["total_exec_time"],
            delta_calls,
            delta_time,
            avg_exec_ms,
            gap_reason,
            is_baseline,
            prev["id"] if prev is not None else None,
        )
        await self._prune(conn, dbid, userid, queryid)
        return {"sample": self._sample_dict(row), "unchanged": False}

    async def _prune(self, conn: asyncpg.Connection, dbid: int, userid: int, queryid: int) -> None:
        """只保留每个目标最近的 RETAIN_SAMPLES 条样本。"""
        await conn.execute(
            """
            DELETE FROM ss_samples
            WHERE dbid = $1 AND userid = $2 AND queryid = $3
              AND id <= (
                  SELECT id FROM ss_samples
                  WHERE dbid = $1 AND userid = $2 AND queryid = $3
                  ORDER BY id DESC
                  OFFSET $4 LIMIT 1
              )
            """,
            dbid,
            userid,
            queryid,
            RETAIN_SAMPLES,
        )

    # ------------------------------------------------------------------ 制造流量

    async def run_traffic(self, times: int) -> dict:
        """反复执行网页固定结算 SELECT，供本机产生可采样流量。"""
        times = max(1, min(int(times), MAX_TRAFFIC_TIMES))
        async with self.pool.acquire() as conn:
            available, reason = await self._check_stats(conn)
            started = time.monotonic()
            for _ in range(times):
                await conn.fetchrow(TRAFFIC_SQL)
            elapsed_ms = (time.monotonic() - started) * 1000
            detected = None
            if available:
                detected = await conn.fetchrow(
                    TRAFFIC_STATS_SQL, f"%{TRAFFIC_MARKER}%"
                )

        target = await self.get_target()
        auto_pinned = False
        sample = None
        warning = None
        if not available:
            warning = f"pg_stat_statements 不可用：{reason}"
        elif detected is None:
            warning = "结算查询已执行，但未在统计中识别到该语句（可能被统计淘汰或未开启跟踪）"
        elif target is None:
            pinned = await self.select_target(
                detected["dbid"], detected["userid"], detected["queryid"]
            )
            target = pinned["target"]
            sample = pinned["sample"]
            auto_pinned = True
        elif target["queryid"] != str(detected["queryid"]):
            warning = "当前采样目标是另一条 SQL，本次流量不会计入该目标"

        return {
            "ok": True,
            "times": times,
            "elapsed_ms": round(elapsed_ms, 3),
            "queryid": str(detected["queryid"]) if detected is not None else None,
            "username": detected["username"] if detected is not None else None,
            "auto_pinned": auto_pinned,
            "target": target,
            "sample": sample,
            "warning": warning,
        }

    # ------------------------------------------------------------------ 序列化

    def _target_dict(self, row) -> dict:
        return {
            "dbid": row["dbid"],
            "userid": row["userid"],
            "queryid": str(row["queryid"]),
            "query": row["query_text"],
            "selected_at": _iso(row["selected_at"]),
        }

    def _sample_dict(self, row) -> dict:
        return {
            "id": row["id"],
            "sampled_at": _iso(row["sampled_at"]),
            "stats_reset": _iso(row["stats_reset"]),
            "total_calls": row["total_calls"],
            "total_exec_time_ms": row["total_exec_time_ms"],
            "delta_calls": row["delta_calls"],
            "delta_exec_time_ms": row["delta_exec_time_ms"],
            "avg_exec_ms": row["avg_exec_ms"],
            "gap_reason": row["gap_reason"],
            "gap_label": GAP_LABELS.get(row["gap_reason"]),
            "is_baseline": row["is_baseline"],
            "state": row["state"],
            "prev_sample_id": row["prev_sample_id"],
        }
