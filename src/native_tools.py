import copy
import json


class NativeToolProtocolError(ValueError):
    pass


def tools_for(label):
    if label == "native_sql_program":
        from .sql_repair_code import TOOL

        return [TOOL]
    if label in {
        "native_sql_start",
        "native_sql_program",
        "native_sql_syntax_repair",
        "native_sql_compile_repair",
    }:
        from .sql_program import TOOLS

        by_name = {item["function"]["name"]: item for item in TOOLS}
        if label in {"native_sql_compile_repair", "native_sql_syntax_repair"}:
            update = copy.deepcopy(by_name["sql_apply"])
            update["function"]["parameters"]["properties"]["checks"]["maxItems"] = 0
            if label == "native_sql_syntax_repair":
                return [update]
            source = copy.deepcopy(by_name["sql_observe"])
            source["function"]["description"] = (
                "Read observed source pages for compiler binding; no data queries."
            )
            source["function"]["parameters"]["properties"].pop("queries", None)
            source["function"]["parameters"]["required"] = ["sources"]
            return [update, source]
        return [by_name["sql_apply"]] if label == "native_sql_start" else TOOLS
    if label == "native_resume_patch":
        return [REPAIR_PYTHON]
    return []


def decode_message(label, message):
    calls = message.get("tool_calls") or []
    if len(calls) != 1:
        raise ValueError(
            "Return exactly one call to an available tool. No tool from this response executed."
        )
    function = calls[0]["function"]
    name = function["name"]
    allowed = {item["function"]["name"] for item in tools_for(label)}
    if name not in allowed:
        raise ValueError(
            "Use one of the actual tools "
            + repr(sorted(allowed))
            + ". Other runtime helpers are Python bindings inside the source code argument."
        )
    arguments = function["arguments"]
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    if label == "native_sql_program" and name == "sql_repair":
        if set(arguments) != {"code"} or not isinstance(arguments["code"], str):
            raise ValueError("sql_repair requires actual Python code")
        return json.dumps(
            {"action": "sql_repair", "code": arguments["code"]}, ensure_ascii=False
        )
    if label in {
        "native_sql_start",
        "native_sql_program",
        "native_sql_syntax_repair",
        "native_sql_compile_repair",
    }:
        if name == "sql_observe":
            arguments = {"sources": [], "queries": [], **arguments}
        expected = {
            "sql_apply": {"files", "edits", "checks", "complete"},
            "sql_observe": {"sources", "queries"},
            "sql_recall": {"ids"},
        }[name]
        if set(arguments) != expected:
            raise ValueError("Tool requires exactly " + repr(sorted(expected)))
        if (
            name not in {"sql_observe", "sql_recall"}
            and type(arguments["complete"]) is not bool
        ):
            raise ValueError("complete must be boolean")
        if any((not isinstance(arguments[k], list) for k in expected - {"complete"})):
            raise ValueError(
                "Program actions use arrays for files/edits/sources/queries"
            )
        return json.dumps({"action": name, **arguments}, ensure_ascii=False)
    if label == "native_resume_patch" and name == "python":
        if (
            set(arguments) != {"code", "retry"}
            or not isinstance(arguments["code"], str)
            or type(arguments["retry"]) is not bool
        ):
            raise ValueError("Repair requires code and a boolean retry decision")
        return json.dumps(arguments, ensure_ascii=False)
    raise ValueError("Unsupported SQL tool call")


PYTHON = {
    "type": "function",
    "function": {
        "name": "python",
        "description": "Submit actual Python source in the current retained scope. At the root it executes; a demanded function definition is installed; in repair it can inspect, apply exact edits and request a retry. Python helpers, values and imports already exist in this scope. Call helpers such as web_open inside this Python code, not as separate tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The actual Python source, with normal quotes and newlines. Use the bound helpers directly. At the root, finish(value) delivers the computed result.",
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}
REPAIR_PYTHON = copy.deepcopy(PYTHON)
REPAIR_PYTHON["function"]["description"] = (
    "Inspect or repair the suspended region using actual Python source, and explicitly choose whether to retry it after this code succeeds. Preserve the return interface required by the supplied pending consumer code. An edit and its retry decision belong in this same call."
)
REPAIR_PYTHON["function"]["parameters"]["properties"]["code"]["description"] = (
    "Actual Python inspection or exact-edit source. Use the bound edit/read helpers. Empty code with retry=false explicitly declines the repair."
)
REPAIR_PYTHON["function"]["parameters"]["properties"]["retry"] = {
    "type": "boolean",
    "description": "True authorizes one retry of the same suspended invocation after this source cell succeeds. False returns inspection feedback for further repair; with empty code it declines repair. Review earlier effects inside the failed region before retrying.",
}
REPAIR_PYTHON["function"]["parameters"]["required"] = ["code", "retry"]
