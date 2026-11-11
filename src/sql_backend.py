import asyncio
import hashlib
import json
import os
from pathlib import Path
import time


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class SQLBackend:
    def __init__(self, database, project=None):
        self.database = str(database)
        self.project = Path(project) if project else None
        from .execution import TrackedExecutor

        self.manifest = None
        self.operations = []
        self.executor = TrackedExecutor("sql")
        self.initial_relations = None
        self.initial_model_files = {}
        self.execution_receipts = {}
        self.owned_database_revision = None

    async def run_dbt(self, argv, project=None):
        project = str(project or self.project)
        env = dict(
            os.environ, DBT_PROFILES_DIR=project, DBT_SEND_ANONYMOUS_USAGE_STATS="false"
        )
        start = time.time()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=project,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            (out, _) = await proc.communicate()
        except asyncio.CancelledError:
            import signal

            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise
        text = out.decode(errors="replace")
        self.operations.append(
            {
                "argv": argv,
                "project": project,
                "seconds": time.time() - start,
                "returncode": proc.returncode,
                "output": text[-12000:],
            }
        )
        if proc.returncode:
            raise RuntimeError(" ".join(argv) + " failed:\n" + text[-14000:])
        return text

    async def initialize(self):
        if self.initial_relations is None:
            self.initial_relations = await self.executor.run(self.relation_inventory)
        if not self.project:
            return
        await self.run_dbt(["dbt", "parse"])
        self.manifest = json.loads((self.project / "target/manifest.json").read_text())
        if not self.initial_model_files:
            self.initial_model_files = {
                str((self.project / n["original_file_path"]).resolve()): hashlib.sha256(
                    (self.project / n["original_file_path"]).read_bytes()
                ).hexdigest()
                for n in self.manifest.get("nodes", {}).values()
                if n.get("resource_type") == "model"
                and n.get("original_file_path")
                and (
                    tuple(
                        (
                            str(n.get(k, "")).casefold()
                            for k in ["database", "schema", "alias"]
                        )
                    )
                    in self.initial_relations
                )
                and (self.project / n["original_file_path"]).is_file()
            }

    def relation_inventory(self):
        import duckdb

        with duckdb.connect(self.database, read_only=True) as con:
            return {
                tuple((str(x).casefold() for x in row))
                for row in con.execute(
                    "SELECT table_catalog,table_schema,table_name FROM information_schema.tables"
                ).fetchall()
            }

    def validate_writes(self, files, outputs):
        for path, contents in files.items():
            original = self.initial_model_files.get(str(Path(path).resolve()))
            if (
                original is not None
                and hashlib.sha256(contents.encode()).hexdigest() != original
            ):
                raise ValueError(
                    "This model defines a supplied input; preserve it and create a separate output: "
                    + path
                )
        for output in outputs:
            if (
                output.get("path")
                and str(Path(output["path"]).resolve()) in self.initial_model_files
            ):
                raise ValueError(
                    "A supplied input model cannot become an output artifact: "
                    + output["path"]
                )

    @staticmethod
    def project_signature(project, dependency_files=None):
        import yaml

        root = Path(project)
        config = yaml.safe_load((root / "dbt_project.yml").read_text()) or {}
        paths = set(root.glob("*.yml")) | set(root.glob("*.yaml"))
        model_index = []
        selected = set(dependency_files) if dependency_files is not None else None
        for key, default in [
            ("model-paths", ["models"]),
            ("macro-paths", ["macros"]),
            ("seed-paths", ["seeds"]),
            ("snapshot-paths", ["snapshots"]),
            ("analysis-paths", ["analyses"]),
        ]:
            for directory in config.get(key, default):
                base = root / directory
                if not base.is_dir():
                    continue
                for path in base.rglob("*"):
                    if not path.is_file():
                        continue
                    if key == "model-paths":
                        model_index.append(str(path.resolve()))
                        if (
                            selected is not None
                            and path.suffix.lower() == ".sql"
                            and (str(path.resolve()) not in selected)
                        ):
                            continue
                    paths.add(path)
        result = {
            str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)
            if p.is_file()
        }
        result["__model_index__"] = hashlib.sha256(
            json.dumps(sorted(model_index)).encode()
        ).hexdigest()
        return result

    def inspect_materialized(self, output, model):
        import duckdb

        relation = model["relation_name"]
        expected = output.get("relation")
        actual = {
            "database": model.get("database"),
            "schema": model["schema"],
            "name": model.get("alias", model["name"]),
        }
        if expected and any(
            (
                str(expected[k]).casefold() != str(actual[k]).casefold()
                for k in ["schema", "name"]
            )
        ):
            raise ValueError(
                json.dumps(
                    {
                        "condition": "physical_output_target",
                        "expected": expected,
                        "actual": actual,
                        "detail": "config.schema is a custom dbt suffix; the compiled manifest determines the actual schema.",
                    }
                )
            )
        with duckdb.connect(self.database, read_only=True) as con:
            schema = [
                (x[0], x[1])
                for x in con.execute("DESCRIBE SELECT * FROM " + relation).fetchall()
            ]
            columns = [name for (name, _) in schema]
            if output.get("columns") is not None and columns != output["columns"]:
                raise ValueError(
                    json.dumps(
                        {
                            "condition": "output_columns",
                            "expected": output["columns"],
                            "actual": columns,
                        }
                    )
                )
            wanted = output.get("materialized") or output.get("config", {}).get(
                "materialized"
            )
            table_type = con.execute(
                "SELECT table_type FROM information_schema.tables WHERE lower(table_catalog)=lower(?) AND lower(table_schema)=lower(?) AND lower(table_name)=lower(?)",
                [actual["database"], actual["schema"], actual["name"]],
            ).fetchone()
            if wanted in {"table", "view"} and (
                table_type is None or (table_type[0] == "VIEW") != (wanted == "view")
            ):
                raise ValueError(
                    json.dumps(
                        {
                            "condition": "output_materialization",
                            "expected": wanted,
                            "actual": table_type,
                        }
                    )
                )
            for field in set(output.get("not_null", [])) | set(
                output.get("unique_by", [])
            ):
                if field not in columns:
                    raise ValueError("Unknown required output field: " + field)
                count = con.execute(
                    "SELECT COUNT(*) FROM "
                    + relation
                    + " WHERE "
                    + quote(field)
                    + " IS NULL"
                ).fetchone()[0]
                if count:
                    raise ValueError(
                        json.dumps(
                            {
                                "condition": "output_not_null",
                                "column": field,
                                "null_rows": count,
                            }
                        )
                    )
            grain = output.get("unique_by", [])
            if grain:
                keys = ", ".join((quote(field) for field in grain))
                duplicates = con.execute(
                    "SELECT COUNT(*) FROM (SELECT "
                    + keys
                    + " FROM "
                    + relation
                    + " GROUP BY "
                    + keys
                    + " HAVING COUNT(*) > 1)"
                ).fetchone()[0]
                if duplicates:
                    raise ValueError(
                        json.dumps(
                            {
                                "condition": "output_grain",
                                "columns": grain,
                                "duplicate_keys": duplicates,
                            }
                        )
                    )
            count = con.execute("SELECT COUNT(*) FROM " + relation).fetchone()[0]
        return {
            "physical_relation": actual,
            "columns": [{"name": n, "type": t} for (n, t) in schema],
            "rows": count,
            "materialized": wanted,
            "declared_relation_checked": bool(expected),
            "checked_columns": output.get("columns"),
            "checked_unique_by": grain,
            "checked_not_null": output.get("not_null", []),
        }

    async def materialize_files(self, outputs, install_dependencies=True):
        from .exceptions import ProgramRevisionRequired
        import yaml
        from .materialization_state import database_revision, execution_signature

        revision = database_revision(self.database)
        if (
            self.owned_database_revision is not None
            and self.owned_database_revision != revision
        ):
            self.execution_receipts.clear()
        if self.initial_relations is None:
            self.initial_relations = await self.executor.run(self.relation_inventory)
        present = await self.executor.run(self.relation_inventory)
        projects = {}
        for out in outputs:
            projects.setdefault(out["project_dir"], []).append(out)
        receipts = {}
        logs = []
        for project, goals in projects.items():
            root = Path(project)
            models = {}
            run_before = None
            run_started = False
            selected = set()
            stamps = {}
            manifest = None

            def retain_completed_execution(successful):
                if any((stamps.get(ident) is None for ident in selected)):
                    self.execution_receipts.clear()
                else:
                    for ident in selected:
                        key = (str(root.resolve()), ident)
                        if ident in successful:
                            self.execution_receipts[key] = stamps[ident]
                        else:
                            self.execution_receipts.pop(key, None)
                self.owned_database_revision = database_revision(self.database)
                if (
                    manifest is not None
                    and self.project
                    and (root.resolve() == self.project.resolve())
                ):
                    self.manifest = manifest

            try:
                if install_dependencies:
                    logs.append(await self.run_dbt(["dbt", "deps"], root))
                logs.append(await self.run_dbt(["dbt", "parse"], root))
                manifest = json.loads((root / "target/manifest.json").read_text())
                if manifest.get("metadata", {}).get("adapter_type") not in {
                    None,
                    "duckdb",
                }:
                    raise ValueError("The task execution backend is DuckDB")
                package = (
                    yaml.safe_load((root / "dbt_project.yml").read_text()) or {}
                )["name"]
                models = manifest.get("nodes", {})
                targets = {}
                selected = set()
                protected = set()
                reused = set()
                stamps = {}

                def relation_key(model):
                    return tuple(
                        (
                            str(model.get(k, "")).casefold()
                            for k in ["database", "schema", "alias"]
                        )
                    )

                for goal in goals:
                    path = Path(goal["path"]).resolve()
                    found = [
                        (ident, m)
                        for (ident, m) in models.items()
                        if m.get("resource_type") == "model"
                        and m.get("package_name") == package
                        and ((root / m.get("original_file_path", "")).resolve() == path)
                    ]
                    if len(found) != 1:
                        raise ValueError(
                            "Artifact is not one model in the parsed project: "
                            + str(path)
                        )
                    (ident, model) = found[0]
                    targets[ident] = (goal, model)
                    actual = {
                        "schema": model["schema"],
                        "name": model.get("alias", model["name"]),
                    }
                    expected = goal.get("relation")
                    if expected and any(
                        (
                            str(expected[k]).casefold() != str(actual[k]).casefold()
                            for k in ["schema", "name"]
                        )
                    ):
                        raise ProgramRevisionRequired(
                            goal["node"],
                            json.dumps(
                                {
                                    "condition": "physical_output_target",
                                    "path": goal["path"],
                                    "expected": expected,
                                    "actual": actual,
                                    "project": project,
                                    "configuration_sources": [
                                        str(root / "profiles.yml"),
                                        str(root / "dbt_project.yml"),
                                        goal["path"],
                                    ],
                                    "detail": "The actual dbt manifest schema can combine profile schema and custom config.schema. Preserve the required physical target.",
                                }
                            ),
                        )
                    if relation_key(model) in self.initial_relations:
                        raise ProgramRevisionRequired(
                            goal["node"],
                            "Output would overwrite a relation supplied as task input: "
                            + model["relation_name"],
                        )

                def visit(ident):
                    if ident in selected or ident in protected or ident in reused:
                        return
                    model = models.get(ident)
                    if not model or model.get("resource_type") != "model":
                        return
                    path = str((root / model.get("original_file_path", "")).resolve())
                    if ident not in targets and (
                        path in self.initial_model_files
                        or relation_key(model) in self.initial_relations
                    ):
                        protected.add(ident)
                        return
                    stamp = execution_signature(self, root, manifest, ident)
                    stamps[ident] = stamp
                    if (
                        stamp is not None
                        and self.execution_receipts.get((str(root.resolve()), ident))
                        == stamp
                        and (relation_key(model) in present)
                    ):
                        reused.add(ident)
                        return
                    selected.add(ident)
                    for parent in model.get("depends_on", {}).get("nodes", []):
                        visit(parent)

                for ident in targets:
                    visit(ident)
                selectors = [
                    "fqn:" + ".".join(models[ident]["fqn"])
                    for ident in sorted(selected)
                ]
                if selectors:
                    prior = root / "target/run_results.json"
                    run_before = (
                        hashlib.sha256(prior.read_bytes()).hexdigest()
                        if prior.exists()
                        else None
                    )
                    run_started = True
                    logs.append(
                        await self.run_dbt(["dbt", "run", "--select", *selectors], root)
                    )
                retain_completed_execution(selected)
                for ident, (goal, model) in targets.items():
                    dependencies = set()

                    def collect(name):
                        if name in dependencies:
                            return
                        dependencies.add(name)
                        for parent in (
                            models.get(name, {}).get("depends_on", {}).get("nodes", [])
                        ):
                            if parent in models:
                                collect(parent)

                    collect(ident)
                    dependency_files = sorted(
                        {
                            str((root / models[name]["original_file_path"]).resolve())
                            for name in dependencies
                            if models[name].get("original_file_path")
                        }
                    )
                    signature = self.project_signature(root, dependency_files)
                    checked = await self.executor.run(
                        self.inspect_materialized, goal, model
                    )
                    receipts[goal["path"]] = {
                        "project_dir": project,
                        "source_signature": signature,
                        "model_unique_id": ident,
                        "dependency_files": dependency_files,
                        "built_models": sorted(selected),
                        "reused_output_models": sorted(reused),
                        "reused_input_models": sorted(protected),
                        **checked,
                    }
                self.owned_database_revision = database_revision(self.database)
                self.operations.append(
                    {
                        "kind": "incremental_materialization",
                        "built_models": sorted(selected),
                        "reused_models": sorted(reused),
                        "protected_inputs": sorted(protected),
                    }
                )
                if self.project and root.resolve() == self.project.resolve():
                    self.manifest = manifest
            except ProgramRevisionRequired:
                if database_revision(self.database) != revision:
                    self.execution_receipts.clear()
                    self.owned_database_revision = None
                raise
            except Exception as exc:
                details = {
                    "condition": "artifact_build_or_check",
                    "project": project,
                    "error": str(exc),
                }
                run_result = root / "target/run_results.json"
                if (
                    run_started
                    and run_result.exists()
                    and (
                        hashlib.sha256(run_result.read_bytes()).hexdigest()
                        != run_before
                    )
                ):
                    result = json.loads(run_result.read_text())
                    successful = {
                        r.get("unique_id")
                        for r in result.get("results", [])
                        if r.get("status") == "success"
                    }
                    retain_completed_execution(successful)
                    self.operations.append(
                        {
                            "kind": "partial_materialization",
                            "successful_models": sorted(successful & selected),
                            "failed_or_skipped_models": sorted(selected - successful),
                            "retained_execution_certificates": len(
                                self.execution_receipts
                            ),
                        }
                    )
                    errors = [
                        r
                        for r in result.get("results", [])
                        if r.get("status") in {"error", "fail"}
                    ]
                    details["failed_models"] = [
                        {
                            "id": r.get("unique_id"),
                            "message": r.get("message"),
                            "path": str(
                                root / models[r["unique_id"]]["original_file_path"]
                            )
                            if r.get("unique_id") in locals().get("models", {})
                            else None,
                        }
                        for r in errors
                    ]
                elif database_revision(self.database) != self.owned_database_revision:
                    self.execution_receipts.clear()
                    self.owned_database_revision = None
                raise ProgramRevisionRequired(
                    goals[0]["node"], json.dumps(details, default=str)
                ) from exc
        return {
            "receipts": receipts,
            "paths": list(receipts),
            "tool_output": "\n".join(logs)[-12000:],
        }

    def query(self, sql, limit=30):
        import duckdb

        with duckdb.connect(self.database, read_only=True) as con:
            cur = con.execute(self.render(sql))
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit + 1)
            return {
                "columns": columns,
                "rows": rows[:limit],
                "truncated": len(rows) > limit,
            }

    def schema(self, sql):
        import duckdb

        with duckdb.connect(self.database, read_only=True) as con:
            return [(r[0], r[1]) for r in con.execute("DESCRIBE " + sql).fetchall()]

    def render(self, body):
        if "{{" not in body and "{%" not in body:
            return body.strip().rstrip(";")
        from jinja2 import Environment, StrictUndefined

        if self.manifest is None:
            path = self.project / "target/manifest.json" if self.project else None
            self.manifest = (
                json.loads(path.read_text()) if path and path.exists() else {}
            )

        def source(group, table):
            for s in self.manifest.get("sources", {}).values():
                if s["source_name"] == group and s["name"] == table:
                    return s["relation_name"]
            raise ValueError(
                f"Unobserved dbt source {group}.{table}; inspect existing source definitions"
            )

        def ref(name):
            for n in self.manifest.get("nodes", {}).values():
                if n.get("name") == name and n.get("relation_name"):
                    return n["relation_name"]
            raise ValueError("Unobserved dbt model " + name)

        target = {"type": "duckdb"}
        return (
            Environment(undefined=StrictUndefined)
            .from_string(body)
            .render(source=source, ref=ref, config=lambda **kw: "", target=target)
            .strip()
            .rstrip(";")
        )
