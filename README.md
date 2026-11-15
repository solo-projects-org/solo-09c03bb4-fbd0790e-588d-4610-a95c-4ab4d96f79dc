# 结算 SQL 计划对照台

迁库后夜间结算 SQL 变慢，用这个小工具在同一份数据上对比 **默认计划** 与
**抑制嵌套循环（`SET LOCAL enable_nestloop=off`）** 的实际执行效果。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000> 。首次启动时数据库会自动建结算样表并灌入
100 万行订单（约需半分钟），`web` 会等 `db` 健康检查通过后再起。

重新初始化样表数据：

```bash
docker compose down -v && docker compose up --build
```

## 它怎么对照

提交一条只读 SELECT 后，后台会开一条**专用连接**，在**同一个
`REPEATABLE READ, READ ONLY` 事务**里执行 4 次 `EXPLAIN (ANALYZE, FORMAT JSON)`：

| 顺序 | 轮次 | 设置 |
|----|----|------------------------|
| #1 | 1  | 默认（enable_nestloop=on） |
| #2 | 1  | 关闭嵌套循环（=off）       |
| #3 | 2  | 关闭嵌套循环（=off）       |
| #4 | 2  | 默认（=on）               |

第二轮颠倒顺序，用来抵消缓存预热造成的偏差。四次执行共享**同一事务快照**，
对照期间其他连接照常写入（页面上的「补记样表数据」按钮可随时插一行），
新数据不会进入本批结果。

## 结论判定规则

- 两轮快慢结论**相反** → 标为「无法判断改善」；
- 两种设置得到**相同计划结构**（节点类型/扫描关系/索引/连接策略一致，
  忽略代价与行数等数值）→ 标为「无法判断改善」；
- 两轮一致 → 给出「关闭嵌套循环更快 / 默认计划更快」的结论；
- **超时、取消或报错** → 本批不给比较结论。

## 运行约束

- 同时只运行一批；运行中再提交会收到 **409 繁忙** 提示，网页也会显示名额占用；
- 每次执行由服务端 `statement_timeout = 10s` 限时，另有 15 秒客户端兜底
  （到点直接断开连接，确保 SQL 实际停止）；
- 「取消」通过 `pg_cancel_backend` 让正在执行的 SQL 真正停下来；
- 运行名额在 **本批 SQL 实际停止 → 事务结束 → 连接关闭** 之后才释放，
  期间其他请求（查询批次、补记数据）照常响应；
- 输入校验：仅单条只读 SELECT（sqlparse 静态检查 + READ ONLY 事务双保险）。

## 样表与示例 SQL

- `merchants`：5000 个商户，类别 A 只有 100 个；
- `settlement_orders`：100 万行结算订单，**类别 A 的 100 个商户沉淀了 70 万单**
  （数据倾斜）。数据库关闭了并行查询（`max_parallel_workers_per_gather=0`，
  模拟迁库后实例未开并行的场景），此时优化器估算「每商户约 200 单」，
  按类别 A 关联时容易误选嵌套循环——实际要捞 70 万行。

页面提供三个预设样例，分别对应三种结论分支：

1. **类别 A 结算汇总**（默认）：`left(m.category, 1) = 'A'` 的表达式过滤让优化器
   只能用默认选择率（把 100 个商户估成 25 个），默认计划误选嵌套循环，
   通常可得到「关闭嵌套循环更快」的结论；
2. **单商户结算明细**：高选择性过滤，索引嵌套循环占绝对优势，
   通常得到「默认计划更快」；
3. **每日结算汇总**：无关联查询，两种设置计划结构相同 → 「无法判断改善」。

默认预置的示例 SQL：

```sql
SELECT m.merchant_id,
       m.merchant_name,
       COUNT(*)      AS order_cnt,
       SUM(o.amount) AS total_amount,
       SUM(o.fee)    AS total_fee
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE left(m.category, 1) = 'A'
GROUP BY m.merchant_id, m.merchant_name
ORDER BY total_amount DESC;
```

## API 一览

