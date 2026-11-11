import json
from pathlib import Path
from .sql_dependencies import sql_dependencies
from .sql_semantics import SQL_SEMANTICS
from .python_context import excerpt

SQL_PROMPT = (
    """Construct the assigned SQL artifacts for the original task. Return
one JSON object {"files":[{"path":"project-relative.sql","sql":"literal SQL"}]}.
The SQL strings ARE the final file contents. Do not emit Python, f-strings, string
formatting helpers or a program which generates SQL. Write normal dbt Jinja
references directly, for example {{ ref('actual_model_name') }}. Use literal ref
and source names. Do not set schema/alias; the runtime owns the declared target.
The actual backend is already bound: emit only its native implementation.
Do not generate inactive target.type branches or alternative backend programs.

Construct the supplied region within the completion token budget. If a
batch_file_limit is supplied, respect it. Implement only the assigned
targets and necessary helper models. Helpers must use the supplied helper_prefix
to avoid conflicts with concurrent independent construction. Other required
targets may be referenced using their public interfaces; runtime retains the
draft and waits for those definitions before building. Do not duplicate another
assigned target or overwrite supplied input models. Use existing source models
and actual observed fields/types. Preserve already accepted output interfaces.
For an existing ref(), compiled_output_interface is its actual returned schema;
upstream raw-source columns are provenance, not that model's returned fields.
input_catalog.project.named_binding_index lists every available named input.
named_bindings expands actual source SQL, fields and keys for this region. If
an additional indexed source is needed, return {"inspect_sources":["model_name"]}
instead of files. Do not guess its schema or repeat complete source discovery.
This is optional addressed inspection, not a mandatory extra stage.

"""
    + SQL_SEMANTICS
    + """

Keep SQL concise and share genuinely common intermediate work; avoid repetitive
boilerplate and explanatory comments. Implement every original condition for the
assigned targets. A successful syntax/build check does not establish semantics.
During repair, return only the corrected affected SQL files; retain unrelated
artifacts. Do not replace valid logic merely to silence an error. No benchmark
graders, reference answers, other runs, or external networks are available.
"""
)


