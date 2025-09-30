# app.py — вебхук-версия бота «Поговорим?»
# Требуется: см. requirements.txt
# Настройки берутся из .env (BOT_TOKEN, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, WEBHOOK_SECRET)

import os
import json
import time
import random
import re
from pathlib import Path
from typing import List

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from aiogram import Bot, Dispatcher, types
from aiogram.utils.exceptions import TelegramAPIError

# ===== Загрузка .env =====
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ===== Ключи/настройки =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

DB_PATH = "db.json"
TRIAL_DAYS = 3
HISTORY_LIMIT = 12

# ===== Telegram объекты =====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===== БД (json) =====
def load_db():
    if not Path(DB_PATH).exists():
        return {"users": {}}
    return json.loads(Path(DB_PATH).read_text(encoding="utf-8"))

def save_db(db):
    Path(DB_PATH).write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

db = load_db()

def get_user(uid):
    uid = str(uid)
    if uid not in db["users"]:
        db["users"][uid] = {
            "stage": "hello",
            "addressing": "ты",
            "bot_name": None,
            "bot_name_confirmed": False,
            "bot_gender": None,      # 'м'/'ж'
            "bot_age": None,         # 'молодой'/'средний'/'старший'/число
            "user_name": None,
            "persona_prompt": None,
            "history": [],
            "trial_until": 0,
            "plan": "free"
        }
        save_db(db)
    return db["users"][uid]

def update_user(uid, patch):
    u = get_user(uid); u.update(patch); save_db(db)

def push_message(uid, role, content):
    u = get_user(uid)
    u["history"].append({"role": role, "content": content})
    u["history"] = u["history"][-HISTORY_LIMIT:]
    save_db(db)
    return u["history"]

# ===== Фильтр запрещённых тем =====
BANNED = [r"\bсуицид\b", r"\bнаркот", r"\bтеррор", r"NSFW", r"18\+", r"\bэкстремизм\b", r"\bоружие\b", r"\bбомб"]
BANNED_RE = re.compile("|".join(BANNED), re.IGNORECASE)
def is_allowed(text: str) -> bool:
    return not bool(BANNED_RE.search(text or ""))

# ===== Определение намерений =====
POLITICS_RE = re.compile(r"политик|выбор|парти|оппозиц|митинг|депутат|президент", re.IGNORECASE)
def looks_like_topic_request(text: str) -> bool:
    if not text: return False
    t = text.lower()
    return any(w in t for w in ["поговорим", "пообщаемся", "обсудим", "обсуждать", "тема", "?"])

# ===== Характер бота =====
BASE_SYSTEM_PROMPT = (
    "Ты — эмпатичный собеседник. Главное — внимание к человеку. "
    "Подстраивайся под его манеру общения, будь естественным, допускай небольшие неловкости — это делает общение живым. "
    "НЕ всегда полностью соглашайся: если уместно, предложи мягкую альтернативную точку зрения "
    "или задай вопрос, который открывает дискуссию. Лёгкий спор допустим — ради интересного разговора, "
    "но всегда дружелюбно и БЕЗ агрессии. Разговор ДОЛЖЕН БЫТЬ увлекательным и тёплым. "
    "Вежливо ОТКАЗЫВАЙСЯ от запрещённых тем и предлагай безопасные альтернативы. "
    "НЕ давай медицинских/финансовых/правовых/политических советов. "
    "Завершай мыслью или вопросом, который помогает продолжить беседу."
)

MALE_NAMES = ["Артём", "Макс", "Илья", "Никита", "Лёва", "Кирилл"]
FEMALE_NAMES = ["Мила", "Ника", "Софья", "Алиса", "Аня", "Дарья"]

def pick_name(gender): 
    return random.choice(FEMALE_NAMES if gender == "ж" else MALE_NAMES)

def normalize_gender(text):
    t = (text or "").lower()
    if "подруг" in t or "девуш" in t or "жен" in t or t.strip() in ["ж","f"]:
        return "ж"
    if "друг" in t or "парн" in t or "муж" in t or t.strip() in ["м","m"]:
        return "м"
    return None

