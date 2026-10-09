# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique `{"emp_code": 1}` for identity and race prevention; `{"department": 1, "emp_code": 1}` for filtered list ordering; `{"department": 1, "joined_on": 1}` and `{"joined_on": 1}` for fast headcount filtering.
   - `attendance_logs`: Unique compound `{"emp_code": 1, "date": 1}` enforcing the one-log-per-day rule; `{"date": -1, "emp_code": 1}` backing list sorting; `{"emp_code": 1, "date": -1}` for punch-out lookups; `{"date": 1, "late_minutes": 1}` providing an IXSCAN for the leaderboard.
   - *Rejected*: An isolated index on `status` due to very low cardinality (5 values), which offers negligible selectivity and increases write overhead.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. The database unique index on `(emp_code, date)` atomically serializes them: one write commits (201 Created), while the other triggers a `DuplicateKeyError`, caught by our handler to return 409 Conflict.

3. **Ties.**
   Using MongoDB's `$rank` window function, ties receive identical ranks (e.g., 1, 2, 2, 4). We filter by `rank <= limit`, so all tied employees at the cutoff are included, potentially returning more than `limit` rows. Tied entries are ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation starts on `employees` (filtering `joined_on <= month_end`) and groups by department before joining `attendance_logs`. Employees with zero logs are preserved, ensuring headcount accurately reflects active staff.

5. **One thing you would change for 100x data.**
   Maintain pre-computed daily department rollups and employee monthly summaries via background workers on punch events, avoiding real-time cross-collection joins and full window aggregations at scale.
