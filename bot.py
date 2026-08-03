import json
import re
import logging
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Путь к файлу хранения кук
COOKIES_FILE = "cookies.json"

# Глобальный словарь кук: { "cookie_name": "cookie_string" }
cookies_db: Dict[str, str] = {}
# Выбор куки пользователем: { user_id: "cookie_name" }
user_selection: Dict[int, str] = {}

# Загрузка кук из файла при старте
try:
    with open(COOKIES_FILE, "r") as f:
        cookies_db = json.load(f)
except FileNotFoundError:
    cookies_db = {}

# Сохранение в файл
def save_cookies():
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies_db, f, indent=2)

# Парсинг строки кук в словарь для requests
def cookie_string_to_dict(cookie_str: str) -> Dict[str, str]:
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            result[key.strip()] = value.strip()
        else:
            # Без знака = пропускаем
            pass
    return result

# Извлечение цены из OLX страницы (оригинальная цена)
def extract_price_from_page(html: str) -> Optional[int]:
    """
    Ищем цену. На OLX цена может быть в элементе с data-testid="ad-price",
    или в <h3 class="css-...">. Поддержка нескольких селекторов.
    """
    soup = BeautifulSoup(html, "html.parser")
    # вариант 1
    price_elem = soup.find(attrs={"data-testid": "ad-price"})
    if price_elem:
        price_text = price_elem.get_text(strip=True)
    else:
        # вариант 2 – ищем по классу, содержащему цену (может меняться, возьмём regex)
        price_text = None
        for h3 in soup.find_all("h3"):
            if "zł" in h3.get_text():
                price_text = h3.get_text(strip=True)
                break
        if not price_text:
            # последняя попытка – мета-тег с ценой
            meta_price = soup.find("meta", property="product:price:amount")
            if meta_price and meta_price.get("content"):
                price_text = meta_price["content"]
    if not price_text:
        return None
    # Извлекаем число
    cleaned = re.sub(r"[^\d]", "", price_text.replace(" ", ""))
    if cleaned:
        return int(cleaned)
    return None

# Извлечение CSRF-токена из страницы
def extract_csrf(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]
    # иногда в js-переменной: window.__INITIAL_STATE__ или отдельном скрипте
    # альтернативный поиск в куках сессии (csrf токен часто дублируется в куках)
    # Здесь проще взять из куки с именем csrf, если она есть.
    # Но мы уже имеем куку в сессии, её передадим в заголовках.
    # Поэтому если нет meta, попробуем из кук, но тогда нужно уже session.cookies
    return None

