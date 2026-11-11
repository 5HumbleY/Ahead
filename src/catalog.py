from collections import Counter
from pathlib import Path
import sqlite3


class PreparedMetadata(dict):
    __construction_evidence__ = True


class ExecutionOnly:
    def __init__(self, contract):
        self.contract = contract

    def __repr__(self):
        return "ExecutionOnly(" + repr(self.contract) + ")"


def quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def inspect_database(
    path, schema=None, table=None, limit=40, start=0, sample=0, match=None
):
    if not 1 <= limit <= 500 or not 0 <= sample <= 100 or start < 0:
        raise ValueError("Invalid catalog page bounds")
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    con = None
    engine = None
    try:
        if path.suffix.lower() != ".duckdb":
            try:
                con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                engine = "sqlite"
            except sqlite3.DatabaseError:
                if con is not None:
                    con.close()
                con = None
        if con is None:
            import duckdb

            con = duckdb.connect(str(path), read_only=True)
            engine = "duckdb"
        result = {"path": str(path), "engine": engine}
        if table is None:
            if engine == "sqlite":
                rows = [
                    ("main", r[0], r[1])
                    for r in con.execute(
                        "SELECT name,type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ]
            else:
                rows = con.execute(
                    "SELECT table_schema,table_name,table_type FROM information_schema.tables ORDER BY table_schema,table_name"
                ).fetchall()
            if schema is not None:
                rows = [r for r in rows if r[0].casefold() == schema.casefold()]
            if match:
                term = str(match).casefold()
                selected = []
                for row in rows:
                    if term in (row[0] + "." + row[1]).casefold():
                        selected.append(row)
                        continue
                    if engine == "sqlite":
                        names = [
                            r[1]
                            for r in con.execute(
                                "PRAGMA "
                                + quote(row[0])
                                + ".table_info("
                                + quote(row[1])
                                + ")"
                            ).fetchall()
                        ]
                    else:
                        names = [
                            r[0]
                            for r in con.execute(
                                "SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=?",
                                [row[0], row[1]],
                            ).fetchall()
                        ]
                    if any((term in name.casefold() for name in names)):
                        selected.append(row)
                rows = selected
            result.update(
                total=len(rows),
                schemas=dict(Counter((r[0] for r in rows))),
                relations=[
                    {"schema": r[0], "name": r[1], "kind": r[2]}
                    for r in rows[start : start + limit]
                ],
                next_start=start + limit if start + limit < len(rows) else None,
            )
        else:
            schema = schema or "main"
            relation = quote(schema) + "." + quote(table)
            if engine == "sqlite":
                cols = con.execute(
                    "PRAGMA " + quote(schema) + ".table_info(" + quote(table) + ")"
                ).fetchall()
                columns = [
                    {"name": r[1], "type": r[2], "nullable": not bool(r[3])}
                    for r in cols
                ]
            else:
                columns = [
                    {"name": r[0], "type": r[1], "nullable": r[2] == "YES"}
                    for r in con.execute(
                        "SELECT column_name,data_type,is_nullable FROM information_schema.columns WHERE lower(table_schema)=lower(?) AND lower(table_name)=lower(?) ORDER BY ordinal_position",
                        [schema, table],
                    ).fetchall()
                ]
            if not columns:
                raise ValueError("No observed table/view: " + relation)
            result.update(
                schema=schema,
                table=table,
                columns=columns[start : start + limit],
                column_count=len(columns),
                next_start=start + limit if start + limit < len(columns) else None,
            )
            if sample:
                result["sample_rows"] = con.execute(
                    "SELECT * FROM " + relation + " LIMIT " + str(sample)
                ).fetchall()
        return result
    finally:
        if con is not None:
            con.close()
