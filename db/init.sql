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

ANALYZE merchants;
ANALYZE settlement_orders;
