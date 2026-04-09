import telebot
import sqlite3
import smtplib
import re
import os
import requests
from telebot import types
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "8630425742:AAHcnb8KBZt8rScUPWGjXde6pyHkjnAO8Sg")
EMAIL_FROM = "info@parametricbox.com"
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "xhdgjemunutbkmrh")
SENDER_NAME = "Устина Алёна, руководитель Parametric Box"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
DB = "struktura.db"
PHOTOS_DIR = "photos"
os.makedirs(PHOTOS_DIR, exist_ok=True)

CONTACTS = [
    {"name": "Лера (тест)",          "title": "Тестовый получатель",       "email": "leraduv@gmail.com"},
    {"name": "Степаненко Андрей",    "title": "Директор проектов",          "email": "andrey.stepanenko@sk-struktura.ru"},
    {"name": "Апанасевич Алексей",   "title": "Руководитель проекта",       "email": "apanasevich@sk-struktura.ru"},
    {"name": "Макарова Надежда",     "title": "Администратор проекта",      "email": "makarova@sk-struktura.ru"},
    {"name": "Василькив Алена",      "title": "Главный архитектор",         "email": "vasilkiv@sk-struktura.ru"},
    {"name": "Сайтфутдинов Динар",  "title": "Рук. BIM отдела",            "email": "d.sayfutdinov@sk-struktura.ru"},
    {"name": "Маняпов Исламнур",     "title": "BIM менеджер",               "email": "manyapov@sk-struktura.ru"},
    {"name": "Потапенко Артём",      "title": "Главный конструктор",        "email": "a.potapenko@sk-struktura.ru"},
    {"name": "Тарасевич Екатерина", "title": "Рук. СДО",                   "email": "tarasevich@sk-struktura.ru"},
]

PRICES_DEFAULT = {
    "week":   {"label": "Меньше недели",    "rate": 8000,  "flat": None},
    "weeks":  {"label": "Несколько недель", "rate": 5000,  "flat": None},
    "month":  {"label": "Месяц",            "rate": None,  "flat": 400000},
    "months": {"label": "Больше месяца",    "rate": None,  "flat": 380000},
}

bot = telebot.TeleBot(TOKEN, parse_mode=None)
sessions = {}

# ── DB ───────────────────────────────────────────────────────────────────────

def db():
    return sqlite3.connect(DB)

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT, date TEXT, task_name TEXT,
            client_name TEXT, client_tg TEXT,
            duration_label TEXT, duration_detail TEXT,
            hours REAL, rate INTEGER, total INTEGER,
            email_body TEXT, recipients TEXT, status TEXT,
            created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS prices (
            category TEXT PRIMARY KEY, rate INTEGER, flat INTEGER)""")
        for cat, p in PRICES_DEFAULT.items():
            c.execute("INSERT OR IGNORE INTO prices VALUES (?,?,?)",
                      (cat, p["rate"], p["flat"]))
        c.execute("""CREATE TABLE IF NOT EXISTS user_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, file_id TEXT, added_at TEXT)""")

def get_prices():
    with db() as c:
        rows = c.execute("SELECT category, rate, flat FROM prices").fetchall()
        return {cat: {"label": PRICES_DEFAULT[cat]["label"], "rate": rate, "flat": flat}
                for cat, rate, flat in rows}

def save_price(cat, rate, flat):
    with db() as c:
        c.execute("UPDATE prices SET rate=?, flat=? WHERE category=?", (rate, flat, cat))

def db_add_photo(user_id, file_id):
    with db() as c:
        exists = c.execute("SELECT 1 FROM user_photos WHERE user_id=? AND file_id=?",
                           (user_id, file_id)).fetchone()
        if not exists:
            c.execute("INSERT INTO user_photos (user_id, file_id, added_at) VALUES (?,?,?)",
                      (user_id, file_id, datetime.now().isoformat()))

def db_get_photos(user_id):
    with db() as c:
        rows = c.execute("SELECT file_id FROM user_photos WHERE user_id=? ORDER BY id",
                         (user_id,)).fetchall()
        return [r[0] for r in rows]

def next_number():
    today = datetime.now().strftime("%d%m%Y")
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM requests WHERE number LIKE ?",
                          (f"{today}%",)).fetchone()[0]
        return today if count == 0 else f"{today}-{count + 1}"

def save_draft(user_id, s):
    with db() as c:
        c.execute("DELETE FROM requests WHERE status='draft' AND number=?", (s["number"],))
        c.execute("""INSERT INTO requests
            (number,date,task_name,client_name,client_tg,duration_label,
             duration_detail,hours,rate,total,email_body,recipients,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            s["number"], s["date"], s["task_name"], s["client_name"],
            s["client_tg"], s.get("duration_label", ""), s.get("duration_detail", ""),
            s.get("hours"), s.get("rate"), s["total"],
            s.get("email_body", ""), ",".join(str(i) for i in s.get("selected", [])),
            "draft", datetime.now().isoformat()))

def save_request(s):
    with db() as c:
        c.execute("UPDATE requests SET status='sent' WHERE number=?", (s["number"],))
        if c.execute("SELECT changes()").fetchone()[0] == 0:
            c.execute("""INSERT INTO requests
                (number,date,task_name,client_name,client_tg,duration_label,
                 duration_detail,hours,rate,total,email_body,recipients,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                s["number"], s["date"], s["task_name"], s["client_name"],
                s["client_tg"], s.get("duration_label", ""), s.get("duration_detail", ""),
                s.get("hours"), s.get("rate"), s["total"],
                s.get("email_body", ""), ",".join(str(i) for i in s.get("selected", [])),
                "sent", datetime.now().isoformat()))

def get_draft():
    with db() as c:
        row = c.execute("""SELECT number,date,task_name,client_name,client_tg,
            duration_label,duration_detail,hours,rate,total,email_body,recipients
            FROM requests WHERE status='draft' ORDER BY id DESC LIMIT 1""").fetchone()
        if not row:
            return None
        keys = ["number", "date", "task_name", "client_name", "client_tg",
                "duration_label", "duration_detail", "hours", "rate", "total",
                "email_body", "recipients"]
        d = dict(zip(keys, row))
        d["selected"] = [int(i) for i in d["recipients"].split(",") if i.strip()]
        return d

# ── Session helpers ───────────────────────────────────────────────────────────

def s(uid):
    if uid not in sessions:
        sessions[uid] = {"step": None, "photos": db_get_photos(uid),
                         "selected_photos": [], "selected": []}
    return sessions[uid]

def ensure_session_restored(uid):
    sess = s(uid)
    if sess.get("number"):
        return True
    draft = get_draft()
    if not draft:
        return False
    sess.update(draft)
    sess["step"] = "confirm"
    sess["is_flat"] = draft.get("rate") is None
    sess["subject"] = f"Заявка №{draft['number']} — {draft['task_name']}"
    sess["photos"] = db_get_photos(uid)
    sess.setdefault("selected_photos", [])
    return True

# ── Cost ─────────────────────────────────────────────────────────────────────

def calc_cost(cat, detail, prices):
    p = prices[cat]
    nums = re.findall(r'\d+(?:[.,]\d+)?', detail)
    num = float(nums[0].replace(",", ".")) if nums else 1.0
    if cat == "week":
        h = num * 8
        return h, p["rate"], round(h * p["rate"]), False, p["label"]
    elif cat == "weeks":
        h = num * 5 * 8
        return h, p["rate"], round(h * p["rate"]), False, p["label"]
    elif cat == "month":
        return None, None, p["flat"], True, p["label"]
    elif cat == "months":
        return None, None, round(num * p["flat"]), True, p["label"]

def fmt(n):
    return f"{int(n):,}".replace(",", " ") + " руб."

def pluralize_days(n):
    if 11 <= n % 100 <= 19:
        return "дней"
    r = n % 10
    if r == 1:   return "день"
    if r <= 4:   return "дня"
    return "дней"

def format_duration(detail):
    time_words = ["день", "дня", "дней", "недел", "месяц", "час"]
    if any(w in detail.lower() for w in time_words):
        return detail
    nums = re.findall(r'\d+', detail)
    if nums:
        n = int(nums[0])
        return f"{n} {pluralize_days(n)}"
    return detail

# ── Email ────────────────────────────────────────────────────────────────────

def build_email(s):
    duration_line = f"{format_duration(s['duration_detail'])} (от даты подписания доп.соглашения)"
    return (
        f"Уважаемые коллеги,\n\n"
        f"настоящим уведомляем вас о поступлении новой заявки на выполнение работ.\n\n"
        f"РЕКВИЗИТЫ ЗАЯВКИ\n"
        f"================\n"
        f"  - Номер запроса: {s['number']}\n"
        f"  - Дата поступления: {s['date']}\n"
        f"  - Название задачи: {s['task_name']}\n"
        f"  - Заказчик: {s['client_name']}\n"
        f"  - Telegram: {s['client_tg']}\n\n"
        f"СТОИМОСТЬ РАБОТ\n"
        f"===============\n"
        f"  - Срок: {duration_line}\n"
        f"  - Стоимость: {fmt(s['total'])}\n\n"
        f"Просим подтвердить получение данной заявки и готовность приступить "
        f"к её выполнению в указанные сроки.\n\n"
        f"С уважением,\n\n"
        f"Устина Алёна\n\n"
        f"руководитель Parametric Box"
    )

def download_photo(file_id):
    try:
        file_info = bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        ext = file_info.file_path.split(".")[-1]
        path = os.path.join(PHOTOS_DIR, f"{file_id}.{ext}")
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as e:
        print("Photo download error:", e)
        return None

def send_email(subject, body, to_list, photo_paths=None):
    msg = MIMEMultipart()
    msg["From"] = f"{SENDER_NAME} <{EMAIL_FROM}>"
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if photo_paths:
        for i, path in enumerate(photo_paths, 1):
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    img = MIMEImage(f.read())
                    img.add_header("Content-Disposition", "attachment",
                                   filename=f"screenshot_{i}.{path.split('.')[-1]}")
                    msg.attach(img)

    errors = []

    # Способ 1: SSL порт 465
    try:
        print(f"Trying SSL port 465...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as srv:
            srv.login(EMAIL_FROM, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_FROM, to_list, msg.as_bytes())
        print("Email sent OK via port 465")
        return True, None
    except Exception as e:
        print(f"Port 465 failed: {e}")
        errors.append(f"465: {e}")

    # Способ 2: STARTTLS порт 587
    try:
        print(f"Trying STARTTLS port 587...")
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(EMAIL_FROM, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_FROM, to_list, msg.as_bytes())
        print("Email sent OK via port 587")
        return True, None
    except Exception as e:
        print(f"Port 587 failed: {e}")
        errors.append(f"587: {e}")

    return False, "\n".join(errors)

# ── Keyboards ────────────────────────────────────────────────────────────────

def kb_categories(prices):
    kb = types.InlineKeyboardMarkup()
    icons = {"week": "⚡", "weeks": "📅", "month": "🗓", "months": "📦"}
    for cat, p in prices.items():
        rate_str = (f"{p['rate']:,} р/ч".replace(",", " ") if p["rate"]
                    else f"{p['flat']:,} р фикс.".replace(",", " "))
        kb.add(types.InlineKeyboardButton(
            text=f"{icons[cat]} {p['label']}  ({rate_str})",
            callback_data=f"cat_{cat}"))
    kb.add(types.InlineKeyboardButton(text="← Назад", callback_data="back_client_tg"))
    return kb

def kb_recipients(selected):
    kb = types.InlineKeyboardMarkup()
    for i, c in enumerate(CONTACTS):
        mark = "✅" if i in selected else "☐"
        kb.add(types.InlineKeyboardButton(
            text=f"{mark}  {c['name']} — {c['title']}",
            callback_data=f"rcpt_{i}"))
    kb.row(
        types.InlineKeyboardButton(text="Выбрать всех", callback_data="rcpt_all"),
        types.InlineKeyboardButton(text="Снять всех",   callback_data="rcpt_none"))
    kb.row(
        types.InlineKeyboardButton(text="← Назад",             callback_data="back_photos"),
        types.InlineKeyboardButton(text="➡️ Показать письмо",  callback_data="rcpt_done"))
    return kb

def kb_photos(all_ids, selected_ids):
    kb = types.InlineKeyboardMarkup()
    for i, fid in enumerate(all_ids, 1):
        mark = "✅" if fid in selected_ids else "☐"
        kb.add(types.InlineKeyboardButton(
            text=f"{mark} Скриншот {i}",
            callback_data=f"photo_{i - 1}"))
    kb.row(
        types.InlineKeyboardButton(text="Выбрать все", callback_data="photo_all"),
        types.InlineKeyboardButton(text="Снять все",   callback_data="photo_none"))
    kb.row(
        types.InlineKeyboardButton(text="← Назад",          callback_data="back_duration"),
        types.InlineKeyboardButton(text="➡️ К получателям", callback_data="photo_done"))
    return kb

def kb_confirm():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="✅ Подтвердить и отправить",  callback_data="confirm_send"))
    kb.add(types.InlineKeyboardButton(text="✏️ Изменить текст письма",    callback_data="confirm_edit_text"))
    kb.add(types.InlineKeyboardButton(text="💰 Изменить стоимость",       callback_data="confirm_adjust"))
    kb.add(types.InlineKeyboardButton(text="👥 Изменить получателей",     callback_data="confirm_recipients"))
    kb.add(types.InlineKeyboardButton(text="📎 Изменить вложения",        callback_data="confirm_photos"))
    kb.add(types.InlineKeyboardButton(text="❌ Отмена",                   callback_data="confirm_cancel"))
    return kb

def kb_prices(prices):
    kb = types.InlineKeyboardMarkup()
    for cat, p in prices.items():
        val = (f"{p['rate']:,} р/ч".replace(",", " ") if p["rate"]
               else f"{p['flat']:,} р".replace(",", " "))
        kb.add(types.InlineKeyboardButton(
            text=f"✏️ {p['label']}: {val}", callback_data=f"editprice_{cat}"))
    return kb

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("📝 Новая заявка"), types.KeyboardButton("💰 Тарифы"))
    kb.row(types.KeyboardButton("❓ Помощь"))
    return kb

# ── Show email preview ────────────────────────────────────────────────────────

def show_email_preview(uid, cid):
    sess = s(uid)
    body = sess.get("email_body") or build_email(sess)
    sess["email_body"] = body
    subject = f"Заявка №{sess['number']} — {sess['task_name']}"
    sess["subject"] = subject

    rcpt_lines = "\n".join(
        f"  • {CONTACTS[i]['name']} ({CONTACTS[i]['title']})"
        for i in sess["selected"])

    selected_photos = sess.get("selected_photos", [])
    attach_note = (f"\nВложений: {len(selected_photos)} скриншот(ов)"
                   if selected_photos else "\nВложений нет")

    bot.send_message(cid,
        f"ЧЕРНОВИК ПИСЬМА\n\n"
        f"Тема: {subject}\n\n"
        f"Кому:\n{rcpt_lines}"
        f"{attach_note}\n\n"
        f"---\n{body}\n---")
    bot.send_message(cid, "Выбери действие:", reply_markup=kb_confirm())
    sess["step"] = "confirm"

# ── Commands ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(m):
    uid = m.from_user.id
    sessions[uid] = {"step": None, "photos": db_get_photos(uid),
                     "selected_photos": [], "selected": []}
    bot.send_message(m.chat.id,
        "Привет, Алёна!\n\n"
        "Я бот управления заявками Parametric Box → Struktura.\n\n"
        "Используй кнопки внизу или меню / слева от поля ввода.",
        reply_markup=main_keyboard())

    draft = get_draft()
    if draft:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(text="🔄 Продолжить незакрытую заявку", callback_data="draft_resume"))
        kb.add(types.InlineKeyboardButton(text="🗑 Удалить черновик", callback_data="draft_discard"))
        bot.send_message(m.chat.id,
            f"У тебя есть незакрытая заявка:\n\n"
            f"Задача: {draft['task_name']}\n"
            f"Заказчик: {draft['client_name']}\n"
            f"Сумма: {fmt(draft['total'])}\n\n"
            f"Продолжить или удалить?",
            reply_markup=kb)

@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda m: m.text == "❓ Помощь")
def cmd_help(m):
    bot.send_message(m.chat.id,
        "Как пользоваться:\n\n"
        "1. Присылай сообщения и скриншоты — бот их запоминает\n"
        "2. /new_request — начать оформление заявки\n"
        "3. Бот спросит данные, рассчитает стоимость\n"
        "4. Выберешь какие скриншоты приложить к письму\n"
        "5. Выберешь получателей из Struktura\n"
        "6. Увидишь черновик — можно редактировать текст, стоимость, получателей\n"
        "7. Подтвердишь — письмо уйдёт с вложениями\n\n"
        "Если на этапе подтверждения захочешь добавить скриншот — просто пришли его боту.")

@bot.message_handler(commands=["prices"])
@bot.message_handler(func=lambda m: m.text == "💰 Тарифы")
def cmd_prices(m):
    prices = get_prices()
    lines = [
        f"• {p['label']}: {p['rate']:,} р/ч".replace(",", " ") if p["rate"]
        else f"• {p['label']}: {p['flat']:,} р фикс.".replace(",", " ")
        for p in prices.values()
    ]
    bot.send_message(m.chat.id,
        "Текущие тарифы:\n\n" + "\n".join(lines) + "\n\nНажми чтобы изменить:",
        reply_markup=kb_prices(prices))

@bot.message_handler(commands=["new_request"])
@bot.message_handler(func=lambda m: m.text == "📝 Новая заявка")
def cmd_new_request(m):
    uid = m.from_user.id
    all_photos = db_get_photos(uid)
    sessions[uid] = {"step": "task_name", "selected": [], "photos": all_photos, "selected_photos": []}
    bot.send_message(m.chat.id, "Новая заявка\n\nШаг 1/5: Введи название задачи:")

# ── Main message handler ──────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True, content_types=["text", "photo"])
def handle_message(m):
    uid = m.from_user.id
    sess = s(uid)
    step = sess.get("step")
    cid = m.chat.id
    text = m.text or m.caption or ""

    if m.photo:
        file_id = m.photo[-1].file_id
        if file_id not in sess["photos"]:
            sess["photos"].append(file_id)
            db_add_photo(uid, file_id)
        count = len(sess["photos"])

        if step == "confirm":
            if file_id not in sess.get("selected_photos", []):
                sess.setdefault("selected_photos", []).append(file_id)
            sess["email_body"] = None
            bot.send_message(cid,
                f"Скриншот добавлен к письму! Всего вложений: {len(sess['selected_photos'])} шт.\n\n"
                "Выбери действие:",
                reply_markup=kb_confirm())
            return
        elif step not in (None,):
            bot.send_message(cid, f"Скриншот сохранён (всего: {count} шт.)")
            if not text:
                return
        else:
            bot.send_message(cid,
                f"Скриншот сохранён. Всего накоплено: {count} шт.\n"
                "Нажми 📝 Новая заявка чтобы оформить.")
            return

    if step and step.startswith("editprice_"):
        cat = step.replace("editprice_", "")
        nums = re.findall(r'\d+', text.replace(" ", ""))
        if not nums:
            bot.send_message(cid, "Введи только цифры, например: 9000")
            return
        val = int(nums[0])
        prices = get_prices()
        p = prices[cat]
        if p["rate"] is not None:
            save_price(cat, val, p["flat"])
        else:
            save_price(cat, p["rate"], val)
        sess["step"] = None
        prices = get_prices()
        bot.send_message(cid, f"Обновлено: {val:,} р.".replace(",", " "),
                         reply_markup=kb_prices(prices))
        return

    if step == "adjust_cost":
        nums = re.findall(r'\d+', text.replace(" ", ""))
        if not nums:
            bot.send_message(cid, "Введи сумму в рублях, например: 150000")
            return
        sess["total"] = int(nums[0])
        sess["is_flat"] = True
        sess["email_body"] = None
        show_email_preview(uid, cid)
        return

    if step == "edit_email_text":
        sess["email_body"] = text
        bot.send_message(cid, "Текст письма обновлён.")
        show_email_preview(uid, cid)
        return

    if step == "task_name":
        if not text:
            bot.send_message(cid, "Введи название задачи текстом:")
            return
        sess["task_name"] = text
        sess["step"] = "client_name"
        bot.send_message(cid, "Шаг 2/5: Имя заказчика (от кого поступила заявка)?")

    elif step == "client_name":
        sess["client_name"] = text
        sess["step"] = "client_tg"
        bot.send_message(cid, "Шаг 3/5: Telegram заказчика (например @username):")

    elif step == "client_tg":
        sess["client_tg"] = text
        sess["step"] = "category"
        prices = get_prices()
        bot.send_message(cid, "Шаг 4/5: Выбери категорию срока:", reply_markup=kb_categories(prices))

    elif step == "duration_detail":
        sess["duration_detail"] = text
        prices = get_prices()
        hours, rate, total, is_flat, label = calc_cost(sess["category"], text, prices)
        sess.update(hours=hours, rate=rate, total=total, is_flat=is_flat, duration_label=label)

        if is_flat:
            cost_txt = f"Расчёт:\n• {label}\n• Срок: {text}\n• Итого: {fmt(total)}"
        else:
            cost_txt = (f"Расчёт:\n• {label}\n• Срок: {text}\n"
                        f"• Часов: {hours:.0f} ч\n• Ставка: {fmt(rate)}/ч\n• Итого: {fmt(total)}")
        bot.send_message(cid, cost_txt)

        sess["number"] = next_number()
        sess["date"] = datetime.now().strftime("%d.%m.%Y")

        if sess["photos"]:
            sess["step"] = "select_photos"
            sess["selected_photos"] = []
            _show_photo_selection(uid, cid)
        else:
            sess["step"] = "recipients"
            bot.send_message(cid, "Шаг 5/5: Выбери получателей письма:",
                             reply_markup=kb_recipients(sess["selected"]))

def _show_photo_selection(uid, cid):
    sess = s(uid)
    all_ids = sess["photos"]
    selected = sess["selected_photos"]
    for i, fid in enumerate(all_ids, 1):
        try:
            bot.send_photo(cid, fid, caption=f"Скриншот {i}")
        except Exception:
            pass
    bot.send_message(cid,
        f"Шаг 5а: У тебя {len(all_ids)} скриншот(ов).\n"
        "Выбери какие прикрепить к письму:",
        reply_markup=kb_photos(all_ids, selected))

# ── Callbacks ─────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("back_"))
def cb_back(c):
    uid = c.from_user.id
    sess = s(uid)
    cid = c.message.chat.id
    target = c.data.replace("back_", "")
    bot.answer_callback_query(c.id)
    if target == "client_tg":
        sess["step"] = "client_tg"
        bot.send_message(cid, "← Назад\n\nTelegram заказчика (например @username):")
    elif target == "duration":
        sess["step"] = "category"
        prices = get_prices()
        bot.send_message(cid, "← Назад\n\nВыбери категорию срока:", reply_markup=kb_categories(prices))
    elif target == "photos":
        if sess["photos"]:
            sess["step"] = "select_photos"
            _show_photo_selection(uid, cid)
        else:
            sess["step"] = "category"
            prices = get_prices()
            bot.send_message(cid, "← Назад\n\nВыбери категорию срока:", reply_markup=kb_categories(prices))

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat_"))
def cb_category(c):
    uid = c.from_user.id
    sess = s(uid)
    cat = c.data.replace("cat_", "")
    sess["category"] = cat
    sess["step"] = "duration_detail"
    prompts = {
        "week":   "Укажи срок (например: 3 дня, 5 дней):",
        "weeks":  "Укажи срок (например: 2 недели, 3 недели):",
        "month":  "Уточни срок (например: 1 месяц):",
        "months": "Укажи количество месяцев (например: 2 месяца):",
    }
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, prompts[cat])

@bot.callback_query_handler(func=lambda c: c.data.startswith("photo_"))
def cb_photos(c):
    uid = c.from_user.id
    sess = s(uid)
    action = c.data.replace("photo_", "")
    cid = c.message.chat.id
    all_ids = sess["photos"]
    selected = sess.get("selected_photos", [])
    if action == "all":
        sess["selected_photos"] = list(all_ids)
    elif action == "none":
        sess["selected_photos"] = []
    elif action == "done":
        bot.answer_callback_query(c.id)
        sess["step"] = "recipients"
        bot.send_message(cid,
            f"Выбрано скриншотов: {len(sess['selected_photos'])} шт.\n\n"
            "Выбери получателей письма:",
            reply_markup=kb_recipients(sess["selected"]))
        return
    else:
        idx = int(action)
        fid = all_ids[idx]
        if fid in selected:
            selected.remove(fid)
        else:
            selected.append(fid)
        sess["selected_photos"] = selected
    bot.answer_callback_query(c.id)
    bot.edit_message_reply_markup(cid, c.message.message_id,
                                  reply_markup=kb_photos(all_ids, sess["selected_photos"]))

@bot.callback_query_handler(func=lambda c: c.data.startswith("rcpt_"))
def cb_recipients(c):
    uid = c.from_user.id
    sess = s(uid)
    action = c.data.replace("rcpt_", "")
    cid = c.message.chat.id
    if action == "all":
        sess["selected"] = list(range(len(CONTACTS)))
    elif action == "none":
        sess["selected"] = []
    elif action == "done":
        if not sess.get("selected"):
            bot.answer_callback_query(c.id, "Выбери хотя бы одного получателя!", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        sess["email_body"] = None
        show_email_preview(uid, cid)
        return
    else:
        idx = int(action)
        if idx in sess["selected"]:
            sess["selected"].remove(idx)
        else:
            sess["selected"].append(idx)
    bot.answer_callback_query(c.id)
    bot.edit_message_reply_markup(cid, c.message.message_id,
                                  reply_markup=kb_recipients(sess["selected"]))

@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_"))
def cb_confirm(c):
    uid = c.from_user.id
    action = c.data.replace("confirm_", "")
    cid = c.message.chat.id

    if not ensure_session_restored(uid):
        bot.answer_callback_query(c.id,
            "Сессия истекла. Начни новую заявку командой /new_request",
            show_alert=True)
        return

    sess = s(uid)

    if action == "send":
        bot.answer_callback_query(c.id)
        to_list = [CONTACTS[i]["email"] for i in sess["selected"] if i < len(CONTACTS)]
        if not to_list:
            bot.send_message(cid, "Нет получателей. Выбери получателей и попробуй снова.")
            show_email_preview(uid, cid)
            return
        names = ", ".join(CONTACTS[i]["name"] for i in sess["selected"] if i < len(CONTACTS))
        photo_paths = []
        selected_photos = sess.get("selected_photos", [])
        if selected_photos:
            bot.send_message(cid, f"Скачиваю {len(selected_photos)} скриншот(ов)...")
            for fid in selected_photos:
                path = download_photo(fid)
                if path:
                    photo_paths.append(path)
        save_draft(uid, sess)
        subject = sess.get("subject") or f"Заявка №{sess['number']} — {sess['task_name']}"
        sess["subject"] = subject
        body = sess.get("email_body") or build_email(sess)
        sess["email_body"] = body
        bot.send_message(cid, "Отправляю письмо...")
        ok, err = send_email(subject, body, to_list, photo_paths)
        if ok:
            save_request(sess)
            attach_info = f" + {len(photo_paths)} скриншот(ов)" if photo_paths else ""
            bot.send_message(cid,
                f"Письмо отправлено{attach_info}!\n\n"
                f"Получатели: {names}\n"
                f"Заявка {sess['number']} закрыта.")
            sessions[uid] = {"step": None, "photos": db_get_photos(uid),
                             "selected_photos": [], "selected": []}
        else:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="draft_retry"))
            kb.add(types.InlineKeyboardButton(text="✏️ Вернуться к письму", callback_data="draft_resume"))
            bot.send_message(cid,
                f"Ошибка отправки письма:\n{err}\n\nДанные сохранены — попробуй снова:",
                reply_markup=kb)
            sess["step"] = "confirm"

    elif action == "edit_text":
        bot.answer_callback_query(c.id)
        sess["step"] = "edit_email_text"
        bot.send_message(cid,
            "Отправь новый текст письма целиком.\n"
            "Скопируй черновик выше, отредактируй и пришли сюда:")

    elif action == "adjust":
        bot.answer_callback_query(c.id)
        sess["step"] = "adjust_cost"
        bot.send_message(cid, "Введи новую итоговую стоимость (только цифры, в рублях):")

    elif action == "recipients":
        bot.answer_callback_query(c.id)
        sess["step"] = "recipients"
        bot.send_message(cid, "Выбери получателей:", reply_markup=kb_recipients(sess["selected"]))

    elif action == "photos":
        bot.answer_callback_query(c.id)
        if sess["photos"]:
            sess["step"] = "select_photos"
            _show_photo_selection(uid, cid)
        else:
            bot.send_message(cid, "Нет сохранённых скриншотов.")

    elif action == "back":
        bot.answer_callback_query(c.id)
        show_email_preview(uid, cid)

    elif action == "cancel":
        bot.answer_callback_query(c.id)
        sessions[uid] = {"step": None, "photos": db_get_photos(uid),
                         "selected_photos": [], "selected": []}
        bot.send_message(cid, "Заявка отменена.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("draft_"))
def cb_draft(c):
    uid = c.from_user.id
    action = c.data.replace("draft_", "")
    cid = c.message.chat.id
    bot.answer_callback_query(c.id)

    if action in ("resume", "retry"):
        draft = get_draft()
        if not draft:
            bot.send_message(cid, "Черновик не найден.")
            return
        sess = s(uid)
        sess.update(draft)
        sess["step"] = "confirm"
        sess["is_flat"] = draft.get("rate") is None
        sess["subject"] = f"Заявка №{draft['number']} — {draft['task_name']}"
        sess["photos"] = db_get_photos(uid)
        sess.setdefault("selected_photos", [])
        if action == "retry":
            to_list = [CONTACTS[i]["email"] for i in sess["selected"] if i < len(CONTACTS)]
            if not to_list:
                show_email_preview(uid, cid)
                return
            names = ", ".join(CONTACTS[i]["name"] for i in sess["selected"] if i < len(CONTACTS))
            body = sess.get("email_body") or build_email(sess)
            sess["email_body"] = body
            bot.send_message(cid, "Отправляю...")
            ok = send_email(sess["subject"], body, to_list)
            if ok:
                save_request(sess)
                bot.send_message(cid, f"Письмо отправлено!\nПолучатели: {names}\nЗаявка {sess['number']} закрыта.")
                sessions[uid] = {"step": None, "photos": db_get_photos(uid), "selected_photos": [], "selected": []}
            else:
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="draft_retry"))
                bot.send_message(cid, "Снова ошибка. Попробуй ещё раз:", reply_markup=kb)
        else:
            bot.send_message(cid, "Заявка восстановлена:")
            show_email_preview(uid, cid)

    elif action == "discard":
        with db() as conn:
            conn.execute("DELETE FROM requests WHERE status='draft'")
        bot.send_message(cid, "Черновик удалён.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("editprice_"))
def cb_editprice(c):
    uid = c.from_user.id
    sess = s(uid)
    cat = c.data.replace("editprice_", "")
    sess["step"] = f"editprice_{cat}"
    prices = get_prices()
    p = prices[cat]
    current = p["rate"] if p["rate"] else p["flat"]
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id,
        f"Редактирование: {p['label']}\n"
        f"Текущее значение: {current:,} р.\n\n"
        f"Введи новое значение (только цифры):".replace(",", " "))

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    bot.set_my_commands([
        telebot.types.BotCommand("/new_request", "Создать новую заявку"),
        telebot.types.BotCommand("/prices",      "Просмотр и изменение тарифов"),
        telebot.types.BotCommand("/help",        "Помощь"),
    ])
    print("Bot running: @applications_struktura_bot")
    bot.infinity_polling()