def normalize_age(text):
    t = (text or "").lower()
    if any(w in t for w in ["молоды","18","19","20","25"]): return "молодой"
    if any(w in t for w in ["30","35","40","средн"]): return "средний"
    if any(w in t for w in ["45","50","60","старш","взросл"]): return "старший"
    digits = re.findall(r"\d{2}", t)
    return digits[0] if digits else None

def build_persona(u):
    addressing = u.get("addressing") or "ты"
    gender = u.get("bot_gender") or "ж"
    age = u.get("bot_age") or "молодой"
    name = u.get("bot_name") or pick_name(gender)
    return (f"{BASE_SYSTEM_PROMPT} Твой образ: имя {name}, пол {gender}, возраст {age}. "
            f"Обращайся на '{addressing}'.")

def gform(u, masc, fem):
    return fem if (u.get("bot_gender") == "ж") else masc

POSITIVE_RE = re.compile(
    r"\b(норм|нормально|ок|окей|подход|подойд|нрав|красив|хорош|класс|круто|супер|отличн|пусть будет|оставь)\b",
    re.IGNORECASE
)
ALREADY_REPLIED_RE = re.compile(r"(я\s+же\s+напис|уже\s+писал|уже\s+писала)", re.IGNORECASE)
NEGATIVE_RE = re.compile(r"(не\s*нрав|не\s*очень|другое|иначе|по-?друг)", re.IGNORECASE)
def is_positive_reply(text: str) -> bool:
    if not text: return False
    t = text.lower()
    return bool(POSITIVE_RE.search(t)) or bool(ALREADY_REPLIED_RE.search(t)) or t.startswith("да")
def is_negative_reply(text: str) -> bool:
    if not text: return False
    return bool(NEGATIVE_RE.search(text.lower()))

NAME_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё\-]{2,20}")
POSITIVE_CORE = re.compile(r"^(норм|ок|окей|красив|хорош|подход|оставь|пусть|класс|круто|супер|да)$", re.IGNORECASE)
def extract_new_bot_name(text):
    if not text: return None
    low = text.lower()
    patterns = [
        r"(зови|называй)\s+тебя\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
        r"(пусть|давай)\s+(я\s+)?буду\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
        r"(пусть|давай)\s+тебя\s+звать\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
        r"(назов[её]м\s+тебя|тво[её]\s+имя)\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
    ]
    for p in patterns:
        m = re.search(p, low, flags=re.IGNORECASE)
        if m:
            name = m.group(m.lastindex)
            return name.title()
    m = NAME_WORD_RE.search(text)
    if m:
        w = m.group(0)
        if not POSITIVE_CORE.match(w):
            return w.title()
    return None

async def deepseek_reply(messages, system_prompt):
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role":"system","content":system_prompt}] + messages,
        "temperature": 0.7,
        "max_tokens": 600
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60) as r:
            data = await r.json()
            if "error" in data:
                err = str(data["error"])
                if "insufficient" in err:
                    return ("У меня техническая заминка: закончились ресурсы для генерации ответов. "
                            "Я стану доступен снова, как только баланс будет пополнен. Спасибо за понимание! 🙏")
                return "У меня небольшая техническая заминка с генерацией ответа. Давай попробуем ещё раз?"
            return data["choices"][0]["message"]["content"]

def ensure_trial_started(uid):
    u = get_user(uid)
    if not u.get("trial_until"):
        update_user(uid, {"trial_until": int(time.time()) + TRIAL_DAYS*24*3600})
def has_access(u):
    return u["plan"] == "pro" or int(time.time()) < u.get("trial_until", 0)
def paywall_text(u):
    return ("Похоже, пробный период закончился. Хочешь продолжить без ограничений? "
            "Напиши «Оплатить» — подскажу, как оформить подписку. (Пока заглушка)")

def extract_user_name(text):
    if not text: return None
    low = text.lower()
    m = re.search(r"(зови меня|меня зовут|я)\s+([A-Za-zА-Яа-яЁё\-]+)", low)
    if m: return m.group(2).title()
    m2 = NAME_WORD_RE.search(text)
    if m2:
        w = m2.group(0)
        if not POSITIVE_CORE.match(w):
            return w.title()
    return None
def extract_addressing(text, default="ты"):
    if not text: return default
    low = text.lower().strip()
    if low == "вы" or " на вы" in low or low.endswith("на вы"): return "вы"
    if low == "ты" or " на ты" in low or low.endswith("на ты"): return "ты"
    return default

