import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    CLINICORP_SUBSCRIBER_ID = os.getenv("CLINICORP_SUBSCRIBER_ID")
    CLINICORP_BUSINESS_ID = os.getenv("CLINICORP_BUSINESS_ID")
    CLINICORP_TOKEN = os.getenv("CLINICORP_TOKEN")
    CLINICORP_BASE_URL = os.getenv("CLINICORP_BASE_URL")