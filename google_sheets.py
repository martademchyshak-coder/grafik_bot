import os
import json
import re
import threading
import time
from typing import Dict, List, Tuple

import gspread
from google.oauth2.service_account import Credentials


SPREADSHEET_ID = "1HSahM1YzFh6J2xJ1AZAR-apsmJ41_hO2xzUR_W-BMug"
SHEET_NAME = "Прихована копія"
MANAGERS_SHEET_NAME = "Менеджери"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
AVAILABILITY_CACHE_TTL = 5

AVAILABILITY_CELLS = {
    "mon": {"first": "C2", "second": "C3", "days_off": "C4"},
    "tue": {"first": "G2", "second": "G3", "days_off": "G4"},
    "wed": {"first": "J2", "second": "J3", "days_off": "J4"},
    "thu": {"first": "M2", "second": "M3", "days_off": "M4"},
    "fri": {"first": "P2", "second": "P3", "days_off": "P4"},
    "sat": {"first": "S2", "second": "S3", "days_off": "S4"},
    "sun": {"first": "V2", "second": "V3", "days_off": "V4"},
}

DAY_START_ROWS = {
    "mon": 9,
    "tue": 58,
    "wed": 107,
    "thu": 156,
    "fri": 205,
    "sat": 254,
    "sun": 303,
}

DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_BLOCK_SIZE = 49

_client = None
_spreadsheet = None
_worksheet = None
_managers_worksheet = None

_connection_lock = threading.RLock()
_sheet_lock = threading.RLock()

_availability_cache: Dict[str, Tuple[float, dict]] = {}
_manager_row_cache: Dict[Tuple[str, str], int] = {}


def get_worksheet():
    global _client, _spreadsheet, _worksheet

    with _connection_lock:
        if _worksheet is not None:
            return _worksheet
        
        credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

        if not credentials_json:
            raise ValueError(
                "У Railway не знайдено змінну GOOGLE_CREDENTIALS_JSON"
        )

        try:
            credentials_info = json.loads(credentials_json)
        except json.JSONDecodeError as error:
            raise ValueError(
                "Змінна GOOGLE_CREDENTIALS_JSON містить неправильний JSON"
            ) from error

        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=SCOPES,
        )

        _client = gspread.authorize(credentials)
        _spreadsheet = _client.open_by_key(SPREADSHEET_ID)
        _worksheet = _spreadsheet.worksheet(SHEET_NAME)

        return _worksheet


def reset_connection_cache():
    global _client, _spreadsheet, _worksheet, _managers_worksheet

    with _connection_lock:
        _client = None
        _spreadsheet = None
        _worksheet = None
        _managers_worksheet = None


def clear_runtime_cache():
    _availability_cache.clear()
    _manager_row_cache.clear()


def check_connection() -> dict:
    worksheet = get_worksheet()

    return {
        "sheet_name": worksheet.title,
        "manager_d9": worksheet.acell("D9").value,
    }


def value_to_number(value) -> float:
    if value is None:
        return 0

    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return 0


def _read_availability_cells(day_code: str) -> dict:
    worksheet = get_worksheet()
    cells = AVAILABILITY_CELLS[day_code]

    with _sheet_lock:
        values = worksheet.batch_get(
            [
                cells["first"],
                cells["second"],
                cells["days_off"],
            ]
        )

    def extract(index: int):
        try:
            return values[index][0][0]
        except (IndexError, TypeError):
            return None

    return {
        "first": value_to_number(extract(0)),
        "second": value_to_number(extract(1)),
        "days_off": value_to_number(extract(2)),
    }


def get_day_free_places(
    day_code: str,
    force_refresh: bool = False,
) -> dict:
    if day_code not in AVAILABILITY_CELLS:
        raise ValueError(f"Невідомий день: {day_code}")

    now = time.monotonic()
    cached = _availability_cache.get(day_code)

    if (
        not force_refresh
        and cached is not None
        and now - cached[0] < AVAILABILITY_CACHE_TTL
    ):
        return dict(cached[1])

    free_places = _read_availability_cells(day_code)
    _availability_cache[day_code] = (now, free_places)

    return dict(free_places)


def get_shift_availability(
    day_code: str,
    force_refresh: bool = False,
) -> dict:
    free_places = get_day_free_places(
        day_code,
        force_refresh,
    )

    first_available = free_places["first"] > 0
    second_available = free_places["second"] > 0
    days_off_available = free_places["days_off"] > 0

    if day_code in ("sat", "sun"):
        first_available = True
        second_available = True

    return {
        "1": first_available,
        "1.1": first_available,
        "2": second_available,
        "3": first_available,
        "4": days_off_available,
        "5": days_off_available,
        "6": first_available,
        "6.1": first_available,
    }


