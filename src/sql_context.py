from .sql_catalog_view import compact_sql_catalog


def constructor_facts(task, contract, catalog, environment):
    catalog = compact_sql_catalog(catalog)
    for binding in catalog.get("project", {}).get("named_bindings", []):
        binding.pop("source_code", None)
        binding["source_access"] = (
            "sql_observe.sources reads exact source lines by model name"
        )
        if binding.get("compiled_output_interface"):
            binding["sources"] = [
                {
                    key: source[key]
                    for key in ("schema", "table", "inspect")
                    if key in source
                }
                for source in binding.get("sources", [])
            ]
    fields = (
        "name",
        "path",
        "schema",
        "physical_schema",
        "columns",
        "grain",
        "entity_key",
        "not_null",
        "source_relation",
        "row_constraints",
        "relation_constraints",
    )
    outputs = {
        name: {key: item[key] for key in fields if key in item}
        for (name, item) in contract["targets"].items()
    }
    return {
        "original_task": task,
        "output_contract": {"schema": contract.get("schema"), "targets": outputs},
        "environment": environment,
        "input_catalog": catalog,
    }


def source_index(context):
    return {
        "index": context.index,
        "read": "sql_observe.sources with model, start and lines returns exact current source",
    }
