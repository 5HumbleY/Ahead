import re


def sql_dependencies(source):
    from jinja2 import Environment, nodes

    if re.search("(?<!\\{)\\{\\s*(?:ref|source)\\s*\\(", source):
        raise ValueError(
            "Unexpanded template expression: SQL files need literal {{ ref(...) }} / {{ source(...) }}, not single braces"
        )
    tree = Environment().parse(source)
    refs = set()
    for call in tree.find_all(nodes.Call):
        if not isinstance(call.node, nodes.Name) or call.node.name != "ref":
            continue
        if not call.args or not all(
            (isinstance(x, nodes.Const) and isinstance(x.value, str) for x in call.args)
        ):
            raise ValueError(
                "Use literal model names in ref(...) so dependencies can be scheduled"
            )
        refs.add(call.args[-1].value)
    return refs
