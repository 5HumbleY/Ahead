import hashlib
import json


class NoInspectionProgress(RuntimeError):
    pass


class InspectionState:
    def __init__(self):
        self.history = []
        self.repeats = {}
        self.last_key = None
        self.streak = 0

    def record(self, request, observation, version, novel):
        encoded = json.dumps(
            {"request": request, "version": version}, sort_keys=True, default=str
        )
        key = hashlib.sha256(encoded.encode()).hexdigest()
        count = self.repeats.get(key, 0) + 1
        self.repeats[key] = count
        self.streak = self.streak + 1 if key == self.last_key and (not novel) else 1
        self.last_key = key
        row = {
            "request": request,
            "source_version": version,
            "observation": observation,
            "new_evidence": bool(novel),
            "times_requested_at_this_version": count,
        }
        self.history.append(row)
        if self.streak >= 3 and (not novel):
            raise NoInspectionProgress(
                "The same inspection has been served three times at the same source version without an edit or new evidence. Repair stopped with its original failure unresolved."
            )
        return row

    def context(self):
        shown = []
        budget = 18000
        for item in reversed(self.history[-8:]):
            row = dict(item)
            size = len(json.dumps(row, ensure_ascii=False, default=str))
            if size > budget:
                row.pop("observation", None)
                row["observation_address"] = (
                    "Cached at this request and source version; repeat the addressed request only if its exact result is no longer visible and needed."
                )
            else:
                budget -= size
            shown.append(row)
        return {
            "served_inspections": list(reversed(shown)),
            "next_action": "Use evidence already delivered. Inspect a genuinely different missing fact, query current data, or produce a causal patch. An unchanged inspection does not advance the repair.",
        }
