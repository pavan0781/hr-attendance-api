# Employee Attendance & Analytics API

FastAPI and MongoDB attendance API for the HROne engineering assignment.

## Run

Requires Python 3.11+ and MongoDB 6.0+.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

The API reads `MONGO_URI` and `MONGO_DB` from the process environment, which
takes precedence over a local `.env`; defaults are
`mongodb://localhost:27017` and `attendance_db`. `GET /health` returns 200 when
MongoDB is ready. The grader or operator must seed the `employees` and
`attendance_logs` collections before using attendance and analytics endpoints.
Indexes are created idempotently at startup.

## Known limitations

- This assignment API has no authentication or authorization;
  `regularized_by` is free text.
- Sample data and local test/scale harnesses are not included in this submission.
- Scale timings are local measurements, not production performance guarantees.
