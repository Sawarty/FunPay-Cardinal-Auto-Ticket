from __future__ import annotations

import re
import logging
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.account import Account
from FunPayAPI.types import OrderStatuses, Order
from telebot.types import CallbackQuery, InlineKeyboardMarkup as K, InlineKeyboardButton as B, Message
from tg_bot import CBT as _CBT, static_keyboards as skb

from pydantic import BaseModel, Field

import httpx
import requests
from bs4 import BeautifulSoup

NAME = "Auto Ticket"
VERSION = "1.0.0"
DESCRIPTION = "Плагин для автоматической отправки информации о неподтвержденных заказах в техподдержку FunPay."
CREDITS = "@kewanmov"
UUID = "d217ee86-8269-4282-a1bc-c0bea1365205"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.auto_ticket")
PREFIX = "[AUTO TICKET]"

CBT_MAIN = "at_main"
CBT_SEND = "at_send"
CBT_EDIT_COUNT = "at_edit_count"
CBT_EDIT_TIME = "at_edit_time"

_PARENT_FOLDER = 'auto_ticket'
_STORAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "storage", "plugins", _PARENT_FOLDER)
os.makedirs(_STORAGE_PATH, exist_ok=True)
_SETTINGS_FILE = os.path.join(_STORAGE_PATH, "settings.json")


class Settings(BaseModel):
    order_age_hours: int = Field(default=24, ge=1, le=720)
    max_orders_in_ticket: int = Field(default=10, ge=1, le=50)
    sent_order_ids: List[str] = Field(default_factory=list)

    def save(self):
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            data = self.model_dump() if hasattr(self, 'model_dump') else self.dict()
            json.dump(data, f, ensure_ascii=False, indent=4)

    @classmethod
    def load(cls):
        if os.path.exists(_SETTINGS_FILE):
            try:
                with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return cls(**data)
            except Exception as e:
                logger.error(f"{PREFIX} Ошибка загрузки настроек: {e}", exc_info=True)
        return cls()


SETTINGS = Settings.load()


def parse_funpay_date(date_obj) -> float:
    if isinstance(date_obj, datetime):
        return date_obj.timestamp()
    date_str = str(date_obj).strip()
    now = datetime.now()
    try:
        if date_str.startswith("Сегодня"):
            t = date_str.replace("Сегодня в ", "")
            h, m = map(int, t.split(":"))
            return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
        if date_str.startswith("Вчера"):
            t = date_str.replace("Вчера в ", "")
            h, m = map(int, t.split(":"))
            yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return yesterday.replace(hour=h, minute=m).timestamp()
        if " в " in date_str:
            return datetime.strptime(date_str, "%d %b в %H:%M").replace(year=now.year).timestamp()
        if "," in date_str:
            return datetime.strptime(date_str, "%d %b, %H:%M").replace(year=now.year).timestamp()
        return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").timestamp()
    except:
        logger.warning(f"{PREFIX} Не удалось распарсить дату заказа: '{date_str}'")
        return 0


async def get_old_orders_for_ticket(acc: Account, age_hours: int, max_count: int) -> List[str]:
    old_orders_ids = []
    cutoff_timestamp = (datetime.now() - timedelta(hours=age_hours)).timestamp()
    start_from = None
    subcs = {}
    locale = acc.locale
    page_count = 0
    max_pages = 10

    while len(old_orders_ids) < max_count and page_count < max_pages:
        try:
            result = acc.get_sales(start_from=start_from, state=OrderStatuses.PAID, locale=locale, subcategories=subcs)
            page_count += 1
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка получения заказов: {e}", exc_info=True)
            break

        if not result or not result[1]:
            break

        batch_timestamps = []
        for order_data in result[1]:
            order_timestamp = parse_funpay_date(order_data.date)
            if order_timestamp == 0:
                continue
            batch_timestamps.append(order_timestamp)

            order_id = str(order_data.id)
            if order_timestamp < cutoff_timestamp and order_id not in SETTINGS.sent_order_ids:
                old_orders_ids.append(order_id)

            if len(old_orders_ids) >= max_count:
                break

        if batch_timestamps:
            min_timestamp = min(batch_timestamps)
            if min_timestamp >= cutoff_timestamp:
                logger.info(f"{PREFIX} Все заказы в батче новее cutoff — останавливаем сканирование.")
                break

        start_from = result[0]
        if not start_from:
            break
        await asyncio.sleep(1)

    if page_count >= max_pages:
        logger.warning(f"{PREFIX} Достигнут лимит страниц ({max_pages}) — возможно, не все старые заказы найдены.")

    return old_orders_ids[:max_count]