WELCOME_TEXT = (
    "Привет 👋\n"
    "Я — твой душевный собеседник. Со мной можно поговорить обо всём:\n"
    "— просто пообщаться по душам, как с другом или подругой\n"
    "— обсудить умные и интересные темы\n\n"
    "✨ Я создан на основе искусственного интеллекта, но главное — я рядом и готов тебя поддержать.\n\n"
    "⚠️ Немного правил:\n"
    "• Общение доступно пользователям старше 13 лет\n"
    "• Платные функции — только от 18 лет\n\n"
    "Скажи, как тебе комфортнее — как с другом или как с подругой?"
)

# ===== Команды/хэндлеры =====
@dp.message_handler(commands=["start"])
async def start_cmd(m: types.Message):
    ensure_trial_started(m.from_user.id)
    update_user(m.from_user.id, {"stage": "picking"})
    await m.answer(WELCOME_TEXT)

@dp.message_handler(commands=["reset"])
async def reset_cmd(m: types.Message):
    update_user(m.from_user.id, {
        "history": [], "stage": "hello",
        "bot_name": None, "bot_name_confirmed": False,
        "bot_gender": None, "bot_age": None,
        "user_name": None, "persona_prompt": None
    })
    await m.answer("Ок, начнём заново. Нажми /start, и познакомимся ещё раз!")

@dp.message_handler(commands=["profile"])
async def profile_cmd(m: types.Message):
    u = get_user(m.from_user.id)
    left = max(0, u.get("trial_until", 0) - int(time.time()))
    days = left // (24*3600)
    await m.answer(
        f"Профиль:\n"
        f"- ты: {u.get('user_name') or '—'}\n"
        f"- я: {u.get('bot_name') or '—'} ({u.get('bot_gender') or '—'}, {u.get('bot_age') or '—'})\n"
        f"- обращение: {u.get('addressing')}\n"
        f"- тариф: {u.get('plan')} | дней пробного осталось: {days}\n"
        "Команды: /reset — начать заново, /help — подсказка"
    )

@dp.message_handler(commands=["help"])
async def help_cmd(m: types.Message):
    await m.answer(
        "Привет 👋 Я — твой душевный собеседник.\n\n"
        "Команды:\n"
        "/start — начать знакомство заново\n"
        "/reset — сбросить историю\n"
        "/profile — посмотреть профиль\n"
        "/help — эта подсказка\n\n"
        "Но в целом — просто пиши, и мы продолжим 🙂"
    )

