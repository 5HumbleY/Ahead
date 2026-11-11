import copy
import asyncio
import hashlib
import json
import re
from .program_checks import CHECK_SCHEMA, normalized

RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved_bindings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    "source": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "population": {"type": "string"},
                },
                "required": ["requirement", "source", "columns", "population"],
                "additionalProperties": False,
            },
        },
        "checks": {"type": "array", "minItems": 0, "items": CHECK_SCHEMA},
    },
    "required": ["resolved_bindings", "checks"],
    "additionalProperties": False,
}


def resolution_schema(payload):
    requirements = payload["requirements"]
    quotes = list(dict.fromkeys((r["requirement"] for r in requirements)))
    sources = list(dict.fromkeys((r["interface"]["name"] for r in requirements)))
    columns = sorted(
        {
            c["name"] if isinstance(c, dict) else c
            for r in requirements
            for c in r["interface"].get("columns", [])
        }
    )
    schema = copy.deepcopy(RESOLUTION_SCHEMA)
    schema["properties"]["resolved_bindings"]["minItems"] = len(quotes)
    schema["properties"]["resolved_bindings"]["maxItems"] = len(quotes)
    binding = schema["properties"]["resolved_bindings"]["items"]["properties"]
    binding["requirement"]["enum"] = quotes
    binding["source"]["enum"] = sources
    if columns:
        binding["columns"]["items"]["enum"] = columns
    if payload.get("resolution_only"):
        schema["properties"].pop("checks")
        schema["required"].remove("checks")
        return schema
    schema["properties"]["checks"]["maxItems"] = len(quotes)
    check = schema["properties"]["checks"]["items"]["properties"]
    check["requirement"]["enum"] = quotes
    check["models"]["items"]["enum"] = list(payload["output_interfaces"])
    return schema


PROMPT = """Resolve ONLY the caller-supplied requirements, one binding per requirement.
Other original-task clauses only constrain these exact choices; they are NOT
new questions. Do not enumerate, implement or check the rest of the task.
Return at most one concise check per supplied requirement. When the clause
does not determine an observable condition, return checks=[]; an unspecified
implementation choice is not a new acceptance rule. Do not
reconstruct a full expected output table or unrelated metrics.

Resolve the exact meaning of the supplied local construction requirements
from the original task and observed/public source interfaces. The implementation
is being constructed separately and is deliberately not supplied. Distinguish
different events, populations and reference frames; a related field or familiar
task convention does not override the literal defining clause. Nullable metadata
alone does not authorize exclusions. Keep lifetime/full populations separate
from a particular window or conditional measure.

Return resolved_bindings (requirement, exact source/model, columns, population)
and minimal checks. A check quotes an original requirement literally, names the
generated models it observes, declares result_kind and supplies one read-only
SQL SELECT. violations returns counterexample rows (empty passes); failure_count
returns one nonnegative scalar (zero passes); boolean returns one SQL BOOLEAN
(true passes). Never interpret a scalar failure flag as a counterexample row. Derive expected expressions from the requirement and immutable
source data, not an assumed generated implementation. Use {{ ref('model') }}
for generated outputs and the observed sql_reference for original sources.
Do not invent dbt source names. Check the COMPLETE relevant population; no guessed
counts, samples, expected answers, unrelated conditions or generic judge score.
Use the original task's filters, grain and NULL policy on both comparison sides.
The declared result_kind determines whether this observation passed. Keep each
check concise and cover the supplied semantic choices together where possible.
These checks are fallible program expressions; do not claim universal proof.
Return only this JSON; do not construct artifacts or review whole-program code.
"""
INPUT_PROMPT = """Resolve only the supplied raw-input meaning choices from the
literal requirements and observed interfaces. Return resolved_bindings with
requirement, exact source, necessary columns, and population. Do not review SQL,
compute task results, create checks, or invent acceptance conditions. These are
construction inputs for another implementation, not a second task solution.
Distinguish different events, populations and reference frames. A familiar task
convention does not override the defining clause. Nullable metadata alone does
not authorize exclusions. Include only columns needed for the supplied concept;
other source attributes remain available to the caller. State remaining freedom
when the task does not uniquely specify a choice. Keep each binding concise.
"""
FINISH_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_matches": {"type": "integer", "minimum": 1},
                },
                "required": ["path", "old", "new", "expected_matches"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edits"],
    "additionalProperties": False,
}
FINISH_PROMPT = """Finish the retained SQL region using its now-resolved raw input
bindings. Its proposed implementation is not installed yet. Return only exact
literal edits {"edits":[{"path":...,"old":...,"new":...,"expected_matches":...}]}.
Do not regenerate unrelated work or return data queries. The original task and
complete proposed source are supplied. Use resolved fields for the quoted event
or concept, preserving other independently required attributes and calculations.
Retain the task's population: a previous tentative field choice or null filter
is not authority to exclude records. Update dependent uses consistently. Keep
all unrelated formulas, target interfaces, predicates and code. If a binding is
already correctly implemented, use edits=[]; do not manufacture a change.
"""


