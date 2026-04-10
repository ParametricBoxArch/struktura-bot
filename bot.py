import telebot
import sqlite3
import re
import os
import base64
import requests
from telebot import types
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "8630425742:AAHcnb8KBZt8rScUPWGjXde6pyHkjnAO8Sg")
EMAIL_FROM = "info@parametricbox.com"
SENDER_NAME = "Parametric Box"
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
_data_dir = "/data" if os.path.isdir("/data") else "."
DB = os.path.join(_data_dir, "struktura.db")
PHOTOS_DIR = os.path.join(_data_dir, "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

CONTACTS = [
    {"name": "Степаненко Андрей",  "title": "Директор проектов",    "email": "andrey.stepanenko@sk-struktura.ru"},
    {"name": "Апанасевич Алексей", "title": "Руководитель проекта", "email": "apanasevich@sk-struktura.ru"},
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
            user_id INTEGER DEFAULT 0,
            number TEXT, date TEXT, task_name TEXT, task_description TEXT,
            client_name TEXT, client_tg TEXT,
            duration_label TEXT, duration_detail TEXT,
            hours REAL, rate INTEGER, total INTEGER,
            email_body TEXT, recipients TEXT, status TEXT,
            created_at TEXT)""")
        # Миграции
        for col, typedef in [
            ("user_id", "INTEGER DEFAULT 0"),
            ("task_description", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE requests ADD COLUMN {col} {typedef}")
            except Exception:
                pass
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

def _pack_recipients(sess):
    parts = [str(i) for i in sess.get("selected", [])]
    parts += [f"email:{e}" for e in sess.get("custom_recipients", [])]
    return ",".join(parts)

def _unpack_recipients(recipients_str):
    selected, custom = [], []
    for part in (recipients_str or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("email:"):
            custom.append(part[6:])
        else:
            try:
                selected.append(int(part))
            except ValueError:
                pass
    return selected, custom

def save_draft(user_id, sess):
    with db() as c:
        existing = c.execute(
            "SELECT id FROM requests WHERE status='draft' AND number=? AND user_id=?",
            (sess["number"], user_id)).fetchone()
        recipients_str = _pack_recipients(sess)
        if existing:
            c.execute("""UPDATE requests SET task_name=?,task_description=?,client_name=?,client_tg=?,
                duration_label=?,duration_detail=?,hours=?,rate=?,total=?,
                email_body=?,recipients=?,created_at=? WHERE id=?""", (
                sess.get("task_name", ""), sess.get("task_description", ""),
                sess.get("client_name", ""), sess.get("client_tg", ""),
                sess.get("duration_label", ""), sess.get("duration_detail", ""),
                sess.get("hours"), sess.get("rate"), sess.get("total", 0),
                sess.get("email_body", ""), recipients_str,
                datetime.now().isoformat(), existing[0]))
        else:
            c.execute("""INSERT INTO requests
                (user_id,number,date,task_name,task_description,client_name,client_tg,
                 duration_label,duration_detail,hours,rate,total,email_body,recipients,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                user_id, sess["number"],
                sess.get("date", datetime.now().strftime("%d.%m.%Y")),
                sess.get("task_name", ""), sess.get("task_description", ""),
                sess.get("client_name", ""), sess.get("client_tg", ""),
                sess.get("duration_label", ""), sess.get("duration_detail", ""),
                sess.get("hours"), sess.get("rate"), sess.get("total", 0),
                sess.get("email_body", ""), recipients_str,
                "draft", datetime.now().isoformat()))