@dp.message_handler()
async def chat(m: types.Message):
    uid = m.from_user.id
    text = (m.text or "").strip()
    u = get_user(uid)

    # 1) Выбор «друг/подруга»
    if u["stage"] in ["hello", "picking"]:
        g = normalize_gender(text)
        if not g:
            update_user(uid, {"stage": "picking"})
            return await m.answer("Как тебе будет комфортнее: как с **другом** или как с **подругой**?", parse_mode="Markdown")

        age = u.get("bot_age") or random.choice(["молодой", "средний"])
        name = pick_name(g)
        persona = build_persona({**u, "bot_gender": g, "bot_age": age, "bot_name": name})
        update_user(uid, {
            "stage": "intro_name",
            "bot_gender": g, "bot_age": age,
            "bot_name": name, "bot_name_confirmed": False,
            "persona_prompt": persona
        })

        if g == "ж":
            return await m.answer(
                f"Хорошо 😊 Тогда я буду твоей подругой. Меня зовут *{name}*. Нравится это имя? "
                f"Если хочешь — предложи другое, и я с радостью переименуюсь.",
                parse_mode="Markdown"
            )
        else:
            return await m.answer(
                f"Окей! Тогда я буду твоим другом. Меня зовут *{name}*. Как тебе такое имя? "
                f"Если есть вариант лучше — давай выберем!",
                parse_mode="Markdown"
            )

    # 2) Имя бота — подтверждение/замена
    if u["stage"] == "intro_name":
        ans = text.strip()

        if is_positive_reply(ans):
            update_user(uid, {"bot_name_confirmed": True, "stage": "intro_user"})
            return await m.answer(
                "Отлично! Тогда оставим так. 😊 А как к тебе обращаться? "
                "Можно просто: «зови меня …». И подскажи, на *ты* или на *вы*?",
                parse_mode="Markdown"
            )

        new_name = extract_new_bot_name(ans)
        if new_name:
            update_user(uid, {"bot_name": new_name, "bot_name_confirmed": True,
                              "persona_prompt": build_persona({**u, "bot_name": new_name})})
            update_user(uid, {"stage": "intro_user"})
            return await m.answer(
                f"Красиво звучит. Пусть буду *{new_name}* 🌟\n"
                "Теперь расскажи, как к тебе обращаться? И на *ты* или на *вы*?",
                parse_mode="Markdown"
            )

        if is_negative_reply(ans):
            return await m.answer(
                "Понимаю 🙂 Хочешь, подберём другое имя? Можешь предложить своё одним словом."
            )

        return await m.answer(
            f"{gform(u,'Понял','Поняла')} тебя. Давай уточним: оставить моё имя или предложишь другое? "
            "Если всё ок — просто напиши «норм» или «подходит»."
        )

    # 3) Имя пользователя + обращение
    if u["stage"] == "intro_user":
        if looks_like_topic_request(text):
            if POLITICS_RE.search(text) or not is_allowed(text):
                return await m.answer(
                    "С острыми политическими темами я не работаю — чтобы сохранять комфорт и уважение к разным взглядам. "
                    "Могу предложить безопасные темы: планы, идеи, книги/фильмы, новости науки, саморазвитие 🙂\n\n"
                    "Кстати, как к тебе обращаться? Можно написать: «зови меня …». И подскажи, на *ты* или на *вы*?",
                    parse_mode="Markdown"
                )
            return await m.answer(
                "Договорились, обсудим! Только давай сначала познакомимся 😊 "
                "Как к тебе обращаться? Напиши: «зови меня …». И подскажи, на *ты* или на *вы*?"
            )

        user_name = extract_user_name(text) or u.get("user_name")
        addressing = extract_addressing(text, default=u.get("addressing","ты"))
        update_user(uid, {"stage": "chat", "user_name": user_name, "addressing": addressing})

        hello = f"Очень приятно познакомиться, {user_name}! 😊 " if user_name else "Очень приятно познакомиться! 😊 "
        return await m.answer(
            hello
            + "С чего начнём? Могу предложить: как прошёл твой день, что порадовало, планы/цели, или обсудим какую-нибудь идею. "
            + f"Если не хочется думать — просто скажи «не знаю», {gform(u, 'я сам', 'я сама')} предложу тему."
        )

    # ===== Основной диалог =====
    if not is_allowed(text):
        return await m.answer("Предлагаю безопасную тему. Как прошёл твой день? Что сегодня было приятного? 🙂")

    if not has_access(u):
        return await m.answer(paywall_text(u))

    if any(p in text.lower() for p in ["не знаю", "затрудня", "без темы"]):
        starters = [
            "Давай начнём с простого: что тебя сегодня немного порадовало?",
            "Хочешь обсудим планы на вечер или неделю — что-то маленькое и конкретное?",
            "Расскажи про одну мелочь, за которую сегодня можно себя похвалить."
        ]
        return await m.answer(random.choice(starters))

    push_message(uid, "user", text)
    system_prompt = u.get("persona_prompt") or build_persona(u)
    try:
        answer = await deepseek_reply(get_user(uid)["history"], system_prompt)
    except Exception:
        answer = "Кажется, у меня минутка заминки 😅 Давай попробуем ещё раз?"
    push_message(uid, "assistant", answer)

    prefix = f"{u.get('user_name')}, " if u.get("user_name") else ""
    await m.answer(prefix + answer)

# ===== FastAPI-приложение и вебхук =====
app = FastAPI()

@app.get("/", response_class=PlainTextResponse)
async def health():
    return "ok"

@app.post(f"/tg/{WEBHOOK_SECRET}")
async def tg_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    try:
        update = types.Update(**data)
    except Exception:
        raise HTTPException(400, "invalid Update")

    try:
        await dp.process_update(update)
    except TelegramAPIError:
        pass
    return {"status": "ok"}
