import copy
import hashlib
import json
from pathlib import Path


class FailureState:
    def __init__(self):
        self.entries = {}

    @staticmethod
    def name(item):
        return (
            Path(item["path"]).stem
            if item.get("path")
            else item.get("id", "").rsplit(".", 1)[-1]
        )

    def prune(self, signature, checks):
        for key, entry in list(self.entries.items()):
            check = checks.get(entry["failure"].get("check_id"))
            current = tuple(((n, signature(n)) for n in entry["support"]))
            if entry["signature"] != current or entry["check"] != check:
                del self.entries[key]

    def replace(self, phase, evaluated, failures, signature, checks):
        for key, entry in list(self.entries.items()):
            if entry["phase"] == phase and set(entry["support"]) <= set(evaluated):
                del self.entries[key]
        for item in failures:
            name = self.name(item)
            identity = {
                k: item.get(k)
                for k in ("path", "id", "condition", "column", "keys", "check_id")
            }
            support = sorted(
                {name}
                | set(item.get("models", []))
                | {
                    item[side]["model"]
                    for side in ("left", "right")
                    if isinstance(item.get(side), dict) and item[side].get("model")
                }
            )
            key = (
                phase
                + ":"
                + hashlib.sha256(
                    json.dumps(identity, sort_keys=True, default=str).encode()
                ).hexdigest()
            )
            self.entries[key] = {
                "phase": phase,
                "model": name,
                "failure": copy.deepcopy(item),
                "support": support,
                "signature": tuple(((n, signature(n)) for n in support)),
                "check": copy.deepcopy(checks.get(item.get("check_id"))),
            }

    def failures(self):
        values = [entry["failure"] for entry in self.entries.values()]
        return (
            {"condition": "unresolved_program_obligations", "failed_models": values}
            if values
            else None
        )

    def blocked(self):
        return {
            entry["model"]
            for entry in self.entries.values()
            if entry["phase"] in {"syntax", "execution"}
        }
