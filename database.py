import sqlite3

from config import DB_PATH, DEFAULT_SYSTEM_SETTINGS
from modules.db.schema import (
    ensure_admin_users_table,
    ensure_employee_profile_columns,
    ensure_users_table,
)



def create_database(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA journal_mode=WAL;")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dept_name TEXT UNIQUE
        );
        """
    )

    default_departments = ["Sales", "Digital Marketing", "Engineering", "Creative"]
    for dept in default_departments:
        cursor.execute("INSERT OR IGNORE INTO departments (dept_name) VALUES (?);", (dept,))

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_name TEXT UNIQUE
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_code TEXT UNIQUE,
            full_name TEXT,
            department_id INTEGER,
            role_id INTEGER,
            phone TEXT,
            email TEXT,
            join_date TEXT,
            status TEXT,
            FOREIGN KEY(department_id) REFERENCES departments(id),
            FOREIGN KEY(role_id) REFERENCES roles(id)
        );
        """
    )

    ensure_employee_profile_columns(conn)
    ensure_users_table(conn)

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            date TEXT,
            check_in TEXT,
            check_out TEXT,
            work_hours REAL,
            break_hours REAL,
            status TEXT,
            late_flag INTEGER,
            auto_marked INTEGER,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            from_date TEXT,
            to_date TEXT,
            days INTEGER,
            leave_type TEXT,
            reason TEXT,
            status TEXT,
            applied_on TEXT,
            approved_by INTEGER,
            approved_on TEXT,
            applicant_name TEXT,
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(approved_by) REFERENCES employees(id)
        );
        """
    )

    leave_request_columns = [
        row[1] for row in cursor.execute("PRAGMA table_info(leave_requests);").fetchall()
    ]
    if "applicant_name" not in leave_request_columns:
        cursor.execute("ALTER TABLE leave_requests ADD COLUMN applicant_name TEXT;")

    cursor.execute(
        """
        UPDATE leave_requests
        SET applicant_name = (
            SELECT full_name
            FROM employees
            WHERE employees.id = leave_requests.employee_id
        )
        WHERE applicant_name IS NULL OR TRIM(applicant_name) = '';
        """
    )

    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_leave_requests_fill_applicant_name
        AFTER INSERT ON leave_requests
        FOR EACH ROW
        BEGIN
            UPDATE leave_requests
            SET applicant_name = (
                SELECT full_name
                FROM employees
                WHERE employees.id = NEW.employee_id
            )
            WHERE id = NEW.id;
        END;
        """
    )

    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_leave_requests_sync_on_employee_change
        AFTER UPDATE OF employee_id ON leave_requests
        FOR EACH ROW
        BEGIN
            UPDATE leave_requests
            SET applicant_name = (
                SELECT full_name
                FROM employees
                WHERE employees.id = NEW.employee_id
            )
            WHERE id = NEW.id;
        END;
        """
    )

    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_leave_requests_sync_on_name_change
        AFTER UPDATE OF full_name ON employees
        FOR EACH ROW
        BEGIN
            UPDATE leave_requests
            SET applicant_name = NEW.full_name
            WHERE employee_id = NEW.id;
        END;
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            title TEXT,
            description TEXT,
            assigned_date TEXT,
            deadline TEXT,
            status TEXT,
            updated_on TEXT,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            action TEXT,
            timestamp TEXT,
            ip_address TEXT,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        );
        """
    )

    existing_setting_keys = {
        row[0] for row in cursor.execute("SELECT setting_key FROM system_settings;").fetchall()
    }
    missing_settings = [
        (key, value)
        for key, value in DEFAULT_SYSTEM_SETTINGS.items()
        if key not in existing_setting_keys
    ]
    if missing_settings:
        cursor.executemany(
            "INSERT INTO system_settings(setting_key, setting_value) VALUES(?, ?);",
            missing_settings,
        )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            notice_date TEXT NOT NULL,
            office_closed INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_on TEXT NOT NULL
        );
        """
    )

    ensure_admin_users_table(conn)

    conn.commit()
    conn.close()
    print(f"Database and tables created successfully at {db_path}.")


if __name__ == "__main__":
    create_database()
