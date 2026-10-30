import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_dotenv_path():
    return os.path.join(BASE_DIR, ".env")