import json
import re
import logging
import os
import traceback
from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# Настройка логирования – всё пишется в stdout, видно в логах Render
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

COOKIES_FILE = "cookies.json"

# ===== Работа с JSON-хранилищем кук =====
def load_cookies():
    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки cookies.json: {e}")
            return {}
    return {}

def save_cookies(data):
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Куки сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения cookies.json: {e}")

# ===== Парсинг кук =====
def cookie_str_to_dict(cookie_str: str) -> dict:
    """Преобразует строку кук 'key=val; key2=val2' в словарь"""
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
    return result

# ===== Извлечение данных из страницы OLX =====
def extract_price(html: str) -> int | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
        # Способ 1: data-testid
        elem = soup.find(attrs={"data-testid": "ad-price"})
        if elem:
            text = elem.get_text(strip=True)
        else:
            # Способ 2: ищем <h3> с "zł"
            text = None
            for h3 in soup.find_all("h3"):
                if "zł" in h3.get_text():
                    text = h3.get_text(strip=True)
                    break
            if not text:
                # Способ 3: мета-тег
                meta = soup.find("meta", property="product:price:amount")
                if meta and meta.get("content"):
                    text = meta["content"]
        if not text:
            return None
        cleaned = re.sub(r"[^\d]", "", text.replace(" ", ""))
        return int(cleaned) if cleaned else None
    except Exception as e:
        logger.error(f"Ошибка парсинга цены: {e}")
        return None

def extract_csrf(html: str, session_obj: requests.Session) -> str | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta and meta.get("content"):
            return meta["content"]
        # Запасной вариант – из кук сессии
        return session_obj.cookies.get("csrftoken") or session_obj.cookies.get("csrf")
    except Exception as e:
        logger.error(f"Ошибка извлечения CSRF: {e}")
        return None

# ===== Главная логика предложения цены =====
def propose_price(url: str, cookie_str: str) -> dict:
    """
    Возвращает словарь с ключами:
    - success: bool
    - original_price: int (если успех)
    - proposed_price: int (если успех)
    - error: str (если ошибка)
    """
    if not cookie_str:
        return {"success": False, "error": "Кука не задана"}

    session = requests.Session()
    try:
        cookie_dict = cookie_str_to_dict(cookie_str)
        for k, v in cookie_dict.items():
            session.cookies.set(k, v)
    except Exception as e:
        return {"success": False, "error": f"Ошибка обработки кук: {e}"}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9",
    }

    # GET страницы
    try:
        resp = session.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.error(f"Ошибка загрузки страницы: {e}")
        return {"success": False, "error": f"Ошибка загрузки страницы: {e}"}

    # ID объявления
    offer_id = None
    match = re.search(r"-ID-(\w+)\.html", url)
    if match:
        offer_id = match.group(1)
    else:
        soup = BeautifulSoup(html, "html.parser")
        meta_og = soup.find("meta", property="og:url")
        if meta_og:
            og_url = meta_og.get("content", "")
            match = re.search(r"-ID-(\w+)\.html", og_url)
            if match:
                offer_id = match.group(1)
        if not offer_id:
            match = re.search(r'"ad_id":"?(\d+)"?', html)
            if match:
                offer_id = match.group(1)
    if not offer_id:
        return {"success": False, "error": "Не удалось извлечь ID объявления"}

    # Оригинальная цена
    original_price = extract_price(html)
    if original_price is None:
        return {"success": False, "error": "Не удалось определить цену на странице"}

    # Вычисление предложенной цены
    if original_price <= 300:
        proposal = original_price - 5
    elif original_price <= 1000:
        proposal = original_price - 10
    else:
        proposal = original_price - 20
    proposal = max(proposal, 1)

    # CSRF токен
    csrf_token = extract_csrf(html, session)
    if not csrf_token:
        return {"success": False, "error": "Не удалось получить CSRF-токен"}

    # Отправка API запроса
    api_url = f"https://www.olx.pl/api/v1/offers/{offer_id}/propose-price/"
    payload = {"price": str(proposal)}
    api_headers = {
        **headers,
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf_token,
        "Referer": url,
        "Origin": "https://www.olx.pl",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        api_resp = session.post(api_url, json=payload, headers=api_headers, timeout=15)
        if api_resp.status_code == 200:
            return {
                "success": True,
                "original_price": original_price,
                "proposed_price": proposal,
                "url": url
            }
        else:
            err_text = api_resp.text[:200]
            logger.warning(f"OLX API вернул {api_resp.status_code}: {err_text}")
            return {"success": False, "error": f"Ошибка API (код {api_resp.status_code}): {err_text}"}
    except Exception as e:
        logger.error(f"Ошибка отправки предложения: {e}")
        return {"success": False, "error": f"Ошибка отправки: {e}"}

# ========== Маршруты ==========

@app.route("/")
def index():
    """Главная страница с интерфейсом"""
    return render_template("index.html")

@app.route("/health")
def health():
    """Проверка работоспособности (удобно для пинга)"""
    return jsonify({"status": "ok"})

@app.route("/api/cookies", methods=["GET", "POST"])
def manage_cookies():
    if request.method == "GET":
        cookies = load_cookies()
        return jsonify(list(cookies.keys()))
    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            name = data.get("name", "").strip()
            cookie_str = data.get("cookie", "").strip()
            if not name or not cookie_str:
                return jsonify({"success": False, "error": "Имя и строка кук обязательны"}), 400
            cookies = load_cookies()
            cookies[name] = cookie_str
            save_cookies(cookies)
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Ошибка в /api/cookies POST: {e}")
            return jsonify({"success": False, "error": "Внутренняя ошибка сервера"}), 500

@app.route("/api/propose", methods=["POST"])
def propose():
    try:
        data = request.get_json(force=True)
        url = data.get("url", "").strip()
        cookie_name = data.get("cookie_name", "").strip()
        if not url or not cookie_name:
            return jsonify({"success": False, "error": "URL и имя куки обязательны"}), 400

        cookies = load_cookies()
        cookie_str = cookies.get(cookie_name)
        if not cookie_str:
            return jsonify({"success": False, "error": "Кука не найдена"}), 404

        result = propose_price(url, cookie_str)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Критическая ошибка в /api/propose: {traceback.format_exc()}")
        return jsonify({"success": False, "error": f"Внутренняя ошибка сервера: {e}"}), 500

@app.errorhandler(500)
def handle_500(e):
    logger.error(f"Unhandled 500 error: {e}")
    return jsonify({"success": False, "error": "Внутренняя ошибка сервера"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
