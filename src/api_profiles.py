import hashlib
import importlib
import importlib.metadata
import inspect
from pathlib import Path
from .catalog import PreparedMetadata


def inspect_api(target, match="", limit=24):
    if isinstance(target, str):
        parts = target.split(".")
        value = importlib.import_module(parts[0])
        for part in parts[1:]:
            value = getattr(value, part)
    else:
        value = target
    module = value if inspect.ismodule(value) else inspect.getmodule(value)
    module_name = getattr(module, "__name__", "")
    file = getattr(module, "__file__", None)
    digest = (
        hashlib.sha256(Path(file).read_bytes()).hexdigest()
        if file and Path(file).is_file()
        else None
    )
    try:
        version = importlib.metadata.version(module_name.split(".")[0])
    except importlib.metadata.PackageNotFoundError:
        version = None
    explicit = callable(value) and (not inspect.ismodule(value))

    def describe(name, item):
        try:
            signature = str(inspect.signature(item))
        except (TypeError, ValueError):
            signature = None
        doc = inspect.getdoc(item) or ""
        length = 6000 if explicit else 1800 if match else 500
        return {
            "name": name,
            "signature": signature,
            "documentation": doc[:length],
            "documentation_characters": len(doc),
            "documentation_complete": len(doc) <= length,
        }

    if explicit:
        members = [describe(getattr(value, "__name__", str(target)), value)]
        total = 1
    else:
        candidates = [
            (name, item)
            for (name, item) in inspect.getmembers_static(value)
            if not name.startswith("_")
            and callable(item)
            and (not match or match.lower() in name.lower())
        ]
        total = len(candidates)
        members = [
            describe(name, item)
            for (name, item) in candidates[: max(1, min(int(limit), 100))]
        ]
    return PreparedMetadata(
        {
            "module": module_name,
            "version": version,
            "source_file": file,
            "source_sha256": digest,
            "callable_count": total,
            "callables": members,
            "scope": "Actual installed callable interfaces. No calls, examples or task computation executed.",
        }
    )


def failure_interfaces(source, line, namespace, limit=3):
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    def resolve(node):
        if isinstance(node, ast.Name):
            return namespace.get(node.id)
        if isinstance(node, ast.Attribute):
            base = resolve(node.value)
            if base is not None:
                try:
                    return inspect.getattr_static(base, node.attr)
                except AttributeError:
                    return base if inspect.ismodule(base) else None
        return None

    result = []
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.lineno <= line <= node.end_lineno:
            continue
        value = resolve(node.func)
        if value is None or not (inspect.ismodule(value) or callable(value)):
            continue
        module = value if inspect.ismodule(value) else inspect.getmodule(value)
        if getattr(module, "__name__", "") in {"builtins", "__main__"}:
            continue
        name = ast.unparse(node.func)
        if name in seen:
            continue
        seen.add(name)
        try:
            profile = inspect_api(value, limit=12)
        except Exception:
            continue
        result.append({"expression": name, "actual_interface": dict(profile)})
        if len(result) >= limit:
            break
    return result
