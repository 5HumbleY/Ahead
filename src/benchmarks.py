import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .catalog import inspect_database
from .data_eng_environment import build_task_environment
from .file_inputs import read_resource
from .media_inputs import MediaInputs
from .task_contract import dbt_contract
from .web_tools import WebTools


@dataclass(frozen=True)
class BenchmarkTask:
    benchmark: str
    task_id: str
    question: str
    data_root: Path

    def as_agent_task(self):
        return {
            "id": self.task_id,
            "question": self.question,
            "data_root": str(self.data_root),
            "benchmark": self.benchmark,
        }


class Benchmark:
    name = ""
    question_fields = ("question", "prompt", "instruction", "task")
    id_fields = ("id", "task_id", "instance_id")

    def __init__(self, data_root):
        self.data_root = Path(data_root).resolve()
        if not self.data_root.is_dir():
            raise NotADirectoryError(self.data_root)

    def resource(self, path):
        path = Path(path)
        resolved = (self.data_root / path).resolve()
        if not resolved.is_relative_to(self.data_root):
            raise ValueError("Resource is outside the task input directory")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved

    def load_task(self, record, *, task_id=None, question_field=None):
        if isinstance(record, (str, Path)):
            record = json.loads(self.resource(record).read_text(encoding="utf-8"))
        if not isinstance(record, Mapping):
            raise TypeError("Task record must be an object")
        fields = (question_field,) if question_field else self.question_fields
        question = next(
            (
                record[key]
                for key in fields
                if isinstance(record.get(key), str) and record[key].strip()
            ),
            None,
        )
        identifier = (
            task_id
            if task_id is not None
            else next(
                (record[key] for key in self.id_fields if record.get(key) is not None),
                None,
            )
        )
        if question is None or identifier is None or not str(identifier).strip():
            raise ValueError("Task requires an identifier and question text")
        return BenchmarkTask(self.name, str(identifier), question, self.data_root)

    def read(self, path, kind="text", **options):
        return read_resource(kind, self.resource(path), **options)

    def database(self, path, **options):
        return inspect_database(self.resource(path), **options)


class DataSpace(Benchmark):
    name = "DataSpace"

    def media(self, workspace, **options):
        return MediaInputs(self.data_root, workspace, options)


class KramaBench(Benchmark):
    name = "KramaBench"


class WebDS(Benchmark):
    name = "WebDS"

    def browser(self, endpoint, directory, *, session=None):
        return WebTools(endpoint, directory, session=session)


class DataEngBench(Benchmark):
    name = "data-eng-bench"

    def contract(self, task, model_roots=("models",)):
        if task.benchmark != self.name:
            raise ValueError("Task belongs to a different benchmark")
        return dbt_contract(task.question, model_roots=model_roots)

    def build_environment(self, task_directory, base_image, record_directory):
        task_directory = (self.data_root / task_directory).resolve()
        if not task_directory.is_relative_to(self.data_root):
            raise ValueError("Task environment is outside the benchmark directory")
        return build_task_environment(task_directory, base_image, record_directory)


BENCHMARKS = {
    "DataSpace": DataSpace,
    "KramaBench": KramaBench,
    "WebDS": WebDS,
    "data-eng-bench": DataEngBench,
}


def get_benchmark(name, data_root):
    if name not in BENCHMARKS:
        raise ValueError("Unknown benchmark: " + str(name))
    return BENCHMARKS[name](data_root)