# Основная функция: предложить цену на OLX
def propose_price_olx(cookie_str: str, product_url: str) -> str:
    """
    Возвращает сообщение об успехе или ошибку.
    """
    if not cookie_str:
        return "❌ Кука не выбрана. Сначала /selectcookie"

    # Создаём сессию
    session = requests.Session()
    # Устанавливаем куки
    cookie_dict = cookie_string_to_dict(cookie_str)
    for k, v in cookie_dict.items():
        session.cookies.set(k, v)

    # 1. GET страницы товара
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = session.get(product_url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return f"❌ Ошибка загрузки страницы: {e}"

    html = resp.text

    # 2. Извлекаем offer_id из URL или из страницы
    # Обычно URL вида https://www.olx.pl/d/oferta/...-ID-номер.html
    offer_id = None
    match = re.search(r"-ID-(\w+)\.html", product_url)
    if match:
        offer_id = match.group(1)
    else:
        # Поиск в мета-теге og:url или canonical
        soup = BeautifulSoup(html, "html.parser")
        meta_og = soup.find("meta", property="og:url")
        if meta_og:
            og_url = meta_og.get("content", "")
            match = re.search(r"-ID-(\w+)\.html", og_url)
            if match:
                offer_id = match.group(1)
        if not offer_id:
            # Последний шанс – из window.__INITIAL_STATE__
            match = re.search(r'"ad_id":"?(\d+)"?', html)
            if match:
                offer_id = match.group(1)
    if not offer_id:
        return "❌ Не удалось извлечь ID объявления из URL."

    # 3. Оригинальная цена
    original_price = extract_price_from_page(html)
    if original_price is None:
        return "❌ Не удалось определить оригинальную цену на странице."

    # 4. Вычисляем предлагаемую цену
    if original_price <= 300:
        proposal = original_price - 5
    elif original_price <= 1000:
        proposal = original_price - 10
    else:
        proposal = original_price - 20

    if proposal < 1:
        proposal = 1  # минимально 1 злотый

    # 5. CSRF-токен (берём из кук, часто 'csrftoken', или из meta)
    csrf_token = extract_csrf(html)
    if not csrf_token:
        # Попробуем взять из кук сессии (OLX ставит csrf в куки)
        csrf_token = session.cookies.get("csrftoken") or session.cookies.get("csrf")
    if not csrf_token:
        return "❌ Не удалось получить CSRF-токен."

    # 6. Отправка POST запроса к API
    api_url = f"https://www.olx.pl/api/v1/offers/{offer_id}/propose-price/"
    payload = {"price": str(proposal)}
    api_headers = {
        **headers,
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf_token,
        "Referer": product_url,
        "Origin": "https://www.olx.pl",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        api_resp = session.post(api_url, json=payload, headers=api_headers, timeout=15)
        if api_resp.status_code == 200:
            return (
                f"✅ Предложение отправлено!\n"
                f"Товар: {product_url}\n"
                f"Оригинальная цена: {original_price} zł\n"
                f"Предложено: {proposal} zł"
            )
        else:
            try:
                err = api_resp.json()
                err_msg = err.get("detail", str(err))
            except:
                err_msg = api_resp.text[:200]
            return f"❌ Ошибка API (код {api_resp.status_code}): {err_msg}"
    except Exception as e:
        return f"❌ Ошибка при отправке предложения: {e}"

# ================= Обработчики команд =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ROCKET OLX Sender готов.\n\n"
        "Команды:\n"
        "/addcookie имя КУКА_СТРОКА – добавить куку\n"
        "/cookies – показать сохранённые куки\n"
        "/selectcookie имя – выбрать куку для работы\n"
        "/cene ССЫЛКА – отправить предложение цены\n"
    )

async def addcookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /addcookie ИМЯ КУКА_СТРОКА")
        return
    name = args[0]
    cookie_str = " ".join(args[1:])
    cookies_db[name] = cookie_str
    save_cookies()
    await update.message.reply_text(f"✅ Кука '{name}' добавлена/обновлена.")

async def list_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not cookies_db:
        await update.message.reply_text("Нет сохранённых кук.")
        return
    lines = ["📋 **Сохранённые куки:**"]
    for i, name in enumerate(cookies_db.keys(), 1):
        lines.append(f"{i}. `{name}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def selectcookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Укажите имя: /selectcookie ИМЯ")
        return
    name = args[0]
    if name not in cookies_db:
        await update.message.reply_text(f"❌ Кука '{name}' не найдена.")
        return
    user_selection[update.effective_user.id] = name
    await update.message.reply_text(f"🔧 Выбрана кука '{name}'.")

async def cene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_selection:
        await update.message.reply_text("❌ Сначала выберите куку: /selectcookie ИМЯ")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите ссылку: /cene https://www.olx.pl/d/oferta/...")
        return

    url = args[0]
    cookie_name = user_selection[user_id]
    cookie_str = cookies_db.get(cookie_name)

    await update.message.reply_text("⏳ Отправляю предложение...")
    result = propose_price_olx(cookie_str, url)
    await update.message.reply_text(result)

# ================= Точка входа =================
def main():
    import os
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        logger.error("BOT_TOKEN не задан в переменных окружения!")
        return

    application = Application.builder().token(TOKEN).build()

    # Регистрация команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addcookie", addcookie))
    application.add_handler(CommandHandler("cookies", list_cookies))
    application.add_handler(CommandHandler("selectcookie", selectcookie))
    application.add_handler(CommandHandler("cene", cene))

    # Запуск поллинга (Worker)
    logger.info("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()