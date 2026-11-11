import __future__
import ast
from .model import GenerationTransportError, ModelCallBudgetExceeded
from .context import ContextBudgetExceeded
import copy
import inspect
import json
import traceback
from .python_context import excerpt, interface
from .native import Session, digest, ContractRevision, NativeStalled, literal_source
from .observed_sources import supported_probe
from . import file_inputs
from .catalog import PreparedMetadata
from .api_profiles import failure_interfaces
from .native_tools import NativeToolProtocolError

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["function", "file", "statement"],
                    },
                    "target": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["kind", "target", "old", "new"],
                "additionalProperties": False,
            },
        },
        "inspection_code": {"type": "string"},
        "run_result": {"type": "boolean"},
    },
    "required": ["edits", "inspection_code", "run_result"],
    "additionalProperties": False,
}
PATCH_PROMPT = """Repair the active region of a suspended analytical program.
Return {"edits":[{"kind":"function|file|statement","target":"name or path",
"old":"exact source substring","new":"replacement source"}],
"inspection_code":"optional focused Python inspection", "run_result":true}.
The caller and its already authored suffix are retained. Successful prefix work
will not run again. Fix the displayed operation against the ORIGINAL task;
preserve the calculations, population, units and requested output. The producer
source and true error are supplied. Do not invent a new global solution or weaken
the result to make execution succeed. old/new are literal source in JSON, not
Python code encoding another patch. Use the smallest coherent edit.

Use a function edit for its actual public function name, a file edit for an
actual file, or a statement edit targeting active_statement. The statement kind
is available only at a top-level checkpoint. Do not rewrite the retained suffix.
If evidence is missing, return no edits, inspection_code using the bound helpers
read_arg(name), read_failed_local(frame, name), inspect_api or actual file/data
readers, and run_result=false. This focused inspection executes on the original
Python thread. You receive its real outcome before another repair decision.
Do not call the failed producer or finish in an inspection.

run_result=true authorizes ONE retry of the failed function/statement with the
same live inputs. Review its internal earlier effects: a failed region is not
rolled back. Repair or account for such effects before retry, or decline with
run_result=false and no inspection. Outer successful statements and the caller
are never replayed. On success, the original suffix executes directly without
another model call. Never access graders, references, other runs or model APIs.
"""
SOURCE_PATCH_PROMPT = """Repair the active region of the suspended analytical program.
Return executable Python source directly, not JSON or a quoted code string.
The real caller, successful prefix, live values and authored suffix are retained.
Fix the displayed operation against the original task; preserve calculations,
population, units and requested outputs. Do not invent a new global solution.

Use the already bound exact-edit functions:
edit_definition(name, old, new) edits one exact region of the existing producer.
edit_file(path, old, new) edits one exact region of an actual generated file.
edit_active_statement(old, new) edits the current top-level statement, when one
is supplied. Preserve one statement and do not rewrite its retained suffix.
old and new are literal Python strings; triple-quoted strings are suitable for
multiline source. Do not define a replacement only in this inspection namespace.

If information is missing, inspect with read_arg(name),
read_failed_local(frame, name), inspect_api or actual file/data readers. These
operations run on the original Python thread and return real observations.
Do not call the failed producer or finish here.

After the necessary edit or state repair, call retry_region() to authorize ONE
retry of the failed function/statement with its same live inputs. The retry
occurs only after this repair code completes successfully. Review earlier
effects inside the failed region: they were not rolled back. Outer successful
statements are never replayed. If a safe repair is not possible, call
decline_repair(). Without either call this response is a focused inspection and
you receive its result before another repair decision. Never access graders,
reference answers, other runs or model endpoints.
"""
TOOL_PATCH_PROMPT = SOURCE_PATCH_PROMPT.replace(
    "Return executable Python source directly, not JSON or a quoted code string.",
    "Call the python tool with actual Python source in code and an explicit boolean retry. The edit and inspection helpers below are Python bindings used inside that code, not separate tools.",
)
TOOL_PATCH_PROMPT = TOOL_PATCH_PROMPT.replace(
    """After the necessary edit or state repair, call retry_region() to authorize ONE
retry of the failed function/statement with its same live inputs. The retry
occurs only after this repair code completes successfully.""",
    """After the necessary edit or state repair, set retry=true in this SAME tool
call to authorize ONE retry with the same live inputs after the code succeeds.
Use retry=false for a focused inspection. Do not spend another model call merely
to request a retry after a completed edit.""",
)
TOOL_PATCH_PROMPT = TOOL_PATCH_PROMPT.replace(
    """If a safe repair is not possible, call
decline_repair(). Without either call this response is a focused inspection and
you receive its result before another repair decision.""",
    """If a safe repair is not possible, use empty code and retry=false. A nonempty
inspection with retry=false returns its actual result for another repair decision.""",
)
TOOL_PATCH_PROMPT += """
The actual pending consumer source is supplied. Preserve the value shape and
meaning needed by those consumers, not just execution of the isolated failing
line. A different source representation is not automatically a compatible
replacement. Prefer a compatible real operation or adapt its actual result to
the existing interface. If the required meaning cannot be preserved locally,
decline rather than silently invalidate the already-authored consumers.
"""
TOOL_PATCH_PROMPT = TOOL_PATCH_PROMPT.replace(
    """edit_active_statement(old, new) edits the current top-level statement, when one
is supplied. Preserve one statement and do not rewrite its retained suffix.""",
    """edit_active_statement(old, new) replaces the failing statement with the
coherent source needed there, including multiple statements when necessary.
At a caller checkpoint, remaining_source() returns the actual unexecuted source.
edit_remaining_source(old, new) edits that source, including affected following
consumers. It cannot touch already committed prefix statements. Use one coherent
edit to update related call sites or representations together. Both operations
compile the replacement before installing it.""",
)
TOOL_PATCH_PROMPT += """
Preserve the declared result binding and original task meaning when revising
remaining code. Fix program call sites or a local adapter when an assumed API
was wrong; do not mutate a library namespace merely to fabricate that API.
A producer-level repair can change its complete function definition. If the
caller's assumptions must change too, decline so its checkpoint can revise the
unexecuted caller region. Already committed prefix work remains retained.
"""


