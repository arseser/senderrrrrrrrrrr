import json
import re
import logging
import os
import traceback
from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

COOKIES_FILE = "cookies.json"

# При запуске создаём файл кук, если его нет
if not os.path.exists(COOKIES_FILE):
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)
    logger.info("Создан пустой cookies.json")

def load_cookies():
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки cookies.json: {e}")
        return {}

def save_cookies(data):
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Куки сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения cookies.json: {e}")
        raise

def cookie_str_to_dict(cookie_str: str) -> dict:
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
    return result

def extract_price(html: str) -> int | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
        elem = soup.find(attrs={"data-testid": "ad-price"})
        if elem:
            text = elem.get_text(strip=True)
        else:
            text = None
            for h3 in soup.find_all("h3"):
                if "zł" in h3.get_text():
                    text = h3.get_text(strip=True)
                    break
            if not text:
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
        return session_obj.cookies.get("csrftoken") or session_obj.cookies.get("csrf")
    except Exception as e:
        logger.error(f"Ошибка извлечения CSRF: {e}")
        return None

def propose_price(url: str, cookie_str: str) -> dict:
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

    try:
        resp = session.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return {"success": False, "error": f"Ошибка загрузки страницы: {e}"}

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

    original_price = extract_price(html)
    if original_price is None:
        return {"success": False, "error": "Не удалось определить цену на странице"}

    if original_price <= 300:
        proposal = original_price - 5
    elif original_price <= 1000:
        proposal = original_price - 10
    else:
        proposal = original_price - 20
    proposal = max(proposal, 1)

    csrf_token = extract_csrf(html, session)
    if not csrf_token:
        return {"success": False, "error": "Не удалось получить CSRF-токен"}

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
            return {"success": False, "error": f"Ошибка API (код {api_resp.status_code}): {err_text}"}
    except Exception as e:
        return {"success": False, "error": f"Ошибка отправки предложения: {e}"}

# ========== Маршруты ==========

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/api/cookies", methods=["GET", "POST"])
def manage_cookies():
    try:
        if request.method == "GET":
            cookies = load_cookies()
            return jsonify(list(cookies.keys()))
        if request.method == "POST":
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
        # ВАЖНО: выводим полный трейсбек
        err = traceback.format_exc()
        logger.error(err)
        return jsonify({"success": False, "error": f"Исключение: {err}"}), 500

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
        err = traceback.format_exc()
        logger.error(err)
        return jsonify({"success": False, "error": f"Исключение: {err}"}), 500

@app.errorhandler(500)
def handle_500(e):
    logger.error(f"Unhandled 500 error: {e}")
    return jsonify({"success": False, "error": f"Unhandled: {traceback.format_exc()}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
