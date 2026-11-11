import re
from pathlib import PurePosixPath

IDENT = "[A-Za-z_][A-Za-z0-9_]*"


def declared_entity_keys(manifest):
    models = {
        i: n
        for (i, n) in manifest.get("nodes", {}).items()
        if n.get("resource_type") == "model"
    }
    declarations = {}
    result = {}
    for ident, node in models.items():
        for constraint in node.get("constraints", []):
            if constraint.get("type") == "primary_key" and constraint.get("columns"):
                result.setdefault(node["name"], []).append(
                    {
                        "columns": [c.casefold() for c in constraint["columns"]],
                        "basis": [
                            {
                                "model": node["name"],
                                "path": node.get("original_file_path"),
                                "declaration": constraint,
                            }
                        ],
                    }
                )
    for test in manifest.get("nodes", {}).values():
        meta = test.get("test_metadata") or {}
        config = test.get("config") or {}
        if test.get("resource_type") != "test" or meta.get("name") not in {
            "unique",
            "not_null",
        }:
            continue
        if (
            meta.get("namespace") not in {None, "dbt"}
            or config.get("where")
            or config.get("enabled") is False
        ):
            continue
        if config.get("error_if", "!= 0").replace(" ", "") != "!=0":
            continue
        parents = [
            p for p in test.get("depends_on", {}).get("nodes", []) if p in models
        ]
        column = test.get("column_name") or meta.get("kwargs", {}).get("column_name")
        if (
            len(parents) != 1
            or not isinstance(column, str)
            or (not re.fullmatch(IDENT, column))
        ):
            continue
        key = (parents[0], column.casefold())
        declarations.setdefault(key, {})[meta["name"]] = {
            "test": test.get("unique_id"),
            "path": test.get("original_file_path"),
            "declaration": meta,
            "where": config.get("where"),
        }
    for (ident, column), declared in declarations.items():
        if {"unique", "not_null"} <= set(declared):
            result.setdefault(models[ident]["name"], []).append(
                {"columns": [column], "basis": list(declared.values())}
            )
    return result


def inherit_entity_keys(contract, manifest):
    keys = {
        name.casefold(): value
        for (name, value) in declared_entity_keys(manifest).items()
    }
    for target in contract["targets"].values():
        target["entity_key"] = []
        target["entity_key_basis"] = []
        for declaration in keys.get(
            (target.get("source_relation") or "").casefold(), []
        ):
            if set(declaration["columns"]) == set(target["grain"]) and target["grain"]:
                target["entity_key"] = target["grain"]
                target["entity_key_basis"] = declaration["basis"]
                break
    return contract


def metric_name(text):
    return re.sub("[^a-z0-9]+", "_", text.casefold().strip("` ")).strip("_")


def aggregate_constraints(question, targets):
    checks = []
    uncompiled = []
    pattern = re.compile(
        "(?P<metric>[\\w` ]+?)\\s+in\\s+`?(?P<left>"
        + IDENT
        + ")`?\\s+(?:should|must|shall)\\s+(?:match|equal|be equal to)\\s+(?P<other>[\\w` ]+?)\\s+in\\s+`?(?P<right>"
        + IDENT
        + ")`?(?:\\s+within\\s+(?P<tolerance>\\d+(?:\\.\\d+)?)\\s*(?:%|percent))?\\s*\\.?$",
        re.I,
    )

    def binding(name, phrase):
        target = targets[name]
        columns = {metric_name(c): c for c in target["columns"]}
        key = metric_name(phrase)
        if not key.startswith(("total_", "sum_of_")):
            return None
        if key.startswith("sum_of_"):
            key = key[7:]
        variants = [key] + ([key[6:]] if key.startswith("total_") else [])
        for variant in variants:
            if variant in columns:
                return {
                    "model": name,
                    "aggregate": "sum",
                    "column": columns[variant],
                    "binding": "declared_column",
                }
        definitions = []
        for owner in targets.values():
            if (
                not target.get("source_relation")
                or owner.get("source_relation") != target["source_relation"]
            ):
                continue
            for alias, definition in owner.get("metric_definitions", {}).items():
                if (
                    metric_name(alias) in variants
                    and definition["source_relation"] == target["source_relation"]
                ):
                    column = columns.get(metric_name(definition["column"]))
                    if column:
                        definitions.append(
                            (column, owner["name"], definition["declaration"])
                        )
        if len({x[0] for x in definitions}) == 1:
            return {
                "model": name,
                "aggregate": "sum",
                "column": definitions[0][0],
                "binding": "explicit_same_source_sum_alias",
                "support": definitions,
            }
        return None

    for line in question.splitlines():
        text = line.strip().removeprefix("Note:").strip().lstrip("-* ").strip()
        found = pattern.fullmatch(text)
        if not found:
            continue
        (left, right) = (found["left"], found["right"])
        if left not in targets or right not in targets or left == right:
            continue
        lhs = binding(left, found["metric"])
        rhs = binding(right, found["other"])
        if lhs and rhs:
            checks.append(
                {
                    "kind": "aggregate_equality",
                    "left": lhs,
                    "right": rhs,
                    "relative_tolerance": float(found["tolerance"] or 0) / 100,
                    "declaration": line.strip(),
                }
            )
        else:
            uncompiled.append(
                {
                    "declaration": line.strip(),
                    "reason": "Aggregate measure binding is ambiguous or undeclared",
                    "models": [left, right],
                }
            )
    return (checks, uncompiled)


