import json
import re
import logging
import os
import time
import traceback
import urllib.parse
from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
UPSTASH_HEADERS = {"Authorization": f"Bearer {UPSTASH_TOKEN}"} if UPSTASH_TOKEN else {}
COOKIES_KEY = "olx_cookies"

MAX_URLS = 20
MAX_WORKERS = 5
DELAY_BETWEEN = 10

def load_cookies():
    if not UPSTASH_URL:
        return {}
    try:
        resp = requests.get(f"{UPSTASH_URL}/get/{COOKIES_KEY}", headers=UPSTASH_HEADERS, timeout=10)
        result = resp.json().get("result")
        if result:
            return json.loads(result)
        return {}
    except Exception as e:
        logger.error(f"Ошибка загрузки кук: {e}")
        return {}

def save_cookies(data):
    if not UPSTASH_URL:
        return
    try:
        payload = json.dumps(data, ensure_ascii=False)
        requests.post(
            f"{UPSTASH_URL}/set/{COOKIES_KEY}",
            headers=UPSTASH_HEADERS,
            data=payload.encode("utf-8"),
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения кук: {e}")

def cookie_str_to_dict(cookie_str: str) -> dict:
    if not cookie_str:
        return {}
    stripped = cookie_str.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            result = {}
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        result[item["name"]] = item["value"]
                if result:
                    return result
            elif isinstance(data, dict):
                if "name" in data and "value" in data:
                    return {data["name"]: data["value"]}
                if all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
                    return data
        except Exception:
            pass
    result = {}
    for item in stripped.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
    return result

def extract_numeric_id(html: str, url: str = "") -> str | None:
    """
    Три метода от Клода:
    1. Из URL: -ID123aBc.html → оставляем только цифры
    2. Из window.__INITIAL_STATE__
    3. Прямой regex: "id":число,"title"
    """
    # 1. Из URL
    if url:
        match = re.search(r'-ID([a-zA-Z0-9]+)\.html', url)
        if match:
            raw_id = match.group(1)
            numeric_id = re.sub(r'\D', '', raw_id)
            if numeric_id and len(numeric_id) >= 7:
                logger.info(f"ID из URL: {numeric_id}")
                return numeric_id

    soup = BeautifulSoup(html, "html.parser")

    # 2. window.__INITIAL_STATE__
    script_tag = soup.find("script", string=re.compile(r'window\.__INITIAL_STATE__'))
    if script_tag and script_tag.string:
        json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', script_tag.string)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                ad_id = data.get("ad", {}).get("currentAd", {}).get("id")
                if ad_id:
                    logger.info(f"ID из __INITIAL_STATE__: {ad_id}")
                    return str(ad_id)
            except Exception:
                pass

    # 3. Прямой regex по всему HTML: "id":число,"title"
    match = re.search(r'"id"\s*:\s*(\d+)\s*,\s*"title"', html)
    if match:
        logger.info(f"ID через regex id+title: {match.group(1)}")
        return match.group(1)

    # 4. Запасной: любой "id": 7+ цифр
    match = re.search(r'"id"\s*:\s*(\d{7,})', html)
    if match:
        logger.info(f"ID через regex 7+ цифр: {match.group(1)}")
        return match.group(1)

    # 5. meta al:android:url
    meta = soup.find("meta", property="al:android:url")
    if meta and meta.get("content"):
        match = re.search(r'item/(\d+)', meta["content"])
        if match:
            return match.group(1)

    return None

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

def extract_bearer_token(html: str, cookie_dict: dict) -> str | None:
    token = cookie_dict.get("access_token")
    if token:
        return token
    match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'"token"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    return None

def check_negotiation(html: str) -> bool:
    if re.search(r'"negotiable"\s*:\s*true', html):
        return True
    if "Negocjuj cenę" in html or "Предложить цену" in html:
        return True
    return False

def process_single_url(url: str, cookie_dict: dict, delay: int = DELAY_BETWEEN) -> dict:
    result = {"url": url}

    session = requests.Session()
    try:
        for k, v in cookie_dict.items():
            session.cookies.set(k, v, domain=".olx.pl")
    except Exception as e:
        result["success"] = False
        result["error"] = f"Ошибка кук: {e}"
        return result

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9",
    }
    session.headers.update(headers)

    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        result["success"] = False
        result["error"] = f"Ошибка загрузки: {e}"
        return result

    numeric_id = extract_numeric_id(html, url)
    if not numeric_id:
        result["success"] = False
        result["error"] = "Не удалось найти числовой ID"
        return result
    result["numeric_id"] = numeric_id

    if not check_negotiation(html):
        result["success"] = False
        result["error"] = "Нет кнопки предложения цены"
        return result

    original_price = extract_price(html)
    if original_price is None:
        result["success"] = False
        result["error"] = "Не удалось определить цену"
        return result

    if original_price <= 300:
        proposal = original_price - 5
    elif original_price <= 1000:
        proposal = original_price - 10
    else:
        proposal = original_price - 20
    proposal = max(proposal, 1)

    bearer_token = extract_bearer_token(html, cookie_dict)
    if not bearer_token:
        result["success"] = False
        result["error"] = "Не найден Bearer токен"
        return result

    xsrf_token = session.cookies.get("XSRF-TOKEN", domain=".olx.pl")
    if xsrf_token:
        xsrf_token = urllib.parse.unquote(xsrf_token)
        session.headers.update({"X-XSRF-TOKEN": xsrf_token})

    session.headers.update({
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    })

    time.sleep(delay)

    thread_url = "https://www.olx.pl/api/v1/chat/threads/"
    thread_payload = {"ad_id": int(numeric_id), "text": ""}
    try:
        thread_resp = session.post(thread_url, json=thread_payload, timeout=15)
        if thread_resp.status_code not in (200, 201):
            result["success"] = False
            result["error"] = f"Ошибка чата: {thread_resp.status_code}"
            return result
        thread_data = thread_resp.json()
        thread_id = thread_data.get("id") or thread_data.get("data", {}).get("id")
        if not thread_id:
            result["success"] = False
            result["error"] = "Не получен ID чата"
            return result
    except Exception as e:
        result["success"] = False
        result["error"] = f"Ошибка создания чата: {e}"
        return result

    message_url = f"https://www.olx.pl/api/v1/chat/threads/{thread_id}/messages/"
    message_payload = {
        "type": "price_proposal",
        "proposal_value": int(proposal),
        "text": f"Proponuje cene {proposal} zl."
    }
    try:
        msg_resp = session.post(message_url, json=message_payload, timeout=15)
        if msg_resp.status_code in (200, 201):
            result["success"] = True
            result["original_price"] = original_price
            result["proposed_price"] = proposal
        else:
            result["success"] = False
            result["error"] = f"Ошибка отправки: {msg_resp.status_code}"
    except Exception as e:
        result["success"] = False
        result["error"] = f"Ошибка сообщения: {e}"

    return result

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>ROCKET OLX Sender</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
        h1 { color: #2c3e50; text-align: center; margin-bottom: 20px; }
        label { display: block; margin-top: 10px; font-weight: bold; font-size: 14px; }
        input[type="text"], textarea { width: 100%; padding: 10px; margin-top: 4px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
        textarea { resize: vertical; }
        button { padding: 12px 24px; margin-top: 10px; background: #27ae60; color: white; border: none; cursor: pointer; border-radius: 6px; font-size: 14px; font-weight: bold; }
        button:hover { background: #219150; }
        button:disabled { background: #95a5a6; cursor: not-allowed; }
        .box { background: white; padding: 20px; margin: 20px 0; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .error { color: #e74c3c; font-size: 13px; }
        .success { color: #27ae60; font-size: 13px; }
        .hint { font-size: 12px; color: #888; margin-top: 4px; }
        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.6); z-index: 1000; justify-content: center; align-items: center; }
        .modal-overlay.active { display: flex; }
        .modal { background: white; border-radius: 12px; padding: 30px; width: 90%; max-width: 700px; max-height: 80vh; display: flex; flex-direction: column; box-shadow: 0 10px 40px rgba(0,0,0,0.3); }
        .modal h2 { margin-bottom: 15px; color: #2c3e50; }
        .modal-results { overflow-y: auto; flex: 1; margin-bottom: 15px; display: flex; flex-direction: column; gap: 10px; }
        .result-card { border: 1px solid #ddd; border-radius: 8px; padding: 12px 15px; font-size: 13px; line-height: 1.5; }
        .result-card.success { border-left: 4px solid #27ae60; background: #f0faf4; }
        .result-card.error { border-left: 4px solid #e74c3c; background: #fef5f5; }
        .result-card .url { font-size: 11px; color: #666; word-break: break-all; margin-bottom: 5px; }
        .result-card .price { font-weight: bold; }
        .modal-close { padding: 10px 20px; background: #3498db; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; align-self: flex-end; }
        .modal-close:hover { background: #2980b9; }
        .spinner { display: inline-block; width: 16px; height: 16px; border: 3px solid #fff; border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite; margin-right: 8px; vertical-align: middle; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <h1>🚀 ROCKET OLX Price Proposer</h1>
    <div class="box">
        <h3>Добавить куку</h3>
        <label>Имя куки:</label>
        <input type="text" id="cookieName" placeholder="Например: main">
        <label>Строка кук (из браузера):</label>
        <textarea id="cookieValue" rows="2" placeholder="Вставь JSON-массив или строку key=value; ..."></textarea>
        <button id="addCookieBtn">Добавить</button>
        <span id="addStatus"></span>
    </div>
    <div class="box">
        <h3>Отправить предложения</h3>
        <label>Выбрать куку:</label>
        <select id="cookieSelect"><option value="">-- Загружаю список... --</option></select>
        <label>Ссылки на товары (до 20, по одной на строку):</label>
        <textarea id="offerUrls" rows="6" placeholder="https://www.olx.pl/d/oferta/...&#10;https://www.olx.pl/d/oferta/...&#10;..."></textarea>
        <div class="hint">Максимум 20 ссылок. Задержка 10 сек между отправками.</div>
        <button id="sendBtn">🚀 Отправить все</button>
    </div>
    <div class="modal-overlay" id="modalOverlay">
        <div class="modal">
            <h2>📋 Результаты отправки</h2>
            <div class="modal-results" id="modalResults"></div>
            <button class="modal-close" id="modalCloseBtn">Закрыть</button>
        </div>
    </div>
    <script>
        console.log("Скрипт загружен");
        document.addEventListener("DOMContentLoaded", function() {
            document.getElementById("addCookieBtn").addEventListener("click", function() {
                var name = document.getElementById("cookieName").value.trim();
                var cookie = document.getElementById("cookieValue").value.trim();
                var status = document.getElementById("addStatus");
                if (!name || !cookie) { status.innerHTML = '<span class="error">Заполните оба поля</span>'; return; }
                var btn = document.getElementById("addCookieBtn");
                btn.disabled = true; btn.textContent = "Добавление...";
                fetch("/api/cookies", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ name: name, cookie: cookie }) })
                    .then(r => r.json())
                    .then(d => {
                        if (d.success) { status.innerHTML = '<span class="success">Кука добавлена!</span>'; document.getElementById("cookieName").value = ""; document.getElementById("cookieValue").value = ""; loadCookies(); }
                        else { status.innerHTML = '<span class="error">Ошибка: ' + (d.error || "Неизвестная") + '</span>'; }
                    })
                    .catch(e => { status.innerHTML = '<span class="error">Ошибка: ' + e.message + '</span>'; })
                    .finally(() => { btn.disabled = false; btn.textContent = "Добавить"; });
            });
            document.getElementById("sendBtn").addEventListener("click", function() {
                var cookie_name = document.getElementById("cookieSelect").value;
                var urlsText = document.getElementById("offerUrls").value.trim();
                if (!cookie_name || !urlsText) { alert("Выберите куку и введите ссылки"); return; }
                var urls = urlsText.split("\n").map(u => u.trim()).filter(u => u.length > 0);
                if (urls.length === 0 || urls.length > 20) { alert("От 1 до 20 ссылок"); return; }
                var sendBtn = document.getElementById("sendBtn");
                sendBtn.disabled = true; sendBtn.innerHTML = '<span class="spinner"></span>Отправка...';
                var overlay = document.getElementById("modalOverlay");
                var results = document.getElementById("modalResults");
                results.innerHTML = urls.map((u, i) => '<div class="result-card" id="card-' + i + '"><div class="url">' + u + '</div><div>Ожидание...</div></div>').join("");
                overlay.classList.add("active");
                fetch("/api/propose_batch", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ cookie_name: cookie_name, urls: urls }) })
                    .then(r => r.json())
                    .then(data => {
                        if (data.results) {
                            data.results.forEach((r, i) => {
                                var card = document.getElementById("card-" + i);
                                if (!card) return;
                                if (r.success) {
                                    card.className = "result-card success";
                                    card.innerHTML = '<div class="url">🔗 ' + r.url + '</div><div>💰 Оригинальная цена: <span class="price">' + r.original_price + ' zł</span></div><div>📉 Предложено: <span class="price">' + r.proposed_price + ' zł</span></div><div>🆔 ID: ' + r.numeric_id + '</div><div>✅ Успешно</div>';
                                } else {
                                    card.className = "result-card error";
                                    card.innerHTML = '<div class="url">🔗 ' + r.url + '</div><div>❌ ' + r.error + '</div>' + (r.numeric_id ? '<div>🆔 ID: ' + r.numeric_id + '</div>' : "");
                                }
                            });
                        }
                    })
                    .catch(e => { results.innerHTML = '<div class="result-card error">Ошибка: ' + e.message + '</div>'; })
                    .finally(() => { sendBtn.disabled = false; sendBtn.innerHTML = "🚀 Отправить все"; });
            });
            document.getElementById("modalCloseBtn").addEventListener("click", function() { document.getElementById("modalOverlay").classList.remove("active"); });
            loadCookies();
        });
        function loadCookies() {
            fetch("/api/cookies")
                .then(r => r.json())
                .then(list => {
                    var select = document.getElementById("cookieSelect");
                    select.innerHTML = '<option value="">-- Выберите куку --</option>';
                    list.forEach(name => { var o = document.createElement("option"); o.value = name; o.textContent = name; select.appendChild(o); });
                });
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return HTML_PAGE

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
            if not data:
                return jsonify({"success": False, "error": "Пустой запрос"}), 400
            name = data.get("name", "").strip()
            cookie_str = data.get("cookie", "").strip()
            if not name or not cookie_str:
                return jsonify({"success": False, "error": "Имя и строка кук обязательны"}), 400
            cookies = load_cookies()
            cookies[name] = cookie_str
            save_cookies(cookies)
            logger.info(f"Кука '{name}' добавлена")
            return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Ошибка cookies: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/propose_batch", methods=["POST"])
def propose_batch():
    try:
        data = request.get_json(force=True)
        cookie_name = data.get("cookie_name", "").strip()
        urls = data.get("urls", [])
        if not cookie_name or not urls:
            return jsonify({"success": False, "error": "Нет данных"}), 400
        if len(urls) > MAX_URLS:
            return jsonify({"success": False, "error": f"Максимум {MAX_URLS} ссылок"}), 400

        cookies = load_cookies()
        cookie_str = cookies.get(cookie_name)
        if not cookie_str:
            return jsonify({"success": False, "error": "Кука не найдена"}), 404

        cookie_dict = cookie_str_to_dict(cookie_str)
        if not cookie_dict:
            return jsonify({"success": False, "error": "Не разобрать куку"}), 400

        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_single_url, url, cookie_dict, DELAY_BETWEEN): url for url in urls}
            for future in as_completed(futures):
                results.append(future.result())

        url_order = {url: i for i, url in enumerate(urls)}
        results.sort(key=lambda r: url_order.get(r["url"], 999))

        return jsonify({"success": True, "results": results})
    except Exception as e:
        logger.error(f"Ошибка propose_batch: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.errorhandler(500)
def handle_500(e):
    return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