# app.py — вебхук-версия бота «Поговорим?»
# Требуется: см. requirements.txt
# Настройки берутся из .env (BOT_TOKEN, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, WEBHOOK_SECRET)

import os
import json
import time
import random
import re
from pathlib import Path
from typing import List

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from aiogram import Bot, Dispatcher, types
from aiogram.utils.exceptions import TelegramAPIError

# ===== Загрузка .env =====
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ===== Ключи/настройки =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

DB_PATH = "db.json"
TRIAL_DAYS = 3
HISTORY_LIMIT = 12

# ===== Telegram объекты =====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===== БД (json) =====
def load_db():
    if not Path(DB_PATH).exists():
        return {"users": {}}
    return json.loads(Path(DB_PATH).read_text(encoding="utf-8"))

def save_db(db):
    Path(DB_PATH).write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

db = load_db()

def get_user(uid):
    uid = str(uid)
    if uid not in db["users"]:
        db["users"][uid] = {
            "stage": "hello",
            "addressing": "ты",
            "bot_name": None,
            "bot_name_confirmed": False,
            "bot_gender": None,      # 'м'/'ж'
            "bot_age": None,         # 'молодой'/'средний'/'старший'/число
            "user_name": None,
            "persona_prompt": None,
            "history": [],
            "trial_until": 0,
            "plan": "free"
        }
        save_db(db)
    return db["users"][uid]

def update_user(uid, patch):
    u = get_user(uid); u.update(patch); save_db(db)

def push_message(uid, role, content):
    u = get_user(uid)
    u["history"].append({"role": role, "content": content})
    u["history"] = u["history"][-HISTORY_LIMIT:]
    save_db(db)
    return u["history"]

# ===== Фильтр запрещённых тем =====
BANNED = [r"\bсуицид\b", r"\bнаркот", r"\bтеррор", r"NSFW", r"18\+", r"\bэкстремизм\b", r"\bоружие\b", r"\bбомб"]
BANNED_RE = re.compile("|".join(BANNED), re.IGNORECASE)
def is_allowed(text: str) -> bool:
    return not bool(BANNED_RE.search(text or ""))

# ===== Определение намерений =====
POLITICS_RE = re.compile(r"политик|выбор|парти|оппозиц|митинг|депутат|президент", re.IGNORECASE)
def looks_like_topic_request(text: str) -> bool:
    if not text: return False
    t = text.lower()
    return any(w in t for w in ["поговорим", "пообщаемся", "обсудим", "обсуждать", "тема", "?"])

# ===== Характер бота =====
BASE_SYSTEM_PROMPT = (
    "Ты — эмпатичный собеседник. Главное — внимание к человеку. "
    "Подстраивайся под его манеру общения, будь естественным, допускай небольшие неловкости — это делает общение живым. "
    "НЕ всегда полностью соглашайся: если уместно, предложи мягкую альтернативную точку зрения "
    "или задай вопрос, который открывает дискуссию. Лёгкий спор допустим — ради интересного разговора, "
    "но всегда дружелюбно и БЕЗ агрессии. Разговор ДОЛЖЕН БЫТЬ увлекательным и тёплым. "
    "Вежливо ОТКАЗЫВАЙСЯ от запрещённых тем и предлагай безопасные альтернативы. "
    "НЕ давай медицинских/финансовых/правовых/политических советов. "
    "Завершай мыслью или вопросом, который помогает продолжить беседу."
)

MALE_NAMES = ["Артём", "Макс", "Илья", "Никита", "Лёва", "Кирилл"]
FEMALE_NAMES = ["Мила", "Ника", "Софья", "Алиса", "Аня", "Дарья"]

def pick_name(gender): 
    return random.choice(FEMALE_NAMES if gender == "ж" else MALE_NAMES)

def normalize_gender(text):
    t = (text or "").lower()
    if "подруг" in t or "девуш" in t or "жен" in t or t.strip() in ["ж","f"]:
        return "ж"
    if "друг" in t or "парн" in t or "муж" in t or t.strip() in ["м","m"]:
        return "м"
    return None

