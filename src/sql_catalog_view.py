import copy


def compact_sql_catalog(catalog):
    result = copy.deepcopy(catalog)

    def visit(value):
        if isinstance(value, dict):
            columns = value.get("columns")
            if (
                isinstance(columns, list)
                and columns
                and all(
                    (
                        isinstance(c, dict) and set(c) == {"name", "type", "nullable"}
                        for c in columns
                    )
                )
            ):
                value["column_fields"] = ["name", "type", "nullable"]
                value["columns"] = [
                    [c[k] for k in value["column_fields"]] for c in columns
                ]
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(result)
    return result
