import ast
import asyncio
import builtins
import contextvars
import copy
import hashlib
import inspect
import io
import json
import re
import threading
import time
import traceback
from .catalog import PreparedMetadata
from .model import GenerationTransportError, ModelCallBudgetExceeded
from .context import ContextBudgetExceeded

_statement_children = contextvars.ContextVar("statement_children", default=None)


class ContractRevision(RuntimeError):
    pass


class NativeStalled(RuntimeError):
    pass


def digest(value):
    if isinstance(value, PreparedMetadata):
        value = json.loads(
            json.dumps(dict(value), sort_keys=True, ensure_ascii=False, default=str)
        )
    elif callable(value):
        code = getattr(value, "__code__", None)
        value = (
            getattr(value, "__module__", ""),
            getattr(value, "__qualname__", ""),
            code.co_code.hex() if code else repr(value),
            repr(code.co_consts) if code else "",
        )
    elif isinstance(value, (ValueRef, CodeRef)):
        value = (type(value).__name__, value.name)
    else:
        from .checkpoint import signature

        try:
            value = signature(value)
        except (TypeError, ValueError, AttributeError):
            value = ("uncacheable", type(value).__qualname__, time.monotonic_ns())
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr).encode()
    ).hexdigest()


def describe(value, limit=600):
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            return {
                "kind": "DataFrame",
                "shape": list(value.shape),
                "columns": [
                    (str(k), str(v)) for (k, v) in list(value.dtypes.items())[:40]
                ],
                "column_count": len(value.columns),
                "more": "Inspect the retained object with ordinary Python; rows were not removed.",
            }
    except ImportError:
        pass
    if isinstance(value, (ValueRef, CodeRef)):
        return {"kind": type(value).__name__, "name": value.name}
    if callable(value):
        return {
            "kind": "function",
            "name": getattr(value, "__name__", type(value).__name__),
        }
    if inspect.ismodule(value):
        return None
    if isinstance(value, io.IOBase):
        return {
            "kind": type(value).__name__,
            "closed": value.closed,
            "representation": repr(value)[:limit],
        }
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return {
            "kind": type(value).__name__,
            "shape": list(value.shape),
            "dtype": str(value.dtype),
        }
    if isinstance(value, (list, tuple, dict, set)) and len(value) > 12:
        import reprlib

        renderer = reprlib.Repr()
        renderer.maxstring = limit
        renderer.maxother = limit
        text = renderer.repr(value)
        return {
            "kind": type(value).__name__,
            "length": len(value),
            "preview": text[:limit],
            "preview_complete": False,
            "more": "Complete data remain live; this is only an interface.",
        }
    text = repr(value)
    if len(text) <= limit and isinstance(
        value, (type(None), bool, int, float, str, list, tuple, dict)
    ):
        try:
            shown = json.loads(json.dumps(value, ensure_ascii=False))
            return {"kind": type(value).__name__, "value": shown}
        except (TypeError, ValueError):
            pass
    return {
        "kind": type(value).__name__,
        "length": len(value) if hasattr(value, "__len__") else None,
        "preview": text[:limit],
        "preview_complete": False,
        "more": "The complete object remains in the Python namespace.",
    }


def literal_source(text):
    source = text.strip()
    fenced = re.fullmatch("```(?:python)?\\s*\\n(.*?)\\n```", source, re.S)
    if fenced:
        source = fenced.group(1)
    if not source.strip():
        raise ValueError("Return executable Python source")
    return source


def bind_helper_imports(tree, helpers):
    resolved = []

    class BindHelperImports(ast.NodeTransformer):
        def visit_Import(self, node):
            statements = []
            for alias in node.names:
                if alias.name in helpers:
                    statements.append(
                        ast.copy_location(
                            ast.Assign(
                                [ast.Name(alias.asname or alias.name, ast.Store())],
                                ast.Subscript(
                                    ast.Name("__runtime_helpers__", ast.Load()),
                                    ast.Constant(alias.name),
                                    ast.Load(),
                                ),
                            ),
                            node,
                        )
                    )
                    resolved.append(alias.name)
                else:
                    statements.append(ast.copy_location(ast.Import([alias]), node))
            return statements

    return (
        ast.fix_missing_locations(BindHelperImports().visit(copy.deepcopy(tree))),
        resolved,
    )


class ValueRef:
    runtime: object
    name: str

    def __await__(self):
        return self.runtime.bridge(self.runtime.value(self.name)).__await__()


class CodeRef:
    runtime: object
    name: str

    def __await__(self):
        return self.runtime.bridge(self.runtime.code(self.name)).__await__()