def purpose_quote(question, quote):
    sections = []
    current = []
    active = False
    for line in question.splitlines():
        label = re.match("^\\s*(?:[-*]\\s*)?\\*\\*([^*]+)\\*\\*\\s*:", line)
        if re.match("^#{1,6}\\s+", line) or label:
            if current:
                sections.append("\n".join(current))
                current = []
            active = bool(label and label[1].strip().casefold() == "purpose")
        if active:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return any((normalized(quote) in normalized(section) for section in sections))


class PrefixArray:
    def __init__(self, callback):
        self.callback = callback
        self.done = False

    def feed(self, text):
        if self.done:
            return
        match = re.search('"semantic_bindings"\\s*:\\s*', text)
        if not match:
            return
        try:
            (value, end) = json.JSONDecoder().raw_decode(text[match.end() :])
        except ValueError:
            return
        if isinstance(value, list):
            self.done = True
            self.callback(value)


def interface_index(agent):
    result = {}
    for db in agent.input_catalog.snapshot.get("databases", []):
        for item in db.get("referenced_relations", []):
            name = item["schema"] + "." + item["table"]
            result[name.casefold()] = {
                "name": name,
                "columns": item.get("columns", []),
                "kind": "observed_immutable_source",
                "sql_reference": '"'
                + item["schema"].replace('"', '""')
                + '"."'
                + item["table"].replace('"', '""')
                + '"',
            }
    for item in agent.input_catalog.snapshot.get("project", {}).get(
        "named_bindings", []
    ):
        interface = item.get("compiled_output_interface") or {}
        if isinstance(interface, dict):
            result[item["model"].casefold()] = {
                "name": item["model"],
                "columns": interface.get("columns", []),
                "kind": "observed_input_model",
            }
    for name, item in agent.contract["targets"].items():
        result[name.casefold()] = {
            "name": name,
            "columns": item.get("columns", []),
            "public_definition": item.get("original_requirement_regions")
            or item.get("declared_requirements", ""),
            "kind": "requested_output_interface",
        }
    return result


