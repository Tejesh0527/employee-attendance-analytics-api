# Employee Attendance & Analytics API

FastAPI + MongoDB backend for employee attendance tracking, regularization, and multi-dimensional analytics.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (or 7.0+) running locally or on MongoDB Atlas

## Installation & Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment (optional, defaults to `mongodb://localhost:27017` and `attendance_db`):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

3. Seed initial sample data:
```bash
python sample_seed.py
```

4. Run the API server:
```bash
uvicorn app.main:app --port 8000
```

Interactive OpenAPI documentation is available at `http://localhost:8000/docs`.

## Key Features & Highlights
- **Lifespan Index Initialization**: Creates all required unique and compound indexes idempotently at startup to prevent concurrency conflicts and support query index scans (`IXSCAN`).
- **Race Condition Prevention**: Database-level unique constraints and atomic find-and-update operations prevent race conditions for simultaneous punch-in, punch-out, and regularization requests.
- **Aggregations in Database**: Analytics endpoints are performed via MongoDB aggregation pipelines (including `$densify` for date range gap filling and `$setWindowFields` for competition ranking and 7-day moving averages).
- **Explain Endpoint**: Dedicated `/admin/explain/{endpoint}` route providing query planner execution stats and proving IXSCAN usage without collection scans (`COLLSCAN`).
