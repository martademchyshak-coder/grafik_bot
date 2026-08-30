import asyncio
import os
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from google_sheets import (
    get_day_free_places,
    get_shift_availability,
    get_shift_group,
    get_manager_access_record,
    is_shift_available,
    load_schedule_for_manager,
    save_one_day_for_manager,
    save_schedule_for_manager,
)

from reminders import (
    ADMIN_TELEGRAM_ID,
    build_admin_report,
    get_admin_report_keyboard,
    reminders_loop,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN у файлі .env")

bot = Bot(token=TOKEN)
dp = Dispatcher()
user_schedules: Dict[int, Dict[str, str]] = {}
sheet_write_lock = asyncio.Lock()
user_save_locks = {}

KYIV_TZ = ZoneInfo("Europe/Kyiv")
PRIORITY_OPEN_TIME = dt_time(12, 0)
GENERAL_OPEN_TIME = dt_time(13, 0)
CLOSE_TIME = dt_time(20, 0)

DAYS = {
    "mon": "Понеділок",
    "tue": "Вівторок",
    "wed": "Середа",
    "thu": "Четвер",
    "fri": "П’ятниця",
    "sat": "Субота",
    "sun": "Неділя",
}

DAY_SHORT = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Нд",
}

SHIFTS = {
    "1": "09:00–17:00",
    "1.1": "08:00–16:00",
    "2": "13:00–21:00",
    "3": "10:00–18:00",
    "4": "Вихідний 1",
    "5": "Вихідний 2",
    "6": "09:00–13:00",
    "6.1": "09:00–13:00 • 17:00–21:00",
    "7": "Відпустка",
}

SHIFT_EMOJI = {
    "1": "🌅",
    "1.1": "🌅",
    "2": "🌃",
    "3": "🌅",
    "4": "🌴",
    "5": "🌴",
    "6": "🌇",
    "6.1": "🌇",
    "7": "🏖️",
}

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="▶️ Start"),
            KeyboardButton(text="📝 Заповнити графік"),
        ],
        [
            KeyboardButton(text="📅 Мій графік"),
            KeyboardButton(text="📖 Інструкція"),
        ],
    ],
    resize_keyboard=True,
)


def get_manager_name(user) -> str:
    manager_name = " ".join(
        part
        for part in [user.last_name, user.first_name]
        if part
    ).strip()

    return manager_name or user.full_name.strip()



def get_access_status(user) -> tuple[bool, str]:
    """Перевіряє активність менеджера, день тижня і поточний час Києва."""

    manager_name = get_manager_name(user)
    record = get_manager_access_record(user.id, manager_name)

    # Менеджера не знайдено в таблиці
    if record is None:
        return (
            False,
            "❌ Вас не знайдено у вкладці «Менеджери».\n\n"
            "Перевірте, чи правильно записане ваше ім’я й Telegram ID "
            "у таблиці, або зверніться до керівника.",
        )

    # Менеджер вимкнений у колонці D
    if not record["active"]:
        return (
            False,
            "⛔ Для вас заповнення графіка зараз вимкнене.\n\n"
            "У вкладці «Менеджери» не встановлена галочка «Активний».",
        )

    now = datetime.now(KYIV_TZ)
    current_time = now.time().replace(tzinfo=None)

    # Галочка в колонці E відкриває графік у будь-який день тижня
    if record.get("early_access", False):
        return True, ""

    # Для всіх без дострокового доступу графік відкривається лише в четвер
    if now.weekday() != 3:
        return (
            False,
            "🔒 Графік зараз закритий.\n\n"
            "Проставляння графіка доступне лише щочетверга.",
        )

    # У четвер після 20:00 графік закритий
    if current_time >= CLOSE_TIME:
        return (
            False,
            "🔒 Графік уже закритий.\n\n"
            "Щочетверга доступ закривається о 20:00.",
        )

    # До 12:00 без дострокового доступу графік закритий
    if current_time < PRIORITY_OPEN_TIME:
        return (
            False,
            "🔒 Графік ще закритий.\n\n"
            "У четвер доступ для пріоритетних менеджерів "
            "відкривається о 12:00.",
        )

    # З 12:00 до 13:00 доступ лише для пріоритетних
    if PRIORITY_OPEN_TIME <= current_time < GENERAL_OPEN_TIME:
        if record.get("priority", False):
            return True, ""

        return (
            False,
            "🔒 Зараз графік відкритий лише для пріоритетних менеджерів.\n\n"
            "Для всіх інших доступ відкриється о 13:00.",
        )

    # З 13:00 до 20:00 доступ для всіх активних менеджерів
    if GENERAL_OPEN_TIME <= current_time < CLOSE_TIME:
        return True, ""

    return (
        False,
        "🔒 Графік зараз закритий.",
    )