class FunPaySupportAPI:
    def __init__(self, funpay_account: Account):
        self.funpay_account: Account = funpay_account
        self.golden_key: str = funpay_account.golden_key
        self.user_agent: str = funpay_account.user_agent
        self.requests_timeout: int = funpay_account.requests_timeout

        self.app_data: dict = {}
        self.csrf_token: str = ""
        self.phpsessid: str = ""

    def method(self, method: str, url: str, headers: dict[str, str] = {}, payload: dict = {},
               exclude_phpsessid: bool = False) -> requests.Response:
        headers["Cookie"] = f"golden_key={self.golden_key}; cookie_prefs=1"
        headers["Cookie"] += f"; PHPSESSID={self.phpsessid}" if self.phpsessid and not exclude_phpsessid else ""
        if self.user_agent:
            headers["User-Agent"] = self.user_agent

        link = url
        for i in range(10):
            response: requests.Response = getattr(requests, method)(link, headers=headers, 
                                                                    data=payload, timeout=self.requests_timeout, 
                                                                    allow_redirects=False)
            if not (300 <= response.status_code < 400) or not response.headers.get('Location') or response.headers.get('Location') == '/':
                break
            link = response.headers['Location']
        else:
            response = getattr(requests, method)(url, headers=headers, data=payload,
                                                 timeout=self.requests_timeout)
        return response
        
    def get(self) -> 'FunPaySupportAPI':
        r = self.method("get", "https://support.funpay.com/", {}, {}, True)
        cookies = r.cookies.get_dict()
        self.phpsessid = cookies.get("PHPSESSID", self.phpsessid)
        r = self.method("get", "https://support.funpay.com/", {}, {}, False)

        html_response = r.content.decode()
        parser = BeautifulSoup(html_response, "lxml")
        self.app_data = json.loads(parser.find("body").get("data-app-config"))

        self.csrf_token = self.app_data["csrfToken"]
        return self
        
    def get_ticket_token(self) -> str:
        headers = {
            "X-CSRF-Token": self.csrf_token,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": "https://support.funpay.com/",
        }
        r = self.method("get", "https://support.funpay.com/tickets/new/1", headers)
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.find("input", attrs={"name": "ticket[_token]"})
        return body.get("value")
        
    def create_ticket(self, order_id: str | None, comment: str) -> dict:
        ticket_token = self.get_ticket_token()
        headers = {
            "Origin": "https://support.funpay.com",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"
        }
        payload = {
            "ticket[fields][1]": self.funpay_account.username,
            "ticket[fields][2]": order_id if order_id else "",
            "ticket[fields][3]": "2",
            "ticket[fields][5]": "201",
            "ticket[comment][body_html]": f"<p>{comment}</p>",
            "ticket[comment][attachments]": "",
            "ticket[_token]": ticket_token
        }
        r = self.method("post", "https://support.funpay.com/tickets/create/1", headers, payload)
        return r.json()


