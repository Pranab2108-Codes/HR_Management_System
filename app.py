import os
import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from flask import Flask, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename
from config import (
    ALLOWED_PROFILE_IMAGE_EXTENSIONS,
    DB_PATH,
    DEFAULT_SYSTEM_SETTINGS,
    FLASK_SECRET_KEY,
    MAX_PROFILE_IMAGE_SIZE,
    PROFILE_IMAGE_UPLOAD_DIR,
    PROFILE_IMAGE_UPLOAD_SUBDIR,
)
from modules.auth import (
    build_employee_slug,
    clear_admin_session,
    clear_employee_session,
    employee_login_required,
    enable_employee_portal_access,
    ensure_admin_users_table,
    ensure_users_table,
    get_admin_auth_record,
    get_admin_profile,
    get_employee_auth_record,
    get_employee_profile,
    hash_password,
    start_admin_session,
    start_employee_session,
    verify_stored_password,
)
from modules.db import ensure_employee_hourly_notes_table, ensure_employee_profile_columns

app = Flask(__name__)
from database import create_database
create_database()
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_PROFILE_IMAGE_SIZE
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=6)
app.config["SESSION_REFRESH_EACH_REQUEST"] = False


def get_ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def get_ist_date():
    return get_ist_now().date()

BLOOD_GROUP_OPTIONS = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_employee_profile_columns(conn)
    return conn


def scalar(conn, query, params=()):
    row = conn.execute(query, params).fetchone()
    return row[0] if row else 0


@app.context_processor
def inject_auth_context():
    employee_name = (session.get("employee_name", "") or "").strip()
    employee_initials = "".join(part[:1].upper() for part in employee_name.split()[:2]) or "E"
    return {
        "current_admin_name": session.get("admin_name", ""),
        "current_admin_email": session.get("admin_email", ""),
        "admin_authenticated": bool(session.get("admin_user_id")),
        "employee_authenticated": bool(session.get("employee_id")),
        "current_employee_initials": employee_initials,
        "latest_notice_id": session.get("latest_notice_id", 0),
    }


def auto_mark_missing_absences(conn):
    today = get_ist_date()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            notice_date TEXT NOT NULL,
            office_closed INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_on TEXT NOT NULL
        )
        """
    )
    for i in range(1, 8):
        check_date_obj = today - timedelta(days=i)
        if check_date_obj.weekday() == 6:  # Skip Sundays
            continue

        check_date = check_date_obj.isoformat()
        closed = conn.execute("SELECT 1 FROM notifications WHERE notice_date = ? AND office_closed = 1 AND is_active = 1", (check_date,)).fetchone()
        if closed:  # Skip official closed dates
            continue

        conn.execute("""
            INSERT INTO attendance (employee_id, date, check_in, check_out, work_hours, break_hours, status, late_flag, auto_marked)
            SELECT e.id, ?, '', '', 0, 0, 'Absent', 0, 1
            FROM employees e
            WHERE LOWER(COALESCE(e.status, 'active')) != 'inactive'
              AND NOT EXISTS (SELECT 1 FROM attendance a WHERE a.employee_id = e.id AND a.date = ?)
              AND NOT EXISTS (SELECT 1 FROM leave_requests lr WHERE lr.employee_id = e.id AND LOWER(COALESCE(lr.status, '')) = 'approved' AND ? BETWEEN lr.from_date AND lr.to_date)
              AND (e.join_date IS NULL OR TRIM(e.join_date) = '' OR e.join_date <= ?)
        """, (check_date, check_date, check_date, check_date))
    conn.commit()


@app.before_request
def protect_admin_routes():
    if not request.path.startswith("/admin"):
        return None

    if request.endpoint in ("static", "admin_login", "admin_logout"):
        return None

    conn = get_db_connection()
    ensure_admin_users_table(conn)
    current_admin = get_admin_profile(conn, session.get("admin_user_id"))

    if not current_admin or not int(current_admin["is_active"] or 0):
        conn.close()
        clear_admin_session()
        return redirect(url_for("admin_login", msg="Please sign in to access the admin panel."))

    auto_mark_missing_absences(conn)
    conn.close()

    return None


def ensure_system_settings(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
        """
    )
    existing_keys = {
        row["setting_key"]
        for row in conn.execute(
            "SELECT setting_key FROM system_settings"
        ).fetchall()
    }
    missing_items = [
        (key, value)
        for key, value in DEFAULT_SYSTEM_SETTINGS.items()
        if key not in existing_keys
    ]
    if missing_items:
        conn.executemany(
            """
            INSERT INTO system_settings (setting_key, setting_value)
            VALUES (?, ?)
            """,
            missing_items,
        )
        conn.commit()


def load_system_settings(conn):
    ensure_system_settings(conn)
    settings = DEFAULT_SYSTEM_SETTINGS.copy()
    rows = conn.execute(
        """
        SELECT setting_key, setting_value
        FROM system_settings
        """
    ).fetchall()
    for row in rows:
        settings[row["setting_key"]] = row["setting_value"]
    return settings


def ensure_notifications_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            notice_date TEXT NOT NULL,
            office_closed INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_on TEXT NOT NULL
        )
        """
    )


def build_notification_card(row, reference_date):
    try:
        notice_day = date.fromisoformat(row["notice_date"])
        delta_days = (notice_day - reference_date).days
        if delta_days == 0:
            day_label = "Today"
        elif delta_days == 1:
            day_label = "Tomorrow"
        elif delta_days > 1:
            day_label = f"In {delta_days} days"
        else:
            day_label = row["notice_date"]
    except ValueError:
        day_label = row["notice_date"]

    return {
        "id": row["id"],
        "title": row["title"],
        "message": row["message"],
        "notice_date": row["notice_date"],
        "office_closed": bool(row["office_closed"]),
        "is_active": bool(row["is_active"]),
        "created_on": row["created_on"],
        "day_label": day_label,
    }


def get_latest_notice_id(conn, reference_date=None):
    ensure_notifications_table(conn)
    reference = reference_date or get_ist_date()
    row = conn.execute(
        """
        SELECT id
        FROM notifications
        WHERE is_active = 1
          AND notice_date >= ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (reference.isoformat(),),
    ).fetchone()
    return row["id"] if row else 0


def load_notifications(conn, include_inactive=False, reference_date=None):
    ensure_notifications_table(conn)
    reference = reference_date or get_ist_date()
    query = """
        SELECT
            id,
            title,
            message,
            notice_date,
            office_closed,
            is_active,
            created_on
        FROM notifications
    """
    params = ()
    if include_inactive:
        query += " ORDER BY is_active DESC, notice_date ASC, id DESC"
    else:
        query += """
            WHERE is_active = 1
              AND notice_date >= ?
            ORDER BY notice_date ASC, id DESC
        """
        params = (reference.isoformat(),)

    rows = conn.execute(query, params).fetchall()
    return [build_notification_card(row, reference) for row in rows]


def is_valid_time_value(value):
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return False
    return True


def calculate_work_hours(check_in_value, check_out_value, break_hours=0):
    if not check_in_value or not check_out_value:
        return 0.0

    check_in_time = datetime.strptime(check_in_value, "%H:%M")
    check_out_time = datetime.strptime(check_out_value, "%H:%M")
    total_hours = (check_out_time - check_in_time).total_seconds() / 3600
    effective_hours = total_hours - float(break_hours or 0)
    return round(max(effective_hours, 0.0), 2)