async def ensure_schedule_access(user, answer_target) -> bool:
    try:
        allowed, message = await asyncio.to_thread(get_access_status, user)
    except Exception as error:
        import traceback
        traceback.print_exc()
        await answer_target.answer(
            "❌ Не вдалося перевірити доступ до графіка.\n\n"
            f"Помилка: {error}"
        )
        return False

    if allowed:
        return True

    await answer_target.answer(message)
    return False


def progress_bar(schedule: dict) -> str:
    completed = len(schedule)
    return "🟩" * completed + "⬜" * (7 - completed)


def build_fill_text(user_id: int, notice: str | None = None) -> str:
    schedule = user_schedules.get(user_id, {})
    lines = [
        "╔══════════════════════╗",
        "      📆 ГРАФІК НА ТИЖДЕНЬ",
        "╚══════════════════════╝",
        "",
        f"{progress_bar(schedule)}  {len(schedule)}/7",
        "",
    ]

    if notice:
        lines.extend([notice, ""])

    lines.append("Оберіть день для заповнення:")
    return "\n".join(lines)


def get_week_keyboard(user_id: int) -> InlineKeyboardMarkup:
    schedule = user_schedules.get(user_id, {})
    buttons = []
    day_items = list(DAYS.items())

    for index in range(0, len(day_items), 2):
        row = []

        for day_code, _ in day_items[index:index + 2]:
            shift_code = schedule.get(day_code)

            if shift_code:
                    if str(shift_code).startswith("manual:"):
                        manual_text = str(shift_code).split(":", 1)[1]
                        text = f"📝 {DAY_SHORT[day_code]} {manual_text}"
                    else:
                        text = f"✅ {DAY_SHORT[day_code]} {SHIFTS[shift_code]}"
            else:
                text = f"▫️ {DAY_SHORT[day_code]} - не вибрано"
                        

            row.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"day:{day_code}",
                )
            )

        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(
                text="✅ Завершити заповнення",
                callback_data="save_schedule"
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_edit_days_keyboard(user_id: int) -> InlineKeyboardMarkup:
    schedule = user_schedules.get(user_id, {})
    buttons = []
    day_items = list(DAYS.items())

    for index in range(0, len(day_items), 2):
        row = []

        for day_code, _ in day_items[index:index + 2]:
            shift_code = schedule.get(day_code)
            text = f"✏️ {DAY_SHORT[day_code]}"

            if shift_code:
                text += f" {SHIFTS[shift_code]}"

            row.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"edit_day:{day_code}",
                )
            )

        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад до графіка",
                callback_data="back_to_saved_schedule",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def get_shifts_keyboard(
    day_code: str,
    edit_mode: bool = False,
) -> InlineKeyboardMarkup:
    availability = await asyncio.to_thread(
    get_shift_availability,
    day_code,
)
    free_places = await asyncio.to_thread(
        get_day_free_places,
        day_code,
    )

    buttons = []

    for shift_code, shift_text in SHIFTS.items():
        is_available = availability.get(shift_code, False)

        if shift_code in ("1", "1.1", "3", "6", "6.1"):
            remaining = free_places["first"]
        elif shift_code == "2":
            remaining = free_places["second"]
        else:
            remaining = free_places["days_off"]

        if (
            day_code in ("sat", "sun")
            and shift_code in ("1", "1.1", "2", "3", "6", "6.1")
        ):
            remaining_text = "без обмежень"
        else:
            remaining_text = f"місць: {int(remaining)}"

        prefix = "🟢" if is_available else "🔴"
        text = f"{prefix} {shift_code} — {shift_text} · {remaining_text}"

        if is_available:
            action = "edit_shift" if edit_mode else "shift"
            callback_data = f"{action}:{day_code}:{shift_code}"
        else:
            callback_data = f"full:{shift_code}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=callback_data,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Оновити місця",
                callback_data=f"refresh_shifts:{day_code}:{int(edit_mode)}",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=(
                    "⬅️ До вибору дня"
                    if edit_mode
                    else "⬅️ До днів тижня"
                ),
                callback_data=(
                    "back_to_edit_days"
                    if edit_mode
                    else "back_to_week"
                ),
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_schedule_card(user_id: int) -> str:
    schedule = user_schedules.get(user_id, {})
    lines = [
        "🎉 Готово!",
        "",
        "╔══════════════════════╗",
        "        📆 ВАШ ГРАФІК",
        "╚══════════════════════╝",
        "",
    ]

    for day_code in DAYS:
        shift_code = schedule.get(day_code)

        if shift_code:
            if str(shift_code).startswith("manual:"):
                emoji = "📝"
                shift_text = str(shift_code).split(":", 1)[1]
            else:
                emoji = SHIFT_EMOJI.get(shift_code, "📌")
                shift_text = SHIFTS.get(shift_code, str(shift_code))
        else:
            emoji = "▫️"
            shift_text = "Не вибрано"

        lines.append(f"{emoji} {DAY_SHORT[day_code]}   {shift_text}")

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "✅ Графік успішно збережено!",
            "",
            "❤️ Дякуємо та бажаємо гарного робочого тижня!",
        ]
    )

    return "\n".join(lines)


