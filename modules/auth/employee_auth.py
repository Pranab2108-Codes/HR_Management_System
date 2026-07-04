import re
from datetime import datetime, timedelta
from functools import wraps

from flask import redirect, session, url_for

from modules.db.schema import ensure_employee_profile_columns, ensure_users_table
from .common import hash_password

EMPLOYEE_SESSION_KEYS = (
    "employee_id",
    "employee_name",
    "employee_emp_code",
    "employee_slug",
    "employee_login_at",
)



def clear_employee_session():
    for key in EMPLOYEE_SESSION_KEYS:
        session.pop(key, None)



def build_employee_slug(full_name):
    cleaned_value = re.sub(r"[^a-z0-9]+", "_", (full_name or "").strip().lower())
    return cleaned_value.strip("_") or "employee"



def start_employee_session(auth_row):
    clear_employee_session()
    session.permanent = True
    session["employee_id"] = auth_row["employee_id"]
    session["employee_name"] = auth_row["full_name"]
    session["employee_emp_code"] = auth_row["emp_code"]
    session["employee_slug"] = build_employee_slug(auth_row["full_name"])
    session["employee_login_at"] = datetime.utcnow().isoformat()



def build_portal_login_id(employee_row, fallback_username=""):
    return (
        (employee_row["email"] or "").strip()
        or (employee_row["emp_code"] or "").strip()
        or (fallback_username or "").strip()
    )



def get_employee_auth_record(conn, login_id):
    identifier = (login_id or "").strip()
    if not identifier:
        return None

    ensure_users_table(conn)
    ensure_employee_profile_columns(conn)
    return conn.execute(
        """
        SELECT
            u.id AS user_id,
            u.employee_id,
            u.username,
            u.password_hash,
            u.must_change_password,
            u.last_login,
            e.full_name,
            e.emp_code,
            e.email,
            e.status
        FROM users AS u
        JOIN employees AS e ON e.id = u.employee_id
        WHERE LOWER(COALESCE(u.username, '')) = LOWER(?)
           OR LOWER(COALESCE(e.email, '')) = LOWER(?)
           OR LOWER(COALESCE(e.emp_code, '')) = LOWER(?)
        LIMIT 1
        """,
        (identifier, identifier, identifier),
    ).fetchone()



def get_employee_profile(conn, employee_id):
    ensure_users_table(conn)
    ensure_employee_profile_columns(conn)
    return conn.execute(
        """
        SELECT
            e.id,
            e.emp_code,
            e.full_name,
            e.email,
            e.phone,
            e.alternate_phone,
            e.address,
            e.date_of_birth,
            e.emergency_contact,
            e.blood_group,
            e.join_date,
            e.department_id,
            e.role_id,
            e.status,
            e.profile_image,
            COALESCE(d.dept_name, 'Unassigned') AS dept_name,
            COALESCE(r.role_name, 'Unassigned') AS role_name,
            u.id AS user_id,
            u.username,
            u.must_change_password,
            u.last_login
        FROM employees AS e
        LEFT JOIN users AS u ON u.employee_id = e.id
        LEFT JOIN departments AS d ON d.id = e.department_id
        LEFT JOIN roles AS r ON r.id = e.role_id
        WHERE e.id = ?
        """,
        (employee_id,),
    ).fetchone()



def employee_login_required(get_db_connection):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(*args, **kwargs):
            employee_id = session.get("employee_id")
            if not employee_id:
                return redirect(
                    url_for("login", msg="Please sign in to access the employee portal.")
                )

            login_at = session.get("employee_login_at")
            if not login_at:
                clear_employee_session()
                return redirect(
                    url_for("login", msg="Session expired after 6 days. Please sign in again.")
                )

            try:
                login_at_dt = datetime.fromisoformat(login_at)
            except (TypeError, ValueError):
                clear_employee_session()
                return redirect(
                    url_for("login", msg="Session expired after 6 days. Please sign in again.")
                )

            if datetime.utcnow() - login_at_dt > timedelta(days=6):
                clear_employee_session()
                return redirect(
                    url_for("login", msg="Session expired after 6 days. Please sign in again.")
                )

            conn = get_db_connection()
            employee = get_employee_profile(conn, employee_id)
            conn.close()

            if (
                not employee
                or not employee["user_id"]
                or (employee["status"] or "").strip().lower() == "inactive"
            ):
                clear_employee_session()
                return redirect(
                    url_for("login", msg="Your employee access is inactive. Please contact HR.")
                )

            return view_func(*args, **kwargs)

        return wrapped_view

    return decorator



def enable_employee_portal_access(conn, employee_id, portal_password):
    password = (portal_password or "").strip()
    if not password:
        return False

    employee = conn.execute(
        """
        SELECT emp_code, email
        FROM employees
        WHERE id = ?
        """,
        (employee_id,),
    ).fetchone()
    if not employee:
        raise ValueError("Employee not found.")

    username = build_portal_login_id(employee)
    existing_user = conn.execute(
        """
        SELECT id
        FROM users
        WHERE employee_id = ?
        """,
        (employee_id,),
    ).fetchone()
    password_hash = hash_password(password)

    if existing_user:
        conn.execute(
            """
            UPDATE users
            SET
                username = ?,
                password_hash = ?,
                must_change_password = 0
            WHERE employee_id = ?
            """,
            (username, password_hash, employee_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO users (employee_id, username, password_hash, must_change_password)
            VALUES (?, ?, ?, 0)
            """,
            (employee_id, username, password_hash),
        )

    return True
