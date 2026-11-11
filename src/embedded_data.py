from html.parser import HTMLParser
import json
import re

LITERAL = "(?:\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*')"
PARSE = re.compile(
    "\\b([A-Za-z_$][\\w$]*)\\s*=\\s*JSON\\.parse\\(\\s*(" + LITERAL + ")\\s*\\)", re.S
)
PLAIN = re.compile("\\b(?:var|let|const)\\s+([A-Za-z_$][\\w$]*)\\s*=\\s*([\\[{])")


def string_value(literal):
    body = literal[1:-1]
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\x08",
        "f": "\x0c",
        "v": "\x0b",
        "0": "\x00",
    }
    result = []
    index = 0
    while index < len(body):
        char = body[index]
        index += 1
        if char != "\\":
            result.append(char)
            continue
        if index >= len(body):
            raise ValueError("Incomplete quoted literal")
        char = body[index]
        index += 1
        if char in ("x", "u"):
            length = 2 if char == "x" else 4
            result.append(chr(int(body[index : index + length], 16)))
            index += length
        elif char == "\n":
            pass
        elif char == "\r":
            if index < len(body) and body[index] == "\n":
                index += 1
        else:
            result.append(escapes.get(char, char))
    text = "".join(result)
    if any((55296 <= ord(char) <= 57343 for char in text)):
        text = text.encode("utf-16", errors="surrogatepass").decode("utf-16")
    return text


def extract_datasets(html):

    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.current = None
            self.items = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.current = [dict(attrs), []]

        def handle_data(self, data):
            if self.current is not None:
                self.current[1].append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self.current is not None:
                self.items.append(self.current)
                self.current = None

    scripts = Scripts()
    scripts.feed(html)
    result = []
    for index, (attrs, parts) in enumerate(scripts.items):
        text = "".join(parts)
        selector = "script:nth-of-type(" + str(index + 1) + ")"
        if attrs.get("type", "").split(";", 1)[0].strip() in {
            "application/json",
            "application/ld+json",
        }:
            try:
                result.append(
                    {
                        "name": attrs.get("id") or "script_" + str(index),
                        "carrier": "json_script",
                        "selector": selector,
                        "data": json.loads(text),
                    }
                )
            except (ValueError, TypeError):
                pass
        for match in PARSE.finditer(text):
            try:
                data = json.loads(string_value(match[2]))
            except (ValueError, TypeError, UnicodeError):
                continue
            result.append(
                {
                    "name": match[1],
                    "carrier": "json_parse_literal",
                    "selector": selector,
                    "data": data,
                }
            )
        for match in PLAIN.finditer(text):
            try:
                (data, end) = json.JSONDecoder().raw_decode(text[match.start(2) :])
            except ValueError:
                continue
            result.append(
                {
                    "name": match[1],
                    "carrier": "json_literal",
                    "selector": selector,
                    "data": data,
                }
            )
    return result


def structure(value, depth=0, _budget=None):
    if _budget is None:
        _budget = [160]
    if _budget[0] <= 0:
        return {"kind": type(value).__name__, "structure_truncated": True}
    _budget[0] -= 1
    if isinstance(value, dict):
        items = list(value.items())
        sample = [item for (_, item) in items[:2] + items[-2:]]
        homogeneous_map = (
            len(value) > 8
            and all((isinstance(item, dict) for item in sample))
            and (len({tuple(item) for item in sample}) == 1)
        )
        result = {"kind": "object", "keys": list(value)[:24], "key_count": len(value)}
        if depth < 5:
            if homogeneous_map:
                result.update(
                    value_structure=structure(sample[0], depth + 1, _budget),
                    sampled_values_share_field_names=True,
                )
            else:
                result["fields"] = {
                    str(key): structure(item, depth + 1, _budget)
                    for (key, item) in items[:24]
                }
        return result
    if isinstance(value, list):
        result = {"kind": "array", "length": len(value)}
        if value and depth < 4:
            first = structure(value[0], depth + 1, _budget)
            result["item_structure"] = first
            last = structure(value[-1], depth + 1, _budget) if len(value) > 1 else first
            result["sampled_item_structures"] = (
                [first] if first == last else [first, last]
            )
            result["schema_scope"] = (
                "Observed head/tail structures; complete data remains at the immutable ref."
            )
        return result
    return {"kind": type(value).__name__}