class SessionExecutor:
    def __init__(self, name):
        self.loop = None
        self.jobs = set()
        self.closed = False
        ready = threading.Event()

        def serve():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()

            def task_factory(loop, coroutine, context=None):
                task = asyncio.Task(coroutine, loop=loop, context=context)
                children = (
                    context.get(_statement_children, None)
                    if context is not None
                    else _statement_children.get()
                )
                if children is not None:
                    children.add(task)
                    task.add_done_callback(children.discard)
                return task

            self.loop.set_task_factory(task_factory)
            self.loop.run_forever()
            self.loop.close()

        self.thread = threading.Thread(
            target=serve, name="ahead-native-" + name, daemon=True
        )
        self.thread.start()
        ready.wait()

    async def run(self, function, *args):
        if self.closed:
            raise RuntimeError("Native session is closed")

        async def invoke():
            value = function(*args)
            return await value if inspect.isawaitable(value) else value

        future = asyncio.run_coroutine_threadsafe(invoke(), self.loop)
        self.jobs.add(future)
        future.add_done_callback(self.jobs.discard)
        return await asyncio.wrap_future(future)

    async def run_statement(self, function, *args):

        async def scoped():
            children = set()
            token = _statement_children.set(children)
            try:
                value = function(*args)
                return await value if inspect.isawaitable(value) else value
            except BaseException:
                pending = [task for task in tuple(children) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                raise
            finally:
                _statement_children.reset(token)

        return await self.run(scoped)

    async def close(self):
        if self.closed:
            return
        self.closed = True

        async def shutdown():
            current = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        stopped = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        await asyncio.wrap_future(stopped)
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self.thread.join)