class SemanticSupport:
    def __init__(self, agent):
        self.a = agent
        self.jobs = {}
        self.admitted = set()
        self.installed = set()

    def choices(self, bindings):
        known = interface_index(self.a)
        choices = []
        for b in bindings:
            if not isinstance(b, dict):
                continue
            quote = b.get("requirement")
            source = b.get("source")
            columns = b.get("columns")
            if (
                not isinstance(quote, str)
                or not isinstance(source, str)
                or (not isinstance(columns, list))
            ):
                continue
            if normalized(quote) not in normalized(self.a.task["question"]):
                continue
            matching = [
                line
                for line in self.a.task["question"].splitlines()
                if normalized(quote) in normalized(line)
            ]
            if matching and all(
                (
                    re.match(
                        "^\\s*(?:[-*]\\s*)?(?:\\*\\*)?Purpose(?:\\*\\*)?\\s*:",
                        line,
                        re.I,
                    )
                    for line in matching
                )
            ):
                continue
            if source.casefold() not in known:
                continue
            if getattr(getattr(self.a, "model", None), "config", {}).get(
                "semantic_inputs_before_execution", False
            ):
                if source.casefold() in {
                    n.casefold() for n in self.a.contract["targets"]
                }:
                    continue
                if purpose_quote(self.a.task["question"], quote):
                    continue
            if any(
                (
                    isinstance(c, str)
                    and re.search(
                        "(?<![A-Za-z0-9_])" + re.escape(c) + "(?![A-Za-z0-9_])",
                        quote,
                        re.I,
                    )
                    for c in columns
                )
            ):
                continue
            if not columns:
                continue
            choices.append(
                {"requirement": quote, "interface": known[source.casefold()]}
            )
        return choices

    def offer(self, bindings, assigned, admit=False):
        choices = self.choices(bindings)
        keys = []
        if choices:
            payload = {
                "original_task": self.a.task["question"],
                "requirements": choices,
                "output_interfaces": {
                    n: {
                        k: v
                        for (k, v) in t.items()
                        if k in {"name", "path", "columns", "grain"}
                    }
                    for (n, t) in self.a.contract["targets"].items()
                },
                "assigned_implementation_models": assigned,
                "resolution_only": getattr(
                    getattr(self.a, "model", None), "config", {}
                ).get("semantic_inputs_before_execution", False),
            }
            identity = {
                k: v
                for (k, v) in payload.items()
                if not payload["resolution_only"]
                or k not in {"assigned_implementation_models", "output_interfaces"}
            }
            key = hashlib.sha256(
                json.dumps(identity, sort_keys=True, default=str).encode()
            ).hexdigest()
            keys = [key]
            if key not in self.jobs:

                async def resolve():
                    self.a.event(
                        "semantic_resolution_started",
                        key=key,
                        requirements=[c["requirement"] for c in choices],
                        proposed_implementation_visible=False,
                    )
                    text = await self.a.model.ask(
                        "native_semantics",
                        [
                            {
                                "role": "system",
                                "content": INPUT_PROMPT
                                if payload["resolution_only"]
                                else PROMPT,
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    payload, ensure_ascii=False, default=str
                                ),
                            },
                        ],
                        raw=True,
                        output_cap=min(4096, 1024 + 1024 * len(choices)),
                    )
                    packet = json.loads(text)
                    if payload["resolution_only"]:
                        packet["checks"] = []
                    permitted = {c["requirement"] for c in choices}
                    if len(packet.get("checks", [])) > len(permitted) or any(
                        (
                            c.get("requirement") not in permitted
                            for c in packet.get("checks", [])
                        )
                    ):
                        raise ValueError(
                            "Semantic response expanded beyond its supplied construction requirements"
                        )
                    self.a.event(
                        "semantic_resolution_completed",
                        key=key,
                        resolved_bindings=packet.get("resolved_bindings"),
                        checks=packet.get("checks"),
                    )
                    return packet

                self.jobs[key] = asyncio.create_task(resolve())
        if admit:
            self.admitted.update(keys)
        return keys

    def install_ready(self, checks):
        installed = False
        for key in self.admitted - self.installed:
            job = self.jobs[key]
            if not job.done():
                continue
            packet = job.result()
            checks.install(packet["checks"])
            self.a.resolved_semantic_bindings = getattr(
                self.a, "resolved_semantic_bindings", {}
            )
            self.a.resolved_semantic_bindings[key] = packet["resolved_bindings"]
            self.installed.add(key)
            installed = True
        return installed

    async def settle(self, checks):
        pending = [self.jobs[k] for k in self.admitted - self.installed]
        if pending:
            await asyncio.gather(*pending)
        return self.install_ready(checks)

    async def finish_packet(self, packet, assigned):
        keys = self.offer(packet.get("semantic_bindings", []), assigned, admit=True)
        if not keys:
            return packet
        resolved = []
        for key in keys:
            result = await self.jobs[key]
            resolved.extend(result["resolved_bindings"])
        proposed = {
            normalized(b["requirement"]): b
            for b in packet.get("semantic_bindings", [])
            if isinstance(b, dict) and b.get("requirement")
        }
        changed = []
        for binding in resolved:
            prior = proposed.get(normalized(binding["requirement"]))
            if not prior:
                continue
            old = {c.casefold() for c in prior.get("columns", [])}
            new = {c.casefold() for c in binding.get("columns", [])}
            if (
                prior.get("source", "").casefold() != binding["source"].casefold()
                or not old <= new
            ):
                changed.append(binding)
        if not changed:
            return packet
        files = {f["path"]: f["sql"] for f in packet["files"]}
        work = {
            "original_task": self.a.task["question"],
            "resolved_raw_inputs": changed,
            "proposed_bindings": [
                proposed[normalized(b["requirement"])] for b in changed
            ],
            "retained_proposed_sql": files,
            "assigned_models": assigned,
        }
        self.a.event(
            "source_binding_finish_started",
            models=assigned,
            bindings=changed,
            execution_started=False,
        )
        while True:
            text = await self.a.model.ask(
                "native_input_binding_finish",
                [
                    {"role": "system", "content": FINISH_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(work, ensure_ascii=False, default=str),
                    },
                ],
                raw=True,
                output_cap=4096,
            )
            edits = None
            try:
                edits = json.loads(text)["edits"]
                staged = dict(files)
                for edit in edits:
                    path = edit["path"]
                    if path not in staged or not edit["old"]:
                        raise ValueError(
                            "Input binding edits must address retained proposed source"
                        )
                    found = staged[path].count(edit["old"])
                    if found != edit["expected_matches"]:
                        raise ValueError(
                            f"Exact source span in {path} matched {found} times, expected {edit['expected_matches']}; original proposal retained"
                        )
                    staged[path] = staged[path].replace(edit["old"], edit["new"])
                break
            except (ValueError, TypeError, KeyError) as exc:
                work["binding_edit_error"] = str(exc)
                work["previous_proposed_edits"] = edits
                self.a.event(
                    "source_binding_edit_rejected",
                    error=str(exc),
                    original_implementation_retained=True,
                )
        self.a.event(
            "source_bindings_consumed",
            models=assigned,
            edits=len(edits),
            execution_started=False,
        )
        return {
            **packet,
            "files": [{"path": path, "sql": sql} for (path, sql) in staged.items()],
        }

    async def close(self):
        for job in self.jobs.values():
            if not job.done():
                job.cancel()
        if self.jobs:
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)


def support(agent):
    if not hasattr(agent, "semantic_support"):
        agent.semantic_support = SemanticSupport(agent)
    return agent.semantic_support
