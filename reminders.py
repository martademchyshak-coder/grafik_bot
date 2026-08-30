import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from google_sheets import get_managers_for_reminder


KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Telegram ID Демчишак Марти
ADMIN_TELEGRAM_ID = 7918173155


REMINDER_MESSAGES = {
    "09:00": (
        "👋 Привіт!\n\n"
        "📅 Не забудь: сьогодні проставляння графіка "
        "на наступний тиждень.\n\n"
        "Заповнити графік потрібно до 20:00."
    ),
    "16:00": (
        "⏰ Нагадування!\n\n"
        "Ти ще не проставив графік на наступний тиждень.\n\n"
        "Будь ласка, заповни його до 20:00."
    ),
    "18:00": (
        "⚠️ Повторне нагадування!\n\n"
        "Твій графік на наступний тиждень ще не заповнений.\n\n"
        "До закриття доступу залишилося 2 години."
    ),
    "19:45": (
        "🚨 Термінове нагадування!\n\n"
        "До закриття графіка залишилося лише 15 хвилин.\n\n"
        "Будь ласка, терміново простав графік до 20:00."
    ),
}


ADMIN_REPORT_TIMES = {
    "16:00",
    "18:00",
    "19:45",
    "20:00",
}


def get_admin_report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Оновити список",
                    callback_data="admin_refresh_incomplete",
                ),
                InlineKeyboardButton(
                    text="📋 Показати всіх",
                    callback_data="admin_show_all",
                ),
            ]
        ]
    )


async def get_schedule_report_data() -> tuple[list, list]:
    all_managers = await asyncio.to_thread(
        get_managers_for_reminder,
        False,
    )

    incomplete_managers = await asyncio.to_thread(
        get_managers_for_reminder,
        True,
    )

    return all_managers, incomplete_managers


async def build_admin_report(
    report_time: str | None = None,
    show_all: bool = False,
) -> str:
    all_managers, incomplete_managers = (
        await get_schedule_report_data()
    )

    total_count = len(all_managers)
    incomplete_count = len(incomplete_managers)
    completed_count = total_count - incomplete_count

    if report_time is None:
        report_time = datetime.now(KYIV_TZ).strftime("%H:%M")

    if report_time == "20:00":
        title = "🔒 Графік закрито"
    else:
        title = f"📊 Стан на {report_time}"

    lines = [
        title,
        "",
        f"✅ Заповнили: {completed_count}",
        f"❌ Не заповнили: {incomplete_count}",
    ]

    if show_all:
        incomplete_ids = {
            str(manager.get("telegram_id", ""))
            for manager in incomplete_managers
        }

        lines.extend(
            [
                "",
                "📋 Усі менеджери:",
                "",
            ]
        )

        for manager in all_managers:
            manager_name = manager.get(
                "manager_name",
                "Без імені",
            )

            telegram_id = str(
                manager.get("telegram_id", "")
            )

            if telegram_id in incomplete_ids:
                status = "❌"
            else:
                status = "✅"

            lines.append(
                f"{status} {manager_name}"
            )

    elif incomplete_managers:
        lines.extend(
            [
                "",
                "Не заповнили:",
                "",
            ]
        )

        for manager in incomplete_managers:
            manager_name = manager.get(
                "manager_name",
                "Без імені",
            )

            lines.append(
                f"• {manager_name}"
            )

    else:
        lines.extend(
            [
                "",
                "🎉 Усі менеджери заповнили графік.",
            ]
        )

    return "\n".join(lines)
async def send_admin_report(
    bot: Bot,
    report_time: str,
) -> None:
    try:
        report_text = await build_admin_report(
            report_time=report_time,
            show_all=False,
        )

        await bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=report_text,
            reply_markup=get_admin_report_keyboard(),
        )

        print(
            f"✅ Звіт адміністратору "
            f"надіслано о {report_time}",
            flush=True,
        )

    except Exception as error:
        print(
            f"❌ Помилка надсилання звіту "
            f"адміністратору: {error}",
            flush=True,
        )


async def send_schedule_reminders(
    bot: Bot,
    reminder_time: str,
) -> None:
    # О 09:00 повідомлення отримують усі активні.
    # Пізніше — тільки ті, хто ще не заповнив графік.
    only_incomplete = reminder_time != "09:00"

    managers = await asyncio.to_thread(
        get_managers_for_reminder,
        only_incomplete,
    )

    message_text = REMINDER_MESSAGES[
        reminder_time
    ]

    for manager in managers:
        telegram_id = manager.get("telegram_id")
        manager_name = manager.get(
            "manager_name",
            "",
        )

        if not telegram_id:
            continue

        try:
            await bot.send_message(
                chat_id=int(telegram_id),
                text=message_text,
            )

            print(
                f"✅ Нагадування {reminder_time} "
                f"надіслано: {manager_name}",
                flush=True,
            )

        except Exception as error:
            print(
                f"❌ Не вдалося надіслати "
                f"нагадування {manager_name}: "
                f"{error}",
                flush=True,
            )

        # Невелика пауза між повідомленнями
        await asyncio.sleep(0.05)


async def reminders_loop(bot: Bot) -> None:
    sent_events = set()

    while True:
        try:
            now = datetime.now(KYIV_TZ)

            # Усе працює тільки щочетверга
            if now.weekday() == 3:
                current_date = now.strftime(
                    "%Y-%m-%d"
                )
                current_time = now.strftime(
                    "%H:%M"
                )
            if "09:00" <= current_time < "10:00":
                current_time = "09:00"
                # Розсилка менеджерам
                reminder_event = (
                    current_date,
                    "manager",
                    current_time,
                )

                if (
                    current_time in REMINDER_MESSAGES
                    and reminder_event not in sent_events
                ):
                    sent_events.add(reminder_event)

                    await send_schedule_reminders(
                        bot,
                        current_time,
                    )

                # Особистий звіт Марті
                admin_event = (
                    current_date,
                    "admin",
                    current_time,
                )

                if (
                    current_time in ADMIN_REPORT_TIMES
                    and admin_event not in sent_events
                ):
                    sent_events.add(admin_event)

                    await send_admin_report(
                        bot,
                        current_time,
                    )

            # Прибираємо старі записи за попередні дні
            today = now.strftime("%Y-%m-%d")

            sent_events = {
                event
                for event in sent_events
                if event[0] == today
            }

        except Exception as error:
            print(
                f"❌ Помилка циклу нагадувань: "
                f"{error}",
                flush=True,
            )

        await asyncio.sleep(20)