def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great-circle distance between two points
    on the Earth (specified in decimal degrees). Returns distance in meters.
    """
    # Convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    # Radius of Earth in meters
    return c * 6371000


def format_schedule_time(time_value):
    return time_value.strftime("%I:%M %p").lstrip("0")


def get_hourly_progress(start_time, end_time, reference_time):
    current_time = reference_time.time()
    start_value = start_time.time()
    end_value = end_time.time()

    if current_time >= end_value:
        return "Completed", "completed"
    if start_value <= current_time < end_value:
        return "On-going", "ongoing"
    return "Upcoming", "upcoming"


def build_employee_hourly_schedule(settings, notes_by_slot=None):
    office_start = datetime.strptime(settings["workday_start_time"], "%H:%M")
    logout_time = datetime.strptime(settings["logout_time"], "%H:%M")
    reference_time = get_ist_now()
    notes_lookup = notes_by_slot or {}

    def get_note_data(slot_key):
        slot_data = notes_lookup.get(slot_key, {})
        if isinstance(slot_data, dict):
            return slot_data.get("note_text", ""), slot_data.get("status", "")
        return slot_data, ""

    schedule = []
    current_time = office_start
    slot_number = 1

    lunch_start_time = datetime.strptime("13:30", "%H:%M").time()
    lunch_end_time = datetime.strptime("14:00", "%H:%M").time()

    while current_time < logout_time:
        # Skip exactly the lunch break period
        if current_time.time() >= lunch_start_time and current_time.time() < lunch_end_time:
            current_time = datetime.combine(current_time.date(), lunch_end_time)
            if current_time >= logout_time:
                break

        next_time = min(current_time + timedelta(hours=1), logout_time)
        # If the next block overlaps with the start of lunch, stop it exactly at lunch start
        if current_time.time() < lunch_start_time and next_time.time() > lunch_start_time:
            next_time = datetime.combine(current_time.date(), lunch_start_time)

        progress_label, progress_class = get_hourly_progress(
            current_time,
            next_time,
            reference_time,
        )
        slot_key = f"{current_time.strftime('%H:%M')}_{next_time.strftime('%H:%M')}"
        note_text, status = get_note_data(slot_key)
        schedule.append(
            {
                "slot_key": slot_key,
                "slot_label": f"Work Hour {slot_number}",
                "time_range": f"{format_schedule_time(current_time)} - {format_schedule_time(next_time)}",
                "activity": "Write what you are working on during this slot.",
                "note_text": note_text,
                "status": status,
                "progress_label": progress_label,
                "progress_class": progress_class,
            }
        )
        current_time = next_time
        slot_number += 1

    summary = {
        "office_start": format_schedule_time(office_start),
        "logout_time": format_schedule_time(logout_time),
    }
    return summary, schedule



def load_employee_hourly_notes(conn, employee_id, entry_date):
    ensure_employee_hourly_notes_table(conn)
    rows = conn.execute(
        """
        SELECT slot_key, note_text, status
        FROM employee_hourly_notes
        WHERE employee_id = ? AND entry_date = ?
        """,
        (employee_id, entry_date),
    ).fetchall()
    return {
        row["slot_key"]: {
            "note_text": (row["note_text"] or ""),
            "status": (row["status"] or "")
        }
        for row in rows
    }


def render_system_settings_page(message="", settings=None, notification_form=None):
    conn = get_db_connection()
    stored_settings = load_system_settings(conn)
    notifications = load_notifications(conn, include_inactive=True)
    conn.close()

    current_settings = stored_settings.copy()
    if settings:
        current_settings.update(settings)

    current_notification_form = {
        "title": "",
        "notice_date": get_ist_date().isoformat(),
        "message": "",
        "office_closed": False,
    }
    if notification_form:
        current_notification_form.update(notification_form)

    return render_template(
        "admin_settings.html",
        section="settings",
        page_title="System Settings",
        page_subtitle="Application preferences and policy controls.",
        message=message,
        settings=current_settings,
        notifications=notifications,
        notification_form=current_notification_form,
    )


def is_valid_month_value(value):
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError:
        return False
    return True


def build_monthly_attendance_export(selected_month, conn):
    rows = conn.execute(
        """
        SELECT
            e.emp_code,
            e.full_name,
            SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'present' THEN 1 ELSE 0 END) AS present_days,
            SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'leave' THEN 1 ELSE 0 END) AS leave_days,
            SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'absent' THEN 1 ELSE 0 END) AS absent_days,
            SUM(CASE WHEN COALESCE(a.late_flag, 0) = 1 THEN 1 ELSE 0 END) AS late_days
        FROM employees AS e
        LEFT JOIN attendance AS a
            ON a.employee_id = e.id
           AND SUBSTR(a.date, 1, 7) = ?
        GROUP BY e.id, e.emp_code, e.full_name
        ORDER BY e.emp_code
        """,
        (selected_month,),
    ).fetchall()

    headers = [
        "Emp Code",
        "Name",
        "Present Days",
        "Leave Days",
        "Absent Days",
        "Late Days",
    ]
    keys = [
        "emp_code",
        "full_name",
        "present_days",
        "leave_days",
        "absent_days",
        "late_days",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row[key] for key in keys])
    return buffer.getvalue()


def next_emp_code(conn):
    row = conn.execute(
        """
        SELECT emp_code
        FROM employees
        WHERE emp_code LIKE 'OG%'
        ORDER BY CAST(SUBSTR(emp_code, 3) AS INTEGER) DESC
        LIMIT 1
        """
    ).fetchone()
    if not row or not row["emp_code"]:
        return "OG2517"

    code = row["emp_code"]
    suffix = code[2:]
    if not suffix.isdigit():
        return "OG2517"
    return f"OG{int(suffix) + 1}"


HALF_DAY_STATUS_VALUES = {"half day", "half-day", "halfday", "half_day"}


def is_half_day_record(status_value, work_hours_value):
    status = (status_value or "").strip().lower()
    if status in HALF_DAY_STATUS_VALUES:
        return True

    if status in ("absent", "leave", "holiday"):
        return False

    try:
        hours = float(work_hours_value or 0)
    except (TypeError, ValueError):
        return False
    return 0 < hours <= 4.5


def format_hour_slot(hour_value):
    hour = hour_value % 24
    display = hour % 12
    if display == 0:
        display = 12
    return f"{display}:00"


def build_hourly_task_slots(task_rows, start_hour=10):
    slots = []
    for index, row in enumerate(task_rows):
        slot_start = format_hour_slot(start_hour + index)
        slot_end = format_hour_slot(start_hour + index + 1)
        slots.append(
            {
                "time_label": f"{slot_start} - {slot_end}",
                "title": row["title"],
                "status": row["status"] or "",
            }
        )
    return slots


def resolve_report_date(conn, requested_date=""):
    return (requested_date or "").strip() or get_ist_date().isoformat()


def build_department_report_cards(conn, report_date):
    total_departments = scalar(conn, "SELECT COUNT(*) FROM departments")
    active_employees_count = scalar(
        conn,
        "SELECT COUNT(*) FROM employees WHERE LOWER(COALESCE(status, 'active')) != 'inactive'",
    )

    department_rows = conn.execute(
        """
        SELECT
            d.id AS department_id,
            d.dept_name,
            e.id AS employee_id,
            e.emp_code,
            e.full_name,
            r.role_name,
            COALESCE(a.status, '') AS attendance_status,
            COALESCE(a.work_hours, 0) AS work_hours
        FROM departments AS d
        LEFT JOIN employees AS e
            ON e.department_id = d.id
           AND LOWER(COALESCE(e.status, 'active')) != 'inactive'
        LEFT JOIN roles AS r ON r.id = e.role_id
        LEFT JOIN attendance AS a
            ON a.employee_id = e.id
           AND a.date = ?
        ORDER BY d.dept_name, e.emp_code, e.full_name
        """,
        (report_date,),
    ).fetchall()

    settings = load_system_settings(conn)
    ensure_employee_hourly_notes_table(conn)
    hourly_note_rows = conn.execute(
        """
        SELECT employee_id, slot_key, note_text, status
        FROM employee_hourly_notes
        WHERE entry_date = ?
        """,
        (report_date,),
    ).fetchall()

    notes_by_employee = {}
    for row in hourly_note_rows:
        if row["employee_id"] not in notes_by_employee:
            notes_by_employee[row["employee_id"]] = {}
        notes_by_employee[row["employee_id"]][row["slot_key"]] = {
            "note_text": row["note_text"] or "",
            "status": row["status"] or "",
        }

    department_lookup = {}
    for row in department_rows:
        department_id = row["department_id"]
        if department_id not in department_lookup:
            department_lookup[department_id] = {
                "department_id": department_id,
                "dept_name": row["dept_name"] or "Unassigned",
                "working_employee_count": 0,
                "members": [],
            }

        if row["employee_id"] is None:
            continue

        attendance_status = (row["attendance_status"] or "").strip()
        attendance_status_lower = attendance_status.lower()
        is_working = attendance_status_lower == "present" or is_half_day_record(
            attendance_status,
            row["work_hours"],
        )

        if not is_working:
            continue

        employee_notes = notes_by_employee.get(row["employee_id"], {})
        _, hourly_schedule = build_employee_hourly_schedule(settings, employee_notes)
        task_slots = []
        for item in hourly_schedule:
            if (item["note_text"] or "").strip() or (item["status"] or "").strip():
                task_slots.append(
                    {
                        "time_label": item["time_range"],
                        "title": item["note_text"] if (item["note_text"] or "").strip() else item["slot_label"],
                        "status": item["status"] or "No Status",
                    }
                )
        department_lookup[department_id]["working_employee_count"] += 1
        department_lookup[department_id]["members"].append(
            {
                "id": row["employee_id"],
                "employee_id": row["employee_id"],
                "emp_code": row["emp_code"],
                "full_name": row["full_name"],
                "role_name": row["role_name"] or "",
                "work_hours": float(row["work_hours"] or 0),
                "task_slots": task_slots,
            }
        )

    department_cards = sorted(
        department_lookup.values(),
        key=lambda item: item["dept_name"].lower(),
    )
    for card in department_cards:
        card["members"].sort(key=lambda item: item["full_name"].lower())

    return total_departments, active_employees_count, department_cards


def get_employee_portal_greeting(reference_time=None):
    current_time = reference_time or get_ist_now()
    if current_time.hour < 12:
        return "Good morning"
    if current_time.hour < 17:
        return "Good afternoon"
    return "Good evening"


def resolve_employee_portal_context(expected_slug=""):
    conn = get_db_connection()
    current_employee = get_employee_profile(conn, session.get("employee_id"))

    if not current_employee:
        conn.close()
        clear_employee_session()
        return None, None, None, redirect(
            url_for("login", msg="Please sign in to access the employee portal.")
        )

    employee_slug = session.get("employee_slug") or build_employee_slug(
        current_employee["full_name"]
    )
    session["employee_slug"] = employee_slug

    if expected_slug and expected_slug != employee_slug:
        conn.close()
        return None, None, None, redirect(
            url_for("employee_dashboard", employee_slug=employee_slug)
        )

    session["latest_notice_id"] = get_latest_notice_id(conn)

    return conn, current_employee, employee_slug, None



def is_allowed_profile_image(filename):
    if not filename or "." not in filename:
        return False
    extension = filename.rsplit(".", 1)[1].lower()
    return extension in ALLOWED_PROFILE_IMAGE_EXTENSIONS



def save_employee_profile_image(employee_id, uploaded_file):
    filename = secure_filename((uploaded_file.filename or "").strip())
    if not filename or not is_allowed_profile_image(filename):
        raise ValueError("Please upload a PNG, JPG, JPEG, or WEBP image.")

    os.makedirs(PROFILE_IMAGE_UPLOAD_DIR, exist_ok=True)
    extension = filename.rsplit(".", 1)[1].lower()
    stored_name = f"employee_{employee_id}_{get_ist_now().strftime('%Y%m%d%H%M%S')}.{extension}"
    absolute_path = os.path.join(PROFILE_IMAGE_UPLOAD_DIR, stored_name)
    uploaded_file.save(absolute_path)
    return os.path.join(PROFILE_IMAGE_UPLOAD_SUBDIR, stored_name).replace("\\", "/")


@app.route("/")
def home():
    return render_template("landing.html")


@app.route("/employee/login", methods=["GET", "POST"])
def login():
    if request.method == "GET" and session.get("employee_id"):
        employee_slug = session.get("employee_slug") or build_employee_slug(session.get("employee_name", "employee"))
        session["employee_slug"] = employee_slug
        return redirect(url_for("employee_dashboard", employee_slug=employee_slug))

    message = request.args.get("msg", "").strip()
    login_id = request.form.get("login_id", "").strip()

    if request.method == "POST":
        password = request.form.get("password", "")
        if not login_id or not password:
            message = "Enter your work email or employee code, then your password."
        else:
            conn = get_db_connection()
            auth_row = get_employee_auth_record(conn, login_id)

            if not auth_row:
                employee_match = conn.execute(
                    """
                    SELECT id
                    FROM employees
                    WHERE LOWER(COALESCE(email, '')) = LOWER(?)
                       OR LOWER(COALESCE(emp_code, '')) = LOWER(?)
                    LIMIT 1
                    """,
                    (login_id, login_id),
                ).fetchone()
                message = (
                    "Your portal access is not set up yet. Please contact HR."
                    if employee_match
                    else "Invalid login details. Please try again."
                )
                conn.close()
            elif (auth_row["status"] or "").strip().lower() == "inactive":
                message = "Your employee access is inactive. Please contact HR."
                conn.close()
            else:
                is_valid_password, should_upgrade_password = verify_stored_password(
                    auth_row["password_hash"],
                    password,
                )
                if not is_valid_password:
                    message = "Invalid login details. Please try again."
                    conn.close()
                else:
                    if should_upgrade_password:
                        conn.execute(
                            """
                            UPDATE users
                            SET
                                password_hash = ?,
                                must_change_password = 0
                            WHERE id = ?
                            """,
                            (hash_password(password), auth_row["user_id"]),
                        )

                    conn.execute(
                        """
                        UPDATE users
                        SET last_login = datetime('now', '+5 hours', '+30 minutes')
                        WHERE id = ?
                        """,
                        (auth_row["user_id"],),
                    )
                    conn.execute(
                        """
                        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
                        VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
                        """,
                        (
                            auth_row["employee_id"],
                            f"Employee logged in: {auth_row['full_name']} ({auth_row['emp_code']})",
                            request.remote_addr,
                        ),
                    )
                    conn.commit()
                    conn.close()

                    start_employee_session(auth_row)
                    return redirect(
                        url_for(
                            "employee_dashboard",
                            employee_slug=session.get("employee_slug") or build_employee_slug(auth_row["full_name"]),
                        )
                    )

    return render_template(
        "login.html",
        message=message,
        login_id=login_id,
    )


@app.route("/<employee_slug>")
@employee_login_required(get_db_connection)
def employee_dashboard(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    today_date = get_ist_date()
    today = today_date.isoformat()
    current_month = today_date.strftime("%Y-%m")
    current_month_label = today_date.strftime("%B %Y")
    today_attendance = conn.execute(
        """
        SELECT
            check_in,
            check_out,
            work_hours,
            status,
            late_flag
        FROM attendance
        WHERE employee_id = ? AND date = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (current_employee["id"], today),
    ).fetchone()
    summary = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'present' THEN 1 ELSE 0 END), 0) AS present_days,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'leave' THEN 1 ELSE 0 END), 0) AS leave_days,
            COALESCE(SUM(CASE WHEN COALESCE(late_flag, 0) = 1 THEN 1 ELSE 0 END), 0) AS late_days
        FROM attendance
        WHERE employee_id = ?
          AND SUBSTR(COALESCE(date, ''), 1, 7) = ?
        """,
        (current_employee["id"], current_month),
    ).fetchone()
    leave_summary = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'pending' THEN 1 ELSE 0 END), 0) AS pending_requests,
            COALESCE(COUNT(*), 0) AS total_requests
        FROM leave_requests
        WHERE employee_id = ?
        """,
        (current_employee["id"],),
    ).fetchone()

    ensure_employee_hourly_notes_table(conn)
    last_day_date_row = conn.execute(
        "SELECT MAX(entry_date) AS last_date FROM employee_hourly_notes WHERE employee_id = ? AND entry_date < ?",
        (current_employee["id"], today),
    ).fetchone()
    last_day_date = last_day_date_row["last_date"] if last_day_date_row else None

    last_day_pending_work = "No"
    if last_day_date:
        last_note_row = conn.execute(
            """
            SELECT note_text FROM employee_hourly_notes
            WHERE employee_id = ? AND entry_date = ? AND TRIM(COALESCE(note_text, '')) != ''
            ORDER BY CASE WHEN slot_key LIKE 'logout_%' THEN 0 ELSE 1 END, updated_on DESC
            LIMIT 1
            """,
            (current_employee["id"], last_day_date),
        ).fetchone()
        if last_note_row:
            last_day_pending_work = last_note_row["note_text"]

    notifications = load_notifications(conn)
    conn.close()

    first_name = (current_employee["full_name"] or "Employee").split()[0]
    return render_template(
        "employee_dashboard.html",
        page_title="Employee Dashboard",
        page_subtitle="Your daily workspace for attendance and leave updates.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        greeting=get_employee_portal_greeting(),
        first_name=first_name,
        today=today,
        today_attendance=today_attendance,
        summary=summary,
        current_month_label=current_month_label,
        leave_summary=leave_summary,
        notifications_count=len(notifications),
        last_day_pending_work=last_day_pending_work,
    )

