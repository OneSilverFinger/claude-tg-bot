from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_kb(group_connected: bool = True) -> InlineKeyboardMarkup:
    rows = [[btn("🖥 Машины", "menu:machines")]]
    if not group_connected:
        rows.append([btn("🔗 Подключить рабочую группу", "menu:connectgroup")])
    rows.append([btn("📁 Проекты и сессии", "menu:projects")])
    rows.append([btn("ℹ️ Статус", "menu:status")])
    return kb(rows)


def stop_kb() -> InlineKeyboardMarkup:
    return kb([[btn("⏹ Остановить", "run:stop")]])


def back_kb(data: str = "menu:main") -> InlineKeyboardMarkup:
    return kb([[btn("⬅️ Назад", data)]])