async def _report_deal_problem_raw(acc: Account, deal_id: str) -> bool:
    try:
        try:
            order = acc.get_order(deal_id)
            if order.status != OrderStatuses.PAID:
                logger.info(f"{PREFIX} Заказ #{deal_id} не в статусе PAID ({order.status}), пропускаем.")
                return False
        except Exception as e:
            logger.warning(f"{PREFIX} Не удалось получить статус заказа #{deal_id}: {e}. Пропускаем.")
            return False
        
        support_api = FunPaySupportAPI(acc)
        support_api.get()
        
        comment = f"Покупатель не подтверждает выполнение заказа #{deal_id}."
        response_json = support_api.create_ticket(deal_id, comment)
        
        if response_json.get('action') == 'message' and 'заявка отправлена' in response_json.get('message', '').lower() and '/tickets/' in response_json.get('url', ''):
            logger.info(f"{PREFIX} ✅ УСПЕХ: Тикет по #{deal_id} отправлен. Ответ: {response_json}")
            return True
        else:
            logger.error(f"{PREFIX} ❌ ПРОВАЛ: #{deal_id}. Ответ: {response_json}")
            return False
                
    except Exception as e:
        logger.error(f"{PREFIX} Ошибка отправки для #{deal_id}: {e}", exc_info=True)
        return False


async def report_deal_problems(acc: Account, orders_ids: List[str]) -> List[str]:
    reported_successfully = []
    
    total = len(orders_ids)
    logger.info(f"{PREFIX} Начинаю отправку тикетов для {total} заказов...")

    for i, deal_id in enumerate(orders_ids):
        if i > 0:
            await asyncio.sleep(2)

        success = await _report_deal_problem_raw(acc, deal_id)
        if success:
            reported_successfully.append(deal_id)
            
    return reported_successfully

def _main_text(status_text: str = "Нет старых заказов.") -> str:
    return (
        f"Плагин для отправки авто-тикетов в поддержку о подтверждении старых заказов\n\n"
        f"Заказов в тикете: {SETTINGS.max_orders_in_ticket}\n"
        f"Старше часов: {SETTINGS.order_age_hours}\n"
        f"Отправлено заказов: {len(SETTINGS.sent_order_ids)}\n\n"
        f"{status_text}"
    )