@app.route('/debug-admins')
def debug_admins():
    conn = get_db_connection()
    admins = conn.execute("SELECT id, full_name, email FROM admin_users").fetchall()
    conn.close()
    return str([dict(a) for a in admins])


@app.route("/debug-employees")
def debug_employees():
    conn = get_db_connection()
    rows = conn.execute("SELECT id, emp_code, full_name, email FROM employees").fetchall()
    conn.close()
    return str([dict(r) for r in rows])


@app.route("/fix-admins")
def fix_admins():
    import sqlite3
    from config import DB_PATH

    conn = sqlite3.connect(DB_PATH)

    # 1. Delete the rows containing newline characters
    conn.execute("""
        DELETE FROM admin_users
        WHERE email LIKE '%\n%' OR email LIKE '%\r%'
    """)

    # 2. Clean remaining rows just in case
    conn.execute("""
        UPDATE admin_users
        SET
            email = TRIM(REPLACE(REPLACE(email, char(10), ''), char(13), '')),
            full_name = TRIM(REPLACE(REPLACE(full_name, char(10), ''), char(13), ''))
    """)

    conn.commit()
    conn.close()

    return "Admins cleaned successfully"


@app.route("/<employee_slug>/profile")
@employee_login_required(get_db_connection)
def employee_profile(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    message = request.args.get("msg", "").strip()
    conn.close()
    return render_template(
        "employee_profile.html",
        page_title="My Profile",
        page_subtitle="View your work and personal profile details.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        message=message,
    )


@app.route("/<employee_slug>/profile/edit", methods=["GET", "POST"])
@employee_login_required(get_db_connection)
def employee_profile_edit(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    message = request.args.get("msg", "").strip()
    form_state = {
        "full_name": request.form.get("full_name", current_employee["full_name"] or "").strip(),
        "email": request.form.get("email", current_employee["email"] or "").strip(),
        "phone": request.form.get("phone", current_employee["phone"] or "").strip(),
        "alternate_phone": request.form.get("alternate_phone", current_employee["alternate_phone"] or "").strip(),
        "date_of_birth": request.form.get("date_of_birth", current_employee["date_of_birth"] or "").strip(),
        "blood_group": request.form.get("blood_group", current_employee["blood_group"] or "").strip().upper(),
        "emergency_contact": request.form.get("emergency_contact", current_employee["emergency_contact"] or "").strip(),
        "address": request.form.get("address", current_employee["address"] or "").strip(),
    }

    if request.method == "POST":
        uploaded_file = request.files.get("profile_image")
        duplicate_email = None
        if form_state["email"]:
            duplicate_email = conn.execute(
                """
                SELECT id
                FROM employees
                WHERE LOWER(COALESCE(email, '')) = LOWER(?)
                  AND id != ?
                LIMIT 1
                """,
                (form_state["email"], current_employee["id"]),
            ).fetchone()

        if not form_state["full_name"]:
            message = "Full name is required."
        elif form_state["date_of_birth"]:
            try:
                date.fromisoformat(form_state["date_of_birth"])
            except ValueError:
                message = "Date of birth must be a valid date."

        if (
            not message
            and form_state["blood_group"]
            and form_state["blood_group"] not in BLOOD_GROUP_OPTIONS
        ):
            message = "Please choose a valid blood group."

        if not message and duplicate_email:
            message = "That email is already in use by another employee."

        if not message:
            profile_image = current_employee["profile_image"] or ""
            if uploaded_file and (uploaded_file.filename or "").strip():
                try:
                    profile_image = save_employee_profile_image(
                        current_employee["id"],
                        uploaded_file,
                    )
                except ValueError as exc:
                    message = str(exc)

            if not message:
                conn.execute(
                    """
                    UPDATE employees
                    SET
                        full_name = ?,
                        email = ?,
                        phone = ?,
                        alternate_phone = ?,
                        date_of_birth = ?,
                        blood_group = ?,
                        emergency_contact = ?,
                        address = ?,
                        profile_image = ?
                    WHERE id = ?
                    """,
                    (
                        form_state["full_name"],
                        form_state["email"],
                        form_state["phone"],
                        form_state["alternate_phone"],
                        form_state["date_of_birth"],
                        form_state["blood_group"],
                        form_state["emergency_contact"],
                        form_state["address"],
                        profile_image,
                        current_employee["id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?
                    WHERE employee_id = ?
                    """,
                    (
                        form_state["email"]
                        or current_employee["emp_code"]
                        or current_employee["username"]
                        or "",
                        current_employee["id"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
                    VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
                    """,
                    (
                        current_employee["id"],
                        "Employee profile updated",
                        request.remote_addr,
                    ),
                )
                conn.commit()
                conn.close()
                start_employee_session(
                    {
                        "employee_id": current_employee["id"],
                        "full_name": form_state["full_name"],
                        "emp_code": current_employee["emp_code"],
                    }
                )
                return redirect(
                    url_for(
                        "employee_profile",
                        employee_slug=session.get("employee_slug"),
                        msg="Profile updated successfully.",
                    )
                )

    conn.close()
    return render_template(
        "employee_profile_edit.html",
        page_title="Edit Profile",
        page_subtitle="Update your personal information and profile photo.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        form_state=form_state,
        blood_group_options=BLOOD_GROUP_OPTIONS,
        message=message,
    )


@app.route("/<employee_slug>/check-in", methods=["GET", "POST"])
@employee_login_required(get_db_connection)
def employee_check_in(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    today = get_ist_date().isoformat()
    message = request.args.get("msg", "").strip()
    today_record = conn.execute(
        """
        SELECT
            id,
            check_in,
            check_out,
            work_hours,
            status,
            late_flag
        FROM attendance
        WHERE employee_id = ? AND date = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (current_employee["id"], today),
    ).fetchone()

    if request.method == "POST":
        attendance_action = request.form.get("attendance_action", "check_in").strip().lower()
        settings = load_system_settings(conn)
        office_lat_str = settings.get("office_latitude", "0.0")
        office_lon_str = settings.get("office_longitude", "0.0")
        allowed_radius_str = settings.get("geofence_radius_meters", "500")

        try:
            office_lat = float(office_lat_str)
            office_lon = float(office_lon_str)
            allowed_radius = int(allowed_radius_str)
        except (ValueError, TypeError):
            office_lat, office_lon, allowed_radius = 0.0, 0.0, 500

        # Only enforce geofence if office location is set (not 0,0)
        # Check-out is allowed from anywhere.
        if attendance_action != "check_out" and (office_lat != 0.0 or office_lon != 0.0):
            user_lat_str = request.form.get("latitude")
            user_lon_str = request.form.get("longitude")

            if not user_lat_str or not user_lon_str:
                conn.close()
                return redirect(
                    url_for(
                        "employee_check_in",
                        employee_slug=employee_slug,
                        msg="Could not get your location. Please enable location services and try again.",
                    )
                )

            user_lat, user_lon = float(user_lat_str), float(user_lon_str)
            distance = calculate_haversine_distance(office_lat, office_lon, user_lat, user_lon)

            if distance > allowed_radius:
                conn.close()
                msg = (
                    f"You are {int(distance)} meters away. You must be within {allowed_radius} meters to "
                    "check in."
                )
                return redirect(url_for("employee_check_in", employee_slug=employee_slug, msg=msg))
        status_value = (today_record["status"] if today_record else "") or ""
        status_lower = status_value.strip().lower()
        if status_lower in ("leave", "holiday"):
            conn.close()
            return redirect(
                url_for(
                    "employee_check_in",
                    employee_slug=employee_slug,
                    msg=f"You cannot update attendance because today is already marked as {status_value}.",
                )
            )

        if attendance_action == "check_out":
            if not today_record or not (today_record["check_in"] or "").strip():
                conn.close()
                return redirect(
                    url_for(
                        "employee_check_in",
                        employee_slug=employee_slug,
                        msg="Please check in first before checking out.",
                    )
                )

            if (today_record["check_out"] or "").strip():
                conn.close()
                return redirect(
                    url_for(
                        "employee_check_in",
                        employee_slug=employee_slug,
                        msg=f"You already checked out today at {today_record['check_out']}.",
                    )
                )

            check_in_time = datetime.strptime(today_record["check_in"], "%H:%M")
            check_out_value = get_ist_now().strftime("%H:%M")
            check_out_time = datetime.strptime(check_out_value, "%H:%M")
            if check_out_time < check_in_time:
                conn.close()
                return redirect(
                    url_for(
                        "employee_check_in",
                        employee_slug=employee_slug,
                        msg="Check-out time must be after check-in time.",
                    )
                )

            work_hours = calculate_work_hours(
                today_record["check_in"],
                check_out_value,
                0,
            )

            conn.execute(
                """
                UPDATE attendance
                SET
                    check_out = ?,
                    work_hours = ?,
                    auto_marked = 0
                WHERE id = ?
                """,
                (check_out_value, work_hours, today_record["id"]),
            )
            conn.execute(
                """
                INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
                VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
                """,
                (
                    current_employee["id"],
                    f"Employee check-out recorded for {today}",
                    request.remote_addr,
                ),
            )
            conn.commit()
            conn.close()
            return redirect(
                url_for(
                    "employee_check_in",
                    employee_slug=employee_slug,
                    msg="Check-out recorded successfully.",
                )
            )

        if today_record and (today_record["check_in"] or "").strip():
            conn.close()
            return redirect(
                url_for(
                    "employee_check_in",
                    employee_slug=employee_slug,
                    msg=f"You already checked in today at {today_record['check_in']}.",
                )
            )

        current_time = get_ist_now()
        check_in_value = current_time.strftime("%H:%M")
        late_mark_time = datetime.strptime(settings["late_mark_threshold"], "%H:%M").time()
        is_late = 1 if current_time.time().replace(second=0, microsecond=0) > late_mark_time else 0

        if today_record:
            conn.execute(
                """
                UPDATE attendance
                SET
                    check_in = ?,
                    status = ?,
                    late_flag = ?,
                    auto_marked = 0
                WHERE id = ?
                """,
                (check_in_value, "Present", is_late, today_record["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO attendance (
                    employee_id,
                    date,
                    check_in,
                    check_out,
                    work_hours,
                    break_hours,
                    status,
                    late_flag,
                    auto_marked
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (current_employee["id"], today, check_in_value, "", 0, 0, "Present", is_late, 0),
            )

        conn.execute(
            """
            INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
            VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
            """,
            (
                current_employee["id"],
                f"Employee check-in recorded for {today}",
                request.remote_addr,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(
            url_for(
                "employee_check_in",
                employee_slug=employee_slug,
                msg="Check-in recorded successfully.",
            )
        )

    conn.close()
    return render_template(
        "employee_check_in.html",
        page_title="Attendance",
        page_subtitle="Record your check-in and check-out for today.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        today=today,
        today_record=today_record,
        can_check_in=not today_record or not (today_record["check_in"] or "").strip(),
        can_check_out=bool(
            today_record
            and (today_record["check_in"] or "").strip()
            and not (today_record["check_out"] or "").strip()
        ),
        message=message,
    )


@app.route("/<employee_slug>/hourly", methods=["GET", "POST"])
@employee_login_required(get_db_connection)
def employee_hourly(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    message = request.args.get("msg", "").strip()
    entry_date = get_ist_date().isoformat()
    settings = load_system_settings(conn)
    notes_by_slot = load_employee_hourly_notes(conn, current_employee["id"], entry_date)
    hourly_summary, hourly_schedule = build_employee_hourly_schedule(settings, notes_by_slot)

    if request.method == "POST":
        ensure_employee_hourly_notes_table(conn)
        for item in hourly_schedule:
            field_name = f"detail__{item['slot_key']}"
            status_field_name = f"status__{item['slot_key']}"
            
            if field_name in request.form or status_field_name in request.form:
                note_text = request.form.get(field_name, "").strip()
                status = request.form.get(status_field_name, "").strip()
                if note_text or status:
                    conn.execute(
                        """
                        INSERT INTO employee_hourly_notes (
                            employee_id,
                            entry_date,
                            slot_key,
                            slot_label,
                            time_range,
                            note_text,
                            status,
                            updated_on
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+5 hours', '+30 minutes'))
                        ON CONFLICT(employee_id, entry_date, slot_key)
                        DO UPDATE SET
                            slot_label = excluded.slot_label,
                            time_range = excluded.time_range,
                            note_text = excluded.note_text,
                            status = excluded.status,
                            updated_on = excluded.updated_on
                        """,
                        (
                            current_employee["id"],
                            entry_date,
                            item["slot_key"],
                            item["slot_label"],
                            item["time_range"],
                            note_text,
                            status,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM employee_hourly_notes
                        WHERE employee_id = ? AND entry_date = ? AND slot_key = ?
                        """,
                        (current_employee["id"], entry_date, item["slot_key"]),
                    )

        conn.execute(
            """
            INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
            VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
            """,
            (
                current_employee["id"],
                f"Updated hourly work notes for {entry_date}",
                request.remote_addr,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(
            url_for(
                "employee_hourly",
                employee_slug=employee_slug,
                msg="Hourly details saved successfully.",
            )
        )

    conn.close()
    return render_template(
        "employee_hourly.html",
        page_title="Hourly",
        page_subtitle=f"Daily office timetable from {hourly_summary['office_start']} to {hourly_summary['logout_time']}.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        hourly_summary=hourly_summary,
        hourly_schedule=hourly_schedule,
        hourly_entry_date=entry_date,
        message=message,
    )


@app.route("/<employee_slug>/apply-leave", methods=["GET", "POST"])
@employee_login_required(get_db_connection)
def employee_apply_leave(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    message = request.args.get("msg", "").strip()
    form_state = {
        "from_date": request.form.get("from_date", "").strip() or get_ist_date().isoformat(),
        "to_date": request.form.get("to_date", "").strip() or get_ist_date().isoformat(),
        "leave_type": request.form.get("leave_type", "Casual Leave").strip() or "Casual Leave",
        "reason": request.form.get("reason", "").strip(),
    }

    if request.method == "POST":
        try:
            start_date = date.fromisoformat(form_state["from_date"])
            end_date = date.fromisoformat(form_state["to_date"])
        except ValueError:
            message = "Please choose valid leave dates."
        else:
            if end_date < start_date:
                message = "The end date must be on or after the start date."
            elif not form_state["reason"]:
                message = "Please enter a reason for leave."
            else:
                overlapping_request = conn.execute(
                    """
                    SELECT id
                    FROM leave_requests
                    WHERE employee_id = ?
                      AND LOWER(COALESCE(status, '')) IN ('pending', 'approved')
                      AND NOT (to_date < ? OR from_date > ?)
                    LIMIT 1
                    """,
                    (
                        current_employee["id"],
                        form_state["from_date"],
                        form_state["to_date"],
                    ),
                ).fetchone()
                if overlapping_request:
                    message = "You already have a pending or approved leave request in that date range."
                else:
                    total_days = (end_date - start_date).days + 1
                    conn.execute(
                        """
                        INSERT INTO leave_requests (
                            employee_id,
                            applicant_name,
                            from_date,
                            to_date,
                            days,
                            leave_type,
                            reason,
                            status,
                            applied_on,
                            approved_by,
                            approved_on
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, NULL, NULL)
                        """,
                        (
                            current_employee["id"],
                            current_employee["full_name"],
                            form_state["from_date"],
                            form_state["to_date"],
                            total_days,
                            form_state["leave_type"],
                            form_state["reason"],
                            get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
                        VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
                        """,
                        (
                            current_employee["id"],
                            f"Leave request submitted from {form_state['from_date']} to {form_state['to_date']}",
                            request.remote_addr,
                        ),
                    )
                    conn.commit()
                    conn.close()
                    return redirect(
                        url_for(
                            "employee_leave_status",
                            employee_slug=employee_slug,
                            msg="Leave request submitted successfully.",
                        )
                    )

    recent_requests = conn.execute(
        """
        SELECT from_date, to_date, days, leave_type, status, applied_on
        FROM leave_requests
        WHERE employee_id = ?
        ORDER BY applied_on DESC, id DESC
        LIMIT 5
        """,
        (current_employee["id"],),
    ).fetchall()
    conn.close()

    return render_template(
        "employee_apply_leave.html",
        page_title="Apply Leave",
        page_subtitle="Submit a new leave request for HR review.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        form_state=form_state,
        recent_requests=recent_requests,
        message=message,
    )


@app.route("/<employee_slug>/attendance")
@employee_login_required(get_db_connection)
def employee_attendance(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    today = get_ist_date()
    start_date = today - timedelta(days=90)

    raw_attendance = conn.execute(
        """
        SELECT date, check_in, check_out, work_hours, break_hours, status, late_flag
        FROM attendance
        WHERE employee_id = ? AND date >= ?
        ORDER BY id ASC
        """,
        (current_employee["id"], start_date.isoformat()),
    ).fetchall()
    conn.close()

    attendance_dict = {row["date"]: dict(row) for row in raw_attendance}
    attendance_rows = []
    for i in range(90):
        current_date_obj = today - timedelta(days=i)
        current_date = current_date_obj.isoformat()
        if current_date in attendance_dict:
            attendance_rows.append(attendance_dict[current_date])
        else:
            attendance_rows.append({
                "date": current_date,
                "check_in": "--",
                "check_out": "--",
                "work_hours": 0,
                "break_hours": 0,
                "status": "Weekend" if current_date_obj.weekday() == 6 else "No Record",
                "late_flag": 0
            })

    return render_template(
        "employee_attendance.html",
        page_title="View Attendance",
        page_subtitle="Your recent attendance history.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        attendance_rows=attendance_rows,
    )


@app.route("/<employee_slug>/leave-status")
@employee_login_required(get_db_connection)
def employee_leave_status(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    message = request.args.get("msg", "").strip()
    leave_rows = conn.execute(
        """
        SELECT from_date, to_date, days, leave_type, reason, status, applied_on, approved_on
        FROM leave_requests
        WHERE employee_id = ?
        ORDER BY applied_on DESC, id DESC
        LIMIT 50
        """,
        (current_employee["id"],),
    ).fetchall()
    conn.close()

    return render_template(
        "employee_leave_status.html",
        page_title="View Leave Status",
        page_subtitle="Track pending, approved, and past leave requests.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        leave_rows=leave_rows,
        message=message,
    )


@app.route("/employee/notices")
@employee_login_required(get_db_connection)
def employee_notices():
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context()
    if redirect_response:
        return redirect_response

    notifications = load_notifications(conn)
    conn.close()

    return render_template(
        "employee_notices.html",
        page_title="Employee Notices",
        page_subtitle="Upcoming office announcements and closure updates for signed-in employees.",
        notifications=notifications,
        current_employee=current_employee,
        employee_slug=employee_slug,
    )


@app.route("/<employee_slug>/policy")
@employee_login_required(get_db_connection)
def employee_policy(employee_slug):
    conn, current_employee, employee_slug, redirect_response = resolve_employee_portal_context(
        employee_slug
    )
    if redirect_response:
        return redirect_response

    settings = load_system_settings(conn)
    conn.close()

    return render_template(
        "employee_policy.html",
        page_title="Company Policy",
        page_subtitle="Official attendance, leave, and discipline rules.",
        current_employee=current_employee,
        employee_slug=employee_slug,
        settings=settings,
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET" and session.get("admin_user_id"):
        return redirect(url_for("admin_dashboard"))

    message = request.args.get("msg", "").strip()
    email = request.form.get("email", "").strip()

    if request.method == "POST":
        password = request.form.get("password", "")
        if not email or not password:
            message = "Enter the admin email and password to continue."
        else:
            conn = get_db_connection()
            ensure_admin_users_table(conn)
            admin_row = get_admin_auth_record(conn, email)

            if not admin_row:
                message = "Invalid admin login details. Please try again."
                conn.close()
            elif not int(admin_row["is_active"] or 0):
                message = "This admin account is inactive."
                conn.close()
            else:
                is_valid_password, should_upgrade_password = verify_stored_password(
                    admin_row["password_hash"],
                    password,
                )
                if not is_valid_password:
                    message = "Invalid admin login details. Please try again."
                    conn.close()
                else:
                    if should_upgrade_password:
                        conn.execute(
                            """
                            UPDATE admin_users
                            SET password_hash = ?
                            WHERE id = ?
                            """,
                            (hash_password(password), admin_row["id"]),
                        )

                    conn.execute(
                        """
                        UPDATE admin_users
                        SET last_login = datetime('now', '+5 hours', '+30 minutes')
                        WHERE id = ?
                        """,
                        (admin_row["id"],),
                    )
                    conn.execute(
                        """
                        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
                        VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
                        """,
                        (
                            None,
                            f"Admin logged in: {admin_row['email']}",
                            request.remote_addr,
                        ),
                    )
                    conn.commit()
                    conn.close()

                    start_admin_session(admin_row)
                    return redirect(url_for("admin_dashboard"))

    return render_template(
        "admin_login.html",
        message=message,
        email=email,
    )


@app.route("/admin")
def admin_home():
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/dashboard")
def admin_dashboard():
    conn = get_db_connection()
    today = get_ist_date().isoformat()
    report_date = resolve_report_date(conn)

    total_employees = scalar(
        conn,
        "SELECT COUNT(*) FROM employees WHERE LOWER(COALESCE(status, 'active')) != 'inactive'",
    )
    present_today = scalar(
        conn,
        "SELECT COUNT(*) FROM attendance WHERE date = ? AND LOWER(status) = 'present'",
        (today,),
    )
    late_today = scalar(
        conn,
        "SELECT COUNT(*) FROM attendance WHERE date = ? AND late_flag = 1",
        (today,),
    )
    on_leave_today = scalar(
        conn,
        """
        SELECT COUNT(DISTINCT employee_id)
        FROM (
            SELECT employee_id
            FROM attendance
            WHERE date = ? AND LOWER(status) = 'leave'

            UNION

            SELECT employee_id
            FROM leave_requests
            WHERE LOWER(status) = 'approved'
              AND ? BETWEEN from_date AND to_date
        )
        """,
        (today, today),
    )

    absent_marked = scalar(
        conn,
        "SELECT COUNT(*) FROM attendance WHERE date = ? AND LOWER(status) = 'absent'",
        (today,),
    )
    attendance_rows_today = scalar(
        conn,
        "SELECT COUNT(*) FROM attendance WHERE date = ?",
        (today,),
    )
    
    is_sunday = get_ist_date().weekday() == 6
    if is_sunday:
        absent_today = absent_marked
    elif attendance_rows_today:
        absent_today = max(0, total_employees - present_today - on_leave_today)
    else:
        absent_today = absent_marked

    recent_activity = conn.execute(
        """
        SELECT
            COALESCE(e.full_name, 'System') AS actor_name,
            a.action,
            a.timestamp
        FROM activity_logs AS a
        LEFT JOIN employees AS e ON e.id = a.employee_id
        ORDER BY a.timestamp DESC
        LIMIT 6
        """
    ).fetchall()

    _, _, department_report_cards = build_department_report_cards(conn, report_date)
    department_cards = [
        {
            "department_id": card["department_id"],
            "dept_name": card["dept_name"],
            "working_employee_count": card["working_employee_count"],
        }
        for card in department_report_cards
    ]

    conn.close()
    return render_template(
        "admin_dashboard.html",
        section="dashboard",
        page_title="Dashboard",
        page_subtitle=f"Today's HR snapshot ({today}).",
        today=today,
        total_employees=total_employees,
        present_today=present_today,
        late_today=late_today,
        on_leave_today=on_leave_today,
        absent_today=absent_today,
        recent_activity=recent_activity,
        report_date=report_date,
        department_cards=department_cards,
    )


@app.route("/admin/employees", methods=["GET"])
def employee_management():
    conn = get_db_connection()
    ensure_users_table(conn)

    default_departments = ["Sales", "Digital Marketing", "Engineering", "Creative"]
    for dept in default_departments:
        conn.execute("INSERT OR IGNORE INTO departments (dept_name) VALUES (?)", (dept,))
    conn.commit()

    query = request.args.get("q", "").strip()
    edit_id = request.args.get("edit_id", type=int)
    message = request.args.get("msg", "").strip()

    departments = conn.execute(
        "SELECT id, dept_name FROM departments ORDER BY dept_name"
    ).fetchall()
    roles = conn.execute("SELECT id, role_name FROM roles ORDER BY role_name").fetchall()

    employee_sql = """
        SELECT
            e.id,
            e.emp_code,
            e.full_name,
            e.department_id,
            e.role_id,
            d.dept_name,
            r.role_name,
            e.email,
            e.phone,
            e.join_date,
            e.status,
            CASE WHEN u.id IS NULL THEN 0 ELSE 1 END AS portal_access_enabled,
            CASE
                WHEN u.id IS NULL THEN ''
                WHEN COALESCE(TRIM(e.email), '') != '' THEN e.email
                WHEN COALESCE(TRIM(e.emp_code), '') != '' THEN e.emp_code
                ELSE COALESCE(u.username, '')
            END AS portal_login_id
        FROM employees AS e
        LEFT JOIN departments AS d ON d.id = e.department_id
        LEFT JOIN roles AS r ON r.id = e.role_id
        LEFT JOIN users AS u ON u.employee_id = e.id
    """
    employee_params = ()
    if query:
        like = f"%{query}%"
        employee_sql += """
            WHERE
                e.emp_code LIKE ?
                OR e.full_name LIKE ?
                OR e.email LIKE ?
                OR d.dept_name LIKE ?
                OR r.role_name LIKE ?
        """
        employee_params = (like, like, like, like, like)

    employee_sql += " ORDER BY e.emp_code"
    employees = conn.execute(employee_sql, employee_params).fetchall()

    edit_employee = None
    if edit_id:
        edit_employee = conn.execute(
            """
            SELECT
                e.id,
                e.emp_code,
                e.full_name,
                e.department_id,
                e.role_id,
                e.phone,
                e.email,
                e.join_date,
                e.status,
                CASE WHEN u.id IS NULL THEN 0 ELSE 1 END AS portal_access_enabled,
                CASE
                    WHEN u.id IS NULL THEN ''
                    WHEN COALESCE(TRIM(e.email), '') != '' THEN e.email
                    WHEN COALESCE(TRIM(e.emp_code), '') != '' THEN e.emp_code
                    ELSE COALESCE(u.username, '')
                END AS portal_login_id
            FROM employees AS e
            LEFT JOIN users AS u ON u.employee_id = e.id
            WHERE e.id = ?
            """,
            (edit_id,),
        ).fetchone()

    new_emp_code = next_emp_code(conn)
    conn.close()

    return render_template(
        "admin_employees.html",
        section="employees",
        page_title="Employee Management",
        page_subtitle="Add, edit, deactivate, view, and search employees.",
        employees=employees,
        departments=departments,
        roles=roles,
        query=query,
        message=message,
        edit_employee=edit_employee,
        new_emp_code=new_emp_code,
    )


@app.route("/admin/employees/add", methods=["POST"])
def add_employee():
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    join_date = request.form.get("join_date", "").strip()
    status = request.form.get("status", "Active").strip() or "Active"
    portal_password = request.form.get("portal_password", "").strip()
    department_id_raw = request.form.get("department_id", "").strip()
    role_id_raw = request.form.get("role_id", "").strip()
    emp_code = request.form.get("emp_code", "").strip()
    new_department_name = request.form.get("new_department_name", "").strip()
    new_role_name = request.form.get("new_role_name", "").strip()

    conn = get_db_connection()
    ensure_users_table(conn)

    department_id = None
    if department_id_raw == "other" and new_department_name:
        try:
            cursor = conn.execute("INSERT INTO departments (dept_name) VALUES (?)", (new_department_name,))
            department_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT id FROM departments WHERE LOWER(dept_name) = LOWER(?)", (new_department_name,)).fetchone()
            department_id = row["id"] if row else None
    elif department_id_raw.isdigit():
        department_id = int(department_id_raw)

    role_id = None
    if role_id_raw == "other" and new_role_name:
        try:
            cursor = conn.execute("INSERT INTO roles (role_name) VALUES (?)", (new_role_name,))
            role_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT id FROM roles WHERE LOWER(role_name) = LOWER(?)", (new_role_name,)).fetchone()
            role_id = row["id"] if row else None
    elif role_id_raw.isdigit():
        role_id = int(role_id_raw)

    if not full_name or not department_id or not role_id:
        conn.close()
        return redirect(
            url_for("employee_management", msg="Please fill required fields for new employee.")
        )
    try:
        if not emp_code:
            emp_code = next_emp_code(conn)

        cursor = conn.execute(
            """
            INSERT INTO employees
            (emp_code, full_name, department_id, role_id, phone, email, join_date, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (emp_code, full_name, department_id, role_id, phone, email, join_date, status),
        )
        employee_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
            VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
            """,
            (employee_id, f"Employee created: {full_name} ({emp_code})", request.remote_addr),
        )
        if enable_employee_portal_access(conn, employee_id, portal_password):
            conn.execute(
                """
                INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
                VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
                """,
                (
                    employee_id,
                    f"Employee portal access enabled: {full_name} ({emp_code})",
                    request.remote_addr,
                ),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return redirect(
            url_for(
                "employee_management",
                msg=(
                    f"Employee code '{emp_code}' already exists, or the portal login ID is already in use."
                ),
            )
        )

    conn.close()
    success_message = f"Employee '{full_name}' added successfully."
    if portal_password:
        success_message += " Portal login is ready."
    return redirect(url_for("employee_management", msg=success_message))


@app.route("/admin/employees/<int:employee_id>/edit", methods=["POST"])
def edit_employee(employee_id):
    full_name = request.form.get("full_name", "").strip()
    emp_code = request.form.get("emp_code", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    join_date = request.form.get("join_date", "").strip()
    status = request.form.get("status", "Active").strip() or "Active"
    portal_password = request.form.get("portal_password", "").strip()
    department_id_raw = request.form.get("department_id", "").strip()
    role_id_raw = request.form.get("role_id", "").strip()
    new_department_name = request.form.get("new_department_name", "").strip()
    new_role_name = request.form.get("new_role_name", "").strip()

    conn = get_db_connection()
    ensure_users_table(conn)

    department_id = None
    if department_id_raw == "other" and new_department_name:
        try:
            cursor = conn.execute("INSERT INTO departments (dept_name) VALUES (?)", (new_department_name,))
            department_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT id FROM departments WHERE LOWER(dept_name) = LOWER(?)", (new_department_name,)).fetchone()
            department_id = row["id"] if row else None
    elif department_id_raw.isdigit():
        department_id = int(department_id_raw)

    role_id = None
    if role_id_raw == "other" and new_role_name:
        try:
            cursor = conn.execute("INSERT INTO roles (role_name) VALUES (?)", (new_role_name,))
            role_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT id FROM roles WHERE LOWER(role_name) = LOWER(?)", (new_role_name,)).fetchone()
            role_id = row["id"] if row else None
    elif role_id_raw.isdigit():
        role_id = int(role_id_raw)

    if not full_name or not department_id or not role_id:
        conn.close()
        return redirect(
            url_for(
                "employee_management",
                edit_id=employee_id,
                msg="Please fill required fields before saving changes.",
            )
        )

    row = conn.execute(
        "SELECT full_name, emp_code FROM employees WHERE id = ?",
        (employee_id,),
    ).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("employee_management", msg="Employee not found."))

    if not emp_code:
        emp_code = row["emp_code"]

    try:
        conn.execute(
            """
            UPDATE employees
            SET
                emp_code = ?,
                full_name = ?,
                department_id = ?,
                role_id = ?,
                phone = ?,
                email = ?,
                join_date = ?,
                status = ?
            WHERE id = ?
            """,
            (
                emp_code,
                full_name,
                department_id,
                role_id,
                phone,
                email,
                join_date,
                status,
                employee_id,
            ),
        )
        if enable_employee_portal_access(conn, employee_id, portal_password):
            conn.execute(
                """
                INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
                VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
                """,
                (
                    employee_id,
                    f"Employee portal password updated: {full_name} ({emp_code})",
                    request.remote_addr,
                ),
            )
        conn.execute(
            """
            INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
            VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
            """,
            (
                employee_id,
                f"Employee updated: {full_name} ({emp_code})",
                request.remote_addr,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return redirect(
            url_for(
                "employee_management",
                edit_id=employee_id,
                msg="This email or employee code is already in use by another employee. Please use a different one.",
            )
        )

    conn.close()
    success_message = f"Employee '{full_name}' updated successfully."
    if portal_password:
        success_message += " Portal password was reset."
    return redirect(url_for("employee_management", msg=success_message))


@app.route("/admin/employees/<int:employee_id>/deactivate", methods=["POST"])
def deactivate_employee(employee_id):
    query = request.form.get("q", "").strip()
    conn = get_db_connection()
    row = conn.execute(
        "SELECT full_name, emp_code, status FROM employees WHERE id = ?",
        (employee_id,),
    ).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("employee_management", q=query, msg="Employee not found.", _anchor="employee-directory-section"))

    if (row["status"] or "").lower() != "inactive":
        conn.execute(
            "UPDATE employees SET status = 'Inactive' WHERE id = ?",
            (employee_id,),
        )
        conn.execute(
            """
            INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
            VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
            """,
            (
                employee_id,
                f"Employee deactivated: {row['full_name']} ({row['emp_code']})",
                request.remote_addr,
            ),
        )
        conn.commit()
        msg = f"Employee '{row['full_name']}' deactivated."
    else:
        msg = f"Employee '{row['full_name']}' is already inactive."

    conn.close()
    return redirect(url_for("employee_management", q=query, msg=msg, _anchor="employee-directory-section"))


@app.route("/admin/policy")
def admin_policy():
    conn = get_db_connection()
    settings = load_system_settings(conn)
    conn.close()

    return render_template(
        "admin_policy.html",
        section="policy",
        page_title="Company Policy",
        page_subtitle="Official attendance, leave, and discipline rules as seen by employees.",
        settings=settings,
    )


@app.route("/admin/policy/<policy_type>")
def admin_policy_detail(policy_type):
    policy_name = policy_type.replace("-", " ").title()
    return render_template(
        "admin_coming_soon.html",
        section="policy",
        page_title=f"{policy_name}",
        page_subtitle="Under Construction",
    )

@app.route("/admin/attendance")
def attendance_management():
    conn = get_db_connection()
    view_mode = request.args.get("view", "daily").strip().lower()
    if view_mode not in ("daily", "monthly"):
        view_mode = "daily"

    selected_date = request.args.get("date", "").strip() or get_ist_date().isoformat()
    selected_month = request.args.get("month", "").strip() or get_ist_date().strftime("%Y-%m")
    override_id = request.args.get("override_id", type=int)
    message = request.args.get("msg", "").strip()

    attendance_rows = []
    monthly_rows = []
    override_row = None

    # Graph 1: daily attendance distribution
    daily_counts = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'present' THEN 1 ELSE 0 END), 0) AS present_count,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'leave' THEN 1 ELSE 0 END), 0) AS leave_count,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'absent' THEN 1 ELSE 0 END), 0) AS absent_count,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'holiday' THEN 1 ELSE 0 END), 0) AS holiday_count
        FROM attendance
        WHERE date = ?
        """,
        (selected_date,),
    ).fetchone()
    daily_chart = {
        "labels": ["Present", "Leave", "Absent", "Holiday"],
        "values": [
            int(daily_counts["present_count"] or 0),
            int(daily_counts["leave_count"] or 0),
            int(daily_counts["absent_count"] or 0),
            int(daily_counts["holiday_count"] or 0),
        ],
    }

    # Graph 2: monthly attendance trend by date
    monthly_trend_rows = conn.execute(
        """
        SELECT
            date,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'present' THEN 1 ELSE 0 END), 0) AS present_count,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'leave' THEN 1 ELSE 0 END), 0) AS leave_count,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) = 'absent' THEN 1 ELSE 0 END), 0) AS absent_count
        FROM attendance
        WHERE SUBSTR(date, 1, 7) = ?
        GROUP BY date
        ORDER BY date
        """,
        (selected_month,),
    ).fetchall()
    monthly_chart = {
        "labels": [row["date"] for row in monthly_trend_rows],
        "present": [int(row["present_count"] or 0) for row in monthly_trend_rows],
        "leave": [int(row["leave_count"] or 0) for row in monthly_trend_rows],
        "absent": [int(row["absent_count"] or 0) for row in monthly_trend_rows],
    }

    if view_mode == "daily":
        attendance_rows = conn.execute(
            """
            SELECT
                a.id,
                e.emp_code,
                e.full_name,
                a.date,
                a.check_in,
                a.check_out,
                a.work_hours,
                a.break_hours,
                a.status,
                a.late_flag,
                a.auto_marked
            FROM attendance AS a
            JOIN employees AS e ON e.id = a.employee_id
            WHERE a.date = ?
            ORDER BY e.emp_code
            """,
            (selected_date,),
        ).fetchall()

        if override_id:
            override_row = conn.execute(
                """
                SELECT
                    a.id,
                    a.employee_id,
                    e.emp_code,
                    e.full_name,
                    a.date,
                    a.check_in,
                    a.check_out,
                    a.work_hours,
                    a.break_hours,
                    a.status,
                    a.late_flag,
                    a.auto_marked
                FROM attendance AS a
                JOIN employees AS e ON e.id = a.employee_id
                WHERE a.id = ?
                """,
                (override_id,),
            ).fetchone()
    else:
        monthly_rows = conn.execute(
            """
            SELECT
                e.id AS employee_id,
                e.emp_code,
                e.full_name,
                SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'present' THEN 1 ELSE 0 END) AS present_days,
                SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'leave' THEN 1 ELSE 0 END) AS leave_days,
                SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'absent' THEN 1 ELSE 0 END) AS absent_days,
                SUM(CASE WHEN COALESCE(a.late_flag, 0) = 1 THEN 1 ELSE 0 END) AS late_days
            FROM employees AS e
            LEFT JOIN attendance AS a
                ON a.employee_id = e.id
               AND SUBSTR(a.date, 1, 7) = ?
            GROUP BY e.id, e.emp_code, e.full_name
            ORDER BY e.emp_code
            """,
            (selected_month,),
        ).fetchall()

    conn.close()

    return render_template(
        "admin_attendance.html",
        section="attendance",
        page_title="Attendance Management",
        page_subtitle="Daily/Monthly attendance, filtering, and manual override.",
        view_mode=view_mode,
        selected_date=selected_date,
        selected_month=selected_month,
        message=message,
        attendance_rows=attendance_rows,
        monthly_rows=monthly_rows,
        override_row=override_row,
        daily_chart=daily_chart,
        monthly_chart=monthly_chart,
    )


@app.route("/admin/attendance/<int:attendance_id>/override", methods=["POST"])
def override_attendance(attendance_id):
    view_mode = request.form.get("view", "daily").strip().lower()
    selected_date = request.form.get("date", "").strip() or get_ist_date().isoformat()
    selected_month = request.form.get("month", "").strip() or get_ist_date().strftime("%Y-%m")

    status = request.form.get("status", "Present").strip() or "Present"
    check_in = request.form.get("check_in", "").strip() or None
    check_out = request.form.get("check_out", "").strip() or None
    work_hours_raw = request.form.get("work_hours", "").strip()
    break_hours_raw = request.form.get("break_hours", "").strip()
    late_flag = 1 if request.form.get("late_flag", "0") in ("1", "true", "True", "on") else 0

    try:
        work_hours = float(work_hours_raw) if work_hours_raw else 0.0
        break_hours = float(break_hours_raw) if break_hours_raw else 0.0
    except ValueError:
        return redirect(
            url_for(
                "attendance_management",
                view=view_mode,
                date=selected_date,
                month=selected_month,
                override_id=attendance_id,
                msg="Invalid number format for work/break hours.",
            )
        )

    conn = get_db_connection()
    row = conn.execute(
        "SELECT id, employee_id FROM attendance WHERE id = ?",
        (attendance_id,),
    ).fetchone()
    if not row:
        conn.close()
        return redirect(
            url_for(
                "attendance_management",
                view=view_mode,
                date=selected_date,
                month=selected_month,
                msg="Attendance record not found.",
            )
        )

    conn.execute(
        """
        UPDATE attendance
        SET
            check_in = ?,
            check_out = ?,
            work_hours = ?,
            break_hours = ?,
            status = ?,
            late_flag = ?,
            auto_marked = 0
        WHERE id = ?
        """,
        (check_in, check_out, work_hours, break_hours, status, late_flag, attendance_id),
    )
    conn.execute(
        """
        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
        VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
        """,
        (
            row["employee_id"],
            f"Attendance override applied for record #{attendance_id}",
            request.remote_addr,
        ),
    )
    conn.commit()
    conn.close()
    return redirect(
        url_for(
            "attendance_management",
            view=view_mode,
            date=selected_date,
            month=selected_month,
            msg="Attendance updated successfully.",
        )
    )


@app.route("/admin/leaves")
def leave_management():
    conn = get_db_connection()
    selected_date = request.args.get("date", "").strip() or get_ist_date().isoformat()
    selected_department_id = request.args.get("dept_detail", type=int)
    message = request.args.get("msg", "").strip()

    employee_snapshot_rows = conn.execute(
        """
        SELECT
            e.id AS employee_id,
            e.emp_code,
            e.full_name,
            d.id AS department_id,
            d.dept_name,
            LOWER(COALESCE(a.status, '')) AS attendance_status,
            COALESCE(a.status, '') AS attendance_status_label,
            approved_leave.id AS approved_leave_id,
            COALESCE(latest_request.status, '') AS leave_request_status,
            latest_request.applied_on AS leave_request_applied_on,
            latest_request.from_date AS leave_request_from_date,
            latest_request.to_date AS leave_request_to_date
        FROM employees AS e
        LEFT JOIN departments AS d ON d.id = e.department_id
        LEFT JOIN attendance AS a
            ON a.employee_id = e.id
           AND a.date = ?
        LEFT JOIN leave_requests AS approved_leave
            ON approved_leave.id = (
                SELECT lr_a.id
                FROM leave_requests AS lr_a
                WHERE lr_a.employee_id = e.id
                  AND LOWER(COALESCE(lr_a.status, '')) = 'approved'
                  AND ? BETWEEN lr_a.from_date AND lr_a.to_date
                ORDER BY COALESCE(lr_a.approved_on, lr_a.applied_on, '') DESC, lr_a.id DESC
                LIMIT 1
            )
        LEFT JOIN leave_requests AS latest_request
            ON latest_request.id = (
                SELECT lr_l.id
                FROM leave_requests AS lr_l
                WHERE lr_l.employee_id = e.id
                ORDER BY COALESCE(lr_l.applied_on, '') DESC, lr_l.id DESC
                LIMIT 1
            )
        WHERE LOWER(COALESCE(e.status, 'active')) != 'inactive'
        ORDER BY COALESCE(d.dept_name, 'Unassigned'), e.emp_code
        """,
        (selected_date, selected_date),
    ).fetchall()
    
    selected_date_obj = date.fromisoformat(selected_date)
    is_sunday = selected_date_obj.weekday() == 6

    department_lookup = {}
    for row in employee_snapshot_rows:
        department_id = row["department_id"] if row["department_id"] is not None else 0
        department_name = row["dept_name"] or "Unassigned"

        if department_id not in department_lookup:
            department_lookup[department_id] = {
                "department_id": department_id,
                "dept_name": department_name,
                "total_members": 0,
                "present_count": 0,
                "absent_count": 0,
                "leave_count": 0,
                "holiday_count": 0,
                "new_request_count": 0,
                "absent_employees": [],
                "present_employees": [],
            }

        card = department_lookup[department_id]
        card["total_members"] += 1

        attendance_status = (row["attendance_status"] or "").strip().lower()
        has_approved_leave = bool(row["approved_leave_id"])
        is_present = attendance_status == "present"
        is_on_leave = attendance_status == "leave" or has_approved_leave
        is_holiday = attendance_status == "holiday"
        is_absent = attendance_status == "absent" or (not is_present and not is_on_leave and not is_holiday and not is_sunday)

        if is_present:
            card["present_count"] += 1
            card["present_employees"].append(
                {
                    "employee_id": row["employee_id"],
                    "emp_code": row["emp_code"],
                    "full_name": row["full_name"],
                    "attendance_status": row["attendance_status_label"] or "Present",
                }
            )
            continue
        
        if is_on_leave:
            card["leave_count"] += 1
        elif is_holiday:
            card["holiday_count"] += 1
        elif is_absent:
            card["absent_count"] += 1
        else:
            continue

        leave_request_status = (row["leave_request_status"] or "").strip() or "No Request"
        leave_request_status_lower = leave_request_status.lower()
        is_new_request = leave_request_status_lower in ("pending", "new", "new request")

        leave_request_window = ""
        if row["leave_request_from_date"] and row["leave_request_to_date"]:
            if row["leave_request_from_date"] == row["leave_request_to_date"]:
                leave_request_window = row["leave_request_from_date"]
            else:
                leave_request_window = f"{row['leave_request_from_date']} to {row['leave_request_to_date']}"

        card["absent_employees"].append(
            {
                "employee_id": row["employee_id"],
                "emp_code": row["emp_code"],
                "full_name": row["full_name"],
                "attendance_status": row["attendance_status_label"] or "Absent",
                "leave_request_status": leave_request_status,
                "leave_request_applied_on": row["leave_request_applied_on"] or "",
                "leave_request_window": leave_request_window,
                "is_new_request": is_new_request,
            }
        )
        if is_new_request:
            card["new_request_count"] += 1

    department_cards = sorted(
        department_lookup.values(),
        key=lambda item: item["dept_name"].lower(),
    )
    selected_department = None
    if selected_department_id is not None:
        selected_department = next(
            (item for item in department_cards if item["department_id"] == selected_department_id),
            None,
        )

    leave_requests = conn.execute(
        """
        SELECT
            id,
            applicant_name,
            leave_type,
            from_date,
            to_date,
            days,
            reason,
            status,
            applied_on
        FROM leave_requests
        ORDER BY from_date DESC, applied_on DESC
        """
    ).fetchall()
    conn.close()

    return render_template(
        "admin_leaves.html",
        section="leaves",
        page_title="Leave Management",
        page_subtitle="Review leave requests and employee details.",
        selected_date=selected_date,
        selected_department_id=selected_department_id,
        selected_department=selected_department,
        department_cards=department_cards,
        message=message,
        leave_requests=leave_requests,
    )


@app.route("/admin/leaves/<int:leave_id>/approve", methods=["POST"])
def approve_leave_request(leave_id):
    selected_date = request.form.get("date", "").strip() or get_ist_date().isoformat()
    selected_department_id = request.form.get("dept_detail", type=int)

    conn = get_db_connection()
    leave_row = conn.execute(
        """
        SELECT
            id,
            employee_id,
            applicant_name,
            status
        FROM leave_requests
        WHERE id = ?
        """,
        (leave_id,),
    ).fetchone()

    if not leave_row:
        conn.close()
        return redirect(
            url_for(
                "leave_management",
                date=selected_date,
                dept_detail=selected_department_id,
                msg="Leave request not found.",
                _anchor="leave-requests-section",
            )
        )

    current_status = (leave_row["status"] or "").strip().lower()
    if current_status not in ("pending", "new", "new request"):
        conn.close()
        return redirect(
            url_for(
                "leave_management",
                date=selected_date,
                dept_detail=selected_department_id,
                msg="Only pending leave requests can be approved.",
                _anchor="leave-requests-section",
            )
        )

    conn.execute(
        """
        UPDATE leave_requests
        SET
            status = 'Approved',
            approved_on = datetime('now', '+5 hours', '+30 minutes')
        WHERE id = ?
        """,
        (leave_id,),
    )
    conn.execute(
        """
        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
        VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
        """,
        (
            leave_row["employee_id"],
            f"Leave request #{leave_id} approved for {leave_row['applicant_name'] or 'employee'}",
            request.remote_addr,
        ),
    )
    conn.commit()
    conn.close()

    return redirect(
        url_for(
            "leave_management",
            date=selected_date,
            dept_detail=selected_department_id,
            msg=f"Leave request #{leave_id} approved and activity updated.",
            _anchor="leave-requests-section",
        )
    )


@app.route("/admin/leaves/<int:leave_id>/reject", methods=["POST"])
def reject_leave_request(leave_id):
    selected_date = request.form.get("date", "").strip() or get_ist_date().isoformat()
    selected_department_id = request.form.get("dept_detail", type=int)

    conn = get_db_connection()
    leave_row = conn.execute(
        """
        SELECT
            id,
            employee_id,
            applicant_name,
            status
        FROM leave_requests
        WHERE id = ?
        """,
        (leave_id,),
    ).fetchone()

    if not leave_row:
        conn.close()
        return redirect(
            url_for(
                "leave_management",
                date=selected_date,
                dept_detail=selected_department_id,
                msg="Leave request not found.",
                _anchor="leave-requests-section",
            )
        )

    current_status = (leave_row["status"] or "").strip().lower()
    if current_status not in ("pending", "new", "new request"):
        conn.close()
        return redirect(
            url_for(
                "leave_management",
                date=selected_date,
                dept_detail=selected_department_id,
                msg="Only pending leave requests can be rejected.",
                _anchor="leave-requests-section",
            )
        )

    conn.execute(
        """
        UPDATE leave_requests
        SET
            status = 'Rejected',
            approved_on = datetime('now', '+5 hours', '+30 minutes')
        WHERE id = ?
        """,
        (leave_id,),
    )
    conn.execute(
        """
        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
        VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
        """,
        (
            leave_row["employee_id"],
            f"Leave request #{leave_id} rejected for {leave_row['applicant_name'] or 'employee'}",
            request.remote_addr,
        ),
    )
    conn.commit()
    conn.close()

    return redirect(
        url_for(
            "leave_management",
            date=selected_date,
            dept_detail=selected_department_id,
            msg=f"Leave request #{leave_id} rejected and activity updated.",
            _anchor="leave-requests-section",
        )
    )


@app.route("/admin/leaves/individual-snapshot")
def individual_attendance_snapshot():
    conn = get_db_connection()
    query = request.args.get("q", "").strip()
    selected_month = request.args.get("month", "").strip() or get_ist_date().strftime("%Y-%m")

    employee_sql = """
        SELECT
            e.id AS employee_id,
            e.emp_code,
            e.full_name,
            COALESCE(d.dept_name, 'Unassigned') AS dept_name,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'present' THEN 1 ELSE 0 END), 0) AS total_present,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'absent' THEN 1 ELSE 0 END), 0) AS total_absent,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(a.status, '')) = 'leave' THEN 1 ELSE 0 END), 0) AS total_leave_days,
            COALESCE(SUM(CASE WHEN COALESCE(a.late_flag, 0) = 1 THEN 1 ELSE 0 END), 0) AS total_late_days
        FROM employees AS e
        LEFT JOIN departments AS d ON d.id = e.department_id
        LEFT JOIN attendance AS a ON a.employee_id = e.id AND SUBSTR(a.date, 1, 7) = ?
        WHERE LOWER(COALESCE(e.status, 'active')) != 'inactive'
    """
    employee_params = [selected_month]
    if query:
        employee_sql += " AND LOWER(COALESCE(e.full_name, '')) LIKE ?"
        employee_params.append(f"%{query.lower()}%")

    employee_sql += """
        GROUP BY e.id, e.emp_code, e.full_name, d.dept_name
        ORDER BY e.emp_code
    """
    employee_totals = conn.execute(employee_sql, tuple(employee_params)).fetchall()

    attendance_rows = []
    if employee_totals:
        employee_ids = [row["employee_id"] for row in employee_totals]
        placeholders = ",".join("?" for _ in employee_ids)
        attendance_rows = conn.execute(
            f"""
            SELECT
                employee_id,
                date,
                status,
                work_hours
            FROM attendance
            WHERE employee_id IN ({placeholders}) AND SUBSTR(date, 1, 7) = ?
            ORDER BY employee_id, date
            """,
            tuple(employee_ids) + (selected_month,),
        ).fetchall()
    conn.close()

    streak_state = {}
    for row in attendance_rows:
        employee_id = row["employee_id"]
        if employee_id not in streak_state:
            streak_state[employee_id] = {
                "current_streak": 0,
                "max_streak": 0,
                "previous_date": None,
                "previous_half_day": False,
            }

        state = streak_state[employee_id]
        record_date = None
        if row["date"]:
            try:
                record_date = date.fromisoformat(row["date"])
            except ValueError:
                record_date = None

        is_half_day = is_half_day_record(row["status"], row["work_hours"])
        if is_half_day:
            if (
                state["previous_half_day"]
                and state["previous_date"]
                and record_date
                and record_date == state["previous_date"] + timedelta(days=1)
            ):
                state["current_streak"] += 1
            elif (
                state["previous_half_day"]
                and state["previous_date"]
                and record_date
                and record_date == state["previous_date"]
            ):
                state["current_streak"] = max(1, state["current_streak"])
            else:
                state["current_streak"] = 1

            state["max_streak"] = max(state["max_streak"], state["current_streak"])
        else:
            state["current_streak"] = 0

        state["previous_half_day"] = is_half_day
        state["previous_date"] = record_date

    employee_cards = []
    for row in employee_totals:
        state = streak_state.get(row["employee_id"], {})
        max_half_day_streak = int(state.get("max_streak", 0) or 0)
        total_leave_days = int(row["total_leave_days"] or 0)
        total_late_days = int(row["total_late_days"] or 0)
        total_absent_days = int(row["total_absent"] or 0)
        red_flag = (total_leave_days >= 3 or total_absent_days >= 3) and total_late_days >= 3
        orange_flag = total_leave_days >= 3 and not red_flag

        employee_cards.append(
            {
                "employee_id": row["employee_id"],
                "emp_code": row["emp_code"],
                "full_name": row["full_name"],
                "dept_name": row["dept_name"],
                "total_present": int(row["total_present"] or 0),
                "total_absent": int(row["total_absent"] or 0),
                "total_leave_days": total_leave_days,
                "total_late_days": total_late_days,
                "max_half_day_streak": max_half_day_streak,
                "red_flag": red_flag,
                "orange_flag": orange_flag,
                "yellow_card": max_half_day_streak >= 3,
            }
        )

    employee_cards.sort(
        key=lambda item: (
            -int(item["red_flag"]),
            -int(item["orange_flag"]),
            -int(item["yellow_card"]),
            item["full_name"].lower(),
        )
    )

    return render_template(
        "admin_individual_snapshot.html",
        section="leaves",
        page_title="Individual Attendance Snapshot",
        query=query,
        selected_month=selected_month,
        employee_cards=employee_cards,
    )
    


@app.route("/admin/reports")
def reports():
    conn = get_db_connection()
    requested_date = request.args.get("date", "").strip()
    report_date = resolve_report_date(conn, requested_date)
    total_departments, active_employees_count, department_report_cards = (
        build_department_report_cards(conn, report_date)
    )
    department_cards = [
        {
            "department_id": card["department_id"],
            "dept_name": card["dept_name"],
            "working_employee_count": card["working_employee_count"],
        }
        for card in department_report_cards
    ]

    conn.close()
    return render_template(
        "admin_reports.html",
        section="reports",
        page_title="Reports",
        page_subtitle=f"Department-wise daily work board for {report_date}.",
        department_cards=department_cards,
        report_date=report_date,
        total_departments=total_departments,
        active_employees_count=active_employees_count,
    )


@app.route("/admin/reports/department/<int:department_id>")
def department_report(department_id):
    conn = get_db_connection()
    requested_date = request.args.get("date", "").strip()
    report_date = resolve_report_date(conn, requested_date)
    _, _, department_cards = build_department_report_cards(conn, report_date)
    conn.close()

    selected_department = next(
        (item for item in department_cards if item["department_id"] == department_id),
        None,
    )
    if not selected_department:
        return redirect(url_for("reports", date=report_date))

    return render_template(
        "admin_department_report.html",
        section="reports",
        page_title=f"{selected_department['dept_name']} Report",
        page_subtitle=f"Employees currently working in {selected_department['dept_name']} on {report_date}.",
        report_date=report_date,
        selected_department=selected_department,
    )


@app.route("/admin/reports/department/<int:department_id>/employee/<int:employee_id>")
def employee_activity_report(department_id, employee_id):
    conn = get_db_connection()
    requested_date = request.args.get("date", "").strip()
    report_date = resolve_report_date(conn, requested_date)
    _, _, department_cards = build_department_report_cards(conn, report_date)
    conn.close()

    selected_department = next(
        (item for item in department_cards if item["department_id"] == department_id),
        None,
    )
    if not selected_department:
        return redirect(url_for("reports", date=report_date))

    selected_employee = next(
        (
            item
            for item in selected_department["members"]
            if item["employee_id"] == employee_id
        ),
        None,
    )
    if not selected_employee:
        return redirect(
            url_for("department_report", department_id=department_id, date=report_date)
        )
    # Load employee hourly notes for this report date so admin can view details
    conn = get_db_connection()
    settings = load_system_settings(conn)
    notes_by_slot = load_employee_hourly_notes(conn, employee_id, report_date)
    hourly_summary, hourly_schedule = build_employee_hourly_schedule(settings, notes_by_slot)
    conn.close()
    
    selected_employee["hourly_schedule"] = hourly_schedule
    selected_employee["hourly_notes"] = notes_by_slot
    return render_template(
        "admin_employee_activity.html",
        section="reports",
        page_title=f"{selected_employee['full_name']} Activity",
        page_subtitle=f"Hourly task breakdown for {selected_employee['full_name']} on {report_date}.",
        report_date=report_date,
        selected_department=selected_department,
        selected_employee=selected_employee,
        hourly_schedule=hourly_schedule,
        hourly_summary=hourly_summary,
    )


@app.route("/admin/settings")
def system_settings():
    message = request.args.get("msg", "").strip()
    return render_system_settings_page(message=message)


@app.route("/admin/settings/save", methods=["POST"])
def save_system_settings():
    settings = {
        "workday_start_time": request.form.get("workday_start_time", "").strip(),
        "logout_time": request.form.get("logout_time", "").strip(),
        "late_mark_threshold": request.form.get("late_mark_threshold", "").strip(),
        "maximum_work_hours": request.form.get("maximum_work_hours", "").strip(),
        "casual_leave_days": request.form.get("casual_leave_days", "").strip(),
        "sick_leave_days": request.form.get("sick_leave_days", "").strip(),
        "office_latitude": request.form.get("office_latitude", "0.0").strip(),
        "office_longitude": request.form.get("office_longitude", "0.0").strip(),
        "geofence_radius_meters": request.form.get("geofence_radius_meters", "500").strip(),
    }

    if not is_valid_time_value(settings["workday_start_time"]):
        return render_system_settings_page(
            message="Workday start time must use HH:MM format.",
            settings=settings,
        )

    if not is_valid_time_value(settings["logout_time"]):
        return render_system_settings_page(
            message="Log out time must use HH:MM format.",
            settings=settings,
        )

    if not is_valid_time_value(settings["late_mark_threshold"]):
        return render_system_settings_page(
            message="Late mark threshold must use HH:MM format.",
            settings=settings,
        )

    workday_start_time = datetime.strptime(settings["workday_start_time"], "%H:%M")
    logout_time = datetime.strptime(settings["logout_time"], "%H:%M")

    if logout_time <= workday_start_time:
        return render_system_settings_page(
            message="Log out time must be after the workday start time.",
            settings=settings,
        )

    try:
        float(settings["office_latitude"])
        float(settings["office_longitude"])
        radius = int(settings["geofence_radius_meters"])
        if radius < 0:
            raise ValueError()
    except ValueError:
        return render_system_settings_page(
            message="Geofencing values must be valid numbers, and radius cannot be negative.",
            settings=settings,
        )

    try:
        maximum_work_hours = float(settings["maximum_work_hours"])
    except ValueError:
        return render_system_settings_page(
            message="Maximum work hours must be a valid number.",
            settings=settings,
        )
    if maximum_work_hours <= 0 or maximum_work_hours > 24:
        return render_system_settings_page(
            message="Maximum work hours must be between 0 and 24.",
            settings=settings,
        )

    try:
        casual_leave_days = int(settings["casual_leave_days"])
        sick_leave_days = int(settings["sick_leave_days"])
    except ValueError:
        return render_system_settings_page(
            message="Casual leave and sick leave must be whole numbers.",
            settings=settings,
        )
    if casual_leave_days < 0 or sick_leave_days < 0:
        return render_system_settings_page(
            message="Leave policy values cannot be negative.",
            settings=settings,
        )

    settings["maximum_work_hours"] = f"{maximum_work_hours:.1f}"
    settings["casual_leave_days"] = str(casual_leave_days)
    settings["sick_leave_days"] = str(sick_leave_days)

    conn = get_db_connection()
    ensure_system_settings(conn)
    conn.executemany(
        """
        INSERT INTO system_settings (setting_key, setting_value)
        VALUES (?, ?)
        ON CONFLICT(setting_key) DO UPDATE SET
            setting_value = excluded.setting_value
        """,
        list(settings.items()),
    )
    conn.execute(
        """
        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
        VALUES (?, ?, ?, ?)
        """,
        (
            None,
            "Updated system settings",
            get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
            request.remote_addr,
        ),
    )
    conn.commit()
    conn.close()

    return redirect(
        url_for("system_settings", msg="System settings updated successfully.")
    )


@app.route("/admin/settings/notifications/add", methods=["POST"])
def add_notification():
    notification_form = {
        "title": request.form.get("title", "").strip(),
        "notice_date": request.form.get("notice_date", "").strip(),
        "message": request.form.get("message", "").strip(),
        "office_closed": request.form.get("office_closed") in ("1", "true", "True", "on"),
    }

    if not notification_form["title"] or not notification_form["message"] or not notification_form["notice_date"]:
        return render_system_settings_page(
            message="Notification title, date, and message are required.",
            notification_form=notification_form,
        )

    try:
        date.fromisoformat(notification_form["notice_date"])
    except ValueError:
        return render_system_settings_page(
            message="Notification date must be a valid date.",
            notification_form=notification_form,
        )

    conn = get_db_connection()
    ensure_notifications_table(conn)
    conn.execute(
        """
        INSERT INTO notifications (title, message, notice_date, office_closed, is_active, created_on)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (
            notification_form["title"],
            notification_form["message"],
            notification_form["notice_date"],
            1 if notification_form["office_closed"] else 0,
            get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.execute(
        """
        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
        VALUES (?, ?, ?, ?)
        """,
        (
            None,
            "Published office notification",
            get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
            request.remote_addr,
        ),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("system_settings", msg="Notification published successfully."))


@app.route("/admin/settings/notifications/<int:notification_id>/toggle", methods=["POST"])
def toggle_notification(notification_id):
    conn = get_db_connection()
    ensure_notifications_table(conn)
    notification = conn.execute(
        """
        SELECT id, title, is_active
        FROM notifications
        WHERE id = ?
        """,
        (notification_id,),
    ).fetchone()
    if not notification:
        conn.close()
        return redirect(url_for("system_settings", msg="Notification not found."))

    new_status = 0 if notification["is_active"] else 1
    conn.execute(
        """
        UPDATE notifications
        SET is_active = ?
        WHERE id = ?
        """,
        (new_status, notification_id),
    )
    conn.execute(
        """
        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
        VALUES (?, ?, ?, ?)
        """,
        (
            None,
            "Archived office notification" if notification["is_active"] else "Reactivated office notification",
            get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
            request.remote_addr,
        ),
    )
    conn.commit()
    conn.close()

    msg = "Notification archived." if notification["is_active"] else "Notification reactivated."
    return redirect(url_for("system_settings", msg=msg))


@app.route("/admin/logs")
def activity_logs():
    conn = get_db_connection()
    logs = conn.execute(
        """
        SELECT
            COALESCE(e.full_name, 'System') AS actor_name,
            a.action,
            a.timestamp,
            a.ip_address
        FROM activity_logs AS a
        LEFT JOIN employees AS e ON e.id = a.employee_id
        ORDER BY a.timestamp DESC
        LIMIT 100
        """
    ).fetchall()
    conn.close()

    return render_template(
        "admin_logs.html",
        section="logs",
        page_title="Activity Logs",
        page_subtitle="Recent administrative and user activity.",
        logs=logs,
    )


@app.route("/admin/backup")
def backup_restore():
    file_size_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    message = request.args.get("msg", "").strip()
    selected_month = request.args.get("month", "").strip() or get_ist_date().isoformat()[:7]
    if not is_valid_month_value(selected_month):
        selected_month = get_ist_date().isoformat()[:7]
    return render_template(
        "admin_backup.html",
        section="backup",
        page_title="Backup and Restore",
        page_subtitle="Database backup status and restore controls.",
        file_size_bytes=file_size_bytes,
        selected_month=selected_month,
        message=message,
    )


@app.route("/admin/backup/export-monthly")
def export_monthly_csv():
    selected_month = request.args.get("month", "").strip() or get_ist_date().isoformat()[:7]
    if not is_valid_month_value(selected_month):
        return redirect(
            url_for("backup_restore", msg="Please choose a valid month for export.")
        )

    conn = get_db_connection()
    csv_content = build_monthly_attendance_export(selected_month, conn)

    conn.execute(
        """
        INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
        VALUES (?, ?, ?, ?)
        """,
        (
            None,
            f"Exported monthly CSV backup for {selected_month}",
            get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
            request.remote_addr,
        ),
    )
    conn.commit()
    conn.close()

    csv_stream = io.BytesIO(csv_content.encode("utf-8"))
    return send_file(
        csv_stream,
        as_attachment=True,
        download_name=f"monthly_attendance_export_{selected_month}.csv",
        mimetype="text/csv",
    )


@app.route("/logout")
def logout():
    employee_id = session.get("employee_id")
    employee_name = session.get("employee_name", "Employee")
    employee_code = session.get("employee_emp_code", "")

    if employee_id:
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
            VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
            """,
            (
                employee_id,
                f"Employee logged out: {employee_name} ({employee_code})",
                request.remote_addr,
            ),
        )
        conn.commit()
        conn.close()

    clear_employee_session()
    return redirect(url_for("home"))


@app.route("/admin/logout")
def admin_logout():
    admin_email = session.get("admin_email", "")

    if session.get("admin_user_id"):
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO activity_logs (employee_id, action, timestamp, ip_address)
            VALUES (?, ?, datetime('now', '+5 hours', '+30 minutes'), ?)
            """,
            (
                None,
                f"Admin logged out: {admin_email}",
                request.remote_addr,
            ),
        )
        conn.commit()
        conn.close()

    clear_admin_session()
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True, use_reloader=True)
