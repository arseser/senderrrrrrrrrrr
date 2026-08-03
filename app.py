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
MESSAGE_TEXT_KEY = "message_text"
BYPASS_MODE_KEY = "bypass_mode"

MAX_URLS = 20
DELAY_BETWEEN = 10
DEFAULT_MESSAGE = "Dzień dobry, czy jest jeszcze dostępne?"

def load_data(key):
    if not UPSTASH_URL:
        return None
    try:
        resp = requests.get(f"{UPSTASH_URL}/get/{key}", headers=UPSTASH_HEADERS, timeout=10)
        result = resp.json().get("result")
        return json.loads(result) if result else None
    except Exception as e:
        logger.error(f"Ошибка загрузки {key}: {e}")
        return None

def save_data(key, data):
    if not UPSTASH_URL:
        return
    try:
        requests.post(f"{UPSTASH_URL}/set/{key}", headers=UPSTASH_HEADERS, data=json.dumps(data, ensure_ascii=False).encode(), timeout=10)
    except Exception as e:
        logger.error(f"Ошибка сохранения {key}: {e}")

def load_cookies():
    return load_data(COOKIES_KEY) or {}

def save_cookies(data):
    save_data(COOKIES_KEY, data)

def load_message_text():
    val = load_data(MESSAGE_TEXT_KEY)
    return val if isinstance(val, str) and val.strip() else DEFAULT_MESSAGE

def save_message_text(text):
    save_data(MESSAGE_TEXT_KEY, text)

def load_bypass_mode():
    return load_data(BYPASS_MODE_KEY) or False

def save_bypass_mode(val):
    save_data(BYPASS_MODE_KEY, bool(val))

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

def process_single_url(url: str, numeric_id: str, cookie_dict: dict, message_text: str, delay: int = DELAY_BETWEEN) -> dict:
    result = {"url": url, "numeric_id": numeric_id}

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

    original_price = extract_price(html)
    proposal = None
    if original_price:
        if original_price <= 300:
            proposal = original_price - 5
        elif original_price <= 1000:
            proposal = original_price - 10
        else:
            proposal = original_price - 20
        proposal = max(proposal, 1)

    bearer_token = cookie_dict.get("access_token")
    if not bearer_token:
        result["success"] = False
        result["error"] = "Не найден access_token в куках"
        return result

    device_guid = cookie_dict.get("deviceGUID")
    xsrf_token = session.cookies.get("XSRF-TOKEN", domain=".olx.pl")

    time.sleep(delay)

    api_headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Platform": "web",
        "Origin": "https://www.olx.pl",
        "Referer": url,
    }
    if device_guid:
        api_headers["X-Device-Id"] = device_guid
    if xsrf_token:
        api_headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf_token)

    # Пробуем все варианты запросов
    attempts = [
        ("POST /api/v1/chat/threads/", "https://www.olx.pl/api/v1/chat/threads/", {"ad_id": int(numeric_id), "text": message_text}),
        ("JSON-RPC chat.createThread", "https://www.olx.pl/api/v1/chat/threads/", {"jsonrpc": "2.0", "method": "chat.createThread", "params": {"ad_id": int(numeric_id), "text": message_text}, "id": 1}),
        ("JSON-RPC chat.sendMessage", "https://www.olx.pl/api/v1/chat/threads/", {"jsonrpc": "2.0", "method": "chat.sendMessage", "params": {"ad_id": int(numeric_id), "text": message_text}, "id": 1}),
        ("POST /api/v1/chats/", "https://www.olx.pl/api/v1/chats/", {"ad_id": int(numeric_id), "message": message_text}),
    ]

    for name, url_target, payload in attempts:
        try:
            r = session.post(url_target, json=payload, headers=api_headers, timeout=15)
            logger.info(f"{name}: {r.status_code} - {r.text[:300]}")
            if r.status_code in (200, 201):
                data = r.json()
                if "error" not in data and "id" in data:
                    result["success"] = True
                    result["original_price"] = original_price or 0
                    result["proposed_price"] = proposal or 0
                    result["method"] = name
                    return result
        except Exception as e:
            logger.warning(f"{name}: {e}")

    result["success"] = False
    result["error"] = "Все методы отклонены. Используйте обходной режим (только сообщение без проверки ответа)."
    return result

def send_raw_message(url: str, numeric_id: str, cookie_dict: dict, message_text: str, delay: int = DELAY_BETWEEN) -> dict:
    """Обходной режим: только отправка, результат не проверяется."""
    result = {"url": url, "numeric_id": numeric_id}

    session = requests.Session()
    for k, v in cookie_dict.items():
        session.cookies.set(k, v, domain=".olx.pl")

    bearer_token = cookie_dict.get("access_token")
    if not bearer_token:
        result["success"] = False
        result["error"] = "Нет токена"
        return result

    time.sleep(delay)

    api_headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Platform": "web",
        "Origin": "https://www.olx.pl",
        "Referer": url,
    }

    payload = {"ad_id": int(numeric_id), "text": message_text}
    try:
        r = session.post("https://www.olx.pl/api/v1/chat/threads/", json=payload, headers=api_headers, timeout=15)
        logger.info(f"Обходной режим: {r.status_code} - {r.text[:300]}")
        result["success"] = True
        result["info"] = f"Отправлено (статус {r.status_code})"
    except Exception as e:
        result["success"] = False
        result["error"] = str(e)

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
        .modal { background: white; border-radius: 12px; padding: 30px; width: 90%; max-width: 500px; box-shadow: 0 10px 40px rgba(0,0,0,0.3); }
        .modal h2 { margin-bottom: 15px; color: #2c3e50; }
        .modal input { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
        .modal-buttons { display: flex; gap: 10px; margin-top: 15px; justify-content: flex-end; }
        .modal-buttons button { padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
        .btn-primary { background: #27ae60; color: white; }
        .btn-primary:hover { background: #219150; }
        .btn-cancel { background: #95a5a6; color: white; }
        .btn-cancel:hover { background: #7f8c8d; }
        .result-card { border: 1px solid #ddd; border-radius: 8px; padding: 12px 15px; font-size: 13px; line-height: 1.5; }
        .result-card.success { border-left: 4px solid #27ae60; background: #f0faf4; }
        .result-card.error { border-left: 4px solid #e74c3c; background: #fef5f5; }
        .result-card .url { font-size: 11px; color: #666; word-break: break-all; margin-bottom: 5px; }
        .result-card .price { font-weight: bold; }
        .spinner { display: inline-block; width: 16px; height: 16px; border: 3px solid #fff; border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite; margin-right: 8px; vertical-align: middle; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <h1>ROCKET OLX Sender</h1>

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
        <h3>Настройки</h3>
        <label>Текст сообщения:</label>
        <textarea id="messageText" rows="3"></textarea>
        <button id="saveMessageBtn">Сохранить</button>
        <span id="msgStatus"></span>
        <br><br>
        <label><input type="checkbox" id="bypassCheck"> Обходной режим (отправлять без проверки ответа)</label>
    </div>

    <div class="box">
        <h3>Отправить</h3>
        <label>Выбрать куку:</label>
        <select id="cookieSelect"><option value="">-- Загружаю список... --</option></select>
        <label>Ссылки на товары (до 20, по одной на строку):</label>
        <textarea id="offerUrls" rows="6" placeholder="https://www.olx.pl/d/oferta/...&#10;https://www.olx.pl/d/oferta/...&#10;..."></textarea>
        <div class="hint">Для каждой ссылки спросит ID объявления.</div>
        <button id="sendBtn">Отправить все</button>
    </div>

    <div class="modal-overlay" id="idModal">
        <div class="modal">
            <h2>Введите ID объявления</h2>
            <p style="font-size:12px;color:#888;margin-bottom:10px;" id="idModalUrl"></p>
            <input type="text" id="idModalInput" placeholder="Например: 1084063708">
            <div class="modal-buttons">
                <button class="btn-cancel" id="idModalSkip">Пропустить</button>
                <button class="btn-primary" id="idModalSubmit">Отправить</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="resultsModal">
        <div class="modal" style="max-width:700px;max-height:80vh;display:flex;flex-direction:column;">
            <h2>Результаты</h2>
            <div id="resultsContent" style="overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:10px;"></div>
            <button class="btn-cancel" id="resultsClose" style="align-self:flex-end;margin-top:10px;">Закрыть</button>
        </div>
    </div>

    <script>
        var pendingUrls = [];
        var pendingIndex = 0;
        var cookieName = "";
        var allResults = [];
        var currentMessage = "";

        document.addEventListener("DOMContentLoaded", function() {
            loadCookies();
            loadMessage();
            loadBypass();

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
                        else { status.innerHTML = '<span class="error">Ошибка: ' + (d.error || "") + '</span>'; }
                    })
                    .catch(e => { status.innerHTML = '<span class="error">Ошибка: ' + e.message + '</span>'; })
                    .finally(() => { btn.disabled = false; btn.textContent = "Добавить"; });
            });

            document.getElementById("saveMessageBtn").addEventListener("click", function() {
                var text = document.getElementById("messageText").value;
                fetch("/api/message", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ text: text }) })
                    .then(r => r.json())
                    .then(d => { document.getElementById("msgStatus").innerHTML = d.success ? '<span class="success">Сохранено</span>' : '<span class="error">Ошибка</span>'; });
            });

            document.getElementById("bypassCheck").addEventListener("change", function() {
                fetch("/api/bypass", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ bypass: this.checked }) });
            });

            document.getElementById("sendBtn").addEventListener("click", function() {
                cookieName = document.getElementById("cookieSelect").value;
                var urlsText = document.getElementById("offerUrls").value.trim();
                if (!cookieName || !urlsText) { alert("Выберите куку и введите ссылки"); return; }
                var urls = urlsText.split("\n").map(u => u.trim()).filter(u => u.length > 0);
                if (urls.length === 0 || urls.length > 20) { alert("От 1 до 20 ссылок"); return; }
                currentMessage = document.getElementById("messageText").value.trim() || "Dzień dobry, czy jest jeszcze dostępne?";
                pendingUrls = urls;
                pendingIndex = 0;
                allResults = [];
                showIdModal(pendingUrls[0]);
            });

            document.getElementById("idModalSubmit").addEventListener("click", function() {
                var id = document.getElementById("idModalInput").value.trim();
                if (!id || !/^\d+$/.test(id)) { alert("Введите числовой ID"); return; }
                document.getElementById("idModal").classList.remove("active");
                sendOne(pendingUrls[pendingIndex], id);
            });

            document.getElementById("idModalSkip").addEventListener("click", function() {
                document.getElementById("idModal").classList.remove("active");
                allResults.push({ url: pendingUrls[pendingIndex], success: false, error: "Пропущено" });
                pendingIndex++;
                if (pendingIndex < pendingUrls.length) { showIdModal(pendingUrls[pendingIndex]); }
                else { showResults(); }
            });

            document.getElementById("resultsClose").addEventListener("click", function() {
                document.getElementById("resultsModal").classList.remove("active");
            });
        });

        function showIdModal(url) {
            document.getElementById("idModalUrl").textContent = url;
            document.getElementById("idModalInput").value = "";
            document.getElementById("idModal").classList.add("active");
        }

        function sendOne(url, id) {
            document.getElementById("resultsContent").innerHTML = '<div class="result-card"><div class="url">' + url + '</div><div>Отправка...</div></div>';
            document.getElementById("resultsModal").classList.add("active");
            fetch("/api/propose_single", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ cookie_name: cookieName, url: url, numeric_id: id, message_text: currentMessage })
            })
            .then(r => r.json())
            .then(r => {
                allResults.push(r);
                pendingIndex++;
                if (pendingIndex < pendingUrls.length) { showIdModal(pendingUrls[pendingIndex]); }
                else { showResults(); }
            })
            .catch(e => {
                allResults.push({ url: url, success: false, error: e.message });
                pendingIndex++;
                if (pendingIndex < pendingUrls.length) { showIdModal(pendingUrls[pendingIndex]); }
                else { showResults(); }
            });
        }

        function showResults() {
            var html = "";
            allResults.forEach(function(r) {
                if (r.success) {
                    html += '<div class="result-card success"><div class="url">' + r.url + '</div><div>🆔 ID: ' + r.numeric_id + '</div><div>✅ ' + (r.info || r.method || "Успешно") + '</div></div>';
                } else {
                    html += '<div class="result-card error"><div class="url">' + r.url + '</div><div>❌ ' + (r.error || "Ошибка") + '</div></div>';
                }
            });
            document.getElementById("resultsContent").innerHTML = html;
        }

        function loadCookies() {
            fetch("/api/cookies").then(r => r.json()).then(list => {
                var s = document.getElementById("cookieSelect");
                s.innerHTML = '<option value="">-- Выберите куку --</option>';
                list.forEach(n => { var o = document.createElement("option"); o.value = n; o.textContent = n; s.appendChild(o); });
            });
        }

        function loadMessage() {
            fetch("/api/message").then(r => r.json()).then(d => { document.getElementById("messageText").value = d.text || ""; });
        }

        function loadBypass() {
            fetch("/api/bypass").then(r => r.json()).then(d => { document.getElementById("bypassCheck").checked = d.bypass || false; });
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
            return jsonify(list(load_cookies().keys()))
        data = request.get_json(force=True)
        if not data or not data.get("name") or not data.get("cookie"):
            return jsonify({"success": False, "error": "Имя и кука обязательны"}), 400
        cookies = load_cookies()
        cookies[data["name"].strip()] = data["cookie"].strip()
        save_cookies(cookies)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/message", methods=["GET", "POST"])
def manage_message():
    try:
        if request.method == "GET":
            return jsonify({"text": load_message_text()})
        data = request.get_json(force=True)
        save_message_text((data.get("text") or "").strip())
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/bypass", methods=["GET", "POST"])
def manage_bypass():
    try:
        if request.method == "GET":
            return jsonify({"bypass": load_bypass_mode()})
        data = request.get_json(force=True)
        save_bypass_mode(data.get("bypass", False))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/propose_single", methods=["POST"])
def propose_single():
    try:
        data = request.get_json(force=True)
        cookie_name = (data.get("cookie_name") or "").strip()
        url = (data.get("url") or "").strip()
        numeric_id = (data.get("numeric_id") or "").strip()
        message_text = (data.get("message_text") or "").strip() or load_message_text()

        if not cookie_name or not url or not numeric_id:
            return jsonify({"success": False, "error": "Нет данных"}), 400

        cookies = load_cookies()
        cookie_str = cookies.get(cookie_name)
        if not cookie_str:
            return jsonify({"success": False, "error": "Кука не найдена"}), 404

        cookie_dict = cookie_str_to_dict(cookie_str)
        if not cookie_dict:
            return jsonify({"success": False, "error": "Не разобрать куку"}), 400

        if load_bypass_mode():
            result = send_raw_message(url, numeric_id, cookie_dict, message_text, 0)
        else:
            result = process_single_url(url, numeric_id, cookie_dict, message_text, 0)

        return jsonify(result)
    except Exception as e:
        logger.error(f"Ошибка: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.errorhandler(500)
def handle_500(e):
    return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
