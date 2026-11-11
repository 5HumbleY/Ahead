import json
import os
import time
from urllib.parse import urlsplit
from .context import ContextBudgetExceeded
from .program_stream import ProgramStreamGuard


def make_request(config, chat_body, raw):
    mode = config.get("responses_format", "json_object")
    if mode != "json_object":
        raise ValueError("Responses currently requires explicit json_object mode")
    body = {
        "model": config["model"],
        "input": chat_body["messages"],
        "reasoning": {"effort": config["reasoning_effort"]},
        "max_output_tokens": min(
            config.get("max_completion_tokens", 4096),
            chat_body.get("max_tokens", config.get("max_completion_tokens", 4096)),
        ),
        "text": {"format": {"type": "text" if raw else "json_object"}},
        "tools": [],
        "tool_choice": "none",
        "store": False,
        "truncation": "disabled",
        "service_tier": config.get("service_tier", "default"),
        "stream": True,
    }
    if config.get("provider") == "deepseek":
        for field in ("store", "truncation", "service_tier", "tools", "tool_choice"):
            body.pop(field)
    return body


def authorization(config):
    endpoint = urlsplit(config["inference_url"])
    if endpoint.scheme != "https" and (
        not (
            endpoint.scheme == "http"
            and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
        )
    ):
        raise ValueError("Responses credentials require HTTPS or a loopback relay")
    variable = config.get("api_key_env", "OPENAI_API_KEY")
    secret = os.environ.get(variable)
    if not secret:
        raise RuntimeError(
            f"Missing {variable}; configure it locally, not in run configuration"
        )
    return {"Authorization": "Bearer " + secret}


async def fit_context(model, body, record, headers):
    limit = model.config.get("context_limit")
    if not limit:
        return
    endpoint = model.config.get(
        "tokenize_url", model.config["inference_url"].rstrip("/") + "/input_tokens"
    )
    payload = {
        k: body[k]
        for k in (
            "model",
            "input",
            "reasoning",
            "text",
            "tools",
            "tool_choice",
            "truncation",
        )
        if k in body
    }
    response = await model.client.post(endpoint, json=payload, headers=headers)
    record["input_count_http_status"] = response.status_code
    response.raise_for_status()
    counts = response.json()
    prompt_tokens = counts["input_tokens"]
    if type(prompt_tokens) is not int or prompt_tokens < 0:
        raise RuntimeError("Responses input token count is invalid")
    requested = body["max_output_tokens"]
    available = limit - prompt_tokens
    record["token_budget"] = {
        "count_source": counts.get("source", "responses/input_tokens"),
        "prompt_tokens": prompt_tokens,
        "context_limit": limit,
        "requested_output_cap": requested,
        "effective_output_cap": max(0, min(requested, available)),
    }
    if "safety_margin" in counts:
        record["token_budget"].update(
            local_count=counts["local_count"], safety_margin=counts["safety_margin"]
        )
    if available < 16:
        raise ContextBudgetExceeded(
            f"Context budget exhausted: {prompt_tokens} input tokens in a {limit}-token window"
        )
    body["max_output_tokens"] = min(requested, available)


