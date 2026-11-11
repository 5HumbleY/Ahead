import hashlib
import json
from pathlib import Path
import re


def database_revision(database):
    result = []
    for path in [Path(database), Path(str(database) + ".wal")]:
        try:
            s = path.stat()
            result.append(
                (
                    str(path.resolve()),
                    s.st_dev,
                    s.st_ino,
                    s.st_size,
                    s.st_mtime_ns,
                    s.st_ctime_ns,
                )
            )
        except FileNotFoundError:
            result.append((str(path.resolve()), None))
    return tuple(result)


def dependencies(models, ident):
    found = set()

    def visit(name):
        if name in found:
            return
        found.add(name)
        for parent in models.get(name, {}).get("depends_on", {}).get("nodes", []):
            visit(parent)

    visit(ident)
    return found


def execution_signature(backend, root, manifest, ident):
    models = manifest.get("nodes", {})
    names = dependencies(models, ident)
    paths = sorted(
        {
            str((root / models[name]["original_file_path"]).resolve())
            for name in names
            if name in models and models[name].get("original_file_path")
        }
    )
    signatures = backend.project_signature(root, paths)
    signatures.pop("__model_index__", None)
    descriptors = {}
    texts = []
    macro_names = set()
    for name in names:
        node = models.get(name) or manifest.get("sources", {}).get(name)
        if not node:
            return None
        if (
            str(node.get("database") or "").casefold()
            != Path(backend.database).stem.casefold()
        ):
            return None
        descriptors[name] = {
            k: node.get(k) for k in ("relation_name", "depends_on", "config")
        }
        config = node.get("config") or {}
        if (
            config.get("pre-hook")
            or config.get("post-hook")
            or config.get("pre_hook")
            or config.get("post_hook")
        ):
            return None
        if node.get("external") or config.get("external_location"):
            return None
        texts.append(node.get("raw_code", node.get("raw_sql", "")))
        macro_names.update(node.get("depends_on", {}).get("macros", []))
    pending = list(macro_names)
    while pending:
        name = pending.pop()
        macro = manifest.get("macros", {}).get(name)
        if not macro:
            return None
        descriptors[name] = {k: macro.get(k) for k in ("macro_sql", "depends_on")}
        texts.append(macro.get("macro_sql", ""))
        for parent in macro.get("depends_on", {}).get("macros", []):
            if parent not in macro_names:
                macro_names.add(parent)
                pending.append(parent)
    if re.search(
        "\\b(?:current_date|current_timestamp|current_time|now|today|random|uuid|read_csv\\w*|read_parquet|read_json\\w*|sqlite_scan|postgres_scan|httpfs|run_query|env_var|run_started_at|invocation_id)\\b",
        "\n".join(texts),
        re.I,
    ):
        return None
    payload = {"source": signatures, "physical_dependencies": descriptors}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