| 方法 | 路径 | 说明 |
|-----|------|------|
| POST | `/api/batches` | 提交对照批次（409=繁忙，400=SQL 校验失败） |
| GET  | `/api/batches` | 批次列表（不含完整计划 JSON） |
| GET  | `/api/batches/{id}` | 批次详情（含 4 次计划 JSON 与结构摘要） |
| POST | `/api/batches/{id}/cancel` | 取消运行中的批次 |
| POST | `/api/sample-row` | 向 `settlement_orders` 补记一行 |
| GET  | `/api/sampler` | 采样器状态、跟踪目标、候选 SQL、采样记录 |
| POST | `/api/sampler/target` | 选定跟踪目标（body: `dbid`/`userid`/`queryid`） |
| DELETE | `/api/sampler/target` | 停止跟踪（保留已有采样记录） |
| POST | `/api/sampler/samples` | 立即采样一次 |
| POST | `/api/sampler/traffic` | 执行网页固定结算 SELECT N 次以产生流量 |
| POST | `/api/sampler/auto` | 开启/关闭定时采样（`running`、`interval_s`） |

## pg_stat_statements 采样

夜班值班员需要从真实结算查询的持续耗时中挑出值得排查的 SQL。数据库已通过
`shared_preload_libraries` 预加载 `pg_stat_statements`（见 `docker-compose.yml`），
`db/init.sql` 里创建扩展。网页上「pg_stat_statements 采样」区域可以：

1. **选择跟踪目标**：从当前数据库中所有涉及 `settlement_orders` 的只读 SELECT
   （`toplevel`、有 queryid、以 SELECT/WITH 开头）里挑一条。计划对照使用的
   `EXPLAIN` 语句与采样器自身的查询被排除。
2. **制造流量**：点击「运行结算查询产生流量」会反复执行一条固定的结算
   SELECT（涉及 `settlement_orders` 与 `merchants`），并可自动把它设为跟踪目标。
3. **采样**：手动「立即采样」或开启定时采样。每次采样记录该 SQL 的
   **累计调用数**、**累计总执行耗时**、**统计起始时间**和**采样时间**，
   并按时间展示相邻有效样本之间的**新增调用数**与**平均耗时**
   （新增总耗时 ÷ 新增调用数）。

增量计算规则：

- 只有 **数据库、用户、queryid、统计起始时间（`pg_stat_statements_info.stats_reset`）
  全部一致**，且累计调用数与累计耗时都未回退时，才计算增量；
- **统计重置**（`stats_reset` 变化）、**条目消失后重现**、**累计计数回退**都会写入
  带 `gap_reason` 的**断档**样本，`delta_calls` / `avg_exec_ms` 为空，绝不跨段相减；
- 条目从统计中消失时记一条「条目消失」样本；连续消失不会重复刷屏；
- **采集失败不推进起点**：整个采样事务回滚，不写样本，下一次成功采样仍与上一条
  有效样本比较；
- **并发安全**：每次采样在同一个事务里取 `pg_advisory_xact_lock`，再读统计、
  算增量、写样本。并发请求排队执行，不会两次使用同一个前置样本，也不会写出
  时间倒序的区间（采样时间在拿到锁之后用 `clock_timestamp()` 取）；
- **持久化**：跟踪目标与样本存在 PostgreSQL 的 `sampler_target` / `ss_samples`
  表，服务重启后从上次有效样本接着采。

## 目录结构

```
├── docker-compose.yml
├── db/init.sql            # 结算样表 + 倾斜数据 + pg_stat_statements 扩展与采样表
└── app/
    ├── Dockerfile
    ├── main.py            # FastAPI 入口
    ├── runner.py          # 批次执行器（单事务 4 次执行、限时、取消、名额管理）
    ├── plans.py           # 计划结构归一化 + 结论判定
    ├── sqlcheck.py        # 只读 SELECT 校验
    ├── sampler.py         # pg_stat_statements 采样（增量、断档、并发串行化、持久化）
    ├── store.py           # 内存批次仓库
    └── static/            # 网页
```

> 说明：该工具面向受信任的内网工程师，输入的 SQL 会被直接执行（只读事务保护），
> 请勿暴露到公网。对照批次数据保存在内存中，重启即清空；采样目标与采样记录
> 保存在 PostgreSQL 中。
