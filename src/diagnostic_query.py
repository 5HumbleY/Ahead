import json
import threading


def diagnostic_query(backend, sql, limit=20, seconds=10):
    import duckdb

    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Supply one read-only diagnostic SELECT")
    rendered = backend.render(sql)
    statements = duckdb.extract_statements(rendered)
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ValueError(
            "Diagnostics accept one SELECT only; original data and files are read-only"
        )
    with duckdb.connect(
        backend.database, read_only=True, config={"enable_external_access": False}
    ) as con:
        timer = threading.Timer(seconds, con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            cursor = con.execute(rendered)
            columns = [x[0] for x in cursor.description]
            rows = cursor.fetchmany(limit + 1)
            result = {
                "sql": sql,
                "columns": columns,
                "rows": rows[:limit],
                "truncated": len(rows) > limit,
            }
            while (
                result["rows"]
                and len(json.dumps(result, default=str, ensure_ascii=False)) > 4000
            ):
                result["rows"].pop()
                result["truncated"] = True
            return result
        finally:
            timer.cancel()
