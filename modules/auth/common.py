from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_HASH_PREFIXES = ("pbkdf2:", "scrypt:")


def password_value_is_hashed(value):
    stored_value = (value or "").strip()
    return any(stored_value.startswith(prefix) for prefix in PASSWORD_HASH_PREFIXES)


def hash_password(password):
    return generate_password_hash((password or "").strip())


def verify_stored_password(stored_password, submitted_password):
    stored_value = (stored_password or "").strip()
    submitted_value = (submitted_password or "").strip()
    if not stored_value or not submitted_value:
        return False, False

    if password_value_is_hashed(stored_value):
        try:
            return check_password_hash(stored_value, submitted_value), False
        except ValueError:
            return False, False

    return stored_value == submitted_value, True
