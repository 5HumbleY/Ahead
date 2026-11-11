import ast
import copy

TOOL = {
    "type": "function",
    "function": {
        "name": "sql_repair",
        "description": "Execute Python over retained SQL state: query(sql) returns {columns, rows} from a read-only SELECT; recall(id) reads retained query evidence; source(model) returns current {sql, sha256, path}; print(value) reports evidence; replace(model, old, new, expected_matches=1) stages an exact edit; write(model, sql) stages a definition; check(name, requirement, models, sql) stages a scalar-BOOLEAN assertion. Use variables, for loops, if statements and ordinary scalar/container calculations. Imports and external I/O are unavailable. Stage all changes justified by current evidence in the same code. On normal exit changes commit atomically and rebuild once; errors commit no code/check changes. Queries and results persist. Completion is automatic when all outputs and obligations pass.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}
PROMPT = """Resolve the concrete remaining failure in the retained SQL program.
Use sql_repair with ordinary Python code. Write the known observation-to-correction
computation now: query the necessary complete data, compare or aggregate its
returned values in Python, and stage the justified correction in the SAME code.
Independent diagnostic queries can also execute in that code. Do not return to
the model between queries when the rule for choosing the next step is already
known. Return query-only code only when the actual evidence is needed to choose
an implementation that cannot yet be expressed.

Capabilities (synchronous inside this code):
- query(sql): one read-only SELECT on actual task relations; returns a dict with
  columns and rows, plus observation metadata. Aggregate in SQL over full data.
- source(model): current complete SQL, path, SHA256 and editability. No rereading
  or regenerating unaffected files is needed.
- recall(id): complete retained query evidence, with current_database_revision
  explicitly distinguishing historical from current values. print(value) reports
  a needed observation, including a selected source substring, to the next turn.
- replace(model, old, new, expected_matches=1): exact literal text replacement;
  the runtime captures and verifies the current source version. Copy old exactly.
- write(model, sql): a coherent definition when exact edits do not suffice.
- check(name, requirement, models, sql): revise/add a model-authored assertion
  for the exact quoted public requirement. sql must return one BOOLEAN, TRUE
  when the requirement holds. A successful row count alone is not a predicate.

All replace/write/check operations are staged. After the whole code succeeds,
runtime validates and commits one update, rebuilds affected dependencies once,
and checks every retained public obligation. An exception commits no changes.
No apply, build, finish, filesystem, import or event-loop call is needed. Use
plain lists/dicts/scalars, comprehensions, if/for and safe builtin functions.
String/container methods such as replace, split, get, items and append work.
Python locals are temporary; source, code, query observations and results persist.

Preserve actual source populations, identity/grain, units, NULL rules and all
original formulas. Read current interfaces before using a source column. An
error at a consumer may originate upstream; correct the responsible producer,
retaining unaffected code and results. Choose parameters from actual data and
the original task conditions; do not embed observed result totals as answers.
Correct a faulty model-authored check together with its code. Such a check is
fallible; it cannot weaken or replace runtime-compiled public requirements.
Never read graders, reference answers, other runs or model endpoints.
"""
METHODS = {
    "get",
    "items",
    "keys",
    "values",
    "copy",
    "append",
    "extend",
    "count",
    "replace",
    "split",
    "strip",
    "lower",
    "upper",
    "startswith",
    "endswith",
    "join",
}
BUILTINS = {
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "sorted": sorted,
    "enumerate": enumerate,
    "zip": zip,
    "range": range,
    "list": list,
    "tuple": tuple,
    "dict": dict,
    "set": set,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "any": any,
    "all": all,
}
CAPABILITIES = {"query", "source", "replace", "write", "check", "recall", "print"}
NODES = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.For,
    ast.If,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Assert,
    ast.Name,
    ast.Constant,
    ast.Call,
    ast.keyword,
    ast.Attribute,
    ast.Load,
    ast.Store,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Lambda,
    ast.arguments,
    ast.arg,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
)


def validate(code):
    tree = ast.parse(code, mode="exec")
    reserved = CAPABILITIES | set(BUILTINS)
    for node in ast.walk(tree):
        if not isinstance(node, NODES):
            raise ValueError(
                "Repair code uses scalar/control-flow Python and the supplied capabilities; unsupported: "
                + type(node).__name__
            )
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or (
                isinstance(node.ctx, ast.Store) and node.id in reserved
            ):
                raise ValueError(
                    "Do not access private names or overwrite a repair capability"
                )
        if isinstance(node, ast.arg) and (
            node.arg.startswith("_") or node.arg in reserved
        ):
            raise ValueError("Do not shadow a repair capability")
        if isinstance(node, ast.Attribute) and (
            node.attr not in METHODS or not isinstance(node.ctx, ast.Load)
        ):
            raise ValueError("Only documented scalar/container methods are available")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in reserved:
                    raise ValueError(
                        "Call only a supplied repair capability or safe builtin: "
                        + node.func.id
                    )
            elif not isinstance(node.func, ast.Attribute):
                raise ValueError("Dynamic callable access is unavailable")
    return compile(tree, "<sql-repair>", "exec")


def evaluate(code, sources, query, recall=None, on_source=None, emit=None):
    compiled = validate(code)
    update = {"files": [], "edits": [], "checks": []}

    def source(name):
        if name not in sources:
            raise ValueError("Use a model from the current source index: " + str(name))
        if on_source:
            on_source(name)
        return copy.deepcopy(sources[name])

    def replace(name, old, new, expected_matches=1):
        item = source(name)
        update["edits"].append(
            {
                "model": name,
                "sha256": item["sha256"],
                "old": old,
                "new": new,
                "expected_matches": expected_matches,
            }
        )

    def write(name, sql):
        path = sources[name]["path"] if name in sources else name
        update["files"].append({"path": path, "sql": sql})

    def check(name, requirement, models, sql):
        update["checks"].append(
            {
                "name": name,
                "requirement": requirement,
                "models": models,
                "sql": sql,
                "result_kind": "boolean",
            }
        )

    def report(*values, sep=" ", end="\n"):
        if emit:
            emit(sep.join((str(v) for v in values)) + end)

    def read_retained(ident):
        if recall is None:
            raise ValueError("No retained observation with this id")
        return recall(ident)

    namespace = {
        "__builtins__": {},
        **BUILTINS,
        "query": query,
        "source": source,
        "replace": replace,
        "write": write,
        "check": check,
        "recall": read_retained,
        "print": report,
    }
    exec(compiled, namespace, namespace)
    return update