def normalized_usage(usage):
    if not usage:
        return {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            raise RuntimeError("Responses terminal usage is invalid")
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise RuntimeError("Responses token totals are inconsistent")
    return {
        "prompt_tokens": usage["input_tokens"],
        "completion_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "prompt_tokens_details": usage.get("input_tokens_details", {}),
        "completion_tokens_details": usage.get("output_tokens_details", {}),
    }


async def read_response(model, body, record, path, headers):
    message = {"role": "assistant", "content": "", "reasoning": ""}
    data = {"choices": [{"message": message, "finish_reason": None}], "usage": {}}
    record["response"] = data
    record["response_complete"] = False
    stream_guard = (
        ProgramStreamGuard()
        if record.get("label") in {"data_program", "data_program_scoped"}
        and record.get("output_contract") != "plain_text"
        else None
    )
    terminal = None
    refusal = ""
    model.begin_generation(record, path)
    with path.with_suffix(".stream.jsonl").open("w") as log:
        async with model.client.stream(
            "POST", model.config["inference_url"], json=body, headers=headers
        ) as response:
            record["http_status"] = response.status_code
            record["request_id"] = response.headers.get("x-request-id")
            model.save_record(record, path)
            if response.is_error:
                record["error_body"] = (await response.aread()).decode()
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                event = json.loads(payload)
                now = time.time()
                log.write(json.dumps({"time": now, "chunk": event}) + "\n")
                log.flush()
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    message["content"] += event.get("delta", "")
                    record.setdefault("first_output_at", now)
                    record["last_output_at"] = now
                    if stream_guard:
                        stream_guard.feed(event.get("delta", ""))
                elif kind == "response.reasoning_text.delta":
                    message["reasoning"] += event.get("delta", "")
                    record.setdefault("first_reasoning_at", now)
                    record["last_reasoning_at"] = now
                elif kind == "response.refusal.delta":
                    refusal += event.get("delta", "")
                elif kind in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    terminal = event["response"]
                    record["provider_response"] = terminal
                    data["usage"] = normalized_usage(terminal.get("usage"))
                    data["choices"][0]["finish_reason"] = {
                        "response.completed": "stop",
                        "response.incomplete": "length",
                        "response.failed": "error",
                    }[kind]
                elif kind == "error":
                    record["provider_error"] = event
                    raise RuntimeError(
                        "Responses stream error: " + str(event.get("message", event))
                    )
    if terminal is None:
        raise RuntimeError(
            "Responses stream ended without a terminal response; usage is incomplete"
        )
    outputs = terminal.get("output", [])
    text_parts = [
        p.get("text", "")
        for item in outputs
        if item.get("type") == "message"
        for p in item.get("content", [])
        if p.get("type") == "output_text"
    ]
    if text_parts:
        message["content"] = "".join(text_parts)
    refusals = [
        p.get("refusal", "")
        for item in outputs
        if item.get("type") == "message"
        for p in item.get("content", [])
        if p.get("type") == "refusal"
    ]
    if refusal or refusals:
        raise RuntimeError(
            "Responses returned a refusal: " + (refusal or "".join(refusals))
        )
    if terminal.get("model") != body["model"]:
        raise RuntimeError(
            "Responses returned an unexpected model: " + str(terminal.get("model"))
        )
    if terminal.get("status") not in {"completed", "incomplete"}:
        raise RuntimeError("Responses failed: " + str(terminal.get("error")))
    if not data["usage"]:
        raise RuntimeError(
            "Responses terminal event omitted usage; stop before another charged request"
        )
    budget = record.get("token_budget")
    if budget:
        actual = data["usage"]["prompt_tokens"]
        record["context_budget_valid"] = (
            actual + body["max_output_tokens"] <= budget["context_limit"]
        )
        if not record["context_budget_valid"]:
            raise RuntimeError(
                "Actual provider input exceeds the predeclared context budget"
            )
    if any((item.get("type", "").endswith("_call") for item in outputs)):
        raise RuntimeError(
            "Unexpected native tool call in a tool-free Responses request"
        )
    record["response_complete"] = True
    return data


async def ask_responses(model, chat_body, record, path, raw):
    record["api"] = "responses"
    record["output_contract"] = (
        "plain_text" if raw else "json_object_without_strict_schema"
    )
    try:
        body = make_request(model.config, chat_body, raw)
        record["request"] = body
        record["generation_sent"] = False
        record["record_status"] = "queued"
        model.save_record(record, path)
        headers = authorization(model.config)
        async with model.budget:
            record["started_at"] = time.time()
            await fit_context(model, body, record, headers)
            data = await read_response(model, body, record, path, headers)
            record["finished_at"] = time.time()
        content = data["choices"][0]["message"]["content"]
        return content if raw else json.loads(content)
    except BaseException as exc:
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)
        raise
    finally:
        record.setdefault("finished_at", time.time())
        record["record_status"] = "finished"
        model.save_record(record, path)
