"""输入 SQL 校验：只接受单条只读 SELECT。

双保险：
1. 这里用 sqlparse 做静态检查（单条语句、SELECT/WITH/VALUES 开头、无写操作关键字）；
2. 执行时在 READ ONLY 事务里跑，任何漏网的写操作都会被 PostgreSQL 直接拒绝。
"""
from __future__ import annotations

import sqlparse
from sqlparse.tokens import DDL, DML, Comment, Keyword, Newline, Whitespace

ALLOWED_START = {"SELECT", "WITH", "VALUES", "TABLE"}

FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER", "CREATE",
    "GRANT", "REVOKE", "COMMENT", "CALL", "COPY", "LOCK", "VACUUM", "ANALYZE",
    "CLUSTER", "REINDEX", "REFRESH", "SET", "RESET", "SHOW", "LISTEN", "NOTIFY",
    "UNLISTEN", "DISCARD", "LOAD", "PREPARE", "EXECUTE", "DEALLOCATE", "DO",
    "EXPLAIN", "SECURITY", "FOR", "IMPORT", "CHECKPOINT", "SAVEPOINT", "RELEASE",
    "START", "BEGIN", "COMMIT", "ROLLBACK", "ABORT", "END",
}


def validate_select_sql(sql: str) -> str:
    """校验通过则返回去掉尾部分号的 SQL，否则抛 ValueError。"""
    s = sql.strip()
    while s.endswith(";"):
        s = s[:-1].strip()
    if not s:
        raise ValueError("SQL 不能为空")

    statements = [st for st in sqlparse.parse(s) if str(st).strip()]
    if len(statements) != 1:
        raise ValueError("只允许单条 SELECT 语句")
    stmt = statements[0]

    tokens = list(stmt.flatten())

    first = None
    for tok in tokens:
        if tok.ttype in (Whitespace, Newline) or tok.ttype in Comment:
            continue
        first = tok
        break
    if first is None:
        raise ValueError("SQL 不能为空")

    head = first.value.upper()
    if not (head in ALLOWED_START or first.value.startswith("(")):
        raise ValueError("只允许 SELECT 查询（含 WITH/VALUES）")

    for tok in tokens:
        if tok.ttype in (Keyword, DML, DDL):
            word = tok.value.upper()
            if word in FORBIDDEN_KEYWORDS or word.startswith("FOR "):
                raise ValueError(f"只允许只读 SELECT，禁止使用关键字：{word}")
    return s