def comma_parts(text):
    result = []
    start = 0
    depth = 0
    quote = None
    for i, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in {'"', "'", "`"}:
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            result.append(text[start:i].strip())
            start = i + 1
    result.append(text[start:].strip())
    return result


def dbt_contract(question, model_roots=("models",)):
    schema = re.search(
        "(?im)^\\s*(?:[-*]\\s*)?(?:target|output)\\s+schema\\s*:\\s*`?([A-Za-z_][A-Za-z0-9_]*)",
        question,
    )
    targets = {}
    active = None
    directory = None
    current = None
    for line in question.splitlines():
        heading = re.match("^(#{1,6})\\s+(.+?)\\s*$", line)
        if heading:
            depth = len(heading[1])
            title = heading[2].strip()
            if re.search(
                "\\brequired\\s+(?:dbt\\s+)?(?:models|outputs|artifacts)\\b",
                title,
                re.I,
            ):
                active = depth
                current = None
                continue
            if active is not None and depth <= active:
                active = None
                current = None
            if active is None:
                continue
            candidates = re.findall("[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+/?", title)
            folder = next(
                (
                    p
                    for p in candidates
                    if any((p.startswith(r.rstrip("/") + "/") for r in model_roots))
                ),
                None,
            )
            if folder and (not folder.endswith(".sql")):
                directory = folder.rstrip("/")
                current = None
                continue
            name = title.strip("` ")
            if re.fullmatch("[a-z_][A-Za-z0-9_]*", name):
                if name in targets:
                    raise ValueError("Repeated required output name: " + name)
                current = {
                    "name": name,
                    "path": str(PurePosixPath(directory) / (name + ".sql"))
                    if directory
                    else None,
                    "body": [],
                }
                targets[name] = current
            else:
                current = None
        elif current is not None:
            current["body"].append(line)
    for target in targets.values():
        body = "\n".join(target.pop("body")).strip()
        target["declared_requirements"] = body
        line = re.search("(?im)^\\s*Columns\\s*:\\s*(.+)$", body)
        columns = []
        annotations = {}
        if line:
            for part in comma_parts(line[1]):
                found = re.match("^`?([A-Za-z_][A-Za-z0-9_]*)`?(?:\\s*\\(|\\s*$)", part)
                if not found:
                    columns = []
                    break
                columns.append(found[1])
                annotations[found[1]] = part
        target["columns"] = columns
        grain = re.search(
            "(?im)^\\s*One row per\\s+(\\([^)]+\\)|`?[A-Za-z_][A-Za-z0-9_]*`?)", body
        )
        keys = (
            [x.strip("()` ").casefold() for x in comma_parts(grain[1].strip("()"))]
            if grain
            else []
        )
        target["grain"] = (
            keys if all((k in {c.casefold() for c in columns} for k in keys)) else []
        )
        target["not_null"] = re.findall(
            "(?i)Exclude rows where\\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\\s+IS NULL", body
        )
        origin = re.search(
            "(?im)^\\s*One row per\\s+(?:\\([^)]+\\)|`?"
            + IDENT
            + "`?)\\s+from\\s+`?("
            + IDENT
            + ")`?([^\\n]*)",
            body,
        )
        target["source_relation"] = origin[1] if origin else None
        target["source_scope"] = origin[0].strip() if origin else None
        target["metric_definitions"] = {}
        for column, annotation in annotations.items():
            aggregate = re.fullmatch(
                "`?"
                + re.escape(column)
                + "`?\\s*\\(\\s*SUM(?:\\s+(?:of\\s+)?|\\s*\\(\\s*)`?("
                + IDENT
                + ")`?\\s*\\)?(?:\\s+from\\s+`?("
                + IDENT
                + ")`?)?\\s*\\)",
                annotation,
                re.I,
            )
            if aggregate:
                target["metric_definitions"][column] = {
                    "aggregate": "sum",
                    "column": aggregate[1],
                    "source_relation": aggregate[2] or target["source_relation"],
                    "declaration": annotation,
                }
    (checks, uncompiled) = aggregate_constraints(question, targets)
    return {
        "schema": schema[1] if schema else None,
        "targets": targets,
        "aggregate_constraints": checks,
        "uncompiled_constraints": uncompiled,
    }