class Resumptions:
    def __init__(self, agent):
        self.agent = agent
        self.sequence = 0
        self.source_sequence = 0

    def _entries(self, tree, source, filename, flags):
        context = (ast.parse(source), source, filename)
        return [
            {
                "source": ast.get_source_segment(source, node) or ast.unparse(node),
                "node": ast.Module(body=[copy.deepcopy(node)], type_ignores=[]),
                "context": context,
                "flags": flags,
            }
            for node in tree.body
        ]

    def _prepare_remaining(self, owner, source, flags):
        from .native import bind_helper_imports

        raw = ast.parse(source)
        tree = self.agent.gaps.transform(raw, source, owner)
        (tree, _) = bind_helper_imports(tree, owner.ns["__runtime_helpers__"])
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body[-1]
            last.value = ast.Call(
                ast.Name("__display_value__", ast.Load()), [last.value], []
            )
        ast.fix_missing_locations(tree)
        self.source_sequence += 1
        filename = (
            "<"
            + owner.name
            + ":"
            + str(owner.revision)
            + ":pending:"
            + str(self.source_sequence)
            + ">"
        )
        compiled = compile(tree, filename, "exec", flags=flags)
        for name in __future__.all_feature_names:
            flag = getattr(__future__, name).compiler_flag
            flags |= compiled.co_flags & flag
        owner.source_versions[filename] = source
        path = self.agent.directory / (
            "pending_source_" + str(self.source_sequence) + ".py"
        )
        path.write_text(source)
        entries = self._entries(tree, source, filename, flags)
        return (entries, str(path))

    @staticmethod
    def _remaining_text(entries):
        return "\n".join((item["source"] for item in entries))

    async def execute(self, session, tree, compiled, source, filename):
        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        for name in __future__.all_feature_names:
            flag = getattr(__future__, name).compiler_flag
            flags |= compiled.co_flags & flag
        pending = self._entries(tree, source, filename, flags)
        committed = []
        while pending:
            item = pending[0]
            region = {
                **item,
                "changed": False,
                "pending_consumer_source": self._remaining_text(pending[1:]),
                "remaining_source": self._remaining_text(pending),
                "replacement": None,
                "committed_statements": len(committed),
            }
            try:
                session.active_region = region
                code = compile(
                    item["node"], item["context"][2], "exec", flags=item["flags"]
                )
                await session.executor.run_statement(eval, code, session.ns)
                session.capture_definitions(item["context"])
                committed.append(item["source"])
                pending.pop(0)
                self.agent.event(
                    "native_statement_committed",
                    session=session.name,
                    cell=session.revision,
                    statement=len(committed) - 1,
                    source_hash=digest(item["source"]),
                )
            except Exception as exc:
                if isinstance(
                    exc,
                    (
                        ContractRevision,
                        NativeStalled,
                        GenerationTransportError,
                        ModelCallBudgetExceeded,
                        ContextBudgetExceeded,
                    ),
                ):
                    raise
                accepted = False
                if self.agent.model.config.get("native_resume_root_repair", True):
                    accepted = await self.repair(session, exc, region=region)
                else:
                    session.failed_continuation = {
                        "committed_statements": len(committed),
                        "failed_statement": item["source"],
                        "remaining_source": self._remaining_text(pending),
                        "note": "Committed prefix is retained. Repair this remaining region in the same root scope; do not replay prior successful statements.",
                    }
                if not accepted:
                    self.agent.event(
                        "native_continuation_retained",
                        cell=session.revision,
                        failed_statement=len(committed),
                        remaining=self._remaining_text(pending),
                    )
                    raise
                if region["replacement"] is not None:
                    pending = region["replacement"]
                else:
                    pending[0] = {
                        key: region[key]
                        for key in ("source", "node", "context", "flags")
                    }
                self.agent.event(
                    "native_statement_retry",
                    cell=session.revision,
                    statement=len(committed),
                )
            finally:
                session.effective_source = "\n".join(
                    committed + [item["source"] for item in pending]
                )
                session.active_region = None

    async def call(self, key, arguments, fn):
        agent = self.agent
        spec = agent.gaps.specs[key]
        repair_attempts = 0
        try:
            while True:
                if spec.get("namespace") is not None:
                    for name, value in spec["owner"].ns.items():
                        if name not in spec.get("protected_globals", set()):
                            spec["namespace"][name] = value
                (pos, kw) = agent.gaps.bind(spec, arguments)
                try:
                    value = fn(*pos, **kw)
                    value = await value if inspect.isawaitable(value) else value
                    spec.pop("failed_arguments", None)
                    spec.pop("failed_call_arguments", None)
                    spec.pop("repair_resume", None)
                    return value
                except Exception as exc:
                    if isinstance(
                        exc,
                        (
                            ContractRevision,
                            NativeStalled,
                            GenerationTransportError,
                            ModelCallBudgetExceeded,
                            ContextBudgetExceeded,
                        ),
                    ):
                        raise
                    spec["failed_arguments"] = digest(arguments)
                    spec["failed_call_arguments"] = dict(arguments)
                    agent.event(
                        "demand_execution_failed",
                        name=spec["name"],
                        key=key,
                        replayed=False,
                    )
                    if getattr(exc, "ahead_repair_declined", False):
                        raise
                    repair_attempts += 1
                    if repair_attempts > agent.model.config.get(
                        "native_repair_max_turns", 48
                    ):
                        exc.ahead_repair_declined = True
                        raise
                    accepted = await agent.bridge(
                        self.repair(spec["owner"], exc, key=key, arguments=arguments)
                    )
                    if not accepted:
                        exc.ahead_repair_declined = True
                        raise
                    spec.pop("repair_resume", None)
                    fn = spec["implementation"]
                    agent.event(
                        "demand_repaired_invocation_resumed",
                        name=spec["name"],
                        key=key,
                        caller_retained=True,
                    )
        finally:
            agent.event("demand_call_returned", name=spec["name"], key=key)

    async def repair(self, owner, exc, *, key=None, arguments=None, region=None):
        agent = self.agent
        self.sequence += 1
        frames = []
        apis = []
        tb = exc.__traceback__
        while tb:
            frame = tb.tb_frame
            if frame.f_code.co_filename.startswith(
                ("<root:", "<demand:", "<demand-edit:", "<edited:")
            ):
                frames.append(
                    {
                        "name": frame.f_code.co_name,
                        "line": tb.tb_lineno,
                        "locals": dict(frame.f_locals),
                    }
                )
                source = None
                if key and frame.f_code.co_filename.startswith(
                    ("<demand:", "<demand-edit:")
                ):
                    source = agent.gaps.specs[key].get("accepted_source")
                elif frame.f_code.co_filename in getattr(owner, "source_versions", {}):
                    source = owner.source_versions[frame.f_code.co_filename]
                elif (
                    getattr(owner, "pending_source", None)
                    and owner.pending_source[2] == frame.f_code.co_filename
                ):
                    source = owner.pending_source[1]
                elif frame.f_code.co_filename.startswith("<" + owner.name + ":"):
                    source = next(
                        (
                            cell["source"]
                            for cell in owner.cells
                            if "<" + owner.name + ":" + str(cell["cell"]) + ">"
                            == frame.f_code.co_filename
                        ),
                        None,
                    )
                elif frame.f_code.co_name in owner.definitions:
                    source = owner.definitions[frame.f_code.co_name]
                if source:
                    apis = (
                        failure_interfaces(
                            source, tb.tb_lineno, {**frame.f_globals, **frame.f_locals}
                        )
                        + apis
                    )
            tb = tb.tb_next
        if key:
            frames = [row for row in frames if row["name"] != "<module>"]
        elif region:
            referenced = {
                node.id
                for node in ast.walk(region["node"])
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            for row in frames:
                if row["name"] == "<module>":
                    row["locals"] = {
                        name: value
                        for (name, value) in row["locals"].items()
                        if name in referenced
                    }
        arguments = arguments or {}
        support = agent.gaps.specs[key]["support"] if key else None

        def read_arg(name):
            if support is not None:
                support["arguments"][name] = digest(arguments[name])
            return arguments[name]

        def read_failed_local(frame, name):
            if key:
                agent.gaps.specs[key]["cacheable"] = False
            return frames[frame]["locals"][name]

        local = Session(
            agent,
            "repair:" + str(self.sequence),
            {"read_arg": read_arg, "read_failed_local": read_failed_local},
            executor=owner.executor,
        )
        agent.sessions[local.name] = local
        if support is not None:
            local.construction_support = support

            def tracked(kind, path, **options):
                (value, metadata) = file_inputs.read_resource(kind, path, **options)
                support["files"][metadata["path"]] = metadata["sha256"]
                return value

            local.ns.update(
                read_source=lambda path, encoding="auto", errors="strict", start=0, length=None: (
                    tracked(
                        "text",
                        path,
                        encoding=encoding,
                        errors=errors,
                        start=start,
                        length=length,
                    )
                ),
                inspect_file=lambda path, delimiter=None, encoding="auto", errors="strict": (
                    tracked(
                        "profile",
                        path,
                        delimiter=delimiter,
                        encoding=encoding,
                        errors=errors,
                    )
                ),
                csv_rows=lambda path, delimiter=None, encoding="auto", errors="strict": (
                    tracked(
                        "rows",
                        path,
                        delimiter=delimiter,
                        encoding=encoding,
                        errors=errors,
                    )
                ),
                read_table=lambda path, header=0, delimiter=None, encoding="auto", errors="strict": (
                    tracked(
                        "table",
                        path,
                        header=header,
                        delimiter=delimiter,
                        encoding=encoding,
                        errors=errors,
                    )
                ),
            )
        agent.api_names.add("read_failed_local")
        local.ns.update(
            definition=owner.definition, edit_definition=owner.edit_definition
        )
        decision = {}

        def remaining_source():
            if region is None:
                raise ValueError(
                    "Remaining-source editing belongs to the caller checkpoint. Edit the producer definition here, or decline to escalate to its caller."
                )
            return region["remaining_source"]

        def install_remaining(changed):
            (entries, path) = self._prepare_remaining(owner, changed, region["flags"])
            old = region["remaining_source"]
            region.update(
                replacement=entries,
                remaining_source=self._remaining_text(entries),
                changed=True,
            )
            if entries:
                region.update(
                    {
                        key: entries[0][key]
                        for key in ("source", "node", "context", "flags")
                    }
                )
                region["pending_consumer_source"] = self._remaining_text(entries[1:])
            else:
                region.update(
                    source="",
                    node=ast.Module(body=[], type_ignores=[]),
                    pending_consumer_source="",
                )
            agent.event(
                "native_pending_program_replaced",
                session=owner.name,
                committed_statements=region["committed_statements"],
                old_source_hash=digest(old),
                new_source_hash=digest(changed),
                pending_statements=len(entries),
                source_file=path,
            )

        def edit_remaining_source(old, new):
            current = remaining_source()
            if not old and current or (old and current.count(old) != 1):
                raise ValueError(
                    "Match exactly one region of remaining_source(), or replace its full returned text"
                )
            install_remaining(current.replace(old, new, 1) if old else new)

        def edit_active_statement(old, new):
            if region is None:
                raise ValueError("No top-level active_statement")
            if not old or region["source"].count(old) != 1:
                raise ValueError("Statement edit must match exactly once")
            changed = region["source"].replace(old, new)
            tail = region["pending_consumer_source"]
            install_remaining(changed + ("\n" + tail if tail else ""))
            agent.event("suspended_statement_edited", source_hash=digest(changed))

        def retry_region():
            decision["retry"] = True

        def decline_repair():
            decision["retry"] = False

        local.ns.update(
            edit_active_statement=edit_active_statement, edit_file=agent.edit_file
        )
        agent.api_names.add("edit_active_statement")
        if region is not None:
            local.ns.update(
                remaining_source=remaining_source,
                edit_remaining_source=edit_remaining_source,
            )
            agent.api_names.update({"remaining_source", "edit_remaining_source"})
        if not agent.model.config.get("native_function_calls", False):
            local.ns.update(retry_region=retry_region, decline_repair=decline_repair)
            agent.api_names.update({"retry_region", "decline_repair"})
        scopes = {owner.name: owner, local.name: local}
        if key:
            constructor = agent.sessions.get(
                agent.gaps.specs[key].get("construction_session")
            )
            if constructor is not None:
                scopes[constructor.name] = constructor

        def read_scoped_evidence(ident):
            record = agent.evidence.records[ident]
            if record["owner"] not in scopes:
                raise ValueError(
                    "Observation is outside this producer and caller scope"
                )
            return agent.evidence.read(scopes[record["owner"]], ident)

        def scoped_index(match="", start=0, limit=40):
            entries = []
            for scope in scopes.values():
                entries.extend(
                    agent.evidence.index(scope, match=match, limit=100)["entries"]
                )
            return {
                "total": len(entries),
                "entries": entries[start : start + limit],
                "next_start": start + limit if start + limit < len(entries) else None,
            }

        local.ns.update(
            read_evidence=read_scoped_evidence,
            evidence_index=scoped_index,
            cell_source=owner.cell_source,
            cell_output=owner.cell_output,
            search_output=owner.search_output,
        )
        source_names = {row["name"] for row in frames}
        if key:
            source_names.add(agent.gaps.specs[key]["name"])
        if region:
            source_names.update(
                (n.id for n in ast.walk(region["node"]) if isinstance(n, ast.Name))
            )
        failure = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        feedback = None
        episode = None
        if agent.model.config.get("native_continuation_messages", False):
            from .continuation import Continuation

            parent = (
                scopes.get(agent.gaps.specs[key].get("construction_session"))
                if key
                else owner
            )
            parent = parent or owner
            seed = getattr(parent, "model_episode", None)
            episode = Continuation(seed=seed.latest_assistant() if seed else None)

        def repair_interface(name, value, frame=None):
            if isinstance(value, PreparedMetadata):
                return {
                    "kind": "construction_evidence",
                    "keys": list(value),
                    "read": f"read_arg({name!r})"
                    if frame is None
                    else f"read_failed_local({frame}, {name!r})",
                }
            return interface(value)

        literal = agent.model.config.get("native_literal_python", False)
        native_tools = agent.model.config.get("native_function_calls", False)
        prompt = (
            TOOL_PATCH_PROMPT
            if native_tools
            else SOURCE_PATCH_PROMPT
            if literal
            else PATCH_PROMPT
        )
        for repair_turn in range(agent.model.config.get("native_repair_max_turns", 48)):
            caller_region = region or getattr(owner, "active_region", None) or {}
            sources = {
                name: owner.definitions[name]
                for name in source_names
                if name in owner.definitions
            }
            if key:
                spec = agent.gaps.specs[key]
                sources[spec["name"]] = spec["accepted_source"]
            context = {
                "failure": excerpt(failure, 5000),
                "producer_source": sources,
                "active_statement": region["source"] if region else None,
                "caller_statement": caller_region.get("source"),
                "pending_consumer_source": caller_region.get(
                    "pending_consumer_source", ""
                ),
                "remaining_program_source": region["remaining_source"]
                if region
                else None,
                "committed_prefix_statements": region["committed_statements"]
                if region
                else None,
                "required_result_binding": getattr(agent, "program_goal", None),
                "caller_escalation": bool(
                    region and getattr(exc, "ahead_repair_declined", False)
                ),
                "argument_interfaces": {
                    name: repair_interface(name, value)
                    for (name, value) in arguments.items()
                },
                "failed_frames": [
                    {
                        "frame": i,
                        "function": row["name"],
                        "line": row["line"],
                        "locals": {
                            name: repair_interface(name, value, i)
                            for (name, value) in row["locals"].items()
                            if name not in arguments
                            and (not name.startswith("_"))
                            and (name not in agent.api_names)
                            and (not inspect.ismodule(value))
                            and (not callable(value))
                        },
                    }
                    for (i, row) in enumerate(frames)
                ],
                "inspection": feedback,
                "actual_failure_api_interfaces": apis[:3],
                "available_capabilities": agent.capabilities,
                "source_acquisition_failure": getattr(exc, "source_failure", None),
                "retained_evidence": [
                    {"session": scope.name, **agent.evidence.view(scope, budget=2000)}
                    for scope in scopes.values()
                    if scope is not local
                ],
                "runtime_api": {
                    name: str(inspect.signature(local.ns[name]))
                    for name in (
                        "read_arg",
                        "read_failed_local",
                        "inspect_api",
                        "read_source",
                        "read_file",
                        "inspect_file",
                        "inspect_database",
                        "query",
                        "edit_definition",
                        "edit_file",
                        "edit_active_statement",
                        "remaining_source",
                        "edit_remaining_source",
                        "retry_region",
                        "decline_repair",
                    )
                    if name in local.ns and callable(local.ns[name])
                },
                "retry_scope": "Only this failed function invocation"
                if key
                else "Only active_statement",
                "preserved": "Outer successful work and authored suffix remain in the original execution.",
            }
            repair_cap = agent.model.config.get("native_output_caps", {}).get(
                "repair", 4096
            )
            if episode:
                (messages, measured, retained) = await episode.render_fitting(
                    agent.model,
                    prompt,
                    {"original_task": agent.task["question"]},
                    context,
                    reserve_tokens=min(8192, repair_cap),
                    label="native_resume_patch" if native_tools else None,
                )
                agent.event(
                    "continuation_context_retained",
                    region=local.name,
                    available_turns=len(episode.turns),
                    retained_turns=retained,
                    prompt_tokens=measured["prompt_tokens"] if measured else None,
                    context_limit=measured["context_limit"] if measured else None,
                )
            else:
                messages = [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"original_task": agent.task["question"]},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(context, ensure_ascii=False, default=str),
                    },
                ]
            agent.event(
                "suspended_repair_requested",
                key=key,
                context_characters=len(messages[-1]["content"]),
            )
            try:
                text = await agent.model.ask(
                    "native_resume_patch", messages, raw=True, output_cap=repair_cap
                )
            except NativeToolProtocolError as exc:
                if episode:
                    episode.remember(agent.model, "native_resume_patch", messages, "")
                feedback = {"tool_call_error": str(exc)}
                continue
            if episode:
                episode.remember(agent.model, "native_resume_patch", messages, text)
            try:
                if literal:
                    repair_packet = json.loads(text) if native_tools else None
                    submitted = (
                        repair_packet["code"] if repair_packet is not None else text
                    )
                    if repair_packet is not None and (not submitted.strip()):
                        return repair_packet["retry"]
                    source = literal_source(submitted)
                    if key and (
                        not supported_probe(
                            ast.parse(source), agent.observed_sources.api_names
                        )
                    ):
                        agent.gaps.specs[key]["cacheable"] = False
                    decision.clear()
                    outcome = await local.execute(source)
                    record = agent.observations[outcome["id"]]
                    feedback = {
                        field: excerpt(record[field] or "", 5000)
                        for field in ("stdout", "error")
                    }
                    if outcome["error"]:
                        continue
                    if repair_packet is not None:
                        if repair_packet["retry"]:
                            return True
                        continue
                    if "retry" in decision:
                        return decision["retry"]
                    continue
                packet = json.loads(text)
                for edit in packet["edits"]:
                    if edit["kind"] == "function":
                        await owner.executor.run(
                            owner.edit_definition,
                            edit["target"],
                            edit["old"],
                            edit["new"],
                        )
                    elif edit["kind"] == "file":
                        agent.edit_file(edit["target"], edit["old"], edit["new"])
                    elif edit["kind"] == "statement":
                        if region is None or edit["target"] != "active_statement":
                            raise ValueError("No top-level active_statement")
                        edit_active_statement(edit["old"], edit["new"])
                    else:
                        raise ValueError("Unknown exact edit kind")
                if packet["inspection_code"]:
                    if key and (
                        not supported_probe(
                            ast.parse(packet["inspection_code"]),
                            agent.observed_sources.api_names,
                        )
                    ):
                        agent.gaps.specs[key]["cacheable"] = False
                    outcome = await local.execute(packet["inspection_code"])
                    record = agent.observations[outcome["id"]]
                    feedback = {
                        field: excerpt(record[field] or "", 5000)
                        for field in ("stdout", "error")
                    }
                    if outcome["error"]:
                        continue
                if packet["run_result"]:
                    return True
                if not packet["inspection_code"]:
                    return False
            except (ValueError, KeyError, TypeError, SyntaxError) as error:
                feedback = {
                    "repair_error": str(error),
                    "current_sources": {
                        name: owner.definitions[name]
                        for name in sources
                        if name in owner.definitions
                    },
                }
        agent.event(
            "suspended_repair_escalated",
            key=key,
            reason="Local repair allowance exhausted; original root continuation retained",
        )
        return False
