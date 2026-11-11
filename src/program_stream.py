import json
import io
import re
import tokenize


class InvalidProgramStream(ValueError):
    pass


class RepetitiveNativeCode(ValueError):
    def __init__(self, draft):
        self.draft = draft
        super().__init__(
            """Generation repeatedly expanded the same statement pattern. No code from this response was executed. Submit a compact loop/batch operation, inspect a bounded sample, or continue the actual implementation; do not enumerate nearly identical operations indefinitely. Unexecuted draft prefix:
"""
            + draft[:2200]
        )


class NativeStreamGuard:
    def __init__(self):
        self.prefix = ""
        self.started = False
        self.closed = False
        self.escaped = False
        self.raw = []
        self.decoded = ""
        self.pending = ""
        self.statements = []

    def feed(self, text):
        if self.closed:
            return
        if not self.started:
            self.prefix += text
            match = re.search('"code"\\s*:\\s*"', self.prefix)
            if not match:
                return
            text = self.prefix[match.end() :]
            self.prefix = ""
            self.started = True
        for char in text:
            if not self.escaped and char == '"':
                self.closed = True
                self._decode(final=True)
                return
            self.raw.append(char)
            if self.escaped:
                self.escaped = False
                if char == "n":
                    self._decode()
            elif char == "\\":
                self.escaped = True

    def _decode(self, final=False):
        try:
            code = json.loads('"' + "".join(self.raw) + '"')
        except ValueError:
            return
        self.pending += code[len(self.decoded) :]
        self.decoded = code
        if final and self.pending and (not self.pending.endswith("\n")):
            self.pending += "\n"
        while self.pending:
            signature = []
            end = None
            try:
                for token in tokenize.generate_tokens(
                    io.StringIO(self.pending).readline
                ):
                    if token.type == tokenize.NEWLINE:
                        end = (
                            sum(
                                (
                                    len(x)
                                    for x in self.pending.splitlines(keepends=True)[
                                        : token.end[0] - 1
                                    ]
                                )
                            )
                            + token.end[1]
                        )
                        break
                    if token.type in {
                        tokenize.INDENT,
                        tokenize.DEDENT,
                        tokenize.NL,
                        tokenize.COMMENT,
                        tokenize.ENDMARKER,
                    }:
                        continue
                    signature.append((token.type, re.sub("\\d+", "#", token.string)))
            except (tokenize.TokenError, IndentationError):
                return
            if end is None:
                return
            self.pending = self.pending[end:]
            if not signature:
                continue
            self.statements.append(tuple(signature))
            self.statements = self.statements[-128:]
            for period in range(1, 9):
                repetitions = max(4, (12 + period - 1) // period)
                span = period * repetitions
                if len(self.statements) < span:
                    continue
                tail = self.statements[-span:]
                if all((tail[i] == tail[i % period] for i in range(period, len(tail)))):
                    raise RepetitiveNativeCode(self.decoded)


def claim_node_id(node, seen):
    if not isinstance(node, dict) or not isinstance(node.get("id"), str):
        raise ValueError("Every program node needs a string id")
    name = node["id"]
    if name in seen:
        raise ValueError(
            "Duplicate node id in one atomic patch: "
            + name
            + ". Send one final definition for each node."
        )
    seen.add(name)


def validate_node_ids(nodes):
    seen = set()
    for node in nodes:
        claim_node_id(node, seen)


class ProgramStreamGuard:
    def __init__(self):
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.root_state = "start"
        self.root_key = None
        self.key_text = None
        self.nodes_array = False
        self.node_text = None
        self.seen = set()

    def feed(self, text):
        for char in text:
            if self.node_text is not None:
                self.node_text.append(char)
            if self.in_string:
                if self.key_text is not None:
                    self.key_text.append(char)
                if self.escaped:
                    self.escaped = False
                elif char == "\\":
                    self.escaped = True
                elif char == '"':
                    self.in_string = False
                    if self.key_text is not None:
                        self.root_key = json.loads("".join(self.key_text))
                        self.key_text = None
                        self.root_state = "colon"
                continue
            if char == '"':
                self.in_string = True
                if self.depth == 1 and self.root_state == "key":
                    self.key_text = ['"']
                continue
            if char in "{[":
                if char == "{" and self.nodes_array and (self.depth == 2):
                    self.node_text = ["{"]
                if (
                    char == "["
                    and self.depth == 1
                    and (self.root_state == "value")
                    and (self.root_key == "nodes")
                ):
                    self.nodes_array = True
                self.depth += 1
                if self.depth == 1:
                    self.root_state = "key"
            elif char in "}]":
                if char == "}" and self.node_text is not None and (self.depth == 3):
                    node = json.loads("".join(self.node_text))
                    self.node_text = None
                    try:
                        claim_node_id(node, self.seen)
                    except ValueError as exc:
                        raise InvalidProgramStream(str(exc)) from exc
                if char == "]" and self.nodes_array and (self.depth == 2):
                    self.nodes_array = False
                self.depth -= 1
            elif char == ":" and self.depth == 1 and (self.root_state == "colon"):
                self.root_state = "value"
            elif char == "," and self.depth == 1:
                self.root_state = "key"
                self.root_key = None
