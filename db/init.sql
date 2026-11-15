-- 结算样表：商户表 + 结算订单表
-- 数据刻意做成倾斜分布：类别 A 的 100 个商户沉淀了 70 万单（占 70%），
-- 其余 4900 个商户共分 60 万单。配合页面示例 SQL 里的表达式过滤
-- （优化器只能用默认选择率，把 100 个商户估成 25 个），默认计划会误选
-- 嵌套循环——这正是迁库后夜间结算变慢的常见诱因，方便演示对照效果。

CREATE TABLE merchants (
    merchant_id   integer PRIMARY KEY,
    merchant_name text        NOT NULL,
    category      text        NOT NULL,
    city          text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO merchants (merchant_id, merchant_name, category, city)
SELECT g,
       '商户-' || lpad(g::text, 5, '0'),
       CASE WHEN g <= 100 THEN 'A' ELSE (ARRAY['B','C','D','E'])[1 + (g % 4)] END,
       (ARRAY['杭州','上海','北京','深圳','广州','成都','南京','武汉','苏州','西安'])[1 + (g % 10)]
FROM generate_series(1, 5000) AS g;

CREATE TABLE settlement_orders (
    order_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_no    text          NOT NULL,
    merchant_id integer       NOT NULL REFERENCES merchants (merchant_id),
    amount      numeric(12,2) NOT NULL,
    fee         numeric(12,2) NOT NULL,
    status      text          NOT NULL DEFAULT 'SETTLED',
    settle_date date          NOT NULL,
    created_at  timestamptz   NOT NULL DEFAULT now()
);

-- 类别 A 商户（1..100）的 70 万单
INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
SELECT 'SO' || lpad(g::text, 10, '0'),
       1 + (random() * 99)::int,
       a.amount,
       round(a.amount * 0.006, 2),
       'SETTLED',
       DATE '2026-08-01' + (random() * 104)::int
FROM generate_series(1, 700000) AS g
CROSS JOIN LATERAL (SELECT round((1 + random() * 499)::numeric, 2) AS amount) AS a;

-- 其余商户（101..5000）的 30 万单
INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
SELECT 'SO' || lpad((g + 700000)::text, 10, '0'),
       101 + (random() * 4899)::int,
       a.amount,
       round(a.amount * 0.006, 2),
       'SETTLED',
       DATE '2026-08-01' + (random() * 104)::int
FROM generate_series(1, 300000) AS g
CROSS JOIN LATERAL (SELECT round((1 + random() * 499)::numeric, 2) AS amount) AS a;

CREATE INDEX idx_orders_merchant   ON settlement_orders (merchant_id);
CREATE INDEX idx_orders_settle_date ON settlement_orders (settle_date);

-- 采样功能依赖 pg_stat_statements 的累计调用数与总执行耗时。
-- 服务端已通过 shared_preload_libraries 预加载，这里只需安装扩展。
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

ANALYZE merchants;
ANALYZE settlement_orders;

-- 采样表由 web 服务启动时用 CREATE TABLE IF NOT EXISTS 建立（见 app/sampler.py），
-- 这样在不重新初始化数据卷的情况下也能补建。这里同样预建一份，便于直接用 psql 查看。
CREATE TABLE IF NOT EXISTS sampler_target (
    id          smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    dbid        bigint      NOT NULL,
    userid      bigint      NOT NULL,
    queryid     bigint      NOT NULL,
    query_text  text        NOT NULL,
    selected_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

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
);

CREATE INDEX IF NOT EXISTS idx_ss_samples_target_id
    ON ss_samples (dbid, userid, queryid, id);
