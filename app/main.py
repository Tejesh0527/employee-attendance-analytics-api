"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# Environment variables take precedence over defaults
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db():
    """Ensure required indexes are created idempotently at startup."""
    try:
        # employees
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])

        # attendance_logs
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    except Exception as e:
        print(f"Index creation warning: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def now_utc_dt() -> datetime:
    """Current UTC datetime truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def round_half_up(val: float, decimals: int = 2) -> float:
    """Round a float using ROUND_HALF_UP (R4, R8)."""
    fmt = "0." + "0" * decimals
    return float(Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST.
    Overnight shift (shift_end <= shift_start):
    If punch-in IST time < shift_end, it belongs to shift that started previous calendar day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight:
        shift_end_t = time(end_h, end_m)
        if dt_ist.time() < shift_end_t:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly > 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    att_date = dt_date.fromisoformat(date_str)
    shift_start_ist = datetime.combine(att_date, time(start_h, start_m), tzinfo=IST)
    punch_in_ist = punch_in_dt.astimezone(IST)

    if punch_in_ist > shift_start_ist:
        diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
        if diff_sec > 600:
            return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_dt: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end falls on next calendar day.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    att_date = dt_date.fromisoformat(date_str)
    if is_overnight:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_ist = datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)
    punch_out_ist = punch_out_dt.astimezone(IST)

    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    sec = Decimal(str(int((punch_out_dt - punch_in_dt).total_seconds())))
    hours = sec / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_history_entry(entry: dict) -> dict:
    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = dt_to_epoch_ms(at_val)
    elif isinstance(at_val, (int, float)):
        at_val = int(at_val)

    changes_out = {}
    for k, v in entry.get("changes", {}).items():
        if isinstance(v, dict):
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = dt_to_epoch_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = dt_to_epoch_ms(to_v)
            changes_out[k] = {"from": from_v, "to": to_v}
        else:
            changes_out[k] = v

    return {
        "at": at_val,
        "by": entry.get("by", ""),
        "reason": entry.get("reason", ""),
        "changes": changes_out,
    }


def format_attendance_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [format_history_entry(h) for h in (doc.get("history") or [])],
    }


def format_employee(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at = dt_to_epoch_ms(created_at)
    elif isinstance(created_at, (int, float)):
        created_at = int(created_at)
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at,
    }


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
EpochMillisType = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisType] = None
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisType] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[EpochMillisType] = None
    punch_out: Optional[EpochMillisType] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Employee Attendance & Analytics Portal</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    body { font-family: 'Inter', sans-serif; }
  </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen flex flex-col">

  <!-- Header -->
  <header class="border-b border-slate-800 bg-slate-950/80 backdrop-blur sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4 flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center text-white shadow-lg shadow-indigo-500/30">
          <i class="fa-solid fa-clock text-lg"></i>
        </div>
        <div>
          <h1 class="text-lg font-bold text-white tracking-tight">Stitch Attendance & Analytics</h1>
          <p class="text-xs text-slate-400">FastAPI + MongoDB Backend & Live Dashboard</p>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <div id="healthBadge" class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          Checking System...
        </div>
        <a href="/docs" target="_blank" class="px-4 py-2 text-xs font-semibold rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white shadow-md transition flex items-center gap-2">
          <i class="fa-solid fa-code"></i> Swagger API Docs
        </a>
        <a href="/redoc" target="_blank" class="px-4 py-2 text-xs font-semibold rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition flex items-center gap-2">
          <i class="fa-solid fa-book"></i> ReDoc
        </a>
      </div>
    </div>
  </header>

  <!-- Main Content -->
  <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
    
    <!-- Quick Stats Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-3">
          <span>TOTAL EMPLOYEES</span>
          <i class="fa-solid fa-users text-indigo-400 text-sm"></i>
        </div>
        <div id="statTotalEmployees" class="text-3xl font-extrabold text-white">--</div>
        <p class="text-xs text-slate-400 mt-2">Active across departments</p>
      </div>

      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-3">
          <span>RECORDED LOGS</span>
          <i class="fa-solid fa-calendar-check text-emerald-400 text-sm"></i>
        </div>
        <div id="statTotalLogs" class="text-3xl font-extrabold text-white">--</div>
        <p class="text-xs text-slate-400 mt-2">Seeded attendance days</p>
      </div>

      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-3">
          <span>SELECTED MONTH</span>
          <i class="fa-solid fa-calendar-days text-amber-400 text-sm"></i>
        </div>
        <div class="flex items-center gap-2">
          <input type="month" id="monthSelector" value="2026-07" onchange="refreshAll()" class="bg-slate-900 border border-slate-700 rounded-lg text-sm px-2 py-1 text-white focus:outline-none focus:border-indigo-500 font-semibold">
        </div>
        <p class="text-xs text-slate-400 mt-2">Filter all monthly analytics</p>
      </div>

      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-3">
          <span>QUICK ACTIONS</span>
          <i class="fa-solid fa-bolt text-purple-400 text-sm"></i>
        </div>
        <div class="flex gap-2">
          <button onclick="openPunchModal('in')" class="flex-1 bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border border-emerald-500/30 rounded-xl py-2 text-xs font-semibold transition">
            Punch In
          </button>
          <button onclick="openPunchModal('out')" class="flex-1 bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 rounded-xl py-2 text-xs font-semibold transition">
            Punch Out
          </button>
        </div>
        <p class="text-xs text-slate-400 mt-2">Simulate real punch events</p>
      </div>
    </div>

    <!-- Analytics Section -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      
      <!-- Department Summary -->
      <div class="lg:col-span-2 bg-slate-800/60 border border-slate-700/60 rounded-2xl p-6 shadow-sm flex flex-col">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-chart-pie text-indigo-400"></i> Department Monthly Summary
            </h2>
            <p class="text-xs text-slate-400">Headcount, present days, and average working hours</p>
          </div>
          <button onclick="loadDeptSummary()" class="text-xs text-slate-400 hover:text-white transition">
            <i class="fa-solid fa-arrows-rotate"></i>
          </button>
        </div>
        <div class="overflow-x-auto flex-1">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-slate-700 text-slate-400 uppercase tracking-wider font-semibold">
                <th class="py-3 px-3">Department</th>
                <th class="py-3 px-3 text-center">Headcount</th>
                <th class="py-3 px-3 text-center">Present Days</th>
                <th class="py-3 px-3 text-center">Avg Work Hrs</th>
                <th class="py-3 px-3 text-center">Late Count</th>
                <th class="py-3 px-3 text-center">Late Mins</th>
                <th class="py-3 px-3 text-center">Leaves</th>
              </tr>
            </thead>
            <tbody id="deptSummaryTable" class="divide-y divide-slate-800 text-slate-200">
              <tr><td colspan="7" class="py-4 text-center text-slate-500">Loading department data...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Late Leaderboard -->
      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-6 shadow-sm flex flex-col">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-trophy text-amber-400"></i> Late Leaderboard
            </h2>
            <p class="text-xs text-slate-400">Standard competition ranking (1, 2, 2, 4)</p>
          </div>
          <button onclick="loadLeaderboard()" class="text-xs text-slate-400 hover:text-white transition">
            <i class="fa-solid fa-arrows-rotate"></i>
          </button>
        </div>
        <div class="flex-1 overflow-y-auto max-h-72">
          <div id="leaderboardList" class="space-y-2">
            <p class="text-xs text-slate-500 text-center py-4">Loading leaderboard...</p>
          </div>
        </div>
      </div>

    </div>

    <!-- Employees Directory & Attendance Logs Tabs -->
    <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-6 shadow-sm">
      <div class="flex flex-wrap items-center justify-between gap-4 border-b border-slate-700 pb-4 mb-6">
        <div class="flex items-center gap-4">
          <button id="tabEmployeesBtn" onclick="switchTab('employees')" class="text-sm font-bold text-indigo-400 border-b-2 border-indigo-400 pb-1">
            <i class="fa-solid fa-id-badge mr-1"></i> Employees Directory
          </button>
          <button id="tabAttendanceBtn" onclick="switchTab('attendance')" class="text-sm font-bold text-slate-400 hover:text-slate-200 pb-1">
            <i class="fa-solid fa-clipboard-user mr-1"></i> Attendance Records
          </button>
        </div>
        <div class="text-xs text-slate-400 flex items-center gap-2">
          <span>Backend status:</span>
          <span class="text-emerald-400 font-semibold">MongoDB 8.3 &bull; FastAPI 2.0</span>
        </div>
      </div>

      <!-- Employees Tab -->
      <div id="tabEmployees">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-slate-700 text-slate-400 uppercase tracking-wider font-semibold">
                <th class="py-3 px-3">Emp Code</th>
                <th class="py-3 px-3">Name</th>
                <th class="py-3 px-3">Email</th>
                <th class="py-3 px-3">Department</th>
                <th class="py-3 px-3">Shift (IST)</th>
                <th class="py-3 px-3">Joined On</th>
              </tr>
            </thead>
            <tbody id="employeesTable" class="divide-y divide-slate-800 text-slate-200">
              <tr><td colspan="6" class="py-4 text-center text-slate-500">Loading employees...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Attendance Tab -->
      <div id="tabAttendance" class="hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-slate-700 text-slate-400 uppercase tracking-wider font-semibold">
                <th class="py-3 px-3">Date</th>
                <th class="py-3 px-3">Emp Code</th>
                <th class="py-3 px-3">Status</th>
                <th class="py-3 px-3">Work Hours</th>
                <th class="py-3 px-3">Late Mins</th>
                <th class="py-3 px-3">Overtime</th>
                <th class="py-3 px-3">Half Day</th>
                <th class="py-3 px-3">History</th>
              </tr>
            </thead>
            <tbody id="attendanceTable" class="divide-y divide-slate-800 text-slate-200">
              <tr><td colspan="8" class="py-4 text-center text-slate-500">Loading attendance logs...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </main>

  <!-- Footer -->
  <footer class="border-t border-slate-800 bg-slate-950 py-4 text-center text-xs text-slate-500">
    Employee Attendance & Analytics API &bull; Running on <a href="http://localhost:8000" class="text-indigo-400 hover:underline">http://localhost:8000</a> &bull; Interactive API Docs at <a href="/docs" class="text-indigo-400 hover:underline">/docs</a>
  </footer>

  <!-- Punch Modal -->
  <div id="punchModal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
      <div class="flex items-center justify-between">
        <h3 id="punchModalTitle" class="text-base font-bold text-white">Punch In</h3>
        <button onclick="closePunchModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="space-y-3 text-xs">
        <div>
          <label class="block text-slate-400 mb-1">Employee Code</label>
          <select id="modalEmpCode" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:border-indigo-500"></select>
        </div>
        <div id="statusSelectGroup">
          <label class="block text-slate-400 mb-1">Status</label>
          <select id="modalStatus" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:border-indigo-500">
            <option value="PRESENT">PRESENT</option>
            <option value="WFH">WFH</option>
            <option value="ON_DUTY">ON_DUTY</option>
          </select>
        </div>
        <div id="modalAlert" class="hidden p-3 rounded-lg text-xs font-semibold"></div>
      </div>
      <div class="flex justify-end gap-2 pt-2">
        <button onclick="closePunchModal()" class="px-4 py-2 rounded-lg bg-slate-800 text-slate-300 hover:bg-slate-700 text-xs font-semibold">Cancel</button>
        <button id="modalSubmitBtn" onclick="submitPunch()" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold">Confirm</button>
      </div>
    </div>
  </div>

  <script>
    let punchMode = 'in';

    async function checkHealth() {
      try {
        const res = await fetch('/health');
        const data = await res.json();
        const badge = document.getElementById('healthBadge');
        if (data.status === 'ok') {
          badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
          badge.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400"></span> Backend Online';
        } else {
          badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/20';
          badge.innerHTML = '<span class="w-2 h-2 rounded-full bg-rose-400"></span> Service Degraded';
        }
      } catch (err) {
        const badge = document.getElementById('healthBadge');
        badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/20';
        badge.innerHTML = '<span class="w-2 h-2 rounded-full bg-rose-400"></span> Server Offline';
      }
    }

    async function loadEmployees() {
      try {
        const res = await fetch('/employees?page=1&page_size=100');
        const data = await res.json();
        document.getElementById('statTotalEmployees').innerText = data.total;
        
        const tbody = document.getElementById('employeesTable');
        const select = document.getElementById('modalEmpCode');
        select.innerHTML = '';

        if (!data.items || data.items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" class="py-4 text-center text-slate-500">No employees found</td></tr>';
          return;
        }

        tbody.innerHTML = data.items.map(e => `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-3 px-3 font-semibold text-indigo-400">${e.emp_code}</td>
            <td class="py-3 px-3 font-medium text-white">${e.name}</td>
            <td class="py-3 px-3 text-slate-400">${e.email}</td>
            <td class="py-3 px-3"><span class="px-2 py-0.5 rounded-md bg-slate-700 text-slate-300 font-medium">${e.department}</span></td>
            <td class="py-3 px-3 text-slate-300">${e.shift_start} - ${e.shift_end}</td>
            <td class="py-3 px-3 text-slate-400">${e.joined_on}</td>
          </tr>
        `).join('');

        select.innerHTML = data.items.map(e => `<option value="${e.emp_code}">${e.emp_code} - ${e.name} (${e.department})</option>`).join('');
      } catch (err) {
        console.error(err);
      }
    }

    async function loadAttendance() {
      try {
        const res = await fetch('/attendance?page=1&page_size=100');
        const data = await res.json();
        document.getElementById('statTotalLogs').innerText = data.total;

        const tbody = document.getElementById('attendanceTable');
        if (!data.items || data.items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" class="py-4 text-center text-slate-500">No records found</td></tr>';
          return;
        }

        tbody.innerHTML = data.items.map(a => `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-3 px-3 font-semibold text-slate-200">${a.date}</td>
            <td class="py-3 px-3 font-semibold text-indigo-400">${a.emp_code}</td>
            <td class="py-3 px-3">
              <span class="px-2 py-0.5 rounded-md text-[11px] font-semibold ${
                a.status === 'PRESENT' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' :
                a.status === 'WFH' ? 'bg-blue-500/10 text-blue-400 border border-blue-500/20' :
                a.status === 'ON_DUTY' ? 'bg-purple-500/10 text-purple-400 border border-purple-500/20' :
                'bg-rose-500/10 text-rose-400 border border-rose-500/20'
              }">
                ${a.status}
              </span>
            </td>
            <td class="py-3 px-3 font-medium text-white">${a.work_hours !== null ? a.work_hours + ' hrs' : '<span class="text-slate-500">In Progress</span>'}</td>
            <td class="py-3 px-3 ${a.late_minutes > 0 ? 'text-amber-400 font-bold' : 'text-slate-400'}">${a.late_minutes} m</td>
            <td class="py-3 px-3 ${a.overtime_minutes > 0 ? 'text-emerald-400 font-bold' : 'text-slate-400'}">${a.overtime_minutes} m</td>
            <td class="py-3 px-3">${a.half_day ? '<span class="text-amber-400 font-semibold">Yes (0.5)</span>' : '<span class="text-slate-500">No</span>'}</td>
            <td class="py-3 px-3 text-slate-400">${a.history && a.history.length > 0 ? a.history.length + ' edit(s)' : 'None'}</td>
          </tr>
        `).join('');
      } catch (err) {
        console.error(err);
      }
    }

    async function loadDeptSummary() {
      const month = document.getElementById('monthSelector').value;
      try {
        const res = await fetch(`/analytics/departments/summary?month=${month}`);
        const data = await res.json();
        const tbody = document.getElementById('deptSummaryTable');

        if (!data.items || data.items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" class="py-4 text-center text-slate-500">No department data for this month</td></tr>';
          return;
        }

        tbody.innerHTML = data.items.map(d => `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-3 px-3 font-semibold text-white">${d.department}</td>
            <td class="py-3 px-3 text-center font-semibold text-slate-300">${d.headcount}</td>
            <td class="py-3 px-3 text-center text-emerald-400 font-semibold">${d.present_days}</td>
            <td class="py-3 px-3 text-center font-semibold text-indigo-300">${d.avg_work_hours !== null ? d.avg_work_hours : 'N/A'}</td>
            <td class="py-3 px-3 text-center ${d.late_count > 0 ? 'text-amber-400 font-bold' : 'text-slate-400'}">${d.late_count}</td>
            <td class="py-3 px-3 text-center ${d.total_late_minutes > 0 ? 'text-amber-400 font-bold' : 'text-slate-400'}">${d.total_late_minutes}</td>
            <td class="py-3 px-3 text-center text-slate-400">${d.leave_count}</td>
          </tr>
        `).join('');
      } catch (err) {
        console.error(err);
      }
    }

    async function loadLeaderboard() {
      const month = document.getElementById('monthSelector').value;
      try {
        const res = await fetch(`/analytics/leaderboard/late?month=${month}&limit=10`);
        const data = await res.json();
        const container = document.getElementById('leaderboardList');

        if (!data.items || data.items.length === 0) {
          container.innerHTML = '<p class="text-xs text-slate-500 text-center py-6">No late employees recorded this month 🎉</p>';
          return;
        }

        container.innerHTML = data.items.map(item => `
          <div class="flex items-center justify-between p-2.5 rounded-xl bg-slate-900/60 border border-slate-700/50 hover:border-slate-600 transition">
            <div class="flex items-center gap-3">
              <span class="w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${
                item.rank === 1 ? 'bg-amber-500 text-slate-950 shadow-sm shadow-amber-500/30' :
                item.rank === 2 ? 'bg-slate-300 text-slate-950' :
                item.rank === 3 ? 'bg-amber-700 text-white' :
                'bg-slate-800 text-slate-400'
              }">${item.rank}</span>
              <div>
                <p class="text-xs font-bold text-white">${item.name} <span class="text-[11px] font-normal text-slate-400">(${item.emp_code})</span></p>
                <p class="text-[10px] text-slate-400">${item.department} &bull; ${item.late_count} late days</p>
              </div>
            </div>
            <div class="text-right">
              <span class="text-xs font-extrabold text-amber-400">${item.total_late_minutes} m</span>
              <p class="text-[10px] text-slate-500">total late</p>
            </div>
          </div>
        `).join('');
      } catch (err) {
        console.error(err);
      }
    }

    function switchTab(tab) {
      const btnEmp = document.getElementById('tabEmployeesBtn');
      const btnAtt = document.getElementById('tabAttendanceBtn');
      const viewEmp = document.getElementById('tabEmployees');
      const viewAtt = document.getElementById('tabAttendance');

      if (tab === 'employees') {
        btnEmp.className = 'text-sm font-bold text-indigo-400 border-b-2 border-indigo-400 pb-1';
        btnAtt.className = 'text-sm font-bold text-slate-400 hover:text-slate-200 pb-1';
        viewEmp.classList.remove('hidden');
        viewAtt.classList.add('hidden');
      } else {
        btnAtt.className = 'text-sm font-bold text-indigo-400 border-b-2 border-indigo-400 pb-1';
        btnEmp.className = 'text-sm font-bold text-slate-400 hover:text-slate-200 pb-1';
        viewAtt.classList.remove('hidden');
        viewEmp.classList.add('hidden');
      }
    }

    function openPunchModal(mode) {
      punchMode = mode;
      document.getElementById('punchModalTitle').innerText = mode === 'in' ? 'Punch In Employee' : 'Punch Out Employee';
      document.getElementById('modalSubmitBtn').innerText = mode === 'in' ? 'Confirm Punch In' : 'Confirm Punch Out';
      document.getElementById('statusSelectGroup').style.display = mode === 'in' ? 'block' : 'none';
      document.getElementById('modalAlert').className = 'hidden';
      document.getElementById('punchModal').classList.remove('hidden');
    }

    function closePunchModal() {
      document.getElementById('punchModal').classList.add('hidden');
    }

    async function submitPunch() {
      const emp_code = document.getElementById('modalEmpCode').value;
      const status = document.getElementById('modalStatus').value;
      const alertBox = document.getElementById('modalAlert');

      try {
        let url = punchMode === 'in' ? '/attendance/punch-in' : '/attendance/punch-out';
        let body = punchMode === 'in' ? { emp_code, status } : { emp_code };

        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const data = await res.json();

        if (res.ok) {
          alertBox.className = 'p-3 rounded-lg text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
          alertBox.innerText = `Success! ${punchMode === 'in' ? 'Punched in for date ' + data.date : 'Punched out! Work hours: ' + data.work_hours}`;
          refreshAll();
          setTimeout(closePunchModal, 1500);
        } else {
          alertBox.className = 'p-3 rounded-lg text-xs font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/20';
          alertBox.innerText = data.detail || 'Failed to process request';
        }
      } catch (err) {
        alertBox.className = 'p-3 rounded-lg text-xs font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/20';
        alertBox.innerText = err.message;
      }
    }

    function refreshAll() {
      checkHealth();
      loadEmployees();
      loadAttendance();
      loadDeptSummary();
      loadLeaderboard();
    }

    // Initialize
    refreshAll();
  </script>
</body>
</html>
"""


@app.get('/', include_in_schema=False, response_class=HTMLResponse)
def root_dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="shift_start must differ from shift_end")

    now = now_utc_dt()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now,
    }

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    punch_in_dt = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_dt()
    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    punch_out_dt = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_dt()

    # Find the most recent record whose punch_in is <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punch-out must be after punch-in")

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punch-out cannot be more than 24 hours after punch-in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_record(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")

    # Determine resulting status
    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when setting status to ABSENT or LEAVE",
            )
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_dt(body.punch_in)
            # Must stay on the record's attendance date
            att_d = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
            if att_d != date:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"punch_in falls on attendance date {att_d}, not {date}",
                )
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires punch_in",
            )

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in",
                )
            if (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, emp["shift_end"], date, emp["shift_start"])
            final_half_day = final_work_hours < 4.50
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)

    # Compare changes
    changes: Dict[str, Any] = {}
    old_status = record.get("status")
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}

    old_pi = record.get("punch_in")
    if old_pi != final_punch_in:
        changes["punch_in"] = {"from": old_pi, "to": final_punch_in}

    old_po = record.get("punch_out")
    if old_po != final_punch_out:
        changes["punch_out"] = {"from": old_po, "to": final_punch_out}

    old_wh = record.get("work_hours")
    if old_wh != final_work_hours:
        changes["work_hours"] = {"from": old_wh, "to": final_work_hours}

    old_late = record.get("late_minutes", 0) or 0
    if old_late != final_late_minutes:
        changes["late_minutes"] = {"from": old_late, "to": final_late_minutes}

    old_ot = record.get("overtime_minutes", 0) or 0
    if old_ot != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_ot, "to": final_overtime_minutes}

    old_hd = bool(record.get("half_day", False))
    if old_hd != final_half_day:
        changes["half_day"] = {"from": old_hd, "to": final_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request did not change any field",
        )

    now = now_utc_dt()
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrent safety: ensure history array hasn't changed underneath us
    current_history = record.get("history") or []
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": current_history},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent regularization conflict")

    return format_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    # Calculate working_days (Mon-Fri) from max(month_start, joined_on) to month_end (R7)
    joined_on = emp["joined_on"]
    start_calc_d = max(month_start, joined_on)
    working_days = 0
    if start_calc_d <= month_end:
        cur_d = dt_date.fromisoformat(start_calc_d)
        end_d = dt_date.fromisoformat(month_end)
        while cur_d <= end_d:
            if cur_d.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur_d += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$let": {
                                            "vars": {
                                                "dow": {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {"dateString": "$date"}
                                                    }
                                                }
                                            },
                                            "in": {"$in": ["$$dow", [2, 3, 4, 5, 6]]},
                                        }
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
        present_days = float(row.get("present_days", 0.0))
    else:
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0
        present_days = 0.0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> list:
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$flat_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "p",
                            "in": {"$cond": [{"$eq": ["$$p.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$flat_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "w",
                            "in": "$$w.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        headcount = r["headcount"]
        if headcount == 0:
            continue
        avg_wh = r["avg_work_hours"]
        if avg_wh is not None:
            avg_wh = round_half_up(float(avg_wh), 2)
        items.append({
            "department": r["department"],
            "headcount": int(headcount),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str] = None) -> list:
    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str, emp_codes: List[str]) -> list:
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}}
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_doc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_doc.count", 0]}, 0]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    # Check department exists
    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    if not dept_emps:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department")

    d_from = dt_date.fromisoformat(from_date)
    d_to = dt_date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' must be on or after 'from'")

    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    emp_codes = [e["emp_code"] for e in dept_emps]
    pipeline = build_department_trend_pipeline(department, from_date, to_date, emp_codes)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        rate = r["attendance_rate"]
        if rate is not None:
            rate = round_half_up(float(rate), 4)
        m_avg = r["moving_avg_7d"]
        if m_avg is not None:
            m_avg = round_half_up(float(m_avg), 4)
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be after date_to")

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code is required")
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Valid month is required (YYYY-MM)")

        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {"_id": None, "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_summary":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Valid month is required (YYYY-MM)")

        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = build_department_summary_pipeline(month_start, month_end, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    elif endpoint == "late_leaderboard":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Valid month is required (YYYY-MM)")

        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department is required")
        if not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'from' and 'to' dates are required")

        d_from = dt_date.fromisoformat(from_date)
        d_to = dt_date.fromisoformat(to_date)
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date range")

        dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = build_department_trend_pipeline(department, from_date, to_date, emp_codes)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown explain endpoint")
