# PostgreSQL Indexing

A PostgreSQL index is an auxiliary data structure that lets the query
planner find rows without scanning the entire table. The right index
turns a sequential scan into an index scan or index-only scan, often
reducing query latency by orders of magnitude.

## Configuring an Index

Common index types and their use cases:

- **B-tree** (default): equality and range queries on ordered types.
- **Hash**: equality only; rarely faster than B-tree in practice.
- **GIN**: containment queries on arrays, JSONB, and `tsvector`.
- **GiST**: geometric, full-text, and exclusion-constraint queries.
- **BRIN**: extremely large tables where rows are clustered by insertion
  order.

```sql
CREATE INDEX idx_orders_user_created
  ON orders (user_id, created_at DESC);

CREATE INDEX idx_events_payload_gin
  ON events USING gin (payload jsonb_path_ops);

CREATE INDEX CONCURRENTLY idx_users_email_lower
  ON users (lower(email));
```

`CREATE INDEX CONCURRENTLY` builds without taking a write lock, at the
cost of two table scans.

## Best Practices

- Index columns used in `WHERE`, `JOIN ... ON`, and `ORDER BY` clauses;
  do not index columns the planner never selects on.
- Composite indexes follow the leftmost-prefix rule — order columns
  from most selective to least.
- Use partial indexes (`WHERE status = 'active'`) when the filtered
  subset dominates the workload.
- Run `ANALYZE` after bulk loads so the planner has fresh statistics.

## Troubleshooting

- `EXPLAIN (ANALYZE, BUFFERS)` shows whether the planner picks the index;
  a sequential scan on a large table is the warning sign.
- Bloat from frequent updates inflates index size; rebuild with
  `REINDEX INDEX CONCURRENTLY`.
- Conflicting statistics may force a sequential scan even when an index
  exists; refresh with `ANALYZE table_name`.
