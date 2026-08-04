import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1HSahM1YzFh6J2xJ1AZAR-apsmJ41_hO2xzUR_W-BMug"
SHEET_NAME = "Графік"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly"
]

credentials = Credentials.from_service_account_file(
    "credentials.json",
    scopes=SCOPES
)

client = gspread.authorize(credentials)

spreadsheet = client.open_by_key(SPREADSHEET_ID)
worksheet = spreadsheet.worksheet(SHEET_NAME)

print("ПІДКЛЮЧЕННЯ УСПІШНЕ")
print("Назва таблиці:", spreadsheet.title)
print("Назва аркуша:", worksheet.title)
print("Значення D9:", worksheet.acell("D9").value)