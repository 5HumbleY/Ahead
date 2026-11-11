import asyncio
import hashlib
import json
from pathlib import Path
from .continuation import Continuation
from .diagnostic_query import diagnostic_query
from .materialization_state import database_revision
from .sql_dependencies import sql_dependencies
from .repair_context import RepairContext
from .sql_context import constructor_facts, source_index
from .program_checks import ProgramChecks, CHECK_SCHEMA


def tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


STR = {"type": "string"}
TOOLS = [
    tool(
        "sql_apply",
        "Apply one coherent program change: new/replacement SQL files, exact versioned edits, and revisions to model-authored checks belong in the same action. Validate the whole change before committing; build the affected dependency closure once. Unchanged code, public obligations and successful results remain. Use empty arrays for unchanged parts. complete=true delivers only after every required output and public obligation passes.",
        {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": STR, "sql": STR},
                    "required": ["path", "sql"],
                    "additionalProperties": False,
                },
            },
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "model": STR,
                        "sha256": STR,
                        "old": STR,
                        "new": STR,
                        "expected_matches": {"type": "integer", "minimum": 1},
                    },
                    "required": ["model", "sha256", "old", "new", "expected_matches"],
                    "additionalProperties": False,
                },
            },
            "checks": {"type": "array", "items": CHECK_SCHEMA},
            "complete": {"type": "boolean"},
        },
        ["files", "edits", "checks", "complete"],
    ),
    tool(
        "sql_observe",
        "Inspect current original/generated SQL source ranges or execute read-only diagnostic SELECTs. Results and failures stay in this construction session across later edits.",
        {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "model": STR,
                        "start": {"type": "integer", "minimum": 1},
                        "lines": {"type": "integer", "minimum": 1},
                    },
                    "required": ["model", "start", "lines"],
                    "additionalProperties": False,
                },
            },
            "queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": STR, "sql": STR},
                    "required": ["name", "sql"],
                    "additionalProperties": False,
                },
            },
        },
        [],
    ),
    tool(
        "sql_recall",
        "Read complete retained diagnostic observations by id, without re-executing SQL. Database revision validity is explicit.",
        {"ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": STR}},
        ["ids"],
    ),
]
PROMPT = """Repair the remaining concrete failure in the retained analytical SQL program.
The requested outputs and complete source index are already bound. Use the actual
sql_apply and sql_observe tools; one tool call per response.
sql_apply commits files, exact versioned edits and check-expression corrections
together. Include every correction already justified by current evidence in
that same action; execution occurs once after the coherent change is installed.
Checks are fallible expressions tied to literal task clauses. Revise an invalid
check alongside its code, without weakening the original requirement or any
runtime-compiled public obligation. Request new observations only for genuinely
unresolved facts needed to choose the correction.

Decide a coherent region from the information needed to write it, not a fixed
number of files. When requirements and source meanings suffice, write the known
program through its consumers together. Define shared normalization/calculation
as helper models and use ref() where multiple consumers need that same meaning.
Helpers can live in models/_ahead_helpers/. Original inputs are immutable.
Do not recreate an existing compatible computation in each report. Different
populations or grains require explicit branches, not accidental reuse.

The requested program has already been constructed. Preserve its accepted
regions. A missing normalization or interpretation can remain a referenced helper
until its implementation is known. Use diagnostic SELECTs only to distinguish
the cause of the reported failure, then edit the responsible implementation.
Do not perform a new global survey or turn diagnostic SELECTs into helper models.
Preserve source entity keys, NULL populations,
units and requested time windows. A sample demonstrates a format, not universal
validity; counts over the complete source can establish a parsing/coverage rule.
Existing ref() schemas come from compiled_output_interface; raw source columns
and SQL text describe provenance and may differ from that existing relation.
Use actual DuckDB SQL and literal dbt ref()/source() references. Paths must match
requested outputs. Do not set schema/alias; runtime owns their physical routes.

sql_apply files are final SQL text, no Python, JSON-writing wrappers or escaped
code generators. It may implement several outputs and shared helpers in one turn,
or leave unresolved referenced models for subsequent construction. Runtime builds
dependency-ready definitions. Missing producers remain pending, never fabricated.

The session retains prior queries, edits, their observed results, current source
versions and validation failures. Read the change in actual counterexamples after
a patch. A failure is not an instruction to restart or only edit its reported
consumer: inspect the relevant producer/consumer definitions and original clauses.
Repair the cause in the shared computation; do not add compensating result totals
or overlapping fallback populations. sql_apply.edits replaces literal observed source spans and
rebuilds affected dependencies. Independent construction can proceed while another
region is unresolved. Missing/failed requirements still block final completion.

Source pages include their SHA256 and exact text. Copy the complete old expression
for an edit; compiled SQL error line numbers are not source-file coordinates.
Never interpret a partial
page as the whole file. sql_observe can obtain an exact missing range or several
independent queries in one call. Earlier query results identify the database
revision they measured; historical results explain changes, not current values.
Do not repeat known facts or full program source. End with complete=true on the
same program/edit action that establishes all requested artifacts; no separate
review or answer-packaging call is required. Successful compilation alone is not
analytical correctness: implement all original identity/population/formula clauses.
Never access other runs, reference answers or graders.
retained_queries is durable execution evidence, independent of the conversational
history. Reuse its results. sql_recall reads an indexed observation in full without
repeating data access. Historical values explain earlier code/data versions;
they are not silently treated as current after a database change. Once the
observed facts establish a correction, apply it to the responsible source rather
than probing equivalent totals again. The complete original task is retained.
"""
SYNTAX_PROMPT = """Repair the parser-invalid retained SQL source for the bound
execution backend. The engine has checked grammar without reading any data.
The complete current failing source and its SHA256 are supplied. Make the
smallest literal source edit that fixes its grammar; preserve all identifiers,
expressions, joins, filters, grouping, NULL handling and output interfaces.
Do not reconstruct the query from memory or change analytical meaning.
Only the active backend program remains: no inactive alternative branch should
be generated or repaired. Read the token before the reported error as well as
the token named by the parser. A valid-looking isolated subexpression does not
establish that the complete statement is syntactically valid.
Use sql_apply.edits with the exact observed source span and version, or its
files array when a coherent replacement is necessary. No data observation can resolve
a grammar error; this repair responsibility has no data-query tool. Runtime
rechecks grammar, executes affected code, and retains separate unresolved
analytical obligations. complete=true still requires the entire task to pass.
"""
COMPILE_PROMPT = """Repair the retained SQL for the concrete compiler diagnostic.
The database has rejected syntax, name/type binding, or aggregate/window scope
before it can execute this statement. Source data values cannot repair this
compiler error. The complete current source, observed interfaces, original task
and declared output grain are supplied. Use only code edits and addressed source
inspection; there is no data-query tool in this responsibility.

Find the expression and its actual SQL scope named by the diagnostic. Check all
CTEs, including expressions after an earlier aggregation. A correctly grouped
inner CTE does not make an outer aggregate with ungrouped columns valid. Preserve
the requested output grain and formulas. Do not silence errors by arbitrary
ANY_VALUE, dropping columns, collapsing populations, or adding a new aggregation
at the wrong grain. Reuse existing computed measures at their declared grain.
Make the smallest versioned literal edit that fixes the failing implementation.
Do not survey data or reproduce a query whose compiler diagnostic is already
known. sql_observe may read source pages only. Other independent semantic/data
obligations remain live and will be checked after compilation and execution.
"""


