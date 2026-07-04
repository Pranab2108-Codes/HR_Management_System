import sqlite3
from config import DB_PATH

def remove_unwanted_departments():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    departments_to_remove = ["Digital Points", "Finance"]
    
    for dept in departments_to_remove:
        # Find the department ID (case-insensitive)
        cursor.execute("SELECT id FROM departments WHERE dept_name LIKE ?", (dept,))
        row = cursor.fetchone()
        
        if row:
            dept_id = row[0]
            # Unassign this department from any employees to prevent errors
            cursor.execute("UPDATE employees SET department_id = NULL WHERE department_id = ?", (dept_id,))
            # Delete the department
            cursor.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
            print(f"Successfully deleted '{dept}' from the database.")
            
    conn.commit()
    conn.close()

if __name__ == "__main__":
    remove_unwanted_departments()