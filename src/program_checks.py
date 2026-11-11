import json
import math
import numbers
import re
from .diagnostic_query import diagnostic_query
from .materialization_state import database_revision

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "requirement": {"type": "string"},
        "models": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "sql": {"type": "string"},
        "result_kind": {
            "type": "string",
            "enum": ["violations", "failure_count", "boolean"],
        },
    },
    "required": ["name", "requirement", "models", "sql", "result_kind"],
    "additionalProperties": False,
}
CHECK_PROMPT = """If a public requirement needs actual computed values to decide
whether the program is complete, include a minimal checks array in this same
response. Each check has name, a literal requirement quote, models it depends
on, a result_kind, and one read-only SQL SELECT. Choose result_kind=violations
for counterexample rows (empty means pass), failure_count for one nonnegative
number (zero means pass), or boolean for one SQL BOOLEAN (true means pass).
Declare the actual result interface; a numeric zero row is not a violating row.
Use literal ref() for generated models. Check the requested population/coverage,
not just an arbitrary sample. Do not invent acceptance rules or hidden targets.
Keep checks empty when no additional data-dependent public condition needs one.
Checks are fallible program expressions: an incorrect check may be revised
against its quoted requirement. A failed check does not automatically prove
that the reported model is the faulty producer. No separate judge is called.
"""


def normalized(text):
    return re.sub("\\s+", " ", text.replace("`", "").replace("*", "")).strip()


class ProgramChecks:
    def __init__(self, agent):
        self.a = agent
        self.definitions = {}
        self.cache = {}
        self.observations = []

    def prepare(self, checks, extra_models=()):
        staged = {}
        question = normalized(self.a.task["question"])
        if not isinstance(checks, list):
            raise ValueError("checks must be an array")
        known = (
            set(self.a.contract["targets"])
            | set(self.a.drafts)
            | set(self.a.produced)
            | set(extra_models)
        )
        for item in checks:
            if (
                not isinstance(item, dict)
                or not {"name", "requirement", "models", "sql"} <= set(item)
                or set(item) - {"name", "requirement", "models", "sql", "result_kind"}
            ):
                raise ValueError(
                    "A check needs name, requirement, models, sql and a declared result kind"
                )
            if item.get("result_kind", "violations") not in {
                "violations",
                "failure_count",
                "boolean",
            }:
                raise ValueError("Unknown check result kind")
            if not all(
                (
                    isinstance(item[k], str) and item[k].strip()
                    for k in ["name", "requirement", "sql"]
                )
            ):
                raise ValueError("Checks need nonempty literal text")
            if normalized(item["requirement"]) not in question:
                raise ValueError(
                    "Quote the original task requirement literally; do not invent a validation rule"
                )
            if (
                not isinstance(item["models"], list)
                or not item["models"]
                or any((n not in known for n in item["models"]))
            ):
                raise ValueError(
                    "Check models must identify declared or retained program outputs"
                )
            key = ",".join(sorted(set(item["models"]))) + ":" + item["name"]
            staged[key] = {**item, "models": sorted(set(item["models"]))}
        return staged

    def install(self, checks):
        staged = self.prepare(checks)
        self.definitions.update(staged)
        if staged:
            self.a.event(
                "program_checks_installed",
                checks=list(staged),
                origin="quoted_public_requirements",
                revisable=True,
            )

    async def run(self):
        observations = []
        failures = []
        a = self.a
        revision = database_revision(a.sql.database)
        for key, item in self.definitions.items():
            if any(
                (
                    n not in a.produced or str(a.produced[n]) not in a.receipts
                    for n in item["models"]
                )
            ):
                continue
            signature = json.dumps([item, revision], sort_keys=True, default=str)
            if signature not in self.cache:
                try:
                    self.cache[signature] = await a.sql.executor.run(
                        diagnostic_query, a.sql, item["sql"]
                    )
                except Exception as exc:
                    self.cache[signature] = {"query_error": str(exc)}
            result = self.cache[signature]
            kind = item.get("result_kind", "violations")
            passed = False
            contract_error = None
            if "query_error" not in result and (not result.get("truncated")):
                rows = result.get("rows", [])
                if kind == "violations":
                    passed = not rows
                elif len(rows) != 1 or len(rows[0]) != 1:
                    contract_error = (
                        "A " + kind + " check must return exactly one scalar"
                    )
                elif kind == "boolean":
                    if type(rows[0][0]) is not bool:
                        contract_error = "A boolean check must return SQL BOOLEAN, not an integer flag"
                    else:
                        passed = rows[0][0]
                else:
                    value = rows[0][0]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, numbers.Number)
                        or (not math.isfinite(float(value)))
                        or (value < 0)
                    ):
                        contract_error = (
                            "failure_count must be one finite nonnegative number"
                        )
                    else:
                        passed = value == 0
            if contract_error:
                result = {**result, "result_contract_error": contract_error}
            observation = {
                "check_id": key,
                **item,
                "observation": result,
                "passed": passed,
                "interpretation_status": "Model-authored expression; revise it if it misstates the quoted requirement.",
            }
            observations.append(observation)
            if not passed:
                failures.append(
                    {
                        "path": str(a.produced[item["models"][0]]),
                        "condition": "revisable_program_check",
                        **observation,
                    }
                )
        self.observations = observations
        if observations:
            a.event("program_checks_observed", observations=observations)
        if failures:
            raise ValueError(
                json.dumps(
                    {
                        "condition": "program_requirement_observations",
                        "failed_models": failures,
                    },
                    default=str,
                )
            )

    def view(self):
        return {
            "definitions": self.definitions,
            "observations": self.observations,
            "revision": "Correct an invalid observation expression together with any supported code correction; preserve its original quoted requirement.",
        }