def get_shift_group(shift_code: str) -> str:
    if shift_code in ("1", "1.1", "3", "6", "6.1"):
        return "first"

    if shift_code == "2":
        return "second"

    if shift_code in ("4", "5"):
        return "days_off"

    raise ValueError(f"Невідомий код зміни: {shift_code}")


def is_shift_available(
    day_code: str,
    shift_code: str,
    force_refresh: bool = True,
) -> bool:
    availability = get_shift_availability(
        day_code,
        force_refresh=force_refresh,
    )

    return bool(availability.get(shift_code, False))


def normalize_text(value: str) -> str:
    value = str(value or "").lower()
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"[^а-яіїєґa-z0-9' ]", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def get_manager_search_words(manager_name: str) -> list:
    normalized_name = normalize_text(manager_name)
    ignored_words = {
        "менеджер",
        "manager",
        "зам",
        "сам",
    }

    words = [
        word
        for word in normalized_name.split()
        if len(word) >= 3
        and word not in ignored_words
        and not word.isdigit()
    ]

    if not words:
        raise ValueError("Не вдалося визначити ім’я менеджера.")

    return words


def find_manager_row_for_day(
    worksheet,
    manager_name: str,
    day_code: str,
) -> int:
    if day_code not in DAY_START_ROWS:
        raise ValueError(f"Невідомий день: {day_code}")

    cache_key = (
        normalize_text(manager_name),
        day_code,
    )

    if cache_key in _manager_row_cache:
        return _manager_row_cache[cache_key]

    manager_words = get_manager_search_words(manager_name)
    start_row = DAY_START_ROWS[day_code]
    end_row = start_row + DAY_BLOCK_SIZE - 1

    with _sheet_lock:
        manager_cells = worksheet.get(
            f"D{start_row}:D{end_row}"
        )

    matches = []

    for index, row_values in enumerate(manager_cells):
        if not row_values:
            continue

        normalized_cell = normalize_text(row_values[0])

        if normalized_cell and all(
            word in normalized_cell
            for word in manager_words
        ):
            matches.append(start_row + index)

    if not matches:
        raise ValueError(
            f"Менеджера «{manager_name}» не знайдено "
            f"у блоці дня {day_code}, "
            f"рядки {start_row}–{end_row}."
        )

    if len(matches) > 1:
        raise ValueError(
            f"Для менеджера «{manager_name}» знайдено "
            f"кілька рядків у дні {day_code}: {matches}"
        )

    _manager_row_cache[cache_key] = matches[0]

    return matches[0]


def shift_to_cells(shift_code: str) -> list:
    cells = [""] * 14

    if shift_code == "1.1":
        for hour in range(8, 17):
            cells[hour - 8] = "1"

    elif shift_code == "1":
        for hour in range(9, 18):
            cells[hour - 8] = "1"

    elif shift_code == "2":
        for hour in range(13, 22):
            cells[hour - 8] = "1"

    elif shift_code == "3":
        for hour in range(10, 19):
            cells[hour - 8] = "1"

    elif shift_code == "4":
        cells[6] = "Вихідний 1"

    elif shift_code == "5":
        cells[6] = "Вихідний 2"

    elif shift_code == "6":
        for hour in range(9, 14):
            cells[hour - 8] = "1"

    elif shift_code == "6.1":
        for hour in range(9, 14):
            cells[hour - 8] = "1"

        for hour in range(17, 22):
            cells[hour - 8] = "1"

    else:
        raise ValueError(f"Невідомий код зміни: {shift_code}")

    return cells


def save_one_day_for_manager(
    manager_name: str,
    day_code: str,
    shift_code: str,
) -> int:
    worksheet = get_worksheet()

    manager_row = find_manager_row_for_day(
        worksheet,
        manager_name,
        day_code,
    )

    with _sheet_lock:
        worksheet.update(
            range_name=f"E{manager_row}:R{manager_row}",
            values=[shift_to_cells(shift_code)],
            value_input_option="USER_ENTERED",
        )

    _availability_cache.pop(day_code, None)

    return manager_row


def save_schedule_for_manager(
    manager_name: str,
    schedule: dict,
) -> dict:
    worksheet = get_worksheet()
    updates: List[dict] = []
    updated_rows = {}

    for day_code in DAY_ORDER:
        if day_code not in schedule:
            raise ValueError(
                f"Не вибрано зміну для дня: {day_code}"
            )

        manager_row = find_manager_row_for_day(
            worksheet,
            manager_name,
            day_code,
        )

        updates.append(
            {
                "range": f"E{manager_row}:R{manager_row}",
                "values": [
                    shift_to_cells(schedule[day_code])
                ],
            }
        )

        updated_rows[day_code] = manager_row

    with _sheet_lock:
        worksheet.batch_update(
            updates,
            value_input_option="USER_ENTERED",
        )

    for day_code in DAY_ORDER:
        _availability_cache.pop(day_code, None)

    return updated_rows
