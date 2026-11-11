import json
import math
import re


def mentions(text, name):
    return bool(
        re.search("(?<![A-Za-z0-9_])" + re.escape(name) + "(?![A-Za-z0-9_])", text)
    )


class ConstructionRegions:
    def __init__(
        self,
        targets,
        source_names=(),
        output_tokens=32768,
        concurrency=2,
        work_tokens=None,
    ):
        self.targets = targets
        self.order = {name: i for (i, name) in enumerate(targets)}
        self.text = {
            name: json.dumps(item, ensure_ascii=False, default=str)
            for (name, item) in targets.items()
        }
        self.refs = {
            name: {
                other for other in targets if other != name and mentions(text, other)
            }
            for (name, text) in self.text.items()
        }
        self.support = {
            name: {source for source in source_names if mentions(text, source)}
            for (name, text) in self.text.items()
        }
        for name, item in targets.items():
            if item.get("source_relation"):
                self.support[name].add(item["source_relation"])
        self.capacity = max(
            1,
            min(
                output_tokens // 2,
                work_tokens if work_tokens is not None else output_tokens // 2,
            ),
        )
        self.hard_capacity = max(1, output_tokens // 2)
        self.concurrency = max(1, concurrency)
        self.cost = {
            name: max(
                384,
                192
                + 48 * len(item.get("columns", []))
                + math.ceil(len(self.text[name]) / 4),
            )
            for (name, item) in targets.items()
        }

    def affinity(self, a, b):
        direct = b in self.refs.get(a, ()) or a in self.refs.get(b, ())
        common = len(self.support.get(a, set()) & self.support.get(b, set()))
        return 4 * int(direct) + common

    def select(self, pending):
        pending = list(dict.fromkeys(pending))
        if not pending:
            return []
        cost = lambda name: self.cost.get(name, 768)
        budget = min(
            self.capacity,
            max(
                self.capacity // 2,
                max(map(cost, pending)),
                math.ceil(sum(map(cost, pending)) / self.concurrency),
            ),
        )
        seed = max(
            pending,
            key=lambda n: (
                sum((self.affinity(n, p) for p in pending)),
                -pending.index(n),
            ),
        )
        region = [seed]
        remaining = [n for n in pending if n != seed]
        used = cost(seed)
        while remaining:
            candidates = [n for n in remaining if used + cost(n) <= budget]
            if not candidates:
                break
            best = max(
                candidates,
                key=lambda n: (
                    sum((self.affinity(n, r) for r in region)),
                    -pending.index(n),
                ),
            )
            if not any((self.affinity(best, r) for r in region)):
                break
            region.append(best)
            remaining.remove(best)
            used += cost(best)
        return sorted(region, key=lambda n: self.order.get(n, -1))

    def select_balanced(self, pending, slots):
        pending = list(dict.fromkeys(pending))
        cost = lambda n: self.cost.get(n, 768)
        if not pending:
            return []
        if slots == 1 and sum(map(cost, pending)) <= self.hard_capacity:
            return pending
        if slots != 2 or len(pending) > 12:
            return self.select(pending)
        best = None
        for mask in range(1 << len(pending) - 1):
            left = [pending[0]] + [
                n for (i, n) in enumerate(pending[1:]) if mask & 1 << i
            ]
            right = [n for n in pending if n not in left]
            if not right:
                continue
            loads = [sum(map(cost, left)), sum(map(cost, right))]
            if max(loads) > self.hard_capacity:
                continue
            cut = sum((self.affinity(a, b) for a in left for b in right))
            score = (max(loads), cut, mask)
            if best is None or score < best[0]:
                best = (score, left, right)
        if best is None:
            return self.select(pending)
        return max(best[1:], key=lambda group: sum(map(cost, group)))

    def relevant_accepted(self, assigned, available):
        needed = set()
        pending = list(assigned)
        visited = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            for parent in self.refs.get(name, ()):
                if parent in available:
                    needed.add(parent)
                pending.append(parent)
        for name in available:
            if any(
                (
                    self.support.get(name, set()) & self.support.get(n, set())
                    for n in assigned
                )
            ):
                needed.add(name)
        return needed
