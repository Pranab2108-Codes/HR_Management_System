from app import app
from database import create_database

create_database()

if __name__ == "__main__":
    app.run()