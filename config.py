import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

# Railway persistent volume path
PERSISTENT_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data"))
# PERSISTENT_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", str(BASE_DIR))) 

# Database path
DB_PATH = str(PERSISTENT_DIR / os.environ.get("HRMS_DB_NAME", "database.db"))

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "hr-management-dev-secret")

DEFAULT_SYSTEM_SETTINGS = {
    "workday_start_time": "09:30",
    "logout_time": "19:00",
    "late_mark_threshold": "09:40",
    "maximum_work_hours": "9.0",
    "casual_leave_days": "12",
    "sick_leave_days": "10",
    "office_latitude": "0.0",
    "office_longitude": "0.0",
    "geofence_radius_meters": "500",
}

DEFAULT_ADMIN_NAME = os.environ.get("DEFAULT_ADMIN_NAME", "System Administrator")
DEFAULT_ADMIN_EMAIL = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@company.com")
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@123")

PROFILE_IMAGE_UPLOAD_SUBDIR = os.path.join("uploads", "profile_pics")

# Profile images also stored in persistent volume
PROFILE_IMAGE_UPLOAD_DIR = str(PERSISTENT_DIR / "static" / "uploads" / "profile_pics")

ALLOWED_PROFILE_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_PROFILE_IMAGE_SIZE = 5 * 1024 * 1024