def save_request(sess):
    with db() as c:
        c.execute("UPDATE requests SET status='sent' WHERE number=?", (sess["number"],))
        if c.execute("SELECT changes()").fetchone()[0] == 0:
            c.execute("""INSERT INTO requests
                (number,date,task_name,task_description,client_name,client_tg,duration_label,
                 duration_detail,hours,rate,total,email_body,recipients,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                sess["number"], sess["date"], sess["task_name"], sess.get("task_description",""),
                sess["client_name"], sess["client_tg"], sess.get("duration_label",""),
                sess.get("duration_detail",""), sess.get("hours"), sess.get("rate"), sess["total"],
                sess.get("email_body",""), _pack_recipients(sess), "sent", datetime.now().isoformat()))

def get_drafts(user_id):
    with db() as c:
        rows = c.execute("""SELECT id,number,date,task_name,task_description,client_name,client_tg,
            duration_label,duration_detail,hours,rate,total,email_body,recipients
            FROM requests WHERE status='draft' AND user_id=?
            ORDER BY id DESC""", (user_id,)).fetchall()
        drafts = []
        for row in rows:
            keys = ["id","number","date","task_name","task_description","client_name","client_tg",
                    "duration_label","duration_detail","hours","rate","total","email_body","recipients"]
            d = dict(zip(keys, row))
            d["selected"], d["custom_recipients"] = _unpack_recipients(d["recipients"])
            drafts.append(d)
        return drafts

def get_draft(user_id):
    drafts = get_drafts(user_id)
    return drafts[0] if drafts else None

# ── Session helpers ───────────────────────────────────────────────────────────

def s(uid):
    if uid not in sessions:
        sessions[uid] = {"step": None, "photos": db_get_photos(uid),
                         "selected_photos": [], "selected": [], "custom_recipients": []}
    return sessions[uid]

def ensure_session_restored(uid):
    sess = s(uid)
    if sess.get("number"):
        return True
    draft = get_draft(uid)
    if not draft:
        return False
    _load_draft_into_session(sess, draft, uid)
    return True

def _load_draft_into_session(sess, draft, uid):
    sess.update(draft)
    sess["step"] = "confirm"
    sess["is_flat"] = draft.get("rate") is None
    sess["subject"] = f"Заявка №{draft['number']} — {draft['task_name']}"
    sess["photos"] = db_get_photos(uid)
    sess.setdefault("selected_photos", [])
    sess.setdefault("custom_recipients", draft.get("custom_recipients", []))
    sess.setdefault("task_description", draft.get("task_description", ""))

# ── Cost ─────────────────────────────────────────────────────────────────────

def parse_duration(text, prices):
    """
    Разбирает свободный ввод типа '8 часов', '2 дня', '3 недели', '1 месяц'.
    Возвращает (hours, rate, total, is_flat, label) или все None при ошибке.
    Правила:
      - часы → 8 000 р/ч (тариф 'week')
      - дни < 5 рабочих дней → 8 000 р/ч; дни >= 5 → 5 000 р/ч (тариф 'weeks')
      - недели → 5 000 р/ч (тариф 'weeks')
      - месяцы → фиксированная ставка (тариф 'month'/'months')
    """
    tl = text.lower().strip()
    nums = re.findall(r'\d+(?:[.,]\d+)?', tl)
    n = float(nums[0].replace(",", ".")) if nums else 1.0

    if "час" in tl:
        h = n
        rate = prices["week"]["rate"]
        total = round(h * rate)
        label = f"{n:.0f} ч"
        return h, rate, total, False, label

    elif any(w in tl for w in ["день", "дня", "дней", "дн."]):
        h = n * 8
        if h < 40:  # меньше 5 рабочих дней = меньше недели
            rate = prices["week"]["rate"]
        else:
            rate = prices["weeks"]["rate"]
        total = round(h * rate)
        label = f"{n:.0f} дн."
        return h, rate, total, False, label

    elif "недел" in tl:
        h = n * 5 * 8  # 5 рабочих дней × 8 часов
        rate = prices["weeks"]["rate"]
        total = round(h * rate)
        label = f"{n:.0f} нед."
        return h, rate, total, False, label

    elif "месяц" in tl or "мес." in tl:
        if n <= 1:
            total = prices["month"]["flat"]
        else:
            total = round(n * prices["months"]["flat"])
        label = f"{n:.0f} мес."
        return None, None, total, True, label

    else:
        return None, None, None, None, None

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
    desc_block = ""
    if s.get("task_description"):
        desc_block = f"  - Описание задачи: {s['task_description']}\n"
    return (
        f"Уважаемые коллеги,\n\n"
        f"настоящим уведомляем вас о поступлении новой заявки на выполнение работ.\n\n"
        f"РЕКВИЗИТЫ ЗАЯВКИ\n"
        f"================\n"
        f"  - Номер запроса: {s['number']}\n"
        f"  - Дата поступления: {s['date']}\n"
        f"  - Название задачи: {s['task_name']}\n"
        f"{desc_block}"
        f"  - Заказчик: {s['client_name']}\n"
        f"  - Telegram: {s['client_tg']}\n\n"
        f"СТОИМОСТЬ РАБОТ\n"
        f"===============\n"
        f"  - Срок: {duration_line}\n"
        f"  - Стоимость: {fmt(s['total'])}\n\n"
        f"Просим подтвердить получение данной заявки и готовность приступить "
        f"к её выполнению в указанные сроки.\n\n"
        f"С уважением,\n"
        f"Parametric Box"
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
    try:
        attachments = []
        if photo_paths:
            for i, path in enumerate(photo_paths, 1):
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        content = base64.b64encode(f.read()).decode()
                    ext = path.split(".")[-1]
                    attachments.append({"name": f"screenshot_{i}.{ext}", "content": content})

        data = {
            "sender": {"name": SENDER_NAME, "email": EMAIL_FROM},
            "to": [{"email": e} for e in to_list],
            "subject": subject,
            "textContent": body,
        }
        if attachments:
            data["attachment"] = attachments

        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json=data,
            timeout=30,
        )

        if resp.status_code in (200, 201, 202):
            print(f"Email sent OK via Brevo to {to_list}")
            return True, None

        print(f"Brevo error {resp.status_code}: {resp.text}")
        return False, f"Brevo {resp.status_code}: {resp.text}"

    except Exception as e:
        print(f"send_email exception: {e}")
        return False, str(e)

# ── Keyboards ────────────────────────────────────────────────────────────────

def kb_recipients(selected, custom_recipients=None):
    kb = types.InlineKeyboardMarkup()
    for i, c in enumerate(CONTACTS):
        mark = "✅" if i in selected else "☐"
        kb.add(types.InlineKeyboardButton(
            text=f"{mark}  {c['name']} — {c['title']}",
            callback_data=f"rcpt_{i}"))
    if custom_recipients:
        for i, email in enumerate(custom_recipients):
            kb.add(types.InlineKeyboardButton(
                text=f"✅  {email}  ❌ удалить",
                callback_data=f"rcpt_del_{i}"))
    kb.row(
        types.InlineKeyboardButton(text="Выбрать всех", callback_data="rcpt_all"),
        types.InlineKeyboardButton(text="Снять всех",   callback_data="rcpt_none"))
    kb.add(types.InlineKeyboardButton(text="✉️ Добавить свой email", callback_data="rcpt_add"))
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
    kb.row(types.KeyboardButton("📝 Новая заявка"), types.KeyboardButton("📋 Черновики"))
    kb.row(types.KeyboardButton("💰 Тарифы"), types.KeyboardButton("❓ Помощь"))
    return kb

def kb_drafts(drafts):
    kb = types.InlineKeyboardMarkup()
    for d in drafts:
        task = d["task_name"] or "Без названия"
        client = d["client_name"] or ""
        label = f"📄 {task}"
        if client:
            label += f" — {client}"
        label += f"  ({d['date']})"
        kb.row(
            types.InlineKeyboardButton(text=label,        callback_data=f"draft_open_{d['id']}"),
            types.InlineKeyboardButton(text="🗑",         callback_data=f"draft_del_{d['id']}"))
    return kb

# ── Show email preview ────────────────────────────────────────────────────────

def show_email_preview(uid, cid):
    sess = s(uid)
    body = sess.get("email_body") or build_email(sess)
    sess["email_body"] = body
    subject = f"Заявка №{sess['number']} — {sess['task_name']}"
    sess["subject"] = subject

    rcpt_lines_list = [
        f"  • {CONTACTS[i]['name']} ({CONTACTS[i]['title']})"
        for i in sess["selected"] if i < len(CONTACTS)
    ]
    rcpt_lines_list += [f"  • {e}" for e in sess.get("custom_recipients", [])]
    rcpt_lines = "\n".join(rcpt_lines_list)

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
                     "selected_photos": [], "selected": [], "custom_recipients": []}
    bot.send_message(m.chat.id,
        "Привет, Алёна!\n\n"
        "Я бот управления заявками Parametric Box → Struktura.\n\n"
        "Используй кнопки внизу или меню / слева от поля ввода.",
        reply_markup=main_keyboard())

    drafts = get_drafts(uid)
    if drafts:
        bot.send_message(m.chat.id,
            f"У тебя {len(drafts)} незакрытых заявок — нажми 📋 Черновики чтобы продолжить любую.")

@bot.message_handler(commands=["drafts"])
@bot.message_handler(func=lambda m: m.text == "📋 Черновики")
def cmd_drafts(m):
    uid = m.from_user.id
    drafts = get_drafts(uid)
    if not drafts:
        bot.send_message(m.chat.id, "Нет сохранённых черновиков.")
        return
    bot.send_message(m.chat.id,
        f"Твои черновики ({len(drafts)}):\n\nНажми чтобы продолжить, 🗑 чтобы удалить:",
        reply_markup=kb_drafts(drafts))

@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda m: m.text == "❓ Помощь")
def cmd_help(m):
    bot.send_message(m.chat.id,
        "Как пользоваться:\n\n"
        "1. Присылай сообщения и скриншоты — бот их запоминает\n"
        "2. /new_request — начать оформление заявки\n"
        "3. Бот спросит данные (название, описание, заказчик, срок)\n"
        "4. Срок пиши так: 8 часов / 2 дня / 3 недели / 1 месяц\n"
        "5. Выберешь какие скриншоты приложить к письму\n"
        "6. Выберешь получателей\n"
        "7. Увидишь черновик — можно редактировать текст, стоимость, получателей\n"
        "8. Подтвердишь — письмо уйдёт с вложениями\n\n"
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

@bot.message_handler(commands=["debug"])
def cmd_debug(m):
    uid = m.from_user.id
    photos = db_get_photos(uid)
    bot.send_message(m.chat.id,
        f"DB файл: {DB}\n"
        f"Фото в базе: {len(photos)} шт.\n"
        f"Фото в сессии: {len(s(uid).get('photos', []))} шт.")

@bot.message_handler(commands=["new_request"])
@bot.message_handler(func=lambda m: m.text == "📝 Новая заявка")
def cmd_new_request(m):
    uid = m.from_user.id
    # Сохранить текущую незавершённую заявку как черновик
    if uid in sessions:
        old = sessions[uid]
        if old.get("task_name") and old.get("number"):
            save_draft(uid, old)
            bot.send_message(m.chat.id, f"Предыдущая заявка «{old['task_name']}» сохранена в черновиках.")
    all_photos = db_get_photos(uid)
    new_number = next_number()
    new_date = datetime.now().strftime("%d.%m.%Y")
    sessions[uid] = {
        "step": "task_name", "selected": [], "photos": all_photos,
        "selected_photos": [], "custom_recipients": [],
        "number": new_number, "date": new_date,
        "task_description": "",
    }
    bot.send_message(m.chat.id, "Новая заявка\n\nШаг 1/5: Введи название задачи:")

# ── Main message handler ──────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "document"])
def handle_message(m):
    uid = m.from_user.id
    sess = s(uid)
    step = sess.get("step")
    cid = m.chat.id
    text = m.text or m.caption or ""

    # Файл-изображение (PNG, JPG и т.д. отправленные без сжатия)
    if m.document and m.document.mime_type and m.document.mime_type.startswith("image/"):
        file_id = m.document.file_id
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
        elif step not in (None,):
            bot.send_message(cid, f"Скриншот сохранён (всего: {count} шт.)")
        else:
            bot.send_message(cid,
                f"Скриншот сохранён. Всего накоплено: {count} шт.\n"
                "Нажми 📝 Новая заявка чтобы оформить.")
        return

    if m.document:
        return

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
        save_draft(uid, sess)
        show_email_preview(uid, cid)
        return

    if step == "edit_email_text":
        sess["email_body"] = text
        save_draft(uid, sess)
        bot.send_message(cid, "Текст письма обновлён.")
        show_email_preview(uid, cid)
        return

    if step == "add_recipient":
        email = text.strip()
        if "@" not in email or "." not in email:
            bot.send_message(cid, "Неверный формат. Введи корректный email, например: name@company.ru")
            return
        sess.setdefault("custom_recipients", []).append(email)
        sess["step"] = "recipients"
        bot.send_message(cid,
            f"Email {email} добавлен.\n\nВыбери получателей:",
            reply_markup=kb_recipients(sess["selected"], sess.get("custom_recipients", [])))
        return

    if step == "task_name":
        if not text:
            bot.send_message(cid, "Введи название задачи текстом:")
            return
        sess["task_name"] = text
        sess["step"] = "task_description"
        save_draft(uid, sess)
        bot.send_message(cid, "Шаг 2/5: Введи описание задачи (кратко, что нужно сделать):")

    elif step == "task_description":
        sess["task_description"] = text
        sess["step"] = "client_name"
        save_draft(uid, sess)
        bot.send_message(cid, "Шаг 3/5: Имя заказчика (от кого поступила заявка)?")

    elif step == "client_name":
        sess["client_name"] = text
        sess["step"] = "client_tg"
        save_draft(uid, sess)
        bot.send_message(cid, "Шаг 4/5: Telegram заказчика (например @username):")

    elif step == "client_tg":
        sess["client_tg"] = text
        sess["step"] = "duration"
        save_draft(uid, sess)
        prices = get_prices()
        rates_info = "\n".join([
            f"  • {p['label']}: {p['rate']:,} р/ч".replace(",", " ") if p["rate"]
            else f"  • {p['label']}: {p['flat']:,} р фикс.".replace(",", " ")
            for p in prices.values()
        ])
        bot.send_message(cid,
            "Шаг 5/5: Введи срок выполнения.\n\n"
            "Примеры: 8 часов / 2 дня / 3 недели / 1 месяц\n\n"
            "Действующие ставки:\n" + rates_info)

    elif step == "duration":
        prices = get_prices()
        hours, rate, total, is_flat, label = parse_duration(text, prices)

        if total is None:
            bot.send_message(cid,
                "Не удалось распознать срок. Напиши так:\n"
                "  • 8 часов\n"
                "  • 2 дня\n"
                "  • 3 недели\n"
                "  • 1 месяц")
            return

        sess["duration_detail"] = text
        sess.update(hours=hours, rate=rate, total=total, is_flat=is_flat, duration_label=label)

        if is_flat:
            cost_txt = f"Расчёт:\n• Срок: {text}\n• Итого: {fmt(total)}"
        else:
            cost_txt = (f"Расчёт:\n• Срок: {text}\n"
                        f"• Часов: {hours:.0f} ч\n• Ставка: {fmt(rate)}/ч\n• Итого: {fmt(total)}")
        bot.send_message(cid, cost_txt)

        save_draft(uid, sess)

        if sess["photos"]:
            sess["step"] = "select_photos"
            sess["selected_photos"] = []
            _show_photo_selection(uid, cid)
        else:
            sess["step"] = "recipients"
            bot.send_message(cid, "Выбери получателей письма:",
                             reply_markup=kb_recipients(sess["selected"], sess.get("custom_recipients", [])))

def _show_photo_selection(uid, cid):
    sess = s(uid)
    all_ids = sess["photos"]
    selected = sess["selected_photos"]
    for i, fid in enumerate(all_ids, 1):
        try:
            bot.send_photo(cid, fid, caption=f"Скриншот {i}")
        except Exception:
            try:
                bot.send_document(cid, fid, caption=f"Скриншот {i}")
            except Exception:
                pass
    bot.send_message(cid,
        f"У тебя {len(all_ids)} скриншот(ов).\n"
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
    if target == "duration":
        sess["step"] = "duration"
        bot.send_message(cid,
            "← Назад\n\nВведи срок выполнения:\n"
            "  • 8 часов\n  • 2 дня\n  • 3 недели\n  • 1 месяц")
    elif target == "photos":
        if sess["photos"]:
            sess["step"] = "select_photos"
            _show_photo_selection(uid, cid)
        else:
            sess["step"] = "duration"
            bot.send_message(cid,
                "← Назад\n\nВведи срок выполнения:\n"
                "  • 8 часов\n  • 2 дня\n  • 3 недели\n  • 1 месяц")

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
            reply_markup=kb_recipients(sess["selected"], sess.get("custom_recipients", [])))
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
    elif action == "add":
        bot.answer_callback_query(c.id)
        sess["step"] = "add_recipient"
        bot.send_message(cid, "Введи email получателя:")
        return
    elif action == "done":
        if not sess.get("selected") and not sess.get("custom_recipients"):
            bot.answer_callback_query(c.id, "Выбери хотя бы одного получателя!", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        sess["email_body"] = None
        save_draft(uid, sess)
        show_email_preview(uid, cid)
        return
    elif action.startswith("del_"):
        idx = int(action.replace("del_", ""))
        custom = sess.get("custom_recipients", [])
        if 0 <= idx < len(custom):
            custom.pop(idx)
        sess["custom_recipients"] = custom
    else:
        idx = int(action)
        if idx in sess["selected"]:
            sess["selected"].remove(idx)
        else:
            sess["selected"].append(idx)
    bot.answer_callback_query(c.id)
    bot.edit_message_reply_markup(cid, c.message.message_id,
                                  reply_markup=kb_recipients(sess["selected"], sess.get("custom_recipients", [])))

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
        to_list += sess.get("custom_recipients", [])
        if not to_list:
            bot.send_message(cid, "Нет получателей. Выбери получателей и попробуй снова.")
            show_email_preview(uid, cid)
            return
        names_parts = [CONTACTS[i]["name"] for i in sess["selected"] if i < len(CONTACTS)]
        names_parts += sess.get("custom_recipients", [])
        names = ", ".join(names_parts)
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
                             "selected_photos": [], "selected": [], "custom_recipients": []}
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
        bot.send_message(cid, "Выбери получателей:",
                         reply_markup=kb_recipients(sess["selected"], sess.get("custom_recipients", [])))

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
                         "selected_photos": [], "selected": [], "custom_recipients": []}
        bot.send_message(cid, "Заявка отменена.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("draft_"))
def cb_draft(c):
    uid = c.from_user.id
    action = c.data.replace("draft_", "")
    cid = c.message.chat.id
    bot.answer_callback_query(c.id)

    if action.startswith("open_"):
        draft_id = int(action.replace("open_", ""))
        with db() as conn:
            row = conn.execute("""SELECT id,number,date,task_name,task_description,client_name,client_tg,
                duration_label,duration_detail,hours,rate,total,email_body,recipients
                FROM requests WHERE id=? AND status='draft'""", (draft_id,)).fetchone()
        if not row:
            bot.send_message(cid, "Черновик не найден.")
            return
        keys = ["id","number","date","task_name","task_description","client_name","client_tg",
                "duration_label","duration_detail","hours","rate","total","email_body","recipients"]
        draft = dict(zip(keys, row))
        draft["selected"], draft["custom_recipients"] = _unpack_recipients(draft["recipients"])
        sess = s(uid)
        _load_draft_into_session(sess, draft, uid)
        bot.send_message(cid, f"Заявка загружена: {draft['task_name']}")
        show_email_preview(uid, cid)

    elif action.startswith("del_"):
        draft_id = int(action.replace("del_", ""))
        msg_id = c.message.message_id
        with db() as conn:
            conn.execute("DELETE FROM requests WHERE id=? AND status='draft'", (draft_id,))
        drafts = get_drafts(uid)
        if drafts:
            try:
                bot.edit_message_reply_markup(cid, msg_id, reply_markup=kb_drafts(drafts))
            except Exception:
                bot.send_message(cid, "Черновик удалён.", reply_markup=kb_drafts(drafts))
        else:
            bot.send_message(cid, "Черновик удалён. Черновиков больше нет.")

    elif action in ("resume", "retry"):
        draft = get_draft(uid)
        if not draft:
            bot.send_message(cid, "Черновик не найден.")
            return
        sess = s(uid)
        _load_draft_into_session(sess, draft, uid)
        if action == "retry":
            to_list = [CONTACTS[i]["email"] for i in sess["selected"] if i < len(CONTACTS)]
            to_list += sess.get("custom_recipients", [])
            if not to_list:
                show_email_preview(uid, cid)
                return
            names = ", ".join(
                [CONTACTS[i]["name"] for i in sess["selected"] if i < len(CONTACTS)] +
                sess.get("custom_recipients", []))
            body = sess.get("email_body") or build_email(sess)
            sess["email_body"] = body
            bot.send_message(cid, "Отправляю...")
            ok, err = send_email(sess["subject"], body, to_list)
            if ok:
                save_request(sess)
                bot.send_message(cid, f"Письмо отправлено!\nПолучатели: {names}\nЗаявка {sess['number']} закрыта.")
                sessions[uid] = {"step": None, "photos": db_get_photos(uid),
                                 "selected_photos": [], "selected": [], "custom_recipients": []}
            else:
                kb2 = types.InlineKeyboardMarkup()
                kb2.add(types.InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="draft_retry"))
                bot.send_message(cid, f"Ошибка: {err}", reply_markup=kb2)
        else:
            bot.send_message(cid, "Заявка восстановлена:")
            show_email_preview(uid, cid)

    elif action == "discard":
        with db() as conn:
            conn.execute("DELETE FROM requests WHERE status='draft' AND user_id=?", (uid,))
        bot.send_message(cid, "Все черновики удалены.")

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
        telebot.types.BotCommand("/drafts",      "Мои черновики"),
        telebot.types.BotCommand("/prices",      "Просмотр и изменение тарифов"),
        telebot.types.BotCommand("/help",        "Помощь"),
    ])
    print("Bot running: @applications_struktura_bot")
    bot.infinity_polling()
