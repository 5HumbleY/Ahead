import contextvars

_PREFIX_OBSERVER = contextvars.ContextVar("model_prefix_observer", default=None)
import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import httpx
from .context import ContextBudgetExceeded
from .program_stream import ProgramStreamGuard, NativeStreamGuard


class GenerationTransportError(RuntimeError):
    pass


class ModelCallBudgetExceeded(RuntimeError):
    pass


class Model:
    def __init__(self, config, directory, concurrency=2, max_calls=None):
        self.config = config
        self.concurrency = concurrency
        self.max_calls = max_calls
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.budget = asyncio.Semaphore(concurrency)
        timeout = (
            httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
            if config.get("streaming_read_deadline_only", False)
            else config.get("request_timeout_seconds", 240)
        )
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self.calls = []
        self.last_native_metrics = {}
        self.context_count_cache = {}

    async def measure_thinking(self, label, body, data, record):
        budget = body.get("thinking_token_budget")
        if budget is None:
            return
        message = data["choices"][0]["message"]
        reasoning = message.get("reasoning") or ""
        metrics = {"limit": budget, "observed_text_tokens": None, "near_limit": False}
        started = time.perf_counter()
        try:
            if reasoning:
                url = urlsplit(self.config["inference_url"])
                endpoint = self.config.get(
                    "tokenize_url",
                    urlunsplit((url.scheme, url.netloc, "/tokenize", "", "")),
                )
                response = await self.client.post(
                    endpoint,
                    json={
                        "model": self.config["model"],
                        "prompt": reasoning,
                        "add_special_tokens": False,
                    },
                )
                response.raise_for_status()
                count = int(response.json()["count"])
            else:
                count = 0
            metrics.update(
                observed_text_tokens=count,
                near_limit=budget == 0 or count >= max(1, budget - 16),
            )
        except Exception as exc:
            metrics["measurement_error"] = str(exc)
        metrics["measurement_seconds"] = time.perf_counter() - started
        record["thinking_budget"] = metrics
        self.last_native_metrics[label] = metrics

    def save_record(self, record, path):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        temporary.replace(path)

    def begin_generation(self, record, path):
        if (
            self.max_calls is not None
            and sum((bool(x.get("generation_sent")) for x in self.calls))
            >= self.max_calls
        ):
            raise ModelCallBudgetExceeded(
                f"Model call budget exhausted: {self.max_calls}"
            )
        record.update(
            generation_sent=True, record_status="in_flight", request_sent_at=time.time()
        )
        self.save_record(record, path)

    def accounting(self):
        usages = [x.get("response", {}).get("usage", {}) for x in self.calls]
        return {
            "model_calls": len(self.calls),
            "generation_calls": sum(
                (bool(x.get("generation_sent")) for x in self.calls)
            ),
            "not_sent_requests": sum(
                (x.get("generation_sent") is False for x in self.calls)
            ),
            "preflight_rejections": sum(
                (x.get("error_type") == "ContextBudgetExceeded" for x in self.calls)
            ),
            "incomplete_usage_calls": sum(
                (
                    bool(x.get("generation_sent")) and (not u)
                    for (x, u) in zip(self.calls, usages)
                )
            ),
            "prompt_tokens": sum((u.get("prompt_tokens", 0) for u in usages)),
            "completion_tokens": sum((u.get("completion_tokens", 0) for u in usages)),
        }

    async def fit_context(self, body, record):
        if not self.config.get("context_limit"):
            return
        url = urlsplit(self.config["inference_url"])
        endpoint = self.config.get(
            "tokenize_url", urlunsplit((url.scheme, url.netloc, "/tokenize", "", ""))
        )
        payload = {
            k: body[k]
            for k in ("model", "messages", "chat_template_kwargs", "tools")
            if k in body
        }
        if self.config.get("vllm"):
            payload = copy.deepcopy(payload)
            for message in payload.get("messages", []):
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if part.get("type") == "image_url":
                        identity = json.dumps(part.get("image_url"), sort_keys=True)
                        part["uuid"] = (
                            "tokenize-only-"
                            + hashlib.sha256(identity.encode()).hexdigest()
                        )
        key = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        counts = self.context_count_cache.get(key)
        if counts is None:
            response = await self.client.post(endpoint, json=payload)
            response.raise_for_status()
            counts = response.json()
            if len(self.context_count_cache) >= 8:
                self.context_count_cache.pop(next(iter(self.context_count_cache)))
            self.context_count_cache[key] = counts
        else:
            record["context_count_reused"] = True
        prompt_tokens = int(counts["count"])
        limit = min(
            self.config["context_limit"],
            counts.get("max_model_len") or self.config["context_limit"],
        )
        requested = body["max_tokens"]
        available = limit - prompt_tokens
        record["token_budget"] = {
            "prompt_tokens": prompt_tokens,
            "context_limit": limit,
            "requested_output_cap": requested,
            "effective_output_cap": max(0, min(requested, available)),
        }
        if available <= 0:
            raise ContextBudgetExceeded(
                f"Context budget exhausted: {prompt_tokens} input tokens in a {limit}-token window"
            )
        body["max_tokens"] = min(requested, available)

    async def count_context(self, messages, label=None):
        if not self.config.get("context_limit"):
            return None
        body = {
            "model": self.config["model"],
            "messages": messages,
            "max_tokens": self.config.get("max_completion_tokens", 4096),
            "chat_template_kwargs": {
                "enable_thinking": self.config["thinking"],
                "reasoning_effort": self.config["reasoning_effort"],
            },
        }
        if label and self.config.get("native_function_calls", False):
            from .native_tools import tools_for

            declared = tools_for(label)
            if declared:
                body["tools"] = declared
        record = {}
        started = time.time()
        try:
            if self.config.get("model_api") == "responses":
                from .responses import make_request, fit_context, authorization

                await fit_context(
                    self,
                    make_request(self.config, body, False),
                    record,
                    authorization(self.config),
                )
            else:
                await self.fit_context(body, record)
        except ContextBudgetExceeded:
            pass
        result = record["token_budget"]
        with (self.directory / "context_measurements.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {"started_at": started, "seconds": time.time() - started, **result}
                )
                + "\n"
            )
        return result

    async def ask_with_prefix(
        self, label, messages, on_prefix, raw=False, output_cap=None
    ):
        token = _PREFIX_OBSERVER.set(on_prefix)
        try:
            return await self.ask(label, messages, raw=raw, output_cap=output_cap)
        finally:
            _PREFIX_OBSERVER.reset(token)

    async def ask(self, label, messages, raw=False, output_cap=None):
        if not self.config.get("isolate_inference_failures", False):
            return await self._ask_once(label, messages, raw, output_cap)
        attempts = self.config.get("generation_transport_retries", 1) + 1
        for attempt in range(attempts):
            try:
                return await self._ask_once(label, messages, raw, output_cap)
            except httpx.HTTPError as exc:
                status = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                retryable = status is None or status == 429 or status >= 500
                retry = retryable and attempt + 1 < attempts
                with (self.directory / "transport_retries.jsonl").open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "at": time.time(),
                                "label": label,
                                "attempt": attempt + 1,
                                "error_type": type(exc).__name__,
                                "http_status": status,
                                "retry": retry,
                                "scope": "same inference request; no program execution or source repair",
                            }
                        )
                        + "\n"
                    )
                if not retry:
                    raise GenerationTransportError(
                        "Inference transport failed after "
                        + str(attempt + 1)
                        + " recorded attempts: "
                        + type(exc).__name__
                        + ". The program has not been blamed or repaired."
                    ) from exc
                await asyncio.sleep(0.25)

    async def _ask_once(self, label, messages, raw=False, output_cap=None):
        prefix_observer = (
            _PREFIX_OBSERVER.get()
            if label in {"native_sql_batch", "native_sql_entry"}
            else None
        )
        if (
            self.max_calls is not None
            and sum((bool(x.get("generation_sent")) for x in self.calls))
            >= self.max_calls
        ):
            raise ModelCallBudgetExceeded(
                f"Model call budget exhausted: {self.max_calls}"
            )
        index = len(self.calls)
        literal_python = bool(
            raw
            and self.config.get("native_literal_python", False)
            and (
                label in {"native_work", "native_resume_patch"}
                or label.startswith("native_function_")
            )
        )
        from .native_tools import tools_for, decode_message, NativeToolProtocolError

        declared_tools = (
            tools_for(label)
            if raw and self.config.get("native_function_calls", False)
            else []
        )
        record = {"id": index, "label": label, "queued_at": time.time()}
        self.calls.append(record)
        body = {
            "model": self.config["model"],
            "messages": messages,
            "temperature": self.config.get("temperature", 0),
            "seed": self.config.get("seed", 17),
            "max_tokens": self.config.get("max_completion_tokens", 4096),
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {
                "enable_thinking": self.config["thinking"],
                "reasoning_effort": self.config["reasoning_effort"],
            },
        }
        if output_cap is not None:
            if type(output_cap) is not int or output_cap < 16:
                raise ValueError("Invalid request output cap")
            body["max_tokens"] = min(body["max_tokens"], output_cap)
        if raw:
            body.pop("response_format", None)
            schema = None
            if label == "native_input_binding_finish":
                from .semantic_support import FINISH_SCHEMA

                schema = FINISH_SCHEMA
            elif label == "native_semantics":
                from .semantic_support import resolution_schema

                schema = resolution_schema(json.loads(messages[-1]["content"]))
            elif label == "native_sql_batch":
                from .sql_schemas import sql_batch_schema

                limit = (
                    None
                    if self.config.get("construction_regions", False)
                    else self.config.get("artifact_batch_files", 4)
                )
                schema = sql_batch_schema(
                    limit, checks=self.config.get("program_requirement_checks", False)
                )
                try:
                    assignment = json.loads(messages[-1]["content"])
                except (ValueError, TypeError):
                    assignment = {}
                if isinstance(assignment, dict) and assignment.get(
                    "complete_assigned_region"
                ):
                    schema["anyOf"][0]["properties"]["files"]["minItems"] = assignment[
                        "minimum_region_files"
                    ]
            elif label == "native_sql_patch":
                from .sql_schemas import SQL_PATCH_SCHEMA

                schema = SQL_PATCH_SCHEMA
            elif label == "native_resume_patch":
                from .resumption import PATCH_SCHEMA

                schema = PATCH_SCHEMA
            if schema is not None:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": label, "strict": True, "schema": schema},
                }
            role = (
                "repair"
                if label in {"native_sql_compile_repair", "native_input_binding_finish"}
                else "sql"
                if label.startswith("native_sql_")
                else "value"
            )
            limit = self.config.get("native_thinking_budgets", {}).get(role)
            if limit is not None:
                if type(limit) is not int or limit < 0:
                    raise ValueError("Invalid thinking budget")
                body["thinking_token_budget"] = limit
        if literal_python:
            body.pop("response_format", None)
            body.pop("structured_outputs", None)
        if declared_tools:
            body.pop("response_format", None)
            body.pop("structured_outputs", None)
            body.update(
                tools=declared_tools, tool_choice="auto", parallel_tool_calls=False
            )
        path = self.directory / f"{index:03d}.json"
        if self.config.get("model_api") == "responses":
            from .responses import ask_responses

            return await ask_responses(self, body, record, path, raw)
        record["request"] = body
        record["generation_sent"] = False
        record["record_status"] = "queued"
        self.save_record(record, path)
        try:
            async with self.budget:
                record["started_at"] = time.time()
                record["generation_sent"] = False
                await self.fit_context(body, record)
                message = {"role": "assistant", "content": "", "reasoning": ""}
                stream_guard = (
                    ProgramStreamGuard()
                    if label in {"data_program", "data_program_scoped"} and (not raw)
                    else NativeStreamGuard()
                    if not literal_python
                    and (not declared_tools)
                    and label.startswith("native_")
                    and self.config.get("native_stream_guard", False)
                    else None
                )
                usage = {}
                finish_reason = None
                record["response"] = {
                    "choices": [{"message": message, "finish_reason": None}],
                    "usage": usage,
                }
                record["response_complete"] = False
                with path.with_suffix(".stream.jsonl").open("w") as log:
                    self.begin_generation(record, path)
                    async with self.client.stream(
                        "POST", self.config["inference_url"], json=body
                    ) as response:
                        record["http_status"] = response.status_code
                        self.save_record(record, path)
                        if response.is_error:
                            record["error_body"] = (await response.aread()).decode()
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data: ") or line == "data: [DONE]":
                                continue
                            chunk = json.loads(line[6:])
                            log.write(
                                json.dumps({"time": time.time(), "chunk": chunk}) + "\n"
                            )
                            log.flush()
                            if chunk.get("error"):
                                record["stream_error"] = chunk["error"]
                                raise GenerationTransportError(
                                    "Provider stream error: "
                                    + json.dumps(chunk["error"], ensure_ascii=False)
                                )
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                                record["response"]["usage"] = usage
                            for choice in chunk.get("choices", []):
                                for update in (
                                    choice.get("delta", {}).get("tool_calls") or []
                                ):
                                    tool_index = int(update.get("index", 0))
                                    calls = message.setdefault("tool_calls", [])
                                    while len(calls) <= tool_index:
                                        calls.append(
                                            {
                                                "id": "",
                                                "type": "function",
                                                "function": {
                                                    "name": "",
                                                    "arguments": "",
                                                },
                                            }
                                        )
                                    call = calls[tool_index]
                                    if update.get("id"):
                                        call["id"] = update["id"]
                                    if update.get("type"):
                                        call["type"] = update["type"]
                                    function = update.get("function") or {}
                                    if function.get("name") or function.get(
                                        "arguments"
                                    ):
                                        now = time.time()
                                        record.setdefault("first_output_at", now)
                                        record["last_output_at"] = now
                                    for key in ("name", "arguments"):
                                        if function.get(key):
                                            call["function"][key] += function[key]
                                for key in ("content", "reasoning"):
                                    delta = choice.get("delta", {}).get(key) or ""
                                    if delta:
                                        phase = (
                                            "output"
                                            if key == "content"
                                            else "reasoning"
                                        )
                                        now = time.time()
                                        record.setdefault("first_" + phase + "_at", now)
                                        record["last_" + phase + "_at"] = now
                                        message[key] += delta
                                        if key == "content" and stream_guard:
                                            stream_guard.feed(delta)
                                        if key == "content" and prefix_observer:
                                            prefix_observer(message["content"])
                                finish_reason = (
                                    choice.get("finish_reason") or finish_reason
                                )
                data = {
                    "choices": [{"message": message, "finish_reason": finish_reason}],
                    "usage": usage,
                }
                record["response"] = data
                record["response_complete"] = True
                record["finished_at"] = time.time()
            await self.measure_thinking(label, body, data, record)
            if declared_tools:
                try:
                    return decode_message(label, data["choices"][0]["message"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise NativeToolProtocolError(str(exc)) from exc
            message = data["choices"][0]["message"]["content"]
            return message if raw else json.loads(message)
        except BaseException as exc:
            record["error_type"] = type(exc).__name__
            record["error"] = str(exc)
            raise
        finally:
            record.setdefault("finished_at", time.time())
            record["record_status"] = "finished"
            self.save_record(record, path)

    async def close(self):
        await self.client.aclose()