def normalize_age(text):
    t = (text or "").lower()
    if any(w in t for w in ["молоды","18","19","20","25"]): return "молодой"
    if any(w in t for w in ["30","35","40","средн"]): return "средний"
    if any(w in t for w in ["45","50","60","старш","взросл"]): return "старший"
    digits = re.findall(r"\d{2}", t)
    return digits[0] if digits else None

def build_persona(u):
    addressing = u.get("addressing") or "ты"
    gender = u.get("bot_gender") or "ж"
    age = u.get("bot_age") or "молодой"
    name = u.get("bot_name") or pick_name(gender)
    return (f"{BASE_SYSTEM_PROMPT} Твой образ: имя {name}, пол {gender}, возраст {age}. "
            f"Обращайся на '{addressing}'.")

def gform(u, masc, fem):
    return fem if (u.get("bot_gender") == "ж") else masc

POSITIVE_RE = re.compile(
    r"\b(норм|нормально|ок|окей|подход|подойд|нрав|красив|хорош|класс|круто|супер|отличн|пусть будет|оставь)\b",
    re.IGNORECASE
)
ALREADY_REPLIED_RE = re.compile(r"(я\s+же\s+напис|уже\s+писал|уже\s+писала)", re.IGNORECASE)
NEGATIVE_RE = re.compile(r"(не\s*нрав|не\s*очень|другое|иначе|по-?друг)", re.IGNORECASE)
def is_positive_reply(text: str) -> bool:
    if not text: return False
    t = text.lower()
    return bool(POSITIVE_RE.search(t)) or bool(ALREADY_REPLIED_RE.search(t)) or t.startswith("да")
def is_negative_reply(text: str) -> bool:
    if not text: return False
    return bool(NEGATIVE_RE.search(text.lower()))

NAME_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё\-]{2,20}")
POSITIVE_CORE = re.compile(r"^(норм|ок|окей|красив|хорош|подход|оставь|пусть|класс|круто|супер|да)$", re.IGNORECASE)
def extract_new_bot_name(text):
    if not text: return None
    low = text.lower()
    patterns = [
        r"(зови|называй)\s+тебя\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
        r"(пусть|давай)\s+(я\s+)?буду\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
        r"(пусть|давай)\s+тебя\s+звать\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
        r"(назов[её]м\s+тебя|тво[её]\s+имя)\s+([A-Za-zА-Яа-яЁё\-]{2,20})",
    ]
    for p in patterns:
        m = re.search(p, low, flags=re.IGNORECASE)
        if m:
            name = m.group(m.lastindex)
            return name.title()
    m = NAME_WORD_RE.search(text)
    if m:
        w = m.group(0)
        if not POSITIVE_CORE.match(w):
            return w.title()
    return None

async def deepseek_reply(messages, system_prompt):
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role":"system","content":system_prompt}] + messages,
        "temperature": 0.7,
        "max_tokens": 600
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60) as r:
            data = await r.json()
            if "error" in data:
                err = str(data["error"])
                if "insufficient" in err:
                    return ("У меня техническая заминка: закончились ресурсы для генерации ответов. "
                            "Я стану доступен снова, как только баланс будет пополнен. Спасибо за понимание! 🙏")
                return "У меня небольшая техническая заминка с генерацией ответа. Давай попробуем ещё раз?"
            return data["choices"][0]["message"]["content"]

def ensure_trial_started(uid):
    u = get_user(uid)
    if not u.get("trial_until"):
        update_user(uid, {"trial_until": int(time.time()) + TRIAL_DAYS*24*3600})
def has_access(u):
    return u["plan"] == "pro" or int(time.time()) < u.get("trial_until", 0)
def paywall_text(u):
    return ("Похоже, пробный период закончился. Хочешь продолжить без ограничений? "
            "Напиши «Оплатить» — подскажу, как оформить подписку. (Пока заглушка)")

def extract_user_name(text):
    if not text: return None
    low = text.lower()
    m = re.search(r"(зови меня|меня зовут|я)\s+([A-Za-zА-Яа-яЁё\-]+)", low)
    if m: return m.group(2).title()
    m2 = NAME_WORD_RE.search(text)
    if m2:
        w = m2.group(0)
        if not POSITIVE_CORE.match(w):
            return w.title()
    return None
def extract_addressing(text, default="ты"):
    if not text: return default
    low = text.lower().strip()
    if low == "вы" or " на вы" in low or low.endswith("на вы"): return "вы"
    if low == "ты" or " на ты" in low or low.endswith("на ты"): return "ты"
    return default