def get_saved_schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Змінити один день",
                    callback_data="edit_schedule",
                )
            ]
        ]
    )


async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return

        await message.answer(text, reply_markup=reply_markup)
@dp.message(F.text == "▶️ Start")
async def start_button(message: Message):
    await start(message)

@dp.message(CommandStart())
async def start(message: Message):
    # Під час /start бот намагається знайти менеджера у вкладці
    # «Менеджери» та автоматично записати Telegram ID, якщо колонка B порожня.
    try:
        await asyncio.to_thread(
            get_manager_access_record,
            message.from_user.id,
            get_manager_name(message.from_user),
        )
    except Exception:
        # /start має працювати навіть якщо Google Sheets тимчасово недоступна.
        pass

    await message.answer(
        "👋 Вітаю!\n\n"
        "Це бот для заповнення графіка менеджерів.",
        reply_markup=main_menu,
    )


@dp.message(F.text == "📝 Заповнити графік")
async def fill_schedule(message: Message):
    if not await ensure_schedule_access(message.from_user, message):
        return

    user_id = message.from_user.id
    manager_name = get_manager_name(message.from_user)

    try:
        schedule = await asyncio.to_thread(
        load_schedule_for_manager,
        manager_name,
        )
        print(
            f"FILL_SCHEDULE: manager={manager_name}, "
            f"schedule={schedule}",
            flush=True,
        )
    except Exception as error:
        print(
            f"Помилка читання графіка: {error}",
            flush=True,
    )
        schedule = {}

    user_schedules[user_id] = schedule
    print(
        f"AFTER_SAVE: user_id={user_id}, "
    f"user_schedule={user_schedules.get(user_id)}",
            flush=True,
        )
    await message.answer(
        build_fill_text(user_id),
        reply_markup=get_week_keyboard(user_id),
    )
 

@dp.message(F.text == "📅 Мій графік")
async def my_schedule(message: Message):
    user_id = message.from_user.id
    manager_name = get_manager_name(message.from_user)

    try:
        schedule = await asyncio.to_thread(
            load_schedule_for_manager,
            manager_name,
        )
    except Exception as error:
        print(
            "Помилка читання графіка: "
            f"user_id={user_id}, "
            f"manager={manager_name}, "
            f"error={error}",
            flush=True,
        )

        await message.answer(
            "❌ Не вдалося прочитати графік із таблиці.\n\n"
            f"Помилка: {error}"
        )
        return

    if not schedule:
        user_schedules.pop(user_id, None)

        await message.answer(
            "⚠️ У таблиці ще немає вашого графіка.\n\n"
            "Натисніть «📝 Заповнити графік»."
        )
        return

    user_schedules[user_id] = schedule

    await message.answer(
        get_schedule_card(user_id),
        reply_markup=get_saved_schedule_keyboard(),
    )
@dp.message(F.text == "📖 Інструкція")
async def instruction(message: Message):
    await message.answer(
        "📖 <b>Інструкція</b>\n\n"
        "1️⃣ Натисніть <b>📝 Заповнити графік</b>.\n"
        "2️⃣ Оберіть зміну для кожного дня.\n"
        "3️⃣ Після заповнення всіх 7 днів натисніть <b>💾 Зберегти графік</b>.\n"
        "4️⃣ Перевірити свій графік можна через кнопку <b>📅 Мій графік</b>.\n\n"
        "Якщо після оновлення бота щось працює некоректно — натисніть <b>▶️ Start</b> для оновлення меню.",
        parse_mode="HTML",
    )
