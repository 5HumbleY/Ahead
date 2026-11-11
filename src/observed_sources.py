import copy
import ast
import json


class SourceData(dict):
    __source_observation__ = True


class ObservedSources:
    def __init__(self):
        self.readers = {}
        self.profiles = {}
        self.sequence = 0
        self.api_names = set()

    def register(self, key, reader):
        self.readers[key] = reader

    def observe(self, session, key, version, profile):
        self.sequence += 1
        self.profiles[key] = {
            "source": key,
            "version": version,
            "profile": copy.deepcopy(profile),
            "sequence": self.sequence,
        }
        support = getattr(session, "construction_support", None)
        if support is not None:
            support.setdefault("resources", {})[key] = version

    def current(self, key):
        reader = self.readers.get(key)
        if reader is None:
            return None
        try:
            return reader()
        except Exception:
            return None

    def valid(self, support):
        return all(
            (
                self.current(key) == version
                for (key, version) in support.get("resources", {}).items()
            )
        )

    def view(self, budget=14000):
        rows = []
        for item in sorted(
            self.profiles.values(), key=lambda x: x["sequence"], reverse=True
        ):
            row = copy.deepcopy({k: v for (k, v) in item.items() if k != "sequence"})
            profile = row["profile"]
            if isinstance(profile, dict):
                for key in ("links", "controls"):
                    if len(profile.get(key, [])) > 32:
                        profile[key + "_total"] = len(profile[key])
                        profile[key] = profile[key][:32]
                if "text_preview" in profile:
                    profile["text_preview"] = profile["text_preview"][:400]
            size = len(json.dumps(row, ensure_ascii=False, default=str))
            if size <= budget:
                rows.append(copy.deepcopy(row))
                budget -= size
            if len(rows) >= 4:
                break
        return rows


def supported_probe(tree, registered):
    safe = {
        "read_arg",
        "read_global",
        "read_source",
        "inspect_file",
        "csv_rows",
        "read_table",
        "print",
        "preview",
        "len",
        "sorted",
        "list",
        "dict",
        "str",
        "type",
        "int",
        "float",
        "enumerate",
        "range",
        "min",
        "max",
        "sum",
        "abs",
    } | set(registered)
    json_names = {"json"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            json_names.update(
                (a.asname or a.name for a in node.names if a.name == "json")
            )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in safe:
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id in json_names)
            and (node.func.attr == "dumps")
        ):
            continue
        return False
    return True
