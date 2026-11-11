import re

_CALL = re.compile(
    "(?P<name>ref|source)\\s*\\(\\s*(?P<a>'(?:[^']|'')*'|\\\"(?:[^\\\"]|\\\"\\\")*\\\")\\s*(?:,\\s*(?P<b>'(?:[^']|'')*'|\\\"(?:[^\\\"]|\\\"\\\")*\\\")\\s*)?\\)",
    re.I,
)


def lower_references(source, models=(), sources=()):
    known_models = set(models)
    known_sources = set(sources)
    changes = []
    i = 0
    previous = None

    def literal(value):
        return value[1:-1].replace(value[0] * 2, value[0])

    while i < len(source):
        if source.startswith(("--", "/*", "{{", "{%", "{#"), i):
            opener = source[i : i + 2]
            closer = {"--": "\n", "/*": "*/", "{{": "}}", "{%": "%}", "{#": "#}"}[
                opener
            ]
            end = source.find(closer, i + 2)
            i = len(source) if end < 0 else end + len(closer)
            if opener in {"{{", "{%"}:
                previous = None
            continue
        if source[i] in "'\"`":
            quote = source[i]
            i += 1
            while i < len(source):
                if source[i] == quote:
                    if source[i : i + 2] == quote * 2:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            previous = None
            continue
        dollar = (
            re.match("\\$(?:[A-Za-z_][A-Za-z0-9_]*)?\\$", source[i:])
            if source[i] == "$"
            else None
        )
        if dollar:
            marker = dollar[0]
            end = source.find(marker, i + len(marker))
            i = len(source) if end < 0 else end + len(marker)
            previous = None
            continue
        word = re.match("[A-Za-z_][A-Za-z0-9_]*", source[i:])
        if word:
            call = _CALL.match(source, i) if previous in {"from", "join"} else None
            if call:
                name = call["name"].lower()
                args = [literal(call[k]) for k in ("a", "b") if call[k]]
                valid = (
                    name == "ref"
                    and len(args) == 1
                    and (args[0] in known_models)
                    or (
                        name == "source"
                        and len(args) == 2
                        and (tuple(args) in known_sources)
                    )
                )
                if valid:
                    changes.append(
                        (
                            i,
                            call.end(),
                            "{{ "
                            + name
                            + "("
                            + ", ".join((repr(x) for x in args))
                            + ") }}",
                        )
                    )
                    i = call.end()
                    previous = None
                    continue
            previous = word[0].lower()
            i += len(word[0])
            continue
        if not source[i].isspace():
            previous = None
        i += 1
    for start, end, replacement in reversed(changes):
        source = source[:start] + replacement + source[end:]
    return source


def bind_observed_references(agent, source):
    manifest = agent.sql.manifest or {}
    models = set(agent.contract["targets"]) | set(agent.drafts) | set(agent.produced)
    models.update(
        (
            n["name"]
            for n in manifest.get("nodes", {}).values()
            if n.get("resource_type") == "model"
        )
    )
    sources = {
        (n["source_name"], n["name"]) for n in manifest.get("sources", {}).values()
    }
    for database in agent.input_catalog.snapshot.get("databases", []):
        sources.update(
            (
                (r["schema"], r["table"])
                for r in database.get("referenced_relations", [])
            )
        )
    lowered = lower_references(source, models, sources)
    if lowered != source:
        agent.event(
            "literal_sql_references_bound", analytical_expressions_unchanged=True
        )
    return lowered