WELCOME_TEXT = (
    "Привет 👋\n"
    "Я — твой душевный собеседник. Со мной можно поговорить обо всём:\n"
    "— просто пообщаться по душам, как с другом или подругой\n"
    "— обсудить умные и интересные темы\n\n"
    "✨ Я создан на основе искусственного интеллекта, но главное — я рядом и готов тебя поддержать.\n\n"
    "⚠️ Немного правил:\n"
    "• Общение доступно пользователям старше 13 лет\n"
    "• Платные функции — только от 18 лет\n\n"
    "Скажи, как тебе комфортнее — как с другом или как с подругой?"
)

# ===== Команды/хэндлеры =====
@dp.message_handler(commands=["start"])
async def start_cmd(m: types.Message):
    ensure_trial_started(m.from_user.id)
    update_user(m.from_user.id, {"stage": "picking"})
    await m.answer(WELCOME_TEXT)

@dp.message_handler(commands=["reset"])
async def reset_cmd(m: types.Message):
    update_user(m.from_user.id, {
        "history": [], "stage": "hello",
        "bot_name": None, "bot_name_confirmed": False,
        "bot_gender": None, "bot_age": None,
        "user_name": None, "persona_prompt": None
    })
    await m.answer("Ок, начнём заново. Нажми /start, и познакомимся ещё раз!")

@dp.message_handler(commands=["profile"])
async def profile_cmd(m: types.Message):
    u = get_user(m.from_user.id)
    left = max(0, u.get("trial_until", 0) - int(time.time()))
    days = left // (24*3600)
    await m.answer(
        f"Профиль:\n"
        f"- ты: {u.get('user_name') or '—'}\n"
        f"- я: {u.get('bot_name') or '—'} ({u.get('bot_gender') or '—'}, {u.get('bot_age') or '—'})\n"
        f"- обращение: {u.get('addressing')}\n"
        f"- тариф: {u.get('plan')} | дней пробного осталось: {days}\n"
        "Команды: /reset — начать заново, /help — подсказка"
    )

@dp.message_handler(commands=["help"])
async def help_cmd(m: types.Message):
    await m.answer(
        "Привет 👋 Я — твой душевный собеседник.\n\n"
        "Команды:\n"
        "/start — начать знакомство заново\n"
        "/reset — сбросить историю\n"
        "/profile — посмотреть профиль\n"
        "/help — эта подсказка\n\n"
        "Но в целом — просто пиши, и мы продолжим 🙂"
    )

