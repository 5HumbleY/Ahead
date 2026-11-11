SQL_SEMANTICS = """Each measure has an input grain and a population. Before joining one-to-many
data, aggregate the child to the parent key when retaining parent counts or
amounts. COUNT DISTINCT alone does not repair duplicated SUMs; SUM DISTINCT
amount incorrectly merges different records with equal amounts. A GROUP BY key
may be NULL: retain that population unless this target explicitly excludes it.
Declared entity_key is different: it comes from existing input key declarations
at the same entity grain and must be unique and nonnull in the cleaned output.
Reuse compatible accepted calculations supplied in reuse_candidates through
ref(...); check their declared scope and SQL before reuse, and recompute ratios
at the requested grain. Do not independently recalculate an existing measure
when its definition and population already satisfy the target.

The assigned target includes verbatim original_requirement_regions when addressed
headings are available. These requirements govern its row identity, multiplicity,
population, directions, filters and formulas; column names alone are not its contract.
Implement EVERY stated clause, including transformations that change row grain
between producer and consumer. Preserve source populations unless the task requires
a filter; do not invent status exclusions. Use actual_sql for accepted upstream
semantics, not a guess from its output column names.

Bind each source field to the event or concept actually requested, using the
observed schema and original clauses, rather than a similar field name. Each
measure keeps its own population and predicate. Reuse an intermediate measure
only when its complete selection and aggregation match the consumer requirement;
a shared alias, correlated value or overlapping window is insufficient. Do not
silently add filters or collapse source records to a different grain.
"""