class SQLConstruction:
    def canonical_path(self, key):
        if not isinstance(key, str):
            raise ValueError("Artifact path must be a string")
        project = self.sql.project.resolve()
        declared = self.contract["targets"].get(Path(key).stem)
        if "/" not in key and declared and declared.get("path"):
            path = project / declared["path"]
        elif "/" not in key:
            path = project / "models/_ahead_helpers" / (Path(key).stem + ".sql")
        else:
            path = Path(key) if Path(key).is_absolute() else project / key
        path = path.resolve()
        if not path.is_relative_to(project) or path.suffix != ".sql":
            raise ValueError("SQL artifacts must stay inside the project")
        if (
            declared
            and declared.get("path")
            and (path != (project / declared["path"]).resolve())
        ):
            raise ValueError("Required artifact path is " + declared["path"])
        if (
            str(path) in self.initial_model_bytes
            and path.stem not in self.contract["targets"]
        ):
            raise ValueError("Supplied input models are read-only: " + str(path))
        return path

    def stage(self, packet, assigned, prefix, repair=False):
        limit = (
            self.model.config.get("artifact_patch_files", 8)
            if repair
            else self.model.config.get("artifact_batch_files", 4)
        )
        if not repair and self.model.config.get("construction_regions", False):
            limit = None
        files = packet.get("files") if isinstance(packet, dict) else None
        if (
            not isinstance(files, list)
            or not files
            or (limit is not None and len(files) > limit)
        ):
            raise ValueError("Return a nonempty bounded files batch")
        staged = {}
        for item in files:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "sql"}
                or (not isinstance(item["sql"], str))
                or (not item["sql"].strip())
            ):
                raise ValueError(
                    "Each artifact needs exactly path and literal sql strings"
                )
            if self.model.config.get("bound_program_repairs", False):
                from .bound_sql import bind_known

                original = item["sql"]
                item = {**item, "sql": bind_known(original)}
                if item["sql"] != original:
                    self.event(
                        "sql_environment_bound",
                        path=item["path"],
                        backend="duckdb",
                        original_characters=len(original),
                        bound_characters=len(item["sql"]),
                    )
            if self.model.config.get("observed_reference_lowering", False):
                from .sql_references import bind_observed_references

                item = {**item, "sql": bind_observed_references(self, item["sql"])}
            path = self.canonical_path(item["path"])
            name = path.stem
            if name in staged:
                raise ValueError("Duplicate artifact name in a batch: " + name)
            if name not in assigned and (not name.startswith(prefix)):
                raise ValueError(
                    "Construct only assigned targets or helpers named with " + prefix
                )
            dependencies = sql_dependencies(item["sql"])
            if name in dependencies:
                raise ValueError("A SQL model cannot ref itself: " + name)
            staged[name] = {
                "path": path,
                "sql": item["sql"],
                "dependencies": dependencies,
            }
        self.drafts.update(staged)
        for name in staged:
            self.rejected.pop(name, None)
        for name, item in staged.items():
            folder = self.directory / "sql_drafts"
            folder.mkdir(exist_ok=True)
            (folder / (name + ".sql")).write_text(item["sql"])
        self.event(
            "sql_batch_staged",
            names=list(staged),
            dependencies={n: sorted(x["dependencies"]) for (n, x) in staged.items()},
        )

    def ready_drafts(self):
        available = set(self.existing_models) | set(self.produced)
        ready = {}
        remaining = dict(self.drafts)
        while True:
            layer = [
                n for (n, d) in remaining.items() if d["dependencies"] <= available
            ]
            if not layer:
                break
            for name in layer:
                ready[name] = remaining.pop(name)
                available.add(name)
        return ready

    def unresolved_dependencies(self):
        supplied = set(self.existing_models) | set(self.produced) | set(self.drafts)
        return sorted(
            {
                n
                for d in self.drafts.values()
                for n in d["dependencies"]
                if n not in supplied
            }
        )

    def reuse_candidates(self, assigned):
        targets = self.contract["targets"]
        selected = {}
        for name in assigned:
            target = targets.get(name, {})
            columns = set(target.get("columns", []))
            grain = set(target.get("grain", []))
            source = target.get("source_relation")
            ranked = []
            for other, path in self.produced.items():
                known = targets.get(other, {})
                if (
                    other in assigned
                    or str(path) not in self.receipts
                    or (not source)
                    or (known.get("source_relation") != source)
                ):
                    continue
                overlap = columns & set(known.get("columns", []))
                other_grain = set(known.get("grain", []))
                if not grain or not grain <= other_grain or len(overlap) < 2:
                    continue
                ranked.append(
                    (
                        len(overlap) / max(1, len(columns)) + (grain == other_grain),
                        other,
                    )
                )
            for _, other in sorted(ranked, reverse=True)[:2]:
                if other not in selected:
                    selected[other] = {
                        "path": str(self.produced[other]),
                        "declared_contract": targets[other],
                        "sql": excerpt(self.produced[other].read_text(), 6000),
                        "candidate_for": [],
                    }
                selected[other]["candidate_for"].append(name)
        return selected

    def stage_edits(self, packet, assigned, prefix, bases):
        edits = packet.get("edits") if isinstance(packet, dict) else None
        if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
            raise ValueError("Return a nonempty bounded edits array")
        changed = {}
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {
                "path",
                "old",
                "new",
                "expected_matches",
            }:
                raise ValueError("An edit needs path, old, new and expected_matches")
            path = self.canonical_path(edit["path"])
            name = path.stem
            if name not in assigned and (not name.startswith(prefix)):
                raise ValueError("Patch only assigned files or new helper_prefix files")
            if (
                not all((isinstance(edit[k], str) for k in ("old", "new")))
                or type(edit["expected_matches"]) is not int
            ):
                raise ValueError("Invalid literal edit")
            if str(path) in bases:
                if not path.is_file() or path.read_text() != bases[str(path)]:
                    raise ValueError(
                        "The file changed since the repair request: " + str(path)
                    )
                body = changed.get(str(path), bases[str(path)])
                if not edit["old"] or not 1 <= edit["expected_matches"] <= 100:
                    raise ValueError(
                        "Existing file edits require nonempty old and positive expected_matches"
                    )
                actual = body.count(edit["old"])
                if actual != edit["expected_matches"]:
                    raise ValueError(
                        f"Exact patch matched {actual} times, expected {edit['expected_matches']}: {path}"
                    )
                changed[str(path)] = body.replace(edit["old"], edit["new"])
            else:
                if (
                    path.exists()
                    or str(path) in changed
                    or (not name.startswith(prefix))
                ):
                    raise ValueError("A new file must be a new prefixed helper")
                if edit["old"] or edit["expected_matches"] != 0:
                    raise ValueError(
                        "New helper edits require empty old and zero matches"
                    )
                changed[str(path)] = edit["new"]
        self.stage(
            {"files": [{"path": p, "sql": s} for (p, s) in changed.items()]},
            assigned,
            prefix,
            repair=True,
        )
        self.event(
            "sql_exact_patch_staged",
            files=list(changed),
            edits=len(edits),
            patch_characters=sum((len(e["old"]) + len(e["new"]) for e in edits)),
            result_characters=sum((len(s) for s in changed.values())),
        )

    async def sql_batch(self, assigned, prefix, feedback=None, repair=False):
        from .construction_inputs import ConstructionInputs
        from .inspection_state import InspectionState

        targets = self.contract["targets"]
        inputs = ConstructionInputs(self.input_catalog.snapshot, targets, assigned)
        inspection = InspectionState()
        region_mode = self.model.config.get("construction_regions", False)
        expanded = set(self.produced)
        if region_mode:
            from .construction_regions import ConstructionRegions

            expanded = ConstructionRegions(targets, inputs.bindings).relevant_accepted(
                assigned, self.produced
            )
            expanded.update(set(getattr(self, "entry_models", ())) & set(self.produced))
        work = {
            "proposed_semantic_bindings": getattr(self, "entry_semantic_bindings", []),
            "assigned_targets": {
                name: targets.get(
                    name,
                    {
                        "name": name,
                        "reason": "A retained draft references this missing model",
                    },
                )
                for name in assigned
            },
            "batch_file_limit": None
            if region_mode
            else self.model.config.get("artifact_batch_files", 4),
            "helper_prefix": prefix,
            "required_schema": self.contract["schema"],
            "repair": repair,
            "feedback": feedback,
            "cross_artifact_constraints": [
                c
                for c in self.contract.get("aggregate_constraints", [])
                if any((c[s]["model"] in assigned for s in ("left", "right")))
            ],
            "uncompiled_public_constraints": [
                c
                for c in self.contract.get("uncompiled_constraints", [])
                if set(c["models"]) & set(assigned)
            ],
            "reuse_candidates": self.reuse_candidates(assigned),
            "rejected_candidates": {
                name: self.rejected[name] for name in assigned if name in self.rejected
            },
            "accepted_interfaces": {
                name: {
                    "path": str(path),
                    "actual_sql": path.read_text(),
                    "declared_contract": {
                        k: targets.get(name, {}).get(k)
                        for k in (
                            "grain",
                            "entity_key",
                            "source_relation",
                            "source_scope",
                            "not_null",
                            "metric_definitions",
                        )
                    },
                    **{
                        k: v
                        for (k, v) in self.receipts.get(str(path), {}).items()
                        if k in {"physical_relation", "columns", "row_count", "rows"}
                    },
                }
                for (name, path) in self.produced.items()
                if name in expanded
            },
            "retained_drafts": {
                n: {"path": str(d["path"]), "dependencies": sorted(d["dependencies"])}
                for (n, d) in self.drafts.items()
            },
        }
        if region_mode:
            from .sql_context import constructor_facts

            work["all_output_interfaces"] = constructor_facts(
                "", self.contract, {}, {}
            )["output_contract"]
            work["accepted_source_index"] = {
                name: {
                    "path": str(path),
                    "read": "inspect_sources with this exact model name returns its complete accepted SQL",
                }
                for (name, path) in self.produced.items()
            }
        if (
            region_mode
            and self.model.config.get("complete_small_regions", False)
            and (len(assigned) > 1)
        ):
            planner = ConstructionRegions(targets, inputs.bindings)
            estimated = sum((planner.cost.get(n, 768) for n in assigned))
            if estimated <= self.model.config.get(
                "construction_region_work_tokens", 4096
            ):
                work["complete_assigned_region"] = True
                work["minimum_region_files"] = len(assigned)
        if repair:
            work["current_sql"] = {
                name: excerpt(self.produced[name].read_text(), 8000)
                for name in assigned
                if name in self.produced
            }
        stable = {
            "original_task": self.task["question"],
            "environment": {
                "project": str(self.sql.project),
                "database": self.sql.database,
                "backend": "duckdb",
            },
            "input_catalog": inputs.view(),
        }
        self.event("sql_batch_requested", assigned=assigned, repair=repair)
        while True:
            stable["input_catalog"] = inputs.view()
            self.event(
                "sql_construction_inputs",
                assigned=assigned,
                expanded=sorted(inputs.selected),
                total_bindings=len(inputs.bindings),
                characters=len(
                    json.dumps(stable["input_catalog"], ensure_ascii=False, default=str)
                ),
            )
            from .semantic_bindings import BINDING_PROMPT, validate_bindings

            prompt = SQL_PROMPT + BINDING_PROMPT
            if work.get("complete_assigned_region"):
                prompt += """
This small bounded region is one complete construction responsibility. Define every assigned target in this response. Future data values flow through the declared producer interfaces during execution; they do not require another construction turn for a known consumer. Use inspect_sources for a genuinely missing source interface before constructing files.
"""
            if any(
                (t.get("row_constraints") for t in work["assigned_targets"].values())
            ):
                prompt += """
The assigned output has executable mandatory row-coverage constraints. Parameter values needed only to execute known logic can be derived in SQL from the complete eligible data. Do not assume guessed fixed thresholds meet a minimum coverage bound. Preserve the original statistic populations and identity, and do not fabricate rows to meet a bound.
"""
            if self.model.config.get("program_requirement_checks", False):
                from .program_checks import CHECK_PROMPT

                prompt += CHECK_PROMPT
            messages = [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(stable, ensure_ascii=False, default=str),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {**work, **inspection.context()},
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ]
            if self.model.config.get(
                "semantic_construction_dependencies", False
            ) and hasattr(self.model, "ask_with_prefix"):
                from .semantic_support import support, PrefixArray

                listener = PrefixArray(
                    lambda bindings: support(self).offer(bindings, assigned)
                )
                text = await self.model.ask_with_prefix(
                    "native_sql_batch",
                    messages,
                    listener.feed,
                    raw=True,
                    output_cap=self.model.config.get(
                        "artifact_batch_output_tokens", 8192
                    ),
                )
            else:
                text = await self.model.ask(
                    "native_sql_batch",
                    messages,
                    raw=True,
                    output_cap=self.model.config.get(
                        "artifact_batch_output_tokens", 8192
                    ),
                )
            packet = json.loads(text)
            if isinstance(packet, dict) and set(packet) == {"inspect_sources"}:
                try:
                    names = packet["inspect_sources"]
                    if (
                        not isinstance(names, list)
                        or not names
                        or any((not isinstance(n, str) for n in names))
                    ):
                        raise ValueError("inspect_sources needs exact indexed names")
                    before = set(inputs.selected) | expanded
                    originals = [n for n in names if n not in self.produced]
                    if originals:
                        inputs.expand(originals)
                    for name in names:
                        if name in self.produced:
                            expanded.add(name)
                            work["accepted_interfaces"][name] = {
                                "path": str(self.produced[name]),
                                "actual_sql": self.produced[name].read_text(),
                                "declared_contract": targets.get(name, {}),
                                **{
                                    k: v
                                    for (k, v) in self.receipts.get(
                                        str(self.produced[name]), {}
                                    ).items()
                                    if k
                                    in {
                                        "physical_relation",
                                        "columns",
                                        "row_count",
                                        "rows",
                                    }
                                },
                            }
                    work.pop("inspection_error", None)
                    inspection.record(
                        packet,
                        {"expanded": sorted(set(inputs.selected) | expanded)},
                        "task_input_snapshot",
                        set(inputs.selected) | expanded != before,
                    )
                except (ValueError, TypeError) as exc:
                    work["inspection_error"] = str(exc)
                continue
            try:
                bindings = validate_bindings(
                    self.task["question"],
                    self.input_catalog.snapshot,
                    packet.get("semantic_bindings", []),
                )
            except ValueError as exc:
                bindings = []
                self.event(
                    "semantic_binding_rejected",
                    assigned=assigned,
                    error=str(exc),
                    analytical_files_retained=True,
                )
            if self.model.config.get("semantic_construction_dependencies", False):
                from .semantic_support import support

                support(self).offer(
                    packet.get("semantic_bindings", []), assigned, admit=True
                )
                if self.model.config.get("semantic_inputs_before_execution", False):
                    packet = await support(self).finish_packet(packet, assigned)
            self.semantic_bindings = getattr(self, "semantic_bindings", {})
            self.semantic_bindings[",".join(assigned)] = bindings
            self.event(
                "semantic_bindings_constructed",
                assigned=assigned,
                bindings=bindings,
                model_authored=True,
            )
            return packet
