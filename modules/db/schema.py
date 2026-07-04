import os
from werkzeug.security import generate_password_hash

EMPLOYEE_PROFILE_COLUMNS = {
    "profile_image": "TEXT",
    "address": "TEXT",
    "date_of_birth": "TEXT",
    "emergency_contact": "TEXT",
    "blood_group": "TEXT",
    "alternate_phone": "TEXT",
}


def ensure_users_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER UNIQUE,
            username TEXT UNIQUE,
            password_hash TEXT,
            must_change_password INTEGER DEFAULT 1,
            device_token TEXT,
            last_login TEXT,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """
    )


def ensure_admin_users_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_login TEXT
        )
        """
    )

    DEFAULT_ADMINS = [
        {
            "full_name": os.environ.get("ADMIN1_NAME", "Omkar Mahanandia").strip(),
            "email": os.environ.get("ADMIN1_EMAIL", "omkaroditech@gmail.com").strip(),
            "password": os.environ.get("ADMIN1_PASSWORD", "omkar@oditech").strip(),
        },
        {
            "full_name": os.environ.get("ADMIN2_NAME", "Prabhu Devendra Rao").strip(),
            "email": os.environ.get("ADMIN2_EMAIL", "cma.ceo@gmail.com").strip(),
            "password": os.environ.get("ADMIN2_PASSWORD", "CEO_CMA@oditech").strip(),
        },
        {
            "full_name": os.environ.get("ADMIN3_NAME", "Human Resource Executive").strip(),
            "email": os.environ.get("ADMIN3_EMAIL", "oditech.HR@gmail.com").strip(),
            "password": os.environ.get("ADMIN3_PASSWORD", "HR_oditech@48").strip(),
        },
    ]

    for admin in DEFAULT_ADMINS:
        if not admin["email"]:
            continue

        conn.execute(
            """
            INSERT INTO admin_users (full_name, email, password_hash, is_active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(email) DO UPDATE SET
                full_name = excluded.full_name,
                password_hash = excluded.password_hash,
                is_active = 1
            """,
            (
                admin["full_name"],
                admin["email"],
                generate_password_hash(admin["password"]),
            ),
        )

    conn.commit()


def ensure_employee_profile_columns(conn):
    employee_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(employees)").fetchall()
    }

    missing_columns = [
        (column_name, column_type)
        for column_name, column_type in EMPLOYEE_PROFILE_COLUMNS.items()
        if column_name not in employee_columns
    ]

    for column_name, column_type in missing_columns:
        conn.execute(f"ALTER TABLE employees ADD COLUMN {column_name} {column_type}")

    if missing_columns:
        conn.commit()


def ensure_employee_hourly_notes_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_hourly_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            slot_label TEXT NOT NULL,
            time_range TEXT NOT NULL,
            note_text TEXT,
            status TEXT,
            updated_on TEXT NOT NULL,
            UNIQUE(employee_id, entry_date, slot_key),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """
    )

    existing_columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(employee_hourly_notes)"
        ).fetchall()
    }

    if "status" not in existing_columns:
        conn.execute("ALTER TABLE employee_hourly_notes ADD COLUMN status TEXT")
        conn.commit()