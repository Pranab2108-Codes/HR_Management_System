import sqlite3
from config import DB_PATH

def clear_demo_data():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Disable foreign key checks temporarily to allow bulk deletion
    cursor.execute("PRAGMA foreign_keys = OFF;")
    
    # List of tables to wipe clean
    tables_to_clear = [
        "attendance",
        "leave_requests",
        "employee_hourly_notes",
        "activity_logs",
        "tasks",
        "users",
        "employees"
    ]
    
    for table in tables_to_clear:
        try:
            # Delete all rows from the table
            cursor.execute(f"DELETE FROM {table};")
            # Reset the auto-incrementing ID counter back to 1
            cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{table}';")
            print(f"Cleared table: {table}")
        except sqlite3.OperationalError:
            pass # Table might not exist, which is fine
            
    # Re-enable foreign key checks
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    conn.commit()
    conn.close()
    print("\nSuccess: All demo employee data has been wiped!")

if __name__ == "__main__":
    clear_demo_data()