def compiler_failures(failures):
    result = []
    for failure in (failures or {}).get("failed_models", []):
        message = str(failure.get("message", ""))
        if (
            failure.get("condition") == "sql_syntax"
            or "Binder Error:" in message
            or "Parser Error:" in message
        ):
            result.append(failure)
    return result


def version(text):
    return hashlib.sha256(text.encode()).hexdigest()


def edit_sources(edits, sources):
    if any(("old" in edit or "new" in edit for edit in edits)):
        changed = {}
        for edit in edits:
            if set(edit) != {"model", "sha256", "old", "new", "expected_matches"}:
                raise ValueError(
                    "Literal edits require model, sha256, old, new and expected_matches"
                )
            name = edit["model"]
            if name not in sources or not sources[name].get("editable"):
                raise ValueError("Only generated program sources may change: " + name)
            source = sources[name]["sql"]
            if edit["sha256"] != version(source):
                raise ValueError(
                    "Stale source version for "
                    + name
                    + "; inspect current source before editing"
                )
            if (
                not isinstance(edit["old"], str)
                or not edit["old"]
                or (not isinstance(edit["new"], str))
            ):
                raise ValueError(
                    "Copy a nonempty exact old source span and supply literal replacement text"
                )
            if (
                type(edit["expected_matches"]) is not int
                or edit["expected_matches"] < 1
            ):
                raise ValueError("Expected matches must be positive")
            body = changed.get(name, source)
            actual = body.count(edit["old"])
            if actual != edit["expected_matches"]:
                raise ValueError(
                    f"Exact source span in {name} matched {actual} times, expected {edit['expected_matches']}; no files changed"
                )
            changed[name] = body.replace(edit["old"], edit["new"])
        return changed
    grouped = {}
    for edit in edits:
        name = edit["model"]
        if name not in sources or not sources[name].get("editable"):
            raise ValueError("Only generated program sources may change: " + name)
        source = sources[name]["sql"]
        lines = source.splitlines(keepends=True)
        if edit["sha256"] != version(source):
            raise ValueError(
                "Stale source version for "
                + name
                + "; inspect current source before editing"
            )
        (lo, hi) = (edit["start"], edit["end"])
        if (
            type(lo) is not int
            or type(hi) is not int
            or (not 1 <= lo <= hi <= len(lines))
        ):
            raise ValueError("Edit must address current source lines")
        grouped.setdefault(name, []).append((lo, hi, edit["sql"]))
    result = {}
    for name, parts in grouped.items():
        parts.sort()
        if any((a[1] >= b[0] for (a, b) in zip(parts, parts[1:]))):
            raise ValueError("Overlapping edits")
        lines = sources[name]["sql"].splitlines(keepends=True)
        for lo, hi, text in reversed(parts):
            lines[lo - 1 : hi] = [
                text + ("\n" if text and (not text.endswith("\n")) else "")
            ]
        result[name] = "".join(lines)
    return result


