from contextvars import ContextVar
import sys

_sink = ContextVar("ahead_execution_output", default=None)


class RoutedStream:
    def __init__(self, original):
        self.original = original

    def write(self, text):
        sink = _sink.get()
        if sink is None:
            return self.original.write(text)
        sink.append(str(text))
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        if _sink.get() is None:
            self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


def begin_capture(output):
    if not isinstance(sys.stdout, RoutedStream):
        sys.stdout = RoutedStream(sys.stdout)
    if not isinstance(sys.stderr, RoutedStream):
        sys.stderr = RoutedStream(sys.stderr)
    return _sink.set(output)


def end_capture(token):
    _sink.reset(token)
