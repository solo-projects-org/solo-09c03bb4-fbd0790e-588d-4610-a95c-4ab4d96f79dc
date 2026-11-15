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

## 目录结构

```
├── docker-compose.yml
├── db/init.sql            # 结算样表 + 倾斜数据
└── app/
    ├── Dockerfile
    ├── main.py            # FastAPI 入口
    ├── runner.py          # 批次执行器（单事务 4 次执行、限时、取消、名额管理）
    ├── plans.py           # 计划结构归一化 + 结论判定
    ├── sqlcheck.py        # 只读 SELECT 校验
    ├── store.py           # 内存批次仓库
    └── static/            # 网页
```

> 说明：该工具面向受信任的内网工程师，输入的 SQL 会被直接执行（只读事务保护），
> 请勿暴露到公网。批次数据保存在内存中，重启即清空。
