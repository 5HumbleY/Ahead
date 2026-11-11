import hashlib
import json


class RepairContext:
    def __init__(self, sources, budget, expand=None):
        self.sources = sources
        self.budget = max(512, int(budget))
        self.index = {
            name: {
                "path": item["path"],
                "editable": item["editable"],
                "characters": len(item["sql"]),
                "lines": len(item["sql"].splitlines()),
                "sha256": hashlib.sha256(item["sql"].encode()).hexdigest(),
                "dependencies": item.get("dependencies", []),
            }
            for (name, item) in sources.items()
        }
        self.pages = {}
        for name, item in sources.items():
            if expand is not None and name not in expand:
                continue
            page = self.page(name, 1, max(1, self.index[name]["lines"]))
            candidate = {**self.pages, name: page}
            if self.size(candidate) <= self.budget:
                self.pages = candidate

    @staticmethod
    def size(pages):
        return len(json.dumps(pages, ensure_ascii=False))

    def page(self, name, start, lines):
        if name not in self.sources:
            raise ValueError("Inspect a model from sql_index: " + str(name))
        if type(start) is not int or type(lines) is not int or start < 1 or (lines < 1):
            raise ValueError(
                "SQL source ranges use positive one-based start and line count"
            )
        source = self.sources[name]["sql"]
        records = source.splitlines(keepends=True)
        if start > max(1, len(records)):
            raise ValueError("SQL source range starts past end of file")
        end = min(len(records), start - 1 + lines)
        return {
            "path": self.sources[name]["path"],
            "sha256": self.index[name]["sha256"],
            "start": start,
            "end": end,
            "complete": start == 1 and end == len(records),
            "sql": "".join(records[start - 1 : end]),
        }

    def inspect(self, requests):
        if not isinstance(requests, list) or not 1 <= len(requests) <= 8:
            raise ValueError("inspect needs 1 to 8 addressed SQL source ranges")
        selected = {}
        for request in requests:
            if not isinstance(request, dict) or set(request) != {
                "model",
                "start",
                "lines",
            }:
                raise ValueError("Each inspect entry needs model, start and lines")
            name = request["model"]
            if name in selected:
                raise ValueError("Request one contiguous source range per model")
            selected[name] = self.page(name, request["start"], request["lines"])
        if self.size(selected) > self.budget:
            raise ValueError(
                "Requested SQL source exceeds the context budget; use smaller line ranges"
            )
        for name, page in self.pages.items():
            if name in selected:
                continue
            candidate = {**selected, name: page}
            if self.size(candidate) <= self.budget:
                selected = candidate
        self.pages = selected

    def view(self):
        return {
            "sql_index": self.index,
            "sql_pages": self.pages,
            "sql_context_character_budget": self.budget,
        }
