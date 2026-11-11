from .native import describe
from .catalog import ExecutionOnly, PreparedMetadata


def excerpt(text, limit=6000):
    if len(text) <= limit:
        return {"text": text, "characters": len(text), "complete": True}
    head = limit * 2 // 3
    tail = limit - head
    return {
        "characters": len(text),
        "complete": False,
        "spans": [
            {"start": 0, "text": text[:head]},
            {"start": len(text) - tail, "text": text[-tail:]},
        ],
    }


def interface(value):
    if isinstance(value, ExecutionOnly):
        return {"kind": "execution_only", "contract": value.contract}
    if isinstance(value, PreparedMetadata):
        return {"kind": "construction_evidence", "value": dict(value)}
    info = describe(value) or {"kind": type(value).__name__}
    if hasattr(value, "dtypes") and hasattr(value, "columns"):
        return {
            "kind": type(value).__name__,
            "columns": [(str(k), str(v)) for (k, v) in value.dtypes.items()],
        }
    if isinstance(value, dict):
        return {
            "kind": "dict",
            "keys": {str(k): type(v).__name__ for (k, v) in value.items()},
        }
    return {k: v for (k, v) in info.items() if k in {"kind", "name", "column_count"}}
