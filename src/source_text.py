import re


def literal_source(text):
    source = text.strip()
    fenced = re.fullmatch("```(?:python)?\\s*\\n(.*?)\\n```", source, re.S)
    if fenced:
        source = fenced.group(1)
    if not source.strip():
        raise ValueError("Return executable Python source")
    return source