@dp.callback_query(F.data.startswith("refresh_shifts:"))
async def refresh_shifts(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    _, day_code, edit_flag = callback.data.split(":")
    edit_mode = edit_flag == "1"

    await safe_edit(
        callback.message,
        f"🗓 {DAYS[day_code]}\n\nМісця оновлено. Оберіть зміну:",
        reply_markup=await get_shifts_keyboard(
            day_code,
            edit_mode=edit_mode,
        ),
    )

    await callback.answer("Місця оновлено 🔄")
    
@dp.callback_query(F.data == "back_to_week")
async def back_to_week(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    user_id = callback.from_user.id

    await safe_edit(
        callback.message,
        build_fill_text(user_id),
        reply_markup=get_week_keyboard(user_id),
    )

    await callback.answer()

@dp.callback_query(F.data == "admin_refresh_incomplete")
async def admin_refresh_incomplete(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer(
            "⛔️ Ця кнопка доступна лише адміністратору.",
            show_alert=True,
        )
        return

    await callback.answer("🔄 Оновлюю список...")

    report_text = await build_admin_report(
        show_all=False,
    )

    await safe_edit(
        callback.message,
        report_text,
        reply_markup=get_admin_report_keyboard(),
    )


@dp.callback_query(F.data == "admin_show_all")
async def admin_show_all(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer(
            "⛔️ Ця кнопка доступна лише адміністратору.",
            show_alert=True,
        )
        return

    await callback.answer("📋 Формую список...")

    report_text = await build_admin_report(
        show_all=True,
    )

    await safe_edit(
        callback.message,
        report_text,
        reply_markup=get_admin_report_keyboard(),
    )

@dp.callback_query(F.data.startswith("day:"))
async def choose_day(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    await callback.answer()
    day_code = callback.data.split(":")[1]

    await safe_edit(
        callback.message,
        f"📅 {DAYS[day_code]}\n\n"
        "Оберіть зміну:\n\n"
        "🟢 — місця є\n"
        "🔴 — місць немає",
        reply_markup=await get_shifts_keyboard(day_code),
    )


@dp.callback_query(F.data.startswith("shift:"))
async def choose_shift(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    _, day_code, shift_code = callback.data.split(":")
    user_id = callback.from_user.id

    available = await asyncio.to_thread(
        is_shift_available, day_code, shift_code, True
    )
    if not available:
        await callback.answer(
            "❌ Поки ви обирали, місця закінчилися. Оновіть список.",
            show_alert=True,
        )
        await safe_edit(
            callback.message,
            f"📅 {DAYS[day_code]}\n\nОберіть іншу зміну:",
            reply_markup=await get_shifts_keyboard(day_code),
        )
        return

    user_schedules.setdefault(user_id, {})
    user_schedules[user_id][day_code] = shift_code
    manager_name = get_manager_name(callback.from_user)
    
    try:
        await asyncio.to_thread(
            save_one_day_for_manager,
            manager_name,
            day_code,
            shift_code,
        )
    except Exception as error:
        print(f"Помилка збереження одного дня: {error}", flush=True)
        await callback.answer(
            "❌ Не вдалося зберегти зміну. Спробуйте ще раз.",
            show_alert=True,
        )
        return
    await safe_edit(
            callback.message,
            build_fill_text(
                user_id,
                f"✅ {DAYS[day_code]}: {SHIFTS[shift_code]}",
            ),
            reply_markup=get_week_keyboard(user_id),
        )
    
    await callback.answer("Зміну вибрано ✅")


@dp.callback_query(F.data == "save_schedule")
async def save_schedule(callback: CallbackQuery):
    if not await ensure_schedule_access(
        callback.from_user,
        callback.message,
    ):
        await callback.answer()
        return

    user_id = callback.from_user.id
    manager_name = get_manager_name(callback.from_user)

    try:
        schedule = await asyncio.to_thread(
            load_schedule_for_manager,
            manager_name,
        )
    except Exception as error:
        print(
            f"Помилка читання графіка при завершенні: {error}",
            flush=True,
        )
        await callback.answer(
            "❌ Не вдалося перевірити графік. Спробуйте ще раз.",
            show_alert=True,
        )
        return

    user_schedules[user_id] = schedule

    if len(schedule) != 7:
        await safe_edit(
            callback.message,
            build_fill_text(
                user_id,
                "⚠️ Ще не всі 7 днів заповнені.",
            ),
            reply_markup=get_week_keyboard(user_id),
        )
        await callback.answer(
            "⚠️ Заповніть усі 7 днів.",
            show_alert=True,
        )
        return

    await safe_edit(
        callback.message,
        get_schedule_card(user_id),
        reply_markup=get_saved_schedule_keyboard(),
    )

    await callback.answer("✅ Графік успішно завершено")

@dp.callback_query(F.data == "edit_schedule")
async def edit_schedule(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    user_id = callback.from_user.id

    await safe_edit(
        callback.message,
        "╔══════════════════════╗\n"
        "       ✏️ ЗМІНА ГРАФІКА\n"
        "╚══════════════════════╝\n\n"
        "Оберіть один день, який потрібно змінити.",
        reply_markup=get_edit_days_keyboard(user_id),
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("edit_day:"))
async def choose_edit_day(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    await callback.answer()
    day_code = callback.data.split(":")[1]

    await safe_edit(
        callback.message,
        f"✏️ {DAYS[day_code]}\n\n"
        "Оберіть нову зміну.\n\n"
        "Після вибору бот перезапише тільки цей день.",
        reply_markup=await get_shifts_keyboard(
            day_code,
            edit_mode=True,
        ),
    )


@dp.callback_query(F.data.startswith("edit_shift:"))
async def save_edited_day(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    _, day_code, shift_code = callback.data.split(":")
    user_id = callback.from_user.id
    manager_name = get_manager_name(callback.from_user)

    old_shift = user_schedules.get(user_id, {}).get(day_code)
    same_group = (
        old_shift is not None
        and get_shift_group(old_shift) == get_shift_group(shift_code)
    )
    if not same_group:
        available = await asyncio.to_thread(
            is_shift_available, day_code, shift_code, True
        )
        if not available:
            await callback.answer(
                "❌ На цю зміну місць уже немає.",
                show_alert=True,
            )
            return

    await callback.answer("⏳ Зберігаю зміну...")

    await safe_edit(
        callback.message,
        "⏳ Оновлюю вибраний день у таблиці...\n\n"
        "Інші дні не змінюються.",
    )

    try:
        await asyncio.to_thread(
            save_one_day_for_manager,
            manager_name,
            day_code,
            shift_code,
        )
    except Exception as error:
        await callback.message.answer(
            "❌ Не вдалося змінити день у Google Sheets:\n\n"
            f"{error}\n\n"
            f"Ім’я з Telegram: {manager_name}"
        )
        return

    user_schedules.setdefault(user_id, {})
    user_schedules[user_id][day_code] = shift_code

    await safe_edit(
        callback.message,
        "✅ День успішно змінено!\n\n"
        f"{SHIFT_EMOJI[shift_code]} "
        f"{DAYS[day_code]} — {SHIFTS[shift_code]}\n\n"
        "Інші дні залишилися без змін.\n\n"
        f"{get_schedule_card(user_id)}",
        reply_markup=get_saved_schedule_keyboard(),
    )


@dp.callback_query(F.data == "back_to_edit_days")
async def back_to_edit_days(callback: CallbackQuery):
    if not await ensure_schedule_access(callback.from_user, callback.message):
        await callback.answer()
        return

    user_id = callback.from_user.id

    await safe_edit(
        callback.message,
        "✏️ Редагування графіка\n\n"
        "Оберіть день, який потрібно змінити:",
        reply_markup=get_edit_days_keyboard(user_id),
    )

    await callback.answer()


@dp.callback_query(F.data == "back_to_saved_schedule")
async def back_to_saved_schedule(callback: CallbackQuery):
    user_id = callback.from_user.id

    await safe_edit(
        callback.message,
        get_schedule_card(user_id),
        reply_markup=get_saved_schedule_keyboard(),
    )

    await callback.answer()


async def main():
    print("✅ Бот запущений")

    reminders_task = asyncio.create_task(
        reminders_loop(bot)
    )

    try:
        await dp.start_polling(bot)
    finally:
        reminders_task.cancel()

        try:
            await reminders_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
