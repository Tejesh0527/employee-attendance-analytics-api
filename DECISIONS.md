# DECISIONS.md

1. **Indexes.**
   `employees`: Unique `{"emp_code": 1}`; `{"department": 1, "emp_code": 1}`; `{"joined_on": 1}` for headcount.
   `attendance_logs`: Unique `{"emp_code": 1, "date": 1}`; `{"date": -1, "emp_code": 1}` for list sorting; `{"emp_code": 1, "punch_in": -1}` for punch-out lookups; `{"date": 1, "late_minutes": 1}` for the leaderboard.
   *Rejected*: Index on `status` due to low cardinality (5 values).

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. The unique index on `(emp_code, date)` serializes them: one write commits (201), the other raises `DuplicateKeyError`, caught and returned as 409 Conflict.

3. **Ties.**
   Using MongoDB's `$rank`, ties receive identical ranks (1, 2, 2, 4). Filtering by `rank <= limit` returns all tied employees at the cutoff, ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation matches `employees` (`joined_on <= month_end`) and groups by department before joining `attendance_logs`, preserving staff with zero logs.

5. **One thing you would change for 100x data.**
   Maintain pre-computed daily department rollups and employee monthly summaries via background workers on punch events, avoiding real-time cross-collection joins and window stages at scale.

## Additional notes

- **Rounding in trend pipeline:** Full precision is kept in MongoDB window calculations. Python rounds to 4 decimals half-up via `round_half_up` to avoid MongoDB `$round` banker's rounding breaking R8. The 7-day moving average is computed over unrounded rates to prevent compounding intermediate rounding errors.
- **Trend range limit:** Evaluated as `(to - from).days > 92`. In Python date arithmetic, subtracting dates yields the duration delta in days (e.g., Jan 1 to Jan 2 is 1 day delta, spanning 2 dates). A 92-day delta permits an inclusive span of up to 93 dates.