class Session:
    def __init__(self, runtime, name, initial=None, executor=None):
        self.runtime = runtime
        self.name = name
        self.ns = dict(initial or {})
        self.output = []
        self.displayed_values = []
        self.definitions = {}
        self.revision = 0
        self.last = None
        self.cells = []
        self.source_versions = {}
        self.effective_source = None
        self.owns_executor = executor is None
        self.executor = executor or SessionExecutor(digest(name)[:8])
        self.ns.update(
            print=self.print,
            shell=self.shell,
            list_files=runtime.list_files,
            preview=self.preview,
            observation=runtime.observation,
            definition=self.definition,
            edit_definition=self.edit_definition,
            cell_source=self.cell_source,
            cell_output=self.cell_output,
            search_output=self.search_output,
            data_root=runtime.task["data_root"],
            workspace=str(runtime.workspace),
        )
        self.ns["__display_value__"] = self.display_value
        if runtime.sql:
            self.ns["query"] = runtime.sql.query
        if hasattr(runtime, "gaps"):
            self.ns["__native_gap__"] = runtime.gaps.call
        if hasattr(runtime, "evidence"):
            runtime.bind_session(self)

    def cell_source(self, cell, start=0, length=None):
        source = next((c["source"] for c in self.cells if c["cell"] == cell))
        return source[start:] if length is None else source[start : start + length]

    def cell_output(self, cell, start=0, length=4000, channel="stdout"):
        record = next((c for c in self.cells if c["cell"] == cell))
        return self.runtime.observation(record["observation"], start, length, channel)

    def search_output(
        self, cell, text, channel="stdout", radius=240, limit=12, start=0
    ):
        if not isinstance(text, str) or not text:
            raise ValueError("Search needs nonempty text")
        record = next((c for c in self.cells if c["cell"] == cell))
        full = self.runtime.observations[record["observation"]][channel] or ""
        if radius < 0 or limit < 1 or start < 0:
            raise ValueError("Search bounds must be nonnegative and limit positive")
        matches = list(re.finditer(re.escape(text), full[start:], re.IGNORECASE))
        hits = []
        for match in matches[:limit]:
            found = start + match.start()
            left = max(0, found - radius)
            right = min(len(full), start + match.end() + radius)
            hits.append({"start": left, "match_start": found, "text": full[left:right]})
        return {
            "cell": cell,
            "channel": channel,
            "hits": hits,
            "characters": len(full),
            "next_start": start + matches[limit - 1].end()
            if len(matches) > limit
            else None,
        }

    def record_cell(self, source, outcome):
        self.cells.append(
            {"cell": self.revision, "source": source, "observation": outcome["id"]}
        )

    def program_view(self):
        result = []
        for cell in self.cells:
            observation = self.runtime.observations[cell["observation"]]
            entry = {
                "cell": cell["cell"],
                "state": "execution_error" if observation["error"] else "executed",
            }
            for field, value in [
                ("source", cell["source"]),
                ("stdout", observation["stdout"]),
                ("error", observation["error"] or ""),
            ]:
                reader = (
                    f"cell_source({cell['cell']}, start={{start}}, length=4000)"
                    if field == "source"
                    else f"cell_output({cell['cell']}, start={{start}}, length=4000, channel={field!r})"
                )
                entry[field] = {
                    "text": value,
                    "characters": len(value),
                    "complete": True,
                    "read": reader.format(start=0),
                    "next_read": None,
                }
            result.append(entry)
        return result

    def print(self, *args, sep=" ", end="\n", file=None, flush=False):
        if file is not None:
            return builtins.print(*args, sep=sep, end=end, file=file, flush=flush)
        text = sep.join((str(x) for x in args)) + end
        self.output.append(text)
        if hasattr(self.runtime, "record_display"):
            self.runtime.record_display(self, text)

    def display_value(self, value):
        if value is not None and (
            not any((value is prior for prior in self.displayed_values))
        ):
            self.print(value)

    def shell(self, command):
        output = self.runtime.shell(command)
        self.print(output, end="" if output.endswith("\n") else "\n")
        self.displayed_values.append(output)
        return output

    def preview(self, value, rows=5, columns=None):
        self.print(json.dumps(describe(value), ensure_ascii=False, default=str))
        if hasattr(value, "head"):
            selected = value[list(columns)] if columns else value
            self.print(selected.head(rows).to_string())
            self.print("Preview only; the complete object remains live.")
        else:
            self.print(
                repr(value)[:4000],
                "[preview; inspect the retained object for the rest]",
            )

    def definition(self, name):
        return self.definitions[name]

    def edit_definition(self, name, old, new):
        if hasattr(self.runtime, "gaps") and self.runtime.gaps.edit(
            self, name, old, new
        ):
            self.revision += 1
            return
        source = self.definitions[name]
        if not old or source.count(old) != 1:
            raise ValueError("An edit must match exactly one existing source region")
        source = source.replace(old, new)
        exec(compile(source, "<edited:" + name + ">", "exec"), self.ns)
        self.definitions[name] = source
        self.revision += 1
        self.runtime.event(
            "native_definition_edited", node=name, source_hash=digest(source)
        )

    async def execute(self, code):
        self.output = []
        self.displayed_values = []
        self.revision += 1
        filename = "<" + self.name + ":" + str(self.revision) + ">"
        self.effective_source = code
        self.source_versions[filename] = code
        self.runtime.event(
            "native_cell_start",
            node=self.name,
            cell=self.revision,
            code_characters=len(code),
        )
        error = None
        capture = None
        if self.runtime.model.config.get("native_capture_library_output", False):
            from .stdio import begin_capture

            capture = begin_capture(self.output)
        try:
            tree = ast.parse(code)
            self.pending_source = (tree, code, filename)
            executable = (
                self.runtime.gaps.transform(tree, code, self)
                if hasattr(self.runtime, "gaps")
                else tree
            )
            helpers = {
                name: value
                for (name, value) in self.ns.items()
                if name in self.runtime.api_names and callable(value)
            }
            self.ns["__runtime_helpers__"] = helpers
            (executable, resolved) = bind_helper_imports(executable, helpers)
            if resolved:
                self.runtime.event(
                    "runtime_helper_import_resolved",
                    session=self.name,
                    names=resolved,
                    behavior="Bind the existing session helper; no Python module or external package was loaded",
                )
            executable = copy.deepcopy(executable)
            if executable.body and isinstance(executable.body[-1], ast.Expr):
                last = executable.body[-1]
                last.value = ast.Call(
                    ast.Name("__display_value__", ast.Load()), [last.value], []
                )
            ast.fix_missing_locations(executable)
            compiled = compile(
                executable, filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
            )
            self.runtime.event(
                "native_code_accepted",
                node=self.name,
                cell=self.revision,
                source_hash=digest(code),
            )
            if self is self.runtime.root and self.runtime.model.config.get(
                "native_resume", False
            ):
                await self.runtime.resumptions.execute(
                    self, executable, compiled, code, filename
                )
            else:
                await self.executor.run(eval, compiled, self.ns)
        except BaseException as exc:
            if isinstance(
                exc,
                (
                    asyncio.CancelledError,
                    ContractRevision,
                    GenerationTransportError,
                    ModelCallBudgetExceeded,
                    ContextBudgetExceeded,
                ),
            ):
                raise
            error = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        finally:
            if capture is not None:
                from .stdio import end_capture

                end_capture(capture)
        self.capture_definitions()
        self.pending_source = None
        self.last = self.runtime.save_observation(
            self.name, "".join(self.output), error
        )
        self.record_cell(self.effective_source or code, self.last)
        self.runtime.event(
            "native_cell_end",
            node=self.name,
            cell=self.revision,
            error=error,
            retained_variables=list(self.variables()),
        )
        return self.last

    def capture_definitions(self, source_context=None):
        context = source_context or getattr(self, "pending_source", None)
        if not context:
            return
        (tree, code, filename) = context
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = self.ns.get(item.name)
                if (
                    callable(fn)
                    and getattr(fn, "__code__", None)
                    and (fn.__code__.co_filename == filename)
                ):
                    retained = (
                        self.runtime.gaps.definition(self, item.name)
                        if hasattr(self.runtime, "gaps")
                        else None
                    )
                    self.definitions[item.name] = retained or ast.get_source_segment(
                        code, item
                    )

    def variables(self):
        result = {}
        for name, value in self.ns.items():
            if name.startswith("_") or name in self.runtime.api_names:
                continue
            item = describe(value)
            if item is not None:
                result[name] = item
        return result