class SQLProgram:
    def __init__(self, agent):
        self.a = agent
        self.episode = Continuation()
        self.last = {}
        self.failures = None
        self.query_cache = {}
        self.history = []
        self.sources = {}
        self.complete = False
        self.query_observations = {}
        self.query_refs = {}
        self.blocked_executions = {}
        from .failure_state import FailureState

        self.obligations = FailureState()
        self.syntax_cache = {}
        self.syntax_episode = Continuation()
        self.requested_sources = set()
        from .action_progress import ActionProgress

        self.progress = ActionProgress()
        self.checks = ProgramChecks(agent)
        self.checks.install(getattr(agent, "entry_checks", []))
        self.refresh()

    def refresh(self):
        a = self.a
        sources = {}
        for binding in a.input_catalog.snapshot.get("project", {}).get(
            "named_bindings", []
        ):
            code = binding.get("source_code")
            path = binding.get("file")
            if code is None and path and Path(path).is_file():
                code = Path(path).read_text()
            if code is not None:
                sources[binding["model"]] = {
                    "path": path or binding["model"],
                    "sql": code,
                    "editable": False,
                }
        for name, path in a.produced.items():
            sources[name] = {
                "path": str(path),
                "sql": path.read_text(),
                "editable": True,
                "dependencies": sorted(sql_dependencies(path.read_text())),
            }
        for name, d in a.drafts.items():
            sources[name] = {
                "path": str(d["path"]),
                "sql": d["sql"],
                "editable": True,
                "dependencies": sorted(d["dependencies"]),
            }
        self.sources = sources
        relevant = []

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"path", "model", "id"} and isinstance(item, str):
                        name = (
                            Path(item).stem
                            if key == "path"
                            else item.rsplit(".", 1)[-1]
                        )
                        if name in sources and name not in relevant:
                            relevant.append(name)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(self.failures)
        for name in relevant:
            for parent in sources[name].get("dependencies", []):
                if parent in sources and parent not in relevant:
                    relevant.append(parent)
        ordered = {n: sources[n] for n in relevant}
        ordered.update(sources)
        previous = getattr(self, "context", None)
        self.relevant_sources = set(relevant)
        scoped = a.model.config.get("bound_program_repairs", False)
        self.context = RepairContext(
            ordered,
            a.model.config.get("artifact_repair_context_chars", 24000),
            expand=self.relevant_sources | self.requested_sources if scoped else None,
        )
        if previous:
            for name, page in previous.pages.items():
                if (
                    name not in relevant
                    and (not scoped or name in self.requested_sources)
                    and (name in self.context.index)
                    and (page["sha256"] == self.context.index[name]["sha256"])
                ):
                    candidate = {**self.context.pages, name: page}
                    if self.context.size(candidate) <= self.context.budget:
                        self.context.pages = candidate

    def state(self):
        a = self.a
        return {
            "program": {
                "required": list(a.contract["targets"]),
                "installed": list(a.produced),
                "pending_definitions": [
                    n
                    for n in a.contract["targets"]
                    if n not in a.produced and n not in a.drafts
                ],
                "drafts": {n: sorted(d["dependencies"]) for (n, d) in a.drafts.items()},
                "missing_producers": a.unresolved_dependencies(),
            },
            "current_failure": self.failures,
            "latest_execution_observation": self.last,
            "progress_feedback": self.progress.warning,
            "current_sources": source_index(self.context),
            "failure_sources": self.context.pages if self.failures else {},
            "program_requirement_observations": self.checks.view(),
            "proposed_semantic_bindings": {
                "entry": getattr(a, "entry_semantic_bindings", []),
                **getattr(a, "semantic_bindings", {}),
            },
            "independently_resolved_bindings": getattr(
                a, "resolved_semantic_bindings", {}
            ),
            "materialized_interfaces": {
                n: {
                    k: v
                    for (k, v) in a.receipts.get(str(p), {}).items()
                    if k in {"columns", "row_count", "rows", "physical_relation"}
                }
                for (n, p) in a.produced.items()
                if not a.model.config.get("bound_program_repairs", False)
                or n in self.relevant_sources
            },
        }

    def prepare_files(self, files):
        a = self.a
        staged = {}
        for item in files:
            if (
                set(item) != {"path", "sql"}
                or not isinstance(item["sql"], str)
                or (not item["sql"].strip())
            ):
                raise ValueError("Supply actual nonempty SQL text")
            if a.model.config.get("bound_program_repairs", False):
                from .bound_sql import bind_known

                original = item["sql"]
                item = {**item, "sql": bind_known(original)}
                if item["sql"] != original:
                    a.event(
                        "sql_environment_bound",
                        path=item["path"],
                        backend="duckdb",
                        original_characters=len(original),
                        bound_characters=len(item["sql"]),
                    )
            if a.model.config.get("observed_reference_lowering", False):
                from .sql_references import bind_observed_references

                item = {**item, "sql": bind_observed_references(a, item["sql"])}
            path = a.canonical_path(item["path"])
            name = path.stem
            if name in staged:
                raise ValueError("Duplicate model in one region")
            if (
                name in self.sources
                and Path(self.sources[name]["path"]).resolve() != path
            ):
                raise ValueError(
                    "An installed model keeps its observed path; edit that file"
                )
            deps = sql_dependencies(item["sql"])
            if name in deps:
                raise ValueError("Model refers to itself")
            staged[name] = {"path": path, "sql": item["sql"], "dependencies": deps}
        return staged

    def stage(self, files):
        staged = self.prepare_files(files)
        self.a.drafts.update(staged)
        self.a.event("sql_program_region_staged", models=list(staged))

    def apply_update(self, packet):
        edits = edit_sources(packet.get("edits", []), self.sources)
        files = list(packet.get("files", []))
        if {Path(f["path"]).stem for f in files} & set(edits):
            raise ValueError("A model is replaced or edited in one update, not both")
        files.extend(
            ({"path": self.sources[n]["path"], "sql": s} for (n, s) in edits.items())
        )
        staged = self.prepare_files(files)
        checks = self.checks.prepare(packet.get("checks", []), extra_models=staged)
        self.a.drafts.update(staged)
        self.checks.definitions.update(checks)
        self.a.event(
            "sql_program_update_applied",
            models=list(staged),
            checks=list(checks),
            atomic=True,
        )

    async def build(self):
        if self.a.model.config.get("bound_program_repairs", False):
            return await self.build_bound()
        a = self.a
        ready = a.ready_drafts()
        if ready:
            a.accept_files({str(d["path"]): d["sql"] for d in ready.values()})
            for name in ready:
                a.drafts.pop(name)
        if not a.produced:
            return {"built": [], "missing_producers": a.unresolved_dependencies()}
        self.refresh()

        def closure(name, seen=None):
            seen = set() if seen is None else seen
            if name in seen:
                return seen
            seen.add(name)
            for parent in self.sources.get(name, {}).get("dependencies", []):
                closure(parent, seen)
            return seen

        def signature(name):
            return tuple(
                sorted(
                    (
                        (n, version(self.sources[n]["sql"]))
                        for n in closure(name)
                        if n in self.sources
                    )
                )
            )

        if (
            a.sql.owned_database_revision is not None
            and database_revision(a.sql.database) != a.sql.owned_database_revision
        ):
            self.blocked_executions.clear()
        active_failures = {
            n: stamp
            for (n, stamp) in self.blocked_executions.items()
            if signature(n) == stamp
        }
        self.blocked_executions = active_failures
        blocked = {n for n in a.produced if closure(n) & set(active_failures)}
        goals = [
            a.output_goal(n, p) for (n, p) in a.produced.items() if n not in blocked
        ]
        prior_failure = self.failures if blocked else None
        if not goals:
            return {
                "passed": False,
                "deferred_unchanged_failures": sorted(blocked),
                "failure": self.failures,
            }
        try:
            a.configure_outputs()
            result = await a.sql.materialize_files(goals, install_dependencies=False)
            a.receipts.update(result["receipts"])
            for name in blocked:
                a.receipts.pop(str(a.produced[name]), None)
            a.check_public_contracts(a.receipts, available=set(a.produced) - blocked)
            await self.checks.run()
            self.failures = prior_failure
            a.event(
                "sql_program_execution",
                passed=not blocked,
                models=[n for n in a.produced if n not in blocked],
                deferred=sorted(blocked),
                execution=a.sql.operations[-1] if a.sql.operations else None,
            )
            return {
                "passed": not blocked,
                "built_or_reused": [n for n in a.produced if n not in blocked],
                "deferred_unchanged_failures": sorted(blocked),
                "missing_producers": a.unresolved_dependencies(),
            }
        except Exception as exc:
            from .model import GenerationTransportError, ModelCallBudgetExceeded
            from .context import ContextBudgetExceeded

            if isinstance(
                exc,
                (
                    GenerationTransportError,
                    ModelCallBudgetExceeded,
                    ContextBudgetExceeded,
                ),
            ):
                raise
            self.failures = a.build_failure(exc)
            if self.failures.get("condition") == "artifact_build_or_check":
                for item in self.failures.get("failed_models", []):
                    name = (
                        Path(item["path"]).stem
                        if item.get("path")
                        else item.get("id", "").rsplit(".", 1)[-1]
                    )
                    if name in self.sources:
                        self.blocked_executions[name] = signature(name)
            if prior_failure:
                self.failures = {
                    "condition": "independent_program_failures",
                    "failed_models": prior_failure.get("failed_models", [])
                    + self.failures.get("failed_models", []),
                    "observations": [prior_failure, self.failures],
                }
            a.event(
                "sql_program_execution",
                passed=False,
                failure=self.failures,
                execution=a.sql.operations[-1] if a.sql.operations else None,
            )
            return {"passed": False, "failure": self.failures}

    async def build_bound(self):
        from .bound_sql import syntax_observation
        from .model import GenerationTransportError, ModelCallBudgetExceeded
        from .context import ContextBudgetExceeded

        a = self.a
        ready = a.ready_drafts()
        if ready:
            a.accept_files({str(d["path"]): d["sql"] for d in ready.values()})
            for name in ready:
                a.drafts.pop(name)
        if not a.produced:
            return {"built": [], "missing_producers": a.unresolved_dependencies()}
        self.refresh()

        def closure(name, seen=None):
            seen = set() if seen is None else seen
            if name in seen:
                return seen
            seen.add(name)
            for parent in self.sources.get(name, {}).get("dependencies", []):
                closure(parent, seen)
            return seen

        def signature(name):
            names = closure(name) if name else self.sources
            return tuple(
                sorted(
                    (
                        (n, version(self.sources[n]["sql"]))
                        for n in names
                        if n in self.sources
                    )
                )
            )

        if (
            a.sql.owned_database_revision is not None
            and database_revision(a.sql.database) != a.sql.owned_database_revision
        ):
            self.obligations.entries.clear()
        self.obligations.prune(signature, self.checks.definitions)
        syntax = []
        for name, path in a.produced.items():
            source = self.sources[name]["sql"]
            key = version(source)
            if key not in self.syntax_cache:
                self.syntax_cache[key] = await a.sql.executor.run(
                    syntax_observation, source
                )
            observed = self.syntax_cache[key]
            if observed.get("valid") is False:
                syntax.append(
                    {
                        "path": str(path),
                        "condition": "sql_syntax",
                        "diagnostic": observed,
                    }
                )
        self.obligations.replace(
            "syntax", set(a.produced), syntax, signature, self.checks.definitions
        )
        blocked = {n for n in a.produced if closure(n) & self.obligations.blocked()}
        available = set(a.produced) - blocked
        evaluated = available | ({""} if available == set(a.produced) else set())
        for name in blocked:
            a.receipts.pop(str(a.produced[name]), None)

        def failures(exc, phase):
            if isinstance(
                exc,
                (
                    GenerationTransportError,
                    ModelCallBudgetExceeded,
                    ContextBudgetExceeded,
                ),
            ):
                raise exc
            detail = a.build_failure(exc)
            values = detail.get("failed_models") or [
                {"condition": phase, "error": detail}
            ]
            self.obligations.replace(
                phase, evaluated, values, signature, self.checks.definitions
            )

        execution = None
        if available:
            try:
                a.configure_outputs()
                result = await a.sql.materialize_files(
                    [
                        a.output_goal(n, p)
                        for (n, p) in a.produced.items()
                        if n in available
                    ],
                    install_dependencies=False,
                )
                a.receipts.update(result["receipts"])
                self.obligations.replace(
                    "execution", evaluated, [], signature, self.checks.definitions
                )
            except Exception as exc:
                failures(exc, "execution")
            else:
                try:
                    a.check_public_contracts(a.receipts, available=available)
                except Exception as exc:
                    failures(exc, "public")
                else:
                    self.obligations.replace(
                        "public", evaluated, [], signature, self.checks.definitions
                    )
                try:
                    await self.checks.run()
                except Exception as exc:
                    failures(exc, "checks")
                else:
                    self.obligations.replace(
                        "checks", evaluated, [], signature, self.checks.definitions
                    )
            execution = a.sql.operations[-1] if a.sql.operations else None
        self.failures = self.obligations.failures()
        a.event(
            "sql_program_execution",
            passed=self.failures is None,
            models=sorted(available),
            deferred=sorted(blocked),
            failure=self.failures,
            execution=execution,
            syntax_checked_without_data=True,
            retained_obligations=len(self.obligations.entries),
        )
        return {
            "passed": self.failures is None,
            "built_or_reused": sorted(available),
            "deferred_unchanged_failures": sorted(blocked),
            "failure": self.failures,
            "missing_producers": a.unresolved_dependencies(),
        }

    async def execute_repair(self, code):
        from .sql_repair_code import evaluate
        from .source_text import literal_source
        import copy

        loop = asyncio.get_running_loop()
        observed = []
        printed = []
        sources = {
            n: {**s, "sha256": version(s["sql"])} for (n, s) in self.sources.items()
        }

        def query(sql):
            result = asyncio.run_coroutine_threadsafe(
                self.act(
                    {
                        "action": "sql_observe",
                        "sources": [],
                        "queries": [
                            {
                                "name": "repair_query_" + str(len(observed) + 1),
                                "sql": sql,
                            }
                        ],
                    }
                ),
                loop,
            ).result()
            value = result["queries"][0]
            observed.append(value)
            if value.get("error") or value.get("query_error"):
                raise ValueError(value.get("error") or value["query_error"])
            return copy.deepcopy(value)

        def recall(ident):
            if ident not in self.query_observations:
                raise ValueError("Use a retained query observation id")
            value = self.query_observations[ident]
            return copy.deepcopy(
                {
                    **value,
                    "current_database_revision": value.get("database_revision")
                    == database_revision(self.a.sql.database),
                }
            )

        try:
            update = await asyncio.to_thread(
                evaluate,
                literal_source(code),
                sources,
                query,
                recall,
                self.requested_sources.add,
                printed.append,
            )
        except Exception as exc:
            self.refresh()
            return {
                "repair_code_error": type(exc).__name__ + ": " + str(exc),
                "stdout": "".join(printed),
                "queries": observed,
                "program_update_committed": False,
            }
        if any(update.values()):
            result = await self.act({"action": "sql_apply", **update, "complete": True})
        else:
            self.refresh()
            result = {"queries": observed, "program_update_committed": False}
        self.a.event(
            "sql_repair_code_executed",
            queries=len(observed),
            updates={k: len(v) for (k, v) in update.items()},
            retained_model_continuations=0,
        )
        return {
            **result,
            "queries": observed,
            "stdout": "".join(printed),
            "program_update_committed": any(update.values()),
        }

    async def act(self, packet):
        a = self.a
        action = packet["action"]
        before = {
            n: version(s["sql"]) for (n, s) in self.sources.items() if s["editable"]
        }
        if action == "sql_repair":
            outcome = await self.execute_repair(packet["code"])
        elif action == "sql_observe":
            self.context.inspect(packet["sources"]) if packet["sources"] else None
            self.requested_sources.update((item["model"] for item in packet["sources"]))
            results = []
            revision = database_revision(a.sql.database)
            for q in packet["queries"]:
                key = json.dumps([q["sql"], revision], default=str)
                reused = key in self.query_cache
                if not reused:
                    try:
                        self.query_cache[key] = await a.sql.executor.run(
                            diagnostic_query, a.sql, q["sql"]
                        )
                    except Exception as exc:
                        self.query_cache[key] = {"sql": q["sql"], "error": str(exc)}
                if key not in self.query_refs:
                    self.query_refs[key] = "q" + str(len(self.query_refs))
                ident = self.query_refs[key]
                record = {
                    "observation_id": ident,
                    "name": q["name"],
                    "reused": reused,
                    "database_revision": revision,
                    **self.query_cache[key],
                }
                self.query_observations[ident] = record
                results.append(record)
            requested = {item["model"] for item in packet["sources"]}
            outcome = {
                "queries": results,
                "source_pages": {
                    name: page
                    for (name, page) in self.context.pages.items()
                    if name in requested
                },
            }
        elif action == "sql_recall":
            unknown = set(packet["ids"]) - set(self.query_observations)
            if unknown:
                raise ValueError(
                    "Use retained observation ids: " + repr(sorted(unknown))
                )
            revision = database_revision(a.sql.database)
            outcome = {
                "recalled_queries": {
                    ident: {
                        **self.query_observations[ident],
                        "current_database_revision": self.query_observations[ident][
                            "database_revision"
                        ]
                        == revision,
                    }
                    for ident in packet["ids"]
                },
                "executed_queries": 0,
            }
        elif action in {"sql_apply", "sql_program", "sql_edit", "sql_checks"}:
            self.apply_update(packet)
            outcome = await self.build()
            self.refresh()
            outcome["changed_versions"] = {
                n: {"before": before.get(n), "after": version(s["sql"])}
                for (n, s) in self.sources.items()
                if s["editable"] and before.get(n) != version(s["sql"])
            }
            if packet["complete"]:
                missing = set(a.contract["targets"]) - set(a.produced)
                if missing or a.drafts or self.failures:
                    outcome["completion_refused"] = {
                        "missing": sorted(missing),
                        "drafts": list(a.drafts),
                        "failed": self.failures is not None,
                    }
                else:
                    self.complete = True
        else:
            raise ValueError("Use the declared SQL program tools")
        self.last = outcome
        self.history.append({"action": packet, "outcome": outcome})
        (a.directory / "sql_program_history.json").write_text(
            json.dumps(self.history, indent=2, default=str)
        )
        return outcome

    async def repair_step(self, stable):
        a = self.a
        label = "native_sql_program"
        episode = self.episode
        prompt = PROMPT
        state = self.state()
        from .sql_repair_code import PROMPT as REPAIR_CODE_PROMPT

        prompt = REPAIR_CODE_PROMPT
        residual = a.model.config.get("residual_repair_context", False)
        if residual:
            from .repair_view import scoped_facts, scoped_state, observation_view

            stable = scoped_facts(
                stable, self.relevant_sources | self.requested_sources
            )
            state = scoped_state(state, self.requested_sources)
            state["retained_queries"] = observation_view(
                self.query_observations, database_revision(a.sql.database)
            )
        syntax = (
            compiler_failures(self.failures)
            if a.model.config.get("compiler_scoped_repair", False)
            else [
                f
                for f in (self.failures or {}).get("failed_models", [])
                if f.get("condition") == "sql_syntax"
            ]
        )
        if a.model.config.get("bound_program_repairs", False) and syntax:
            names = {Path(f["path"]).stem for f in syntax}
            label = (
                "native_sql_compile_repair"
                if a.model.config.get("compiler_scoped_repair", False)
                else "native_sql_syntax_repair"
            )
            episode = self.syntax_episode
            prompt = (
                COMPILE_PROMPT
                if label == "native_sql_compile_repair"
                else SYNTAX_PROMPT
            )
            stable = {
                "backend": "duckdb",
                "responsibility": "Repair grammar while preserving the retained analytical computation",
            }
            observations = [
                {
                    **f,
                    "diagnostic": {
                        k: v
                        for (k, v) in f.get("diagnostic", {}).items()
                        if k != "active_sql"
                    },
                }
                for f in syntax
            ]
            state = {
                "syntax_failures": observations,
                "sources": {
                    n: {
                        "sha256": version(self.sources[n]["sql"]),
                        "sql": self.sources[n]["sql"],
                        "path": self.sources[n]["path"],
                    }
                    for n in names
                },
                "latest_execution_observation": self.last
                if self.last.get("action_error")
                else None,
                "other_unresolved_obligations": len(
                    (self.failures or {}).get("failed_models", [])
                )
                - len(syntax),
            }
            if label == "native_sql_compile_repair":
                stable = {
                    "backend": "duckdb",
                    "original_task": a.task["question"],
                    "output_contracts": {
                        n: a.contract["targets"].get(n, {}) for n in names
                    },
                }
                state["source_index"] = source_index(self.context)
                state["independently_resolved_bindings"] = getattr(
                    a, "resolved_semantic_bindings", {}
                )
                from .semantic_support import interface_index
                from .sql_catalog_view import compact_sql_catalog

                state["original_interfaces"] = compact_sql_catalog(
                    {
                        n: {
                            k: v
                            for (k, v) in item.items()
                            if k in {"name", "columns", "sql_reference"}
                        }
                        for (n, item) in interface_index(a).items()
                        if item.get("kind") != "requested_output_interface"
                    }
                )
                state["known_interfaces"] = {
                    n: {
                        k: v
                        for (k, v) in receipt.items()
                        if k in {"columns", "physical_relation"}
                    }
                    for (n, p) in a.produced.items()
                    if (receipt := a.receipts.get(str(p)))
                }
        a.event(
            "program_repair_routed",
            label=label,
            syntax_models=[Path(f["path"]).stem for f in syntax],
            context_characters=len(json.dumps(state, default=str)),
        )
        if label == "native_sql_program":
            state.get("current_sources", {})["read"] = (
                "source(model) returns complete current source; print a selected range when new source evidence is needed."
            )
            state.get("retained_queries", {})["access"] = (
                "recall(id) returns complete retained evidence; historical values are explicitly labelled."
            )
            project = stable.get("input_catalog", {}).get("project", {})
            if "source_access" in project:
                project["source_access"] = (
                    "source(model) reads the complete retained original/generated definition; query(sql) observes current data."
                )
            for binding in project.get("named_bindings", []):
                if "source_access" in binding:
                    binding["source_access"] = (
                        "source("
                        + repr(binding.get("model"))
                        + ") returns its complete retained SQL."
                    )
        if residual and label == "native_sql_program":
            messages = episode.render(prompt, stable, state, history_limit=1)
            measured = (
                await a.model.count_context(messages, label=label)
                if hasattr(a.model, "count_context")
                else None
            )
            kept = min(1, len(episode.turns))
        else:
            (messages, measured, kept) = await episode.render_fitting(
                a.model, prompt, stable, state, reserve_tokens=32768, label=label
            )
        if measured and measured["context_limit"] - measured["prompt_tokens"] < 16384:
            messages = episode.render(prompt, stable, state, history_limit=0)
            measured = await a.model.count_context(messages, label=label)
            if (
                measured
                and measured["context_limit"] - measured["prompt_tokens"] < 16384
            ):
                from .context import ContextBudgetExceeded

                raise ContextBudgetExceeded(
                    "SQL repair needs at least 16384 output tokens; indexed current state exceeds its input allowance"
                )
        text = None
        try:
            text = await a.model.ask(label, messages, raw=True)
            episode.remember(a.model, label, messages, text)
            packet = json.loads(text)
            if label in {"native_sql_syntax_repair", "native_sql_compile_repair"}:
                changed = {e["model"] for e in packet.get("edits", [])} | {
                    Path(f["path"]).stem for f in packet.get("files", [])
                }
                source_read = (
                    label == "native_sql_compile_repair"
                    and packet.get("action") == "sql_observe"
                    and packet.get("sources")
                    and (not packet.get("queries"))
                )
                if not source_read and (
                    packet.get("action") not in {"sql_apply", "sql_edit", "sql_program"}
                    or not changed
                    or (not changed <= names)
                    or packet.get("checks")
                ):
                    raise ValueError(
                        "Compiler repair edits only the supplied failing sources or reads their observed interfaces; data queries belong to data-dependent failures."
                    )
            outcome = await self.act(packet)
            self.progress.record(
                packet,
                {"stdout": outcome},
                {n: version(s["sql"]) for (n, s) in self.sources.items()},
            )
        except (ValueError, TypeError, KeyError) as exc:
            self.last = {"action_error": str(exc), "no_unrecorded_execution": True}
            if text:
                self.history.append({"unaccepted_action": text, "error": str(exc)})

    async def run(self):
        a = self.a
        a.existing_models = {
            n["name"]
            for n in a.sql.manifest.get("nodes", {}).values()
            if n.get("resource_type") == "model"
            and n["name"] not in a.contract["targets"]
        }
        stable = constructor_facts(
            a.task["question"],
            a.contract,
            a.input_catalog.snapshot,
            {
                "backend": "duckdb",
                "project": str(a.sql.project),
                "database": a.sql.database,
            },
        )
        limit = a.model.config.get("artifact_batch_files", 4)
        concurrency = a.model.config.get("model_concurrency", 2)
        regions = None
        if a.model.config.get("construction_regions", False):
            from .construction_regions import ConstructionRegions

            regions = ConstructionRegions(
                a.contract["targets"],
                [
                    n["model"]
                    for n in a.input_catalog.snapshot.get("project", {}).get(
                        "named_bindings", []
                    )
                ],
                a.model.config.get("max_completion_tokens", 32768),
                concurrency,
                work_tokens=a.model.config.get("construction_region_work_tokens"),
            )
        semantics = None
        if a.model.config.get("semantic_construction_dependencies", False):
            from .semantic_support import support

            semantics = support(a)
        jobs = {}
        initial = bool(a.drafts)

        async def construct(names, prefix):
            return await a.sql_batch(names, prefix, a.sql_feedback)

        try:
            while not self.complete:
                assigned = {n for (names, _) in jobs.values() for n in names}
                missing = a.unresolved_dependencies()
                pending = [
                    n
                    for n in dict.fromkeys(missing + list(a.contract["targets"]))
                    if n not in assigned and n not in a.produced and (n not in a.drafts)
                ]
                while pending and len(jobs) < concurrency:
                    names = (
                        (
                            regions.select_balanced(pending, concurrency - len(jobs))
                            if a.model.config.get(
                                "balanced_construction_regions", False
                            )
                            else regions.select(pending)
                        )
                        if regions
                        else pending[:limit]
                    )
                    pending = [n for n in pending if n not in names]
                    if regions:
                        a.event(
                            "construction_region_selected",
                            targets=names,
                            estimated_output_tokens=sum(
                                (regions.cost.get(n, 768) for n in names)
                            ),
                            basis="estimated_remaining_work_balance"
                            if a.model.config.get(
                                "balanced_construction_regions", False
                            )
                            else "declared_dependency_and_shared_source",
                            completion_limit=a.model.config.get(
                                "max_completion_tokens", 32768
                            ),
                        )
                    a.batch_number += 1
                    prefix = "_ahead_b" + str(a.batch_number) + "_"
                    job = asyncio.create_task(construct(names, prefix))
                    jobs[job] = (names, prefix)
                if initial:
                    initial = False
                    self.last = await self.build()
                    self.refresh()
                    continue
                if jobs:
                    (done, _) = await asyncio.wait(
                        jobs, return_when=asyncio.FIRST_COMPLETED
                    )
                    staged = []
                    for job in sorted(done, key=lambda j: jobs[j][1]):
                        (names, prefix) = jobs.pop(job)
                        try:
                            packet = await job
                            a.stage(packet, names, prefix)
                            self.checks.install(packet.get("checks", []))
                            staged.extend(
                                (Path(f["path"]).stem for f in packet["files"])
                            )
                            a.sql_feedback = None
                        except (ValueError, TypeError, KeyError) as exc:
                            a.sql_feedback = {
                                "invalid_batch": str(exc),
                                "assigned": names,
                            }
                    if semantics:
                        semantics.install_ready(self.checks)
                    if staged:
                        self.last = await self.build()
                    self.refresh()
                    continue
                if semantics and await semantics.settle(self.checks):
                    self.last = await self.build()
                    self.refresh()
                if a.drafts:
                    self.last = {
                        "pending_program": {
                            n: sorted(d["dependencies"]) for (n, d) in a.drafts.items()
                        },
                        "required_action": "Resolve the retained dependency cycle or missing implementation; preserve known consumers.",
                    }
                if self.failures or a.drafts:
                    await self.repair_step(stable)
                    continue
                if set(a.contract["targets"]) <= set(a.produced):
                    self.complete = True
                else:
                    raise RuntimeError(
                        "No construction job covers the remaining SQL targets"
                    )
        finally:
            if semantics:
                await semantics.close()
            for job in jobs:
                if not job.done():
                    job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
        a.validation_receipts = a.receipts
        a.event("task_artifacts_verified", models=list(a.produced))
        return a.receipts