def save_schedule_for_manager(manager_name: str, schedule: dict) -> dict:
    # тут твій наявний код
    # ...

    return updated_rows


def load_schedule_for_manager(manager_name: str) -> dict:
    worksheet = get_worksheet()
    schedule = {}

    for day_code in DAY_ORDER:
        row = find_manager_row_for_day(
            worksheet,
            manager_name,
            day_code,
        )

        with _sheet_lock:
            values = worksheet.get(
                f"E{row}:R{row}"
            )

        row_values = values[0] if values else []
        row_values = row_values + [""] * (14 - len(row_values))

        if "Вихідний 1" in row_values:
            schedule[day_code] = "4"

        elif "Вихідний 2" in row_values:
            schedule[day_code] = "5"

        else:
            work = [
                index
                for index, value in enumerate(row_values)
                if str(value).strip() == "1"
            ]

            if work == list(range(0, 9)):
                schedule[day_code] = "1.1"

            elif work == list(range(1, 10)):
                schedule[day_code] = "1"

            elif work == list(range(5, 14)):
                schedule[day_code] = "2"

            elif work == list(range(2, 11)):
                schedule[day_code] = "3"

            elif work == list(range(1, 6)):
                schedule[day_code] = "6"

            elif work == (
                list(range(1, 6))
                + list(range(9, 14))
            ):
                schedule[day_code] = "6.1"

    return schedule

# =========================
# ВКЛАДКА «МЕНЕДЖЕРИ»
# A — Менеджер
# B — Telegram ID
# C — Пріоритет
# D — Активний
# E — Ранній доступ
# =========================

def get_managers_worksheet():
    global _client
    global _managers_worksheet

    with _connection_lock:
        if _managers_worksheet is not None:
            return _managers_worksheet

        get_worksheet()

        managers_spreadsheet = _client.open_by_key(
            "12G8hE-Y4vQXbpPWYQcmNI70jJeikDvmMWFGM2bUvtik"
        )

        try:
            _managers_worksheet = (
                managers_spreadsheet.worksheet(
                    MANAGERS_SHEET_NAME
                )
            )
        except gspread.WorksheetNotFound:
            _managers_worksheet = (
                managers_spreadsheet.get_worksheet(0)
            )

        return _managers_worksheet


def checkbox_to_bool(value) -> bool:
    normalized = str(value or "").strip().lower()

    return normalized in {
        "true",
        "1",
        "так",
        "yes",
        "y",
        "on",
        "✓",
        "✅",
    }


def normalize_telegram_id(value) -> str:
    text = str(value or "").strip()

    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]

    return text


def _read_managers_rows() -> list:
    worksheet = get_managers_worksheet()

    with _sheet_lock:
        values = worksheet.get("A2:E")

    rows = []

    for sheet_row, row in enumerate(
        values,
        start=2,
    ):
        padded = list(row) + [""] * (5 - len(row))

        (
            manager_name,
            telegram_id,
            priority,
            active,
            early_access,
        ) = padded[:5]

        if not str(manager_name or "").strip():
            continue

        rows.append(
            {
                "row": sheet_row,
                "manager_name": str(
        manager_name or ""
                ).strip(),
                "telegram_id": normalize_telegram_id(
                    telegram_id
                ),
                "priority": checkbox_to_bool(priority),
                "active": checkbox_to_bool(active),
                "early_access": checkbox_to_bool(
                    early_access
                ),
            }
        )

    return rows


def get_manager_access_record(
    telegram_id: int | str,
    manager_name: str = "",
) -> dict | None:
    telegram_id_text = normalize_telegram_id(
        telegram_id
    )

    rows = _read_managers_rows()

    for row in rows:
        if row["telegram_id"] == telegram_id_text:
            return row

    if manager_name:
        search_words = get_manager_search_words(
            manager_name
        )

        matches = []

        for row in rows:
            normalized_cell = normalize_text(
                row["manager_name"]
            )

            if normalized_cell and all(
                word in normalized_cell
                for word in search_words
            ):
                matches.append(row)

        if len(matches) == 1:
            match = matches[0]

            if not match["telegram_id"]:
                worksheet = get_managers_worksheet()

                with _sheet_lock:
                    worksheet.update(
                        range_name=f"B{match['row']}",
                        values=[[telegram_id_text]],
                        value_input_option="USER_ENTERED",
                    )

                match["telegram_id"] = telegram_id_text

            return match

    return None
# Railway fixed version