def _main_kb() -> K:
    kb = K()
    kb.add(B(f"🔗 Отправить заказы в ТП", callback_data=f"{CBT_SEND}:"))
    kb.add(B(f"📋 Заказов в 1 тикете: {SETTINGS.max_orders_in_ticket}", callback_data=f"{CBT_EDIT_COUNT}:"))
    kb.add(B(f"⏳ Заказы старше: {SETTINGS.order_age_hours} часов", callback_data=f"{CBT_EDIT_TIME}:"))
    kb.add(B(f"⬅️ Назад", callback_data=f"{_CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def init_commands(cardinal: Cardinal, *args):
    if not cardinal.telegram:
        return

    tg = cardinal.telegram
    bot = tg.bot

    def _edit(c: CallbackQuery, text: str, kb: K):
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
        except:
            bot.answer_callback_query(c.id, "Ошибка обновления", show_alert=True)

    def _set_state(chat_id, user_id, state, text, callback: Optional[CallbackQuery] = None):
        msg = bot.send_message(chat_id, text, reply_markup=skb.CLEAR_STATE_BTN(), parse_mode="HTML")
        tg.set_state(chat_id, msg.id, user_id, state, {})
        if callback:
            bot.answer_callback_query(callback.id)

    def open_menu(c: CallbackQuery):
        _edit(c, _main_text(), _main_kb())
        bot.answer_callback_query(c.id)

    def act_send_ticket(c: CallbackQuery):
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            logger.debug(f"{PREFIX} Использую существующий event loop.")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.debug(f"{PREFIX} Создан новый event loop для потока.")

        bot.answer_callback_query(c.id, "Сканирую заказы...")
        _edit(c, _main_text("<b>Сканирование...</b>"), _main_kb())

        orders = loop.run_until_complete(get_old_orders_for_ticket(cardinal.account, SETTINGS.order_age_hours, SETTINGS.max_orders_in_ticket))
        if not orders:
            _edit(c, _main_text("Нет старых неподтверждённых заказов."), _main_kb())
            return

        reported_orders = loop.run_until_complete(report_deal_problems(cardinal.account, orders))

        total_orders = len(orders)
        sent_count = len(reported_orders)
        skipped_count = total_orders - sent_count

        if sent_count > 0:
            orders_list = ", ".join([f"#{oid}" for oid in reported_orders])
            success_text = f"Тикет отправлен! Отправлено: {sent_count} (<code>{orders_list}</code>). Пропущено: {skipped_count} (недоступны)."
        elif skipped_count == total_orders:
            success_text = "Нет доступных неподтверждённых заказов (возможно, уже завершены)."
        else:
            success_text = f"Ошибка отправки для всех заказов ({total_orders}). См. логи."

        _edit(c, _main_text(success_text), _main_kb())
        try:
            bot.answer_callback_query(c.id, f"Отправлено: {sent_count} заказов", show_alert=True)
        except Exception as e:
            logger.warning(f"{PREFIX} Не удалось отправить алерт в Telegram: {e}")

        if sent_count > 0:
            SETTINGS.sent_order_ids.extend(reported_orders)
            SETTINGS.save()

    def act_edit_time(c: CallbackQuery):
        def handler(m: Message):
            try:
                val = int(m.text)
                if 1 <= val <= 720:
                    SETTINGS.order_age_hours = val
                    SETTINGS.save()
                    bot.send_message(m.chat.id, _main_text(), reply_markup=_main_kb(), parse_mode="HTML")
                else:
                    raise ValueError
            except:
                bot.send_message(m.chat.id, "Неверно. Введите число 1–720.", parse_mode="HTML")
            tg.clear_state(m.chat.id, m.from_user.id, True)

        _set_state(c.message.chat.id, c.from_user.id, CBT_EDIT_TIME,
                   f"Введите новое время (часы):\nТекущее: <b>{SETTINGS.order_age_hours}</b>", c)
        tg.msg_handler(handler, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_EDIT_TIME))

    def act_edit_count(c: CallbackQuery):
        def handler(m: Message):
            try:
                val = int(m.text)
                if 1 <= val <= 50:
                    SETTINGS.max_orders_in_ticket = val
                    SETTINGS.save()
                    bot.send_message(m.chat.id, _main_text(), reply_markup=_main_kb(), parse_mode="HTML")
                else:
                    raise ValueError
            except:
                bot.send_message(m.chat.id, "Неверно. Введите число 1–50.", parse_mode="HTML")
            tg.clear_state(m.chat.id, m.from_user.id, True)

        _set_state(c.message.chat.id, c.from_user.id, CBT_EDIT_COUNT,
                   f"Введите макс. заказов в тикете:\nТекущее: <b>{SETTINGS.max_orders_in_ticket}</b>", c)
        tg.msg_handler(handler, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_EDIT_COUNT))

    def open_menu_command(m: Message):
        bot.send_message(m.chat.id, _main_text(), reply_markup=_main_kb(), parse_mode="HTML")

    tg.cbq_handler(open_menu, lambda c: f"{_CBT.PLUGIN_SETTINGS}:{UUID}" in c.data or f"{CBT_MAIN}:" in c.data)
    tg.cbq_handler(act_send_ticket, lambda c: f"{CBT_SEND}:" in c.data)
    tg.cbq_handler(act_edit_time, lambda c: f"{CBT_EDIT_TIME}:" in c.data)
    tg.cbq_handler(act_edit_count, lambda c: f"{CBT_EDIT_COUNT}:" in c.data)
    tg.msg_handler(open_menu_command, commands=["auto_ticket"])
    cardinal.add_telegram_commands(UUID, [("auto_ticket", "открыть меню авто-тикетов", True)])


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None