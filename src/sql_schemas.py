def sql_batch_schema(limit=4, checks=False):
    schema = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "minItems": 1,
                "maxItems": limit,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sql": {"type": "string"},
                    },
                    "required": ["path", "sql"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["files"],
        "additionalProperties": False,
    }
    if limit is None:
        schema["properties"]["files"].pop("maxItems")
    inspection = {
        "type": "object",
        "properties": {
            "inspect_sources": {
                "type": "array",
                "minItems": 1,
                "maxItems": 16,
                "items": {"type": "string"},
            }
        },
        "required": ["inspect_sources"],
        "additionalProperties": False,
    }
    if checks:
        from .program_checks import CHECK_SCHEMA

        schema["properties"]["checks"] = {"type": "array", "items": CHECK_SCHEMA}
        schema["required"].append("checks")
    from .semantic_bindings import BINDING_SCHEMA

    schema["properties"] = {
        "semantic_bindings": {"type": "array", "items": BINDING_SCHEMA},
        **schema["properties"],
    }
    schema["required"].insert(0, "semantic_bindings")
    return {"anyOf": [schema, inspection]}


SQL_PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_matches": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                    },
                },
                "required": ["path", "old", "new", "expected_matches"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edits"],
    "additionalProperties": False,
}
SQL_PATCH_SCHEMA = {
    "anyOf": [
        SQL_PATCH_SCHEMA,
        {
            "type": "object",
            "properties": {
                "inspect": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string"},
                            "start": {"type": "integer", "minimum": 1},
                            "lines": {"type": "integer", "minimum": 1},
                        },
                        "required": ["model", "start", "lines"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["inspect"],
            "additionalProperties": False,
        },
    ]
}
SQL_PATCH_SCHEMA["anyOf"].append(
    {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "sql": {"type": "string"},
                    },
                    "required": ["name", "sql"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["queries"],
        "additionalProperties": False,
    }
)
