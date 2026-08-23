import os
import json
import re
import threading
import time
from typing import Dict, List, Tuple

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials


SPREADSHEET_ID = "1HSahM1YzFh6J2xJ1AZAR-apsmJ41_hO2xzUR_W-BMug"
SHEET_NAME = "Прихована копія"
MANAGERS_SHEET_NAME = "Менеджери"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
AVAILABILITY_CACHE_TTL = 30
MANAGERS_CACHE_TTL = 60

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
    "tue": 65,
    "wed": 121,
    "thu": 177,
    "fri": 233,
    "sat": 289,
    "sun": 345,
}

DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_BLOCK_SIZE = 56

_client = None
_spreadsheet = None
_worksheet = None
_managers_worksheet = None

_connection_lock = threading.RLock()
_sheet_lock = threading.RLock()

_availability_cache: Dict[str, Tuple[float, dict]] = {}
_manager_row_cache: Dict[Tuple[str, str], int] = {}
_manager_day_cells_cache = {}
_managers_rows_cache = None
_managers_rows_cache_time = 0.0

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
    global _managers_rows_cache, _managers_rows_cache_time

    _availability_cache.clear()
    _manager_row_cache.clear()
    _manager_day_cells_cache.clear()

    _managers_rows_cache = None
    _managers_rows_cache_time = 0.0

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

    for attempt in range(4):
        try:
            with _sheet_lock:
                values = worksheet.batch_get(
                    [
                        cells["first"],
                        cells["second"],
                        cells["days_off"],
                    ]
                )
            break

        except APIError as e:
            if getattr(e.response, "status_code", None) != 429:
                raise

            if attempt == 3:
                raise

            time.sleep(2 ** attempt)

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
    force_refresh: bool = False  ,
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

    if day_code in _manager_day_cells_cache:
        manager_cells = _manager_day_cells_cache[day_code]
    else:
        with _sheet_lock:
            manager_cells = worksheet.get(
            f"D{start_row}:D{end_row}"
        )

    _manager_day_cells_cache[day_code] = manager_cells

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

def paint_shift(worksheet, row: int, shift_code: str) -> None:
    purple = {
        "red": 194 / 255,
        "green": 123 / 255,
        "blue": 160 / 255,
    }

    yellow = {
        "red": 1,
        "green": 242 / 255,
        "blue": 204 / 255,
    }

    white = {
        "red": 1,
        "green": 1,
        "blue": 1,
    }

    # Індекси всередині E:R, які треба фарбувати
    color_ranges = {
        "1.1": [(0, 9, yellow)],       # E:M — 08:00–16:00
        "1": [(1, 10, purple)],        # F:N — 09:00–17:00
        "2": [(5, 14, purple)],        # J:R — 13:00–21:00
        "3": [(2, 11, purple)],        # G:O — 10:00–18:00
        "4": [],                       # Вихідний — білий
        "5": [],                       # Вихідний — білий
        "6": [(1, 6, yellow)],         # F:J — 09:00–13:00
        "6.1": [
            (1, 6, yellow),            # F:J — 09:00–13:00
            (9, 14, yellow),           # N:R — 17:00–21:00
        ],
    }

    sheet_id = worksheet.id
    row_index = row - 1

    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": 4,
                    "endColumnIndex": 18,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": white,
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        }
    ]

    for start_index, end_index, color in color_ranges.get(shift_code, []):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 4 + start_index,
                        "endColumnIndex": 4 + end_index,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )

    worksheet.spreadsheet.batch_update(
        {"requests": requests}
    )

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
        paint_shift(worksheet, manager_row, shift_code)

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
            paint_shift(
                worksheet,
                updated_rows[day_code],
                schedule[day_code],
            )

    for day_code in DAY_ORDER:
        _availability_cache.pop(day_code, None)

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

        elif "Відпустка" in row_values:
            schedule[day_code] = "7"

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
    global _managers_rows_cache, _managers_rows_cache_time

    now = time.monotonic()

    if (
        _managers_rows_cache is not None
        and now - _managers_rows_cache_time < MANAGERS_CACHE_TTL
    ):
        return [
            dict(row)
            for row in _managers_rows_cache
        ]

    worksheet = get_managers_worksheet()

    for attempt in range(4):
        try:
            with _sheet_lock:
                values = worksheet.get("A2:E")
            break

        except APIError as e:
            if getattr(e.response, "status_code", None) != 429:
                raise

            if attempt == 3:
                raise

            time.sleep(2 ** attempt)

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

    _managers_rows_cache = rows
    _managers_rows_cache_time = now

    return [
        dict(row)
        for row in rows
    ]

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

def get_managers_for_reminder(
    only_incomplete: bool = False,
) -> list:
    """
    Повертає активних менеджерів із Telegram ID.

    only_incomplete=False:
        усі активні менеджери — для нагадування о 09:00.

    only_incomplete=True:
        тільки менеджери, які ще не заповнили всі 7 днів.
    """

    manager_records = [
        row
        for row in _read_managers_rows()
        if row.get("active")
        and row.get("telegram_id")
    ]

    if not only_incomplete:
        return manager_records

    worksheet = get_worksheet()

    first_schedule_row = min(DAY_START_ROWS.values())
    last_schedule_row = max(
        start_row + DAY_BLOCK_SIZE - 1
        for start_row in DAY_START_ROWS.values()
    )

    # Одним запитом читаємо весь графік:
    # колонка D — імена, колонки E:R — години.
    with _sheet_lock:
        schedule_values = worksheet.get(
            f"D{first_schedule_row}:R{last_schedule_row}"
        )

    def detect_shift(row_values: list) -> str | None:
        cells = list(row_values[:14])
        cells += [""] * (14 - len(cells))

        normalized_cells = [
            str(value or "").strip()
            for value in cells
        ]

        if "Вихідний 1" in normalized_cells:
            return "4"

        if "Вихідний 2" in normalized_cells:
            return "5"

        work_indexes = [
            index
            for index, value in enumerate(normalized_cells)
            if value == "1"
        ]

        if work_indexes == list(range(0, 9)):
            return "1.1"

        if work_indexes == list(range(1, 10)):
            return "1"

        if work_indexes == list(range(5, 14)):
            return "2"

        if work_indexes == list(range(2, 11)):
            return "3"

        if work_indexes == list(range(1, 6)):
            return "6"

        if work_indexes == (
            list(range(1, 6))
            + list(range(9, 14))
        ):
            return "6.1"

        return None

    incomplete_managers = []

    for manager in manager_records:
        manager_name = manager["manager_name"]
        search_words = get_manager_search_words(
            manager_name
        )

        completed_days = 0

        for day_code in DAY_ORDER:
            start_row = DAY_START_ROWS[day_code]

            block_start = (
                start_row - first_schedule_row
            )
            block_end = (
                block_start + DAY_BLOCK_SIZE
            )

            day_block = schedule_values[
                block_start:block_end
            ]

            matches = []

            for row_values in day_block:
                if not row_values:
                    continue

                normalized_name = normalize_text(
                    row_values[0]
                )

                if normalized_name and all(
                    word in normalized_name
                    for word in search_words
                ):
                    matches.append(row_values)

            # Має бути знайдений рівно один рядок менеджера
            if len(matches) != 1:
                continue

            # Перша клітинка — ім’я з колонки D.
            # Далі йдуть 14 клітинок E:R.
            shift_code = detect_shift(
                matches[0][1:15]
            )

            if shift_code:
                completed_days += 1

        if completed_days < 7:
            incomplete_managers.append(manager)

    return incomplete_managers
