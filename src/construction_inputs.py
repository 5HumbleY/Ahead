import copy
from .sql_catalog_view import compact_sql_catalog
import json
import re


class ConstructionInputs:
    def __init__(self, snapshot, targets, assigned):
        self.snapshot = snapshot
        self.bindings = {
            item["model"]: item
            for item in snapshot.get("project", {}).get("named_bindings", [])
        }
        region = {name: targets.get(name, {"name": name}) for name in assigned}
        pending = list(region)
        visited = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            text = json.dumps(targets.get(name, {}), ensure_ascii=False)
            for other in targets:
                if other not in visited and self.mentions(text, other):
                    region[other] = targets[other]
                    pending.append(other)
        text = json.dumps(region, ensure_ascii=False)
        self.selected = {name for name in self.bindings if self.mentions(text, name)}
        if not self.selected:
            self.selected = set(self.bindings)
        self.initial_selected = sorted(self.selected)

    @staticmethod
    def mentions(text, name):
        return bool(
            re.search("(?<![A-Za-z0-9_])" + re.escape(name) + "(?![A-Za-z0-9_])", text)
        )

    def expand(self, names):
        if (
            not isinstance(names, list)
            or not 1 <= len(names) <= 16
            or any((not isinstance(n, str) for n in names))
        ):
            raise ValueError(
                "inspect_sources needs 1 to 16 names from named_binding_index"
            )
        unknown = set(names) - set(self.bindings)
        if unknown:
            raise ValueError("Unknown source bindings: " + repr(sorted(unknown)))
        self.selected.update(names)

    def view(self):
        result = compact_sql_catalog(self.snapshot)
        project = result.get("project")
        if not project or not self.bindings:
            return compact_sql_catalog(result)
        project["named_bindings"] = [
            copy.deepcopy(b)
            for (name, b) in self.bindings.items()
            if name in self.selected
        ]
        project["named_binding_index"] = [
            {
                k: copy.deepcopy(b[k])
                for k in ("model", "file", "compiled_relation", "read_source")
                if k in b
            }
            for b in self.bindings.values()
        ]
        project["named_binding_scope"] = {
            "complete_index": True,
            "expanded": sorted(self.selected),
            "total": len(self.bindings),
            "request_missing": 'Return {"inspect_sources":["exact model name"]} to obtain complete source SQL/schema/key declarations before generating files.',
        }
        return compact_sql_catalog(result)
