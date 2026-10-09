"""uvicorn app.main:app --port 8000"""

from copy import deepcopy
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
logger = logging.getLogger(__name__)
indexes_ready = False

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
VALID_STATUS = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
PRESENCE_STATUS = {"PRESENT", "WFH", "ON_DUTY"}
IST_TZ = "+05:30"
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def truncate_instant(value: datetime) -> datetime:
    """Normalize an instant to the whole-second precision of the API contract."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def ms_to_dt(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Epoch milliseconds must be an integer")
    if not (100000000000 <= value <= 4102444800000):
        raise ValueError("Epoch milliseconds out of range")
    return truncate_instant(datetime.fromtimestamp(value // 1000, tz=UTC))


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_instant(value).timestamp() * 1000)


def truncate_record_instants(record: Dict[str, Any]) -> Dict[str, Any]:
    for field in ("punch_in", "punch_out"):
        if record.get(field) is not None:
            record[field] = truncate_instant(record[field])
    return record


def normalize_history_instants(history: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    normalized = deepcopy(history)
    for entry in normalized:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = truncate_instant(entry["at"])
        for field, change in (entry.get("changes") or {}).items():
            if field not in {"punch_in", "punch_out"}:
                continue
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = truncate_instant(change[side])
    return normalized


def normalize_stored_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    record = doc
    for _ in range(6):
        normalized = truncate_record_instants(dict(record))
        history = record.get("history")
        normalized_history = (
            normalize_history_instants(history)
            if isinstance(history, list)
            else history
        )

        updates = {}
        query = {"_id": record["_id"]}
        for field in ("punch_in", "punch_out"):
            if normalized.get(field) != record.get(field):
                updates[field] = normalized[field]
                query[field] = (
                    record[field]
                    if field in record
                    else {"$exists": False}
                )
        if normalized_history != history:
            updates["history"] = normalized_history
            query["history"] = (
                history if "history" in record else {"$exists": False}
            )
        if not updates:
            return record

        result = db.attendance_logs.update_one(query, {"$set": updates})
        if result.matched_count:
            refreshed = db.attendance_logs.find_one({"_id": record["_id"]})
            if refreshed is None:
                raise HTTPException(status_code=404, detail="Attendance record not found")
            record = refreshed
            continue

        refreshed = db.attendance_logs.find_one({"_id": record["_id"]})
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Attendance record not found")
        record = refreshed

    raise HTTPException(
        status_code=409,
        detail="Attendance record changed repeatedly during timestamp normalization",
    )


def normalize_stored_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    employee = doc
    for _ in range(6):
        created_at = employee.get("created_at")
        if not isinstance(created_at, datetime):
            return employee
        normalized = truncate_instant(created_at)
        if normalized == created_at:
            return employee

        db.employees.update_one(
            {"_id": employee["_id"], "created_at": created_at},
            {"$set": {"created_at": normalized}},
        )
        refreshed = db.employees.find_one({"_id": employee["_id"]})
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Employee not found")
        employee = refreshed

    raise HTTPException(
        status_code=409,
        detail="Employee changed repeatedly during timestamp normalization",
    )


def round_half_up(value: float | int | Decimal, places: int) -> float:
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def is_working_day(day: str) -> bool:
    return datetime.strptime(day, "%Y-%m-%d").weekday() < 5


def parse_time_hhmm(value: str) -> tuple[int, int]:
    if not TIME_RE.fullmatch(value):
        raise ValueError(f"Invalid time: {value}")
    h, m = value.split(":")
    return int(h), int(m)


def dt_in_ist(value: datetime) -> datetime:
    return truncate_instant(value).astimezone(IST)


def date_string(value: datetime) -> str:
    return dt_in_ist(value).strftime("%Y-%m-%d")


def resolve_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_ist = dt_in_ist(punch_in)
    start_h, start_m = parse_time_hhmm(shift_start)
    end_h, end_m = parse_time_hhmm(shift_end)
    shift_is_overnight = end_h < start_h or (end_h == start_h and end_m <= start_m)
    if shift_is_overnight:
        end_time = (end_h, end_m)
        current_time = (punch_ist.hour, punch_ist.minute)
        if current_time < end_time:
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def effective_shift_start(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    punch_ist = dt_in_ist(punch_in)
    start_h, start_m = parse_time_hhmm(shift_start)
    end_h, end_m = parse_time_hhmm(shift_end)
    shift_is_overnight = end_h < start_h or (end_h == start_h and end_m <= start_m)
    d = punch_ist.date()
    if shift_is_overnight and (punch_ist.hour, punch_ist.minute) < (end_h, end_m):
        d = d - timedelta(days=1)
    return datetime.combine(d, datetime.min.time().replace(hour=start_h, minute=start_m), tzinfo=IST)


def effective_shift_end(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    end_h, end_m = parse_time_hhmm(shift_end)
    start_h, start_m = parse_time_hhmm(shift_start)
    shift_is_overnight = end_h < start_h or (end_h == start_h and end_m <= start_m)
    d = datetime.strptime(attendance_date, "%Y-%m-%d").date()
    if shift_is_overnight:
        d = d + timedelta(days=1)
    return datetime.combine(d, datetime.min.time().replace(hour=end_h, minute=end_m), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    start = effective_shift_start(punch_in, shift_start, shift_end)
    punch_ist = dt_in_ist(punch_in)
    elapsed_seconds = (punch_ist - start).total_seconds()
    if elapsed_seconds > 10 * 60:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = truncate_instant(punch_in)
    punch_out = truncate_instant(punch_out)
    seconds = Decimal(int((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    end = effective_shift_end(attendance_date, shift_start, shift_end)
    punch_ist = dt_in_ist(punch_out)
    delta_minutes = int((punch_ist - end).total_seconds() / 60)
    if delta_minutes >= 30:
        return delta_minutes
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return round_half_up(work_hours, 2) < 4.5


def employee_from_db(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc = normalize_stored_employee(doc)
    out = dict(doc)
    out["created_at"] = dt_to_ms(out["created_at"])
    if "_id" in out:
        out.pop("_id", None)
    return out


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = {"at": dt_to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": {}}
    for key, change in (entry.get("changes") or {}).items():
        out["changes"][key] = {
            "from": dt_to_ms(change["from"]) if key in {"punch_in", "punch_out"} and change.get("from") is not None else change.get("from"),
            "to": dt_to_ms(change["to"]) if key in {"punch_in", "punch_out"} and change.get("to") is not None else change.get("to"),
        }
    return out


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc = normalize_stored_attendance(doc)
    out = {}
    out["emp_code"] = doc.get("emp_code")
    out["date"] = doc.get("date")
    out["status"] = doc.get("status")
    out["punch_in"] = dt_to_ms(doc.get("punch_in")) if doc.get("punch_in") is not None else None
    out["punch_out"] = dt_to_ms(doc.get("punch_out")) if doc.get("punch_out") is not None else None
    out["work_hours"] = doc.get("work_hours")
    out["late_minutes"] = doc.get("late_minutes")
    out["overtime_minutes"] = doc.get("overtime_minutes")
    out["half_day"] = doc.get("half_day", False)
    out["history"] = [serialize_history_entry(entry) for entry in (doc.get("history") or [])]
    return out


def build_employee_record(body: "EmployeeIn") -> Dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = truncate_instant(datetime.now(tz=UTC))
    return doc


def fetch_employee_or_404(emp_code: str) -> Dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


def fetch_attendance_or_404(emp_code: str, date_str: str) -> Dict[str, Any]:
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not doc:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    return doc


def validate_month(month: str) -> None:
    try:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError
        datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid month") from exc


def validate_date_string(value: str, field_name: str = "date") -> None:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}") from exc


def next_month(month: str) -> str:
    year, month_num = month.split("-")
    dt = datetime.strptime(f"{year}-{month_num}-01", "%Y-%m-%d")
    if month_num == "12":
        return f"{int(year) + 1}-01"
    return f"{year}-{int(month_num) + 1:02d}"


def month_range(month: str) -> tuple[str, str]:
    first = f"{month}-01"
    y, m = month.split("-")
    year = int(y)
    month_num = int(m)
    if month_num == 12:
        last_day = 31
    else:
        last_day = (datetime(year, month_num + 1, 1) - timedelta(days=1)).day
    return first, f"{month}-{last_day:02d}"


def compute_month_end(month: str) -> str:
    return month_range(month)[1]


def employee_monthly_pipeline(emp_code: str, month: str):
    start, end = month_range(month)
    month_end = {
        "$dateFromString": {
            "dateString": {"$literal": end},
            "timezone": "+00:00",
        }
    }
    effective_start = {
        "$dateFromString": {
            "dateString": {
                "$cond": [
                    {"$gt": ["$joined_on", start]},
                    "$joined_on",
                    {"$literal": start},
                ]
            },
            "timezone": "+00:00",
        }
    }
    working_days = {
        "$cond": [
            {"$lte": ["$joined_on", end]},
            {
                "$size": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": {
                                    "$range": [
                                        0,
                                        {
                                            "$add": [
                                                {
                                                    "$dateDiff": {
                                                        "startDate": effective_start,
                                                        "endDate": month_end,
                                                        "unit": "day",
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                "as": "day_offset",
                                "in": {
                                    "$dateAdd": {
                                        "startDate": effective_start,
                                        "unit": "day",
                                        "amount": "$$day_offset",
                                        "timezone": "+00:00",
                                    }
                                },
                            }
                        },
                        "as": "calendar_day",
                        "cond": {
                            "$lte": [
                                {"$isoDayOfWeek": "$$calendar_day"},
                                5,
                            ]
                        },
                    }
                }
            },
            0,
        ]
    }
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "emp_code": emp_code,
                            "date": {"$gte": start, "$lte": end},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$set": {"working_days": working_days}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "working_days": {"$first": "$working_days"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {
                                    "$lte": [
                                        {
                                            "$isoDayOfWeek": {
                                                "$dateFromString": {
                                                    "dateString": "$logs.date",
                                                    "timezone": "+00:00",
                                                }
                                            }
                                        },
                                        5,
                                    ]
                                },
                            ]
                        },
                        {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                        0,
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
        }},
        {
            "$set": {
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        {
                            "$multiply": [
                                {"$divide": ["$present_days", "$working_days"]},
                                100,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$project": {"_id": 0, "working_days": 1, "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1, "attendance_pct": 1}},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start, end = month_range(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$emp_code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_employees": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "timezone": "+00:00"}}}, 5]},
                        ]},
                        {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                        0,
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$ne": ["$logs", None]}, "$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]}, 1, 0]}},
            "avg_work_hours": {
                "$avg": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]},
                            ]
                        },
                        "$logs.work_hours",
                        None,
                    ]
                }
            },
        }},
        {"$match": {"_id": {"$ne": None}}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount_employees"},
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "avg_work_hours": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, department: Optional[str] = None, limit: int = 10):
    start, end = month_range(month)
    emp_filter = {"joined_on": {"$lte": end}}
    if department is not None:
        emp_filter["department"] = department
    return [
        {"$match": emp_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$emp_code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$name"},
            "department": {"$first": "$department"},
            "total_late_minutes": {"$sum": {"$cond": [{"$ne": ["$logs", None]}, "$logs.late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
            "rank": 1,
        }},
    ]


def department_trend_pipeline(department: str, from_date: str, to_date: str):
    start_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=UTC)
    end_exclusive = (
        datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=UTC)
        + timedelta(days=1)
    )
    return [
        {"$documents": [{"date_obj": start_dt}]},
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {"$sort": {"date_obj": 1}},
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "date": "$date_obj",
                        "format": "%Y-%m-%d",
                        "timezone": "+00:00",
                    }
                },
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$date_obj"}, 5]
                },
            }
        },
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$department", {"$literal": department}]}, {"$lte": ["$joined_on", "$$day"]}]}}},
            ],
            "as": "employees",
        }},
        {"$addFields": {"headcount": {"$size": "$employees"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date", "codes": "$employees.emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$date",
            "date": {"$first": "$date"},
            "is_working_day": {"$first": "$is_working_day"},
            "headcount": {"$first": "$headcount"},
            "present_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]}, 1, 0]}},
        }},
        {"$sort": {"date": 1}},
        {"$addFields": {
            "attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


def with_history_changes(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    changes = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_val = before.get(key)
        new_val = after.get(key)
        if old_val != new_val:
            changes[key] = {
                "from": old_val,
                "to": new_val,
            }
    return changes


def rebuild_derived_fields(emp: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    truncate_record_instants(record)
    status = record.get("status")
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        record["date"] = record.get("date")
        record["punch_in"] = None
        record["punch_out"] = None
        record["work_hours"] = None
        record["late_minutes"] = 0
        record["overtime_minutes"] = 0
        record["half_day"] = False
        return record
    if punch_in is None:
        raise HTTPException(status_code=422, detail="Presence status requires a punch_in")
    if record.get("date") is None:
        record["date"] = resolve_attendance_date(punch_in, emp["shift_start"], emp["shift_end"])
    record["date"] = resolve_attendance_date(punch_in, emp["shift_start"], emp["shift_end"])
    record["late_minutes"] = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"])
    if punch_out is None:
        record["work_hours"] = None
        record["overtime_minutes"] = 0
        record["half_day"] = False
        return record
    record["work_hours"] = compute_work_hours(punch_in, punch_out)
    record["overtime_minutes"] = compute_overtime_minutes(punch_out, emp["shift_end"], emp["shift_start"], record["date"])
    record["half_day"] = compute_half_day(record["work_hours"])
    return record


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not EMP_CODE_RE.fullmatch(value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if len(value) > 120 or not EMAIL_RE.fullmatch(value):
            raise ValueError("Invalid email")
        return value

    @field_validator("department")
    @classmethod
    def validate_department(cls, value: str) -> str:
        if not value or len(value) > 50:
            raise ValueError("department must be 1-50 characters")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or len(value) > 100:
            raise ValueError("name must be 1-100 characters")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        parse_time_hhmm(value)
        return value

    @model_validator(mode="after")
    def validate_shift_diff(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        return value


class AttendanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def normalize_contract_fields(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("punched_at", "punch_in", "punch_out"):
            if field in normalized and normalized[field] is None:
                raise ValueError(f"{field} must not be null when supplied")
        for field in ("work_hours", "late_minutes", "overtime_minutes", "half_day"):
            normalized.pop(field, None)
        return normalized


class PunchInIn(AttendanceRequest):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not EMP_CODE_RE.fullmatch(value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUS:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[StrictInt]) -> Optional[StrictInt]:
        if value is None:
            return None
        ms_to_dt(value)
        return value


class PunchOutIn(AttendanceRequest):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not EMP_CODE_RE.fullmatch(value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[StrictInt]) -> Optional[StrictInt]:
        if value is None:
            return None
        ms_to_dt(value)
        return value


class RegularizeRequest(AttendanceRequest):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in VALID_STATUS:
            raise ValueError("Invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[StrictInt]) -> Optional[StrictInt]:
        if value is None:
            return None
        ms_to_dt(value)
        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if len(value) < 5 or len(value) > 200:
            raise ValueError("reason must be 5-200 characters")
        return value

    @field_validator("regularized_by")
    @classmethod
    def validate_by(cls, value: str) -> str:
        if len(value) < 1 or len(value) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return value


# --------------------------------------------------------------------------- #
# Startup / health
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup_indexes():
    try:
        ensure_indexes()
    except PyMongoError:
        logger.exception("MongoDB indexes could not be initialized during startup")


def ensure_indexes():
    global indexes_ready
    if indexes_ready:
        return
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    indexes_ready = True


@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
        return {"status": "ok"}
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc


# --------------------------------------------------------------------------- #
# Employee endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        db.employees.insert_one(build_employee_record(body))
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc = db.employees.find_one({"emp_code": body.emp_code})
    return employee_from_db(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    items = []
    for doc in db.employees.find(query).sort("emp_code", 1).skip(skip).limit(page_size):
        items.append(employee_from_db(doc))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> Dict[str, Any]:
    if date_from is not None:
        validate_date_string(date_from, "date_from")
    if date_to is not None:
        validate_date_string(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        if status not in VALID_STATUS:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status
    return {
        "filter": query,
        "sort": [("date", -1), ("emp_code", 1)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = fetch_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_instant(datetime.now(tz=UTC))
    date_str = resolve_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    record = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": date_str})
    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = fetch_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_instant(datetime.now(tz=UTC))
    latest = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    candidate = None
    if latest is not None:
        punch_in = truncate_instant(latest["punch_in"])
        if (
            latest.get("status") in PRESENCE_STATUS
            and latest.get("punch_out") is None
            and ts > punch_in
            and ts - punch_in <= timedelta(hours=24)
        ):
            candidate = latest
    record = None
    if candidate is not None:
        normalized = truncate_record_instants(dict(candidate))
        work_hours = compute_work_hours(normalized["punch_in"], ts)
        overtime_minutes = compute_overtime_minutes(
            ts,
            emp["shift_end"],
            emp["shift_start"],
            normalized["date"],
        )
        record = db.attendance_logs.find_one_and_update(
            {
                "_id": candidate["_id"],
                "status": {"$in": list(PRESENCE_STATUS)},
                "punch_out": None,
                "punch_in": {
                    "$eq": candidate["punch_in"],
                    "$lt": ts,
                },
            },
            [
                {
                    "$set": {
                        "punch_in": {"$literal": normalized["punch_in"]},
                        "punch_out": {"$literal": ts},
                        "work_hours": {"$literal": work_hours},
                        "late_minutes": {
                            "$literal": compute_late_minutes(
                                normalized["punch_in"],
                                emp["shift_start"],
                                emp["shift_end"],
                            )
                        },
                        "overtime_minutes": {"$literal": overtime_minutes},
                        "half_day": {"$literal": compute_half_day(work_hours)},
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
    if record is None:
        existing = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
            sort=[("punch_in", -1)],
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="Attendance record not found")
        if existing.get("status") not in PRESENCE_STATUS:
            raise HTTPException(status_code=422, detail="Only presence statuses can be punched out")
        if existing.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="Record is already punched out")
        if ts <= existing["punch_in"]:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if ts - existing["punch_in"] > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        raise HTTPException(status_code=409, detail="Attendance record was modified concurrently")
    return serialize_attendance_record(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = build_attendance_list_query(
        emp_code,
        date_from,
        date_to,
        status,
        page,
        page_size,
    )
    total = db.attendance_logs.count_documents(query["filter"])
    items = []
    for doc in (
        db.attendance_logs.find(query["filter"])
        .sort(query["sort"])
        .skip(query["skip"])
        .limit(query["limit"])
    ):
        items.append(serialize_attendance_record(doc))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    validate_date_string(date)
    emp = fetch_employee_or_404(emp_code)
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="No changes supplied")

    for attempt in range(6):
        stored_record = fetch_attendance_or_404(emp_code, date)
        record = truncate_record_instants(dict(stored_record))
        before = {
            "status": record.get("status"),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "work_hours": record.get("work_hours"),
            "late_minutes": record.get("late_minutes"),
            "overtime_minutes": record.get("overtime_minutes"),
            "half_day": record.get("half_day", False),
        }

        new_status = body.status if body.status is not None else record["status"]
        new_punch_in = record.get("punch_in") if body.punch_in is None else ms_to_dt(body.punch_in)
        new_punch_out = record.get("punch_out") if body.punch_out is None else ms_to_dt(body.punch_out)

        if new_status in {"ABSENT", "LEAVE"}:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=422, detail="ABSENT/LEAVE records cannot have punch times")
            new_punch_in = None
            new_punch_out = None
        else:
            if new_punch_in is None:
                raise HTTPException(status_code=422, detail="Presence status requires a punch_in")
            if resolve_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"]) != date:
                raise HTTPException(status_code=422, detail="punch_in must be on the attendance date")
            if new_punch_out is not None and new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if new_punch_out is not None and new_punch_out - new_punch_in > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

        updated = dict(record)
        updated["status"] = new_status
        updated["punch_in"] = new_punch_in
        updated["punch_out"] = new_punch_out
        updated = rebuild_derived_fields(emp, updated)

        after = {
            "status": updated.get("status"),
            "punch_in": updated.get("punch_in"),
            "punch_out": updated.get("punch_out"),
            "work_hours": updated.get("work_hours"),
            "late_minutes": updated.get("late_minutes"),
            "overtime_minutes": updated.get("overtime_minutes"),
            "half_day": updated.get("half_day", False),
        }
        changes = with_history_changes(before, after)
        if not changes:
            raise HTTPException(status_code=422, detail="No changes supplied")

        entry = {
            "at": truncate_instant(datetime.now(tz=UTC)),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
        update_filter = {
            "_id": stored_record["_id"],
            "status": stored_record.get("status"),
            "punch_in": stored_record.get("punch_in"),
            "punch_out": stored_record.get("punch_out"),
        }
        if "history" in stored_record:
            update_filter["history"] = stored_record["history"]
        else:
            update_filter["history"] = {"$exists": False}
        result = db.attendance_logs.update_one(
            update_filter,
            {
                "$set": {
                    "status": updated["status"],
                    "punch_in": updated["punch_in"],
                    "punch_out": updated["punch_out"],
                    "work_hours": updated["work_hours"],
                    "late_minutes": updated["late_minutes"],
                    "overtime_minutes": updated["overtime_minutes"],
                    "half_day": updated["half_day"],
                    "date": updated.get("date", date),
                },
                "$push": {"history": entry},
            },
        )
        if result.modified_count == 1:
            doc = db.attendance_logs.find_one({"_id": stored_record["_id"]})
            return serialize_attendance_record(doc)
        if attempt == 5:
            raise HTTPException(status_code=409, detail="Attendance record was modified concurrently")

    raise HTTPException(status_code=409, detail="Attendance record was modified concurrently")


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    validate_month(month)
    fetch_employee_or_404(emp_code)
    pipeline = employee_monthly_pipeline(emp_code, month)
    first = next(db.employees.aggregate(pipeline), None)
    if first is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    working_days = int(first.get("working_days", 0) or 0)
    present_days = float(first.get("present_days", 0) or 0)
    result = {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(first.get("leave_days", 0) or 0),
        "late_count": int(first.get("late_count", 0) or 0),
        "total_late_minutes": int(first.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(first.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": (
            round_half_up(Decimal(str(first["attendance_pct"])), 4)
            if first.get("attendance_pct") is not None
            else None
        ),
    }
    return result


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    validate_month(month)
    pipeline = department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))
    results = []
    for row in rows:
        value = {
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": float(row.get("present_days", 0) or 0),
            "avg_work_hours": (
                round_half_up(Decimal(str(row["avg_work_hours"])), 2)
                if row.get("avg_work_hours") is not None
                else None
            ),
            "late_count": int(row.get("late_count", 0) or 0),
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "leave_count": int(row.get("leave_count", 0) or 0),
            "on_duty_count": int(row.get("on_duty_count", 0) or 0),
        }
        results.append(value)
    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    validate_month(month)
    pipeline = late_leaderboard_pipeline(month, department, limit)
    rows = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": [{
        "rank": int(row["rank"]),
        "emp_code": row["emp_code"],
        "name": row["name"],
        "department": row["department"],
        "total_late_minutes": int(row["total_late_minutes"]),
        "late_count": int(row["late_count"]),
    } for row in rows]}


def validate_department_trend_request(
    department: str,
    from_date: str,
    to: str,
) -> None:
    validate_date_string(from_date, "from")
    validate_date_string(to, "to")
    if to < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (datetime.strptime(to, "%Y-%m-%d") - datetime.strptime(from_date, "%Y-%m-%d")).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Range exceeds 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Department not found")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):
    validate_department_trend_request(department, from_date, to)
    pipeline = department_trend_pipeline(department, from_date, to)
    rows = list(db.aggregate(pipeline))
    results = []
    for row in rows:
        rate = row.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(float(rate), 4)
        moving = row.get("moving_avg_7d")
        if moving is not None:
            moving = round_half_up(float(moving), 4)
        results.append({
            "date": row["date"],
            "is_working_day": bool(row.get("is_working_day", False)),
            "headcount": int(row.get("headcount", 0) or 0),
            "present_count": float(row.get("present_count", 0) or 0),
            "late_count": int(row.get("late_count", 0) or 0),
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
    return {"department": department, "items": results}


# --------------------------------------------------------------------------- #
# Admin explain endpoint
# --------------------------------------------------------------------------- #
def explain_aggregation(
    collection: Optional[str],
    pipeline: list[Dict[str, Any]],
) -> Dict[str, Any]:
    command = {
        "aggregate": collection if collection is not None else 1,
        "pipeline": pipeline,
        "cursor": {},
    }
    return db.command("explain", command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = build_attendance_list_query(
            emp_code,
            date_from,
            date_to,
            status,
            page,
            page_size,
        )
        collection = "attendance_logs"
        find_command = {
            "find": collection,
            "filter": query["filter"],
            "sort": dict(query["sort"]),
            "skip": query["skip"],
            "limit": query["limit"],
        }
        explain_doc = db.command("explain", find_command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain_doc}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        validate_month(month)
        fetch_employee_or_404(emp_code)
        pipeline = employee_monthly_pipeline(emp_code, month)
        explain_doc = explain_aggregation("employees", pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        validate_month(month)
        pipeline = department_summary_pipeline(month, department)
        explain_doc = explain_aggregation("employees", pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        validate_month(month)
        pipeline = late_leaderboard_pipeline(month, department, limit)
        explain_doc = explain_aggregation("employees", pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}
    if endpoint == "department_trend":
        if not department or not from_date or not to:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        validate_department_trend_request(department, from_date, to)
        pipeline = department_trend_pipeline(department, from_date, to)
        explain_doc = explain_aggregation(None, pipeline)
        return {"endpoint": endpoint, "collection": "database", "explain": explain_doc}
    raise HTTPException(status_code=404, detail="Unknown endpoint")


# --------------------------------------------------------------------------- #
# End of file
# --------------------------------------------------------------------------- #
