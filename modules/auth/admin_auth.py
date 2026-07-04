from functools import wraps

from flask import redirect, session, url_for

from modules.db.schema import ensure_admin_users_table

ADMIN_SESSION_KEYS = (
    "admin_user_id",
    "admin_name",
    "admin_email",
)


def clear_admin_session():
    for key in ADMIN_SESSION_KEYS:
        session.pop(key, None)


def start_admin_session(admin_row):
    clear_admin_session()
    session["admin_user_id"] = admin_row["id"]
    session["admin_name"] = admin_row["full_name"]
    session["admin_email"] = admin_row["email"]


def get_admin_auth_record(conn, email):
    identifier = (email or "").strip()
    if not identifier:
        return None

    ensure_admin_users_table(conn)
    return conn.execute(
        """
        SELECT
            id,
            full_name,
            email,
            password_hash,
            is_active,
            last_login
        FROM admin_users
        WHERE LOWER(email) = LOWER(?)
        LIMIT 1
        """,
        (identifier,),
    ).fetchone()


def get_admin_profile(conn, admin_user_id):
    if not admin_user_id:
        return None

    ensure_admin_users_table(conn)
    return conn.execute(
        """
        SELECT
            id,
            full_name,
            email,
            is_active,
            last_login
        FROM admin_users
        WHERE id = ?
        """,
        (admin_user_id,),
    ).fetchone()


def admin_login_required(get_db_connection):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(*args, **kwargs):
            conn = get_db_connection()
            admin = get_admin_profile(conn, session.get("admin_user_id"))
            conn.close()

            if not admin or not admin["is_active"]:
                clear_admin_session()
                return redirect(
                    url_for("admin_login", msg="Please sign in to access the admin panel.")
                )

            return view_func(*args, **kwargs)

        return wrapped_view

    return decorator

