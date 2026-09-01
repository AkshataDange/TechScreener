"""
config.py
App-wide settings loaded from environment variables or .env file.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "TechScreen AI"
    debug: bool = True
    groq_api_key: str
    database_url: str
    admin_email: str = "admin@techscreen.com"
    admin_password: str = "admin123"
    admin_name: str = "TechScreen Admin"

    # WhisperLive settings
    whisperlive_ws_url: str = "ws://127.0.0.1:8080"
    whisperlive_model: str = "base"
    whisperlive_language: str = "en"
    whisperlive_use_vad: bool = False

    # SMTP / email (used to send candidate credentials)
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_from_email: str = ""
    interview_login_url: str = "http://localhost:8000/user"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
