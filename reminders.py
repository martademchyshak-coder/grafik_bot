import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from google_sheets import get_managers_for_reminder


KYIV_TZ = ZoneInfo("Europe/Kyiv")


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


async def send_schedule_reminders(
    bot: Bot,
    reminder_time: str,
) -> None:
    only_incomplete = reminder_time != "09:00"

    managers = await asyncio.to_thread(
        get_managers_for_reminder,
        only_incomplete,
    )

    message_text = REMINDER_MESSAGES[reminder_time]

    for manager in managers:
        telegram_id = manager.get("telegram_id")
        manager_name = manager.get("manager_name", "")

        if not telegram_id:
            continue

        try:
            await bot.send_message(
                chat_id=int(telegram_id),
                text=message_text,
            )

            print(
                f"✅ Нагадування {reminder_time} надіслано: "
                f"{manager_name}",
                flush=True,
            )

        except Exception as error:
            print(
                f"❌ Не вдалося надіслати нагадування "
                f"{manager_name}: {error}",
                flush=True,
            )

        # Маленька пауза, щоб не надсилати всі повідомлення одночасно
        await asyncio.sleep(0.05)


async def reminders_loop(bot: Bot) -> None:
    sent_reminders = set()

    while True:
        now = datetime.now(KYIV_TZ)

        # Нагадування працюють тільки в четвер
        if now.weekday() == 3:
            current_time = now.strftime("%H:%M")
            reminder_key = (
                now.strftime("%Y-%m-%d"),
                current_time,
            )

            if (
                current_time in REMINDER_MESSAGES
                and reminder_key not in sent_reminders
            ):
                sent_reminders.add(reminder_key)

                await send_schedule_reminders(
                    bot,
                    current_time,
                )

        # Видаляємо зі списку записи за минулі дні
        today = now.strftime("%Y-%m-%d")

        sent_reminders = {
            key
            for key in sent_reminders
            if key[0] == today
        }

        await asyncio.sleep(20)
