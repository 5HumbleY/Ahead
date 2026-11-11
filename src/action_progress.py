import ast
import hashlib
import json
import re


class NoObservedProgress(RuntimeError):
    pass


class ActionProgress:
    def __init__(self):
        self.seen = {}
        self.warning = None

    def record(self, packet, observation, state):
        packet = dict(packet)
        if packet.get("action") == "python":
            try:
                packet["code"] = ast.dump(
                    ast.parse(packet["code"]), include_attributes=False
                )
            except SyntaxError:
                pass
        error = re.sub("<([^>]+):\\d+>", "<\\1:cell>", observation.get("error") or "")
        payload = [packet, observation.get("stdout"), error, state]
        key = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        count = self.seen.get(key, 0) + 1
        self.seen[key] = count
        self.warning = None
        if count >= 2:
            self.warning = {
                "same_action_and_observed_state": count,
                "next_action": "This action has returned the same observation with no visible state change. Use retained evidence, choose a different supported dependency, or report unavailable evidence with task_unavailable(reason). Do not repeat installations, unavailable URLs or source discovery.",
            }
        if count >= 3:
            raise NoObservedProgress(
                "Stopped unresolved: the same action, result and observable state repeated three times. This is a bounded no-progress policy, not proof that opaque external state cannot change."
            )
        return self.warning