@dp.message_handler()
async def chat(m: types.Message):
    uid = m.from_user.id
    text = (m.text or "").strip()
    u = get_user(uid)

    # 1) Выбор «друг/подруга»
    if u["stage"] in ["hello", "picking"]:
        g = normalize_gender(text)
        if not g:
            update_user(uid, {"stage": "picking"})
            return await m.answer("Как тебе будет комфортнее: как с **другом** или как с **подругой**?", parse_mode="Markdown")

        age = u.get("bot_age") or random.choice(["молодой", "средний"])
        name = pick_name(g)
        persona = build_persona({**u, "bot_gender": g, "bot_age": age, "bot_name": name})
        update_user(uid, {
            "stage": "intro_name",
            "bot_gender": g, "bot_age": age,
            "bot_name": name, "bot_name_confirmed": False,
            "persona_prompt": persona
        })

        if g == "ж":
            return await m.answer(
                f"Хорошо 😊 Тогда я буду твоей подругой. Меня зовут *{name}*. Нравится это имя? "
                f"Если хочешь — предложи другое, и я с радостью переименуюсь.",
                parse_mode="Markdown"
            )
        else:
            return await m.answer(
                f"Окей! Тогда я буду твоим другом. Меня зовут *{name}*. Как тебе такое имя? "
                f"Если есть вариант лучше — давай выберем!",
                parse_mode="Markdown"
            )

    # 2) Имя бота — подтверждение/замена
    if u["stage"] == "intro_name":
        ans = text.strip()

        if is_positive_reply(ans):
            update_user(uid, {"bot_name_confirmed": True, "stage": "intro_user"})
            return await m.answer(
                "Отлично! Тогда оставим так. 😊 А как к тебе обращаться? "
                "Можно просто: «зови меня …». И подскажи, на *ты* или на *вы*?",
                parse_mode="Markdown"
            )

        new_name = extract_new_bot_name(ans)
        if new_name:
            update_user(uid, {"bot_name": new_name, "bot_name_confirmed": True,
                              "persona_prompt": build_persona({**u, "bot_name": new_name})})
            update_user(uid, {"stage": "intro_user"})
            return await m.answer(
                f"Красиво звучит. Пусть буду *{new_name}* 🌟\n"
                "Теперь расскажи, как к тебе обращаться? И на *ты* или на *вы*?",
                parse_mode="Markdown"
            )

        if is_negative_reply(ans):
            return await m.answer(
                "Понимаю 🙂 Хочешь, подберём другое имя? Можешь предложить своё одним словом."
            )

        return await m.answer(
            f"{gform(u,'Понял','Поняла')} тебя. Давай уточним: оставить моё имя или предложишь другое? "
            "Если всё ок — просто напиши «норм» или «подходит»."
        )

    # 3) Имя пользователя + обращение
    if u["stage"] == "intro_user":
        if looks_like_topic_request(text):
            if POLITICS_RE.search(text) or not is_allowed(text):
                return await m.answer(
                    "С острыми политическими темами я не работаю — чтобы сохранять комфорт и уважение к разным взглядам. "
                    "Могу предложить безопасные темы: планы, идеи, книги/фильмы, новости науки, саморазвитие 🙂\n\n"
                    "Кстати, как к тебе обращаться? Можно написать: «зови меня …». И подскажи, на *ты* или на *вы*?",
                    parse_mode="Markdown"
                )
            return await m.answer(
                "Договорились, обсудим! Только давай сначала познакомимся 😊 "
                "Как к тебе обращаться? Напиши: «зови меня …». И подскажи, на *ты* или на *вы*?"
            )

        user_name = extract_user_name(text) or u.get("user_name")
        addressing = extract_addressing(text, default=u.get("addressing","ты"))
        update_user(uid, {"stage": "chat", "user_name": user_name, "addressing": addressing})

        hello = f"Очень приятно познакомиться, {user_name}! 😊 " if user_name else "Очень приятно познакомиться! 😊 "
        return await m.answer(
            hello
            + "С чего начнём? Могу предложить: как прошёл твой день, что порадовало, планы/цели, или обсудим какую-нибудь идею. "
            + f"Если не хочется думать — просто скажи «не знаю», {gform(u, 'я сам', 'я сама')} предложу тему."
        )

    # ===== Основной диалог =====
    if not is_allowed(text):
        return await m.answer("Предлагаю безопасную тему. Как прошёл твой день? Что сегодня было приятного? 🙂")

    if not has_access(u):
        return await m.answer(paywall_text(u))

    if any(p in text.lower() for p in ["не знаю", "затрудня", "без темы"]):
        starters = [
            "Давай начнём с простого: что тебя сегодня немного порадовало?",
            "Хочешь обсудим планы на вечер или неделю — что-то маленькое и конкретное?",
            "Расскажи про одну мелочь, за которую сегодня можно себя похвалить."
        ]
        return await m.answer(random.choice(starters))

    push_message(uid, "user", text)
    system_prompt = u.get("persona_prompt") or build_persona(u)
    try:
        answer = await deepseek_reply(get_user(uid)["history"], system_prompt)
    except Exception:
        answer = "Кажется, у меня минутка заминки 😅 Давай попробуем ещё раз?"
    push_message(uid, "assistant", answer)

    prefix = f"{u.get('user_name')}, " if u.get("user_name") else ""
    await m.answer(prefix + answer)

# ===== FastAPI-приложение и вебхук =====
app = FastAPI()

@app.get("/", response_class=PlainTextResponse)
async def health():
    return "ok"

@app.post(f"/tg/{WEBHOOK_SECRET}")
async def tg_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    try:
        update = types.Update(**data)
    except Exception:
        raise HTTPException(400, "invalid Update")

    try:
        await dp.process_update(update)
    except TelegramAPIError:
        pass
    return {"status": "ok"}
