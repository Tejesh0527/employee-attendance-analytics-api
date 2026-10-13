# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique `{"emp_code": 1}` for identity and race prevention; `{"department": 1, "emp_code": 1}` for filtered list ordering; `{"department": 1, "joined_on": 1}` and `{"joined_on": 1}` for fast headcount filtering.
   - `attendance_logs`: Unique compound `{"emp_code": 1, "date": 1}` enforcing the one-log-per-day rule; `{"date": -1, "emp_code": 1}` backing list sorting; `{"emp_code": 1, "punch_in": -1}` for punch-out lookups (the punch-out query filters on `emp_code` and `punch_in` and sorts by `punch_in` descending — **not** `date` — so an `(emp_code, date)` index would require an in-memory sort); `{"date": 1, "late_minutes": 1}` providing an IXSCAN for the leaderboard.
   - *Rejected*: An isolated index on `status` due to very low cardinality (5 values), which offers negligible selectivity and increases write overhead.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. The database unique index on `(emp_code, date)` atomically serializes them: one write commits (201 Created), while the other triggers a `DuplicateKeyError`, caught by our handler to return 409 Conflict.

3. **Ties.**
   Using MongoDB's `$rank` window function, ties receive identical ranks (e.g., 1, 2, 2, 4). We filter by `rank <= limit`, so all tied employees at the cutoff are included, potentially returning more than `limit` rows. Tied entries are ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation starts on `employees` (filtering `joined_on <= month_end`) and groups by department before joining `attendance_logs`. Employees with zero logs are preserved, ensuring headcount accurately reflects active staff.

5. **Rounding in the trend pipeline.**
   `attendance_rate` and `moving_avg_7d` are kept at full floating-point precision inside the MongoDB pipeline. `$round` is intentionally **not** used in the pipeline because MongoDB's `$round` uses round-half-to-even (banker's rounding), which breaks R8's half-up requirement (e.g. 1/32 = 0.03125 would round to 0.0312 instead of the correct 0.0313). Both values are rounded to 4 decimal places in Python using `round_half_up()` after retrieval. The 7-day moving average is computed over unrounded `attendance_rate` values so that each window average is as precise as possible before the final Python rounding step.

6. **Trend date range limit ("exceeds 92 days").**
   The endpoint validates `(to - from).days > 92`. With date difference arithmetic in Python, `(to - from).days` measures the elapsed duration in days (e.g., from 2026-01-01 to 2026-01-02 is 1 day difference, representing a 2-day inclusive span). Thus, a 92-day difference allows an inclusive span of up to 93 calendar days (corresponding to roughly one full calendar quarter / 3 consecutive months such as Jan 1 to Apr 3), matching the standard interpretation of "range does not exceed 92 days of delta".

7. **One thing you would change for 100x data.**
   Maintain pre-computed daily department rollups and employee monthly summaries via background workers on punch events, avoiding real-time cross-collection joins and full window aggregations at scale.

