import hashlib
from jinja2 import Environment, StrictUndefined


def render_known(source, dialect="duckdb", grammar=False):
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    env.globals.clear()

    def call(name, args, kwargs):
        if grammar:
            if name == "config":
                return ""
            return (
                '"'
                + ("__" + name + "_" + "_".join((str(a) for a in args))).replace(
                    '"', '""'
                )
                + '"'
            )
        values = [repr(a) for a in args] + [
            str(k) + "=" + repr(v) for (k, v) in kwargs.items()
        ]
        return "{{ " + name + "(" + ", ".join(values) + ") }}"

    return env.from_string(source).render(
        target={"type": dialect},
        ref=lambda *a, **k: call("ref", a, k),
        source=lambda *a, **k: call("source", a, k),
        config=lambda *a, **k: call("config", a, k),
    )


def bind_known(source, dialect="duckdb"):
    if "{%" not in source:
        return source
    try:
        return render_known(source, dialect)
    except Exception:
        return source


def syntax_observation(source, dialect="duckdb"):
    if dialect != "duckdb":
        return {"checked": False, "reason": "Unsupported grammar engine"}
    try:
        rendered = render_known(source, dialect, grammar=True)
    except Exception as exc:
        return {
            "checked": False,
            "reason": "Requires actual dbt macro context: " + str(exc),
        }
    import duckdb

    with duckdb.connect(":memory:") as con:
        try:
            con.extract_statements(rendered)
        except duckdb.ParserException as exc:
            return {
                "checked": True,
                "valid": False,
                "engine": "duckdb " + duckdb.__version__,
                "error_type": "ParserException",
                "error": str(exc),
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "active_sql": rendered,
                "scope": "Grammar only; references are identifier placeholders. No data queried.",
            }
    return {"checked": True, "valid": True, "engine": "duckdb " + duckdb.__version__}
