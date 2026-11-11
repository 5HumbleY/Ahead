import copy
import json


def tool_response(value):
    return (
        "<tool_response>\n"
        + json.dumps(value, ensure_ascii=False, default=str)
        + "\n</tool_response>"
    )


def compact_observation(context):
    if context.get("latest_execution_observation"):
        return {"execution": context["latest_execution_observation"]}
    if context.get("feedback"):
        return {"runtime_feedback": context["feedback"]}
    if context.get("inspection") is not None:
        return {
            "inspection": context["inspection"],
            "retry_scope": context.get("retry_scope"),
        }
    if context.get("program"):
        row = context["program"][-1]
        return {
            "execution": {
                key: value
                for (key, value) in row.items()
                if key not in {"source", "error_source"}
            }
        }
    return context


class Continuation:
    def __init__(self, seed=None):
        self.turns = []
        if seed:
            self.turns.append({"assistant": copy.deepcopy(seed), "observation": None})

    def latest_assistant(self):
        return copy.deepcopy(self.turns[-1]["assistant"]) if self.turns else None

    def remember(self, model, label, messages, text):
        assistant = {"role": "assistant", "content": text}
        for record in reversed(getattr(model, "calls", [])):
            if not isinstance(record, dict) or record.get("label") != label:
                continue
            if record.get("request", {}).get("messages") is not messages:
                continue
            message = (
                record.get("response", {}).get("choices", [{}])[0].get("message", {})
            )
            if "content" in message:
                assistant["content"] = message.get("content") or ""
            if message.get("tool_calls"):
                assistant["content"] = message.get("content") or ""
                assistant["tool_calls"] = copy.deepcopy(message["tool_calls"])
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            if reasoning:
                assistant["reasoning"] = reasoning
            break
        self.turns.append({"assistant": assistant, "observation": None})

    def render(self, prompt, stable, context, history_limit=None):
        if self.turns and self.turns[-1]["observation"] is None:
            self.turns[-1]["observation"] = copy.deepcopy(context)
        facts = {
            key: stable[key]
            for key in ("input_catalog", "completed_runtime_preparation")
            if key in stable
        }
        task = {key: value for (key, value) in stable.items() if key not in facts}
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(task, ensure_ascii=False, default=str),
            },
        ]
        if facts:
            messages.append(
                {
                    "role": "user",
                    "content": tool_response({"acquired_runtime_facts": facts}),
                }
            )
        turns = (
            self.turns
            if history_limit is None
            else self.turns[-history_limit:]
            if history_limit
            else []
        )
        for index, turn in enumerate(turns):
            assistant = copy.deepcopy(turn["assistant"])
            if index < len(turns) - 1:
                assistant.pop("reasoning_content", None)
                assistant.pop("reasoning", None)
            elif assistant.get("reasoning_content") and (
                not assistant.get("reasoning")
            ):
                assistant["reasoning"] = assistant.pop("reasoning_content")
            messages.append(assistant)
            observed = (
                context
                if index == len(turns) - 1
                else compact_observation(turn["observation"] or {})
            )
            if assistant.get("tool_calls"):
                for call in assistant["tool_calls"]:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["function"]["name"],
                            "content": json.dumps(
                                observed, ensure_ascii=False, default=str
                            ),
                        }
                    )
            else:
                messages.append({"role": "user", "content": tool_response(observed)})
        if not turns:
            messages.append({"role": "user", "content": tool_response(context)})
        return messages

    async def render_fitting(
        self,
        model,
        prompt,
        stable,
        context,
        reserve_tokens=8192,
        label=None,
        extra_messages=None,
    ):
        total = len(self.turns)
        messages = self.render(prompt, stable, context) + (extra_messages or [])
        if not hasattr(model, "count_context"):
            return (messages, None, total)

        async def count(value):
            return await model.count_context(value, **{"label": label} if label else {})

        measured = await count(messages)

        def fits(value):
            return (
                not value
                or value["prompt_tokens"] <= value["context_limit"] - reserve_tokens
            )

        if fits(measured) or total <= 1:
            return (messages, measured, total)
        kept = 1
        best = self.render(prompt, stable, context, history_limit=kept) + (
            extra_messages or []
        )
        best_count = await count(best)
        if not fits(best_count):
            return (best, best_count, kept)
        (low, high) = (2, total - 1)
        while low <= high:
            middle = (low + high) // 2
            candidate = self.render(prompt, stable, context, history_limit=middle) + (
                extra_messages or []
            )
            measured_candidate = await count(candidate)
            if fits(measured_candidate):
                (best, best_count, kept) = (candidate, measured_candidate, middle)
                low = middle + 1
            else:
                high = middle - 1
        return (best, best_count, kept)
