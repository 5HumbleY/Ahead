import copy
import json


def scoped_facts(stable, relevant):
    result = copy.deepcopy(stable)
    outputs = result.get("output_contract", {}).get("targets", {})
    result["output_index"] = {
        name: {k: v for (k, v) in item.items() if k in {"path", "schema"}}
        for (name, item) in outputs.items()
    }
    if outputs:
        result["output_contract"]["targets"] = {
            n: v for (n, v) in outputs.items() if n in relevant
        }
    project = result.get("input_catalog", {}).get("project", {})
    bindings = project.get("named_bindings", [])
    chosen = [b for b in bindings if b.get("model") in relevant]
    if chosen:
        project["named_bindings"] = chosen
        project["named_binding_index"] = [
            {
                k: v
                for (k, v) in b.items()
                if k in {"model", "file", "compiled_relation"}
            }
            for b in bindings
        ]
        project["source_access"] = (
            "All original source addresses remain indexed; sql_observe.sources reads exact source and queries inspect real relations."
        )
    project.pop("directories", None)
    return result


def scoped_state(state, requested=()):
    result = copy.deepcopy(state)
    index = result.get("current_sources", {}).get("index", {})
    result["failure_sources"] = {
        n: p
        for (n, p) in result.get("failure_sources", {}).items()
        if index.get(n, {}).get("editable") or n in requested
    }
    if index:
        result["current_sources"]["index"] = {
            n: {
                k: v
                for (k, v) in item.items()
                if k in {"path", "editable", "lines", "dependencies"}
            }
            for (n, item) in index.items()
        }
    last = result.get("latest_execution_observation", {})
    if isinstance(last, dict):
        if last.get("failure") == result.get("current_failure"):
            last.pop("failure", None)
        if "queries" in last:
            last["query_observation_ids"] = [
                q.get("observation_id") for q in last.pop("queries")
            ]
        pages = last.get("source_pages", {})
        if pages:
            last["source_pages"] = {
                n: p
                for (n, p) in pages.items()
                if p != result["failure_sources"].get(n)
            }
    checks = result.get("program_requirement_observations", {})
    if checks.get("observations"):
        checks["observations"] = [
            {k: v for (k, v) in q.items() if k in {"check_id", "passed"}}
            for q in checks["observations"]
        ]
    return result


def observation_view(observations, current_revision, budget=10000):
    index = {}
    expanded = {}
    used = 0
    for ident, item in observations.items():
        index[ident] = {
            "name": item.get("name"),
            "columns": item.get("columns"),
            "rows": len(item.get("rows", [])),
            "sql_preview": item.get("sql", "")[:200],
            "current_database_revision": item.get("database_revision")
            == current_revision,
        }
    for ident, item in reversed(list(observations.items())):
        size = len(json.dumps(item, ensure_ascii=False, default=str))
        if used + size <= budget:
            expanded[ident] = {
                **item,
                "current_database_revision": item.get("database_revision")
                == current_revision,
            }
            used += size
    return {
        "index": index,
        "expanded": expanded,
        "access": "sql_recall(ids=[...]) returns the full retained observation without executing a query. Historical observations are labelled; re-query only if current values are needed after a database change.",
    }
