from .program_checks import normalized

BINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "requirement": {"type": "string"},
        "source": {"type": "string"},
        "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "population": {"type": "string"},
    },
    "required": ["requirement", "source", "columns", "population"],
    "additionalProperties": False,
}
BINDING_PROMPT = """Before the SQL files in this SAME response, return semantic_bindings:
a small array of source-meaning choices needed by this implementation. Each item
quotes the original requirement literally, names one observed source relation or
model, gives the exact columns realizing that concept, and states that measure's
population. This binds the task's words to actual source fields before expanding
code; it is not a new planning or review request. Do not repeat unambiguous
column-to-column passthroughs; use [] if no semantic choice is needed.

A source may contain several fields describing different events. Choose the
field for the event named by the quoted requirement, even when another field
looks more related to the task title. For each population, distinguish the
original source records from the records surviving later predicates. A predicate
needed by one measure is not a global filter for all measures. Keep distinct
requested populations on separate branches of the program. A missing attribute
does not itself authorize dropping a record. The SQL must implement these bound
choices; preserve them during a local syntax or representation repair.
"""


def validate_bindings(task, catalog, bindings):
    if not isinstance(bindings, list):
        raise ValueError("semantic_bindings must be an array")
    known = {}
    for db in catalog.get("databases", []):
        for source in db.get("referenced_relations", []):
            known[(source["schema"] + "." + source["table"]).casefold()] = {
                c["name"].casefold()
                for c in source.get("columns", [])
                if isinstance(c, dict)
            }
    for source in catalog.get("project", {}).get("named_bindings", []):
        interface = source.get("compiled_output_interface") or {}
        columns = interface.get("columns", []) if isinstance(interface, dict) else []
        known[source["model"].casefold()] = {
            c["name"].casefold() if isinstance(c, dict) else str(c).casefold()
            for c in columns
        }
    question = normalized(task)
    for b in bindings:
        if not isinstance(b, dict) or set(b) != {
            "requirement",
            "source",
            "columns",
            "population",
        }:
            raise ValueError(
                "A semantic binding needs requirement, source, columns and population"
            )
        if not all(
            (
                isinstance(b[k], str) and b[k].strip()
                for k in ["requirement", "source", "population"]
            )
        ):
            raise ValueError("Semantic choices need nonempty text")
        if normalized(b["requirement"]) not in question:
            raise ValueError(
                "Anchor semantic choices to literal original requirement text"
            )
        fields = known.get(b["source"].casefold())
        if fields is None:
            raise ValueError(
                "Semantic binding source must be an observed physical relation or named model"
            )
        if (
            not isinstance(b["columns"], list)
            or not b["columns"]
            or any(
                (
                    not isinstance(c, str) or c.casefold() not in fields
                    for c in b["columns"]
                )
            )
        ):
            raise ValueError(
                "Semantic binding columns must exist in the observed source interface"
            )
    return bindings
