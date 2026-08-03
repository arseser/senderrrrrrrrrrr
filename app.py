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

COOKIES_FILE = "cookies.json"
MAX_URLS = 20
MAX_WORKERS = 5
DELAY_BETWEEN = 10  # секунд между отправками

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
                return result
            elif isinstance(data, dict):
                if "name" in data and "value" in data:
                    return {data["name"]: data["value"]}
                return data
        except Exception:
            pass
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
    return result

def extract_numeric_id(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find("meta", property="al:android:url")
    if meta and meta.get("content"):
        match = re.search(r'item/(\d+)', meta["content"])
        if match:
            logger.info(f"ID найден в al:android:url: {match.group(1)}")
            return match.group(1)

    meta = soup.find("meta", property="og:url")
    if meta and meta.get("content"):
        match = re.search(r'-ID(\d+)\.html', meta["content"])
        if match:
            logger.info(f"ID найден в og:url: {match.group(1)}")
            return match.group(1)

    for script in soup.find_all("script"):
        if script.string:
            match = re.search(r'"adId"\s*:\s*"?(\d+)"?', script.string)
            if match:
                logger.info(f"ID найден в JS adId: {match.group(1)}")
                return match.group(1)
            match = re.search(r'"id"\s*:\s*(\d{7,})', script.string)
            if match:
                logger.info(f"ID найден в JS id: {match.group(1)}")
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

def process_single_url(url: str, cookie_dict: dict, delay: int = DELAY_BETWEEN) -> dict:
    """Обрабатывает одну ссылку с задержкой перед отправкой."""
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://www.olx.pl",
        "Referer": url,
    }
    session.headers.update(headers)

    # Загружаем страницу
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        result["success"] = False
        result["error"] = f"Ошибка загрузки: {e}"
        return result

    # Извлекаем ID
    numeric_id = extract_numeric_id(html)
    if not numeric_id:
        result["success"] = False
        result["error"] = "Не найден числовой ID"
        return result

    # Оригинальная цена
    original_price = extract_price(html)
    if original_price is None:
        result["success"] = False
        result["error"] = "Не удалось определить цену"
        return result

    # Вычисление предложения
    if original_price <= 300:
        proposal = original_price - 5
    elif original_price <= 1000:
        proposal = original_price - 10
    else:
        proposal = original_price - 20
    proposal = max(proposal, 1)

    # ЗАДЕРЖКА ПЕРЕД ОТПРАВКОЙ
    logger.info(f"Ожидание {delay} сек перед отправкой для {url}")
    time.sleep(delay)

    # XSRF-TOKEN
    xsrf_token = session.cookies.get("XSRF-TOKEN", domain=".olx.pl")
    if xsrf_token:
        xsrf_token = urllib.parse.unquote(xsrf_token)
        session.headers.update({"X-XSRF-TOKEN": xsrf_token})

    # Bearer token
    access_token = cookie_dict.get("access_token")
    if access_token:
        session.headers.update({"Authorization": f"Bearer {access_token}"})

    # Отправка предложения
    api_url = f"https://www.olx.pl/api/v1/offers/{numeric_id}/propose-price/"
    payload = {"price": int(proposal), "currency": "PLN"}
    session.headers.update({"Content-Type": "application/json"})

    try:
        api_resp = session.post(api_url, json=payload, timeout=15)
        if api_resp.status_code == 200:
            result["success"] = True
            result["original_price"] = original_price
            result["proposed_price"] = proposal
            result["numeric_id"] = numeric_id
        else:
            err_text = api_resp.text[:200]
            result["success"] = False
            result["error"] = f"API {api_resp.status_code}: {err_text}"
    except Exception as e:
        result["success"] = False
        result["error"] = f"Ошибка отправки: {e}"

    return result

# ========== HTML ==========
HTML_PAGE = """<!DOCTYPE html>
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

        /* Модальное окно */
        .modal-overlay {
            display: none;
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.6); z-index: 1000;
            justify-content: center; align-items: center;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: white; border-radius: 12px; padding: 30px;
            width: 90%; max-width: 700px; max-height: 80vh;
            display: flex; flex-direction: column;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
        }
        .modal h2 { margin-bottom: 15px; color: #2c3e50; }
        .modal-results {
            overflow-y: auto; flex: 1; margin-bottom: 15px;
            display: flex; flex-direction: column; gap: 10px;
        }
        .result-card {
            border: 1px solid #ddd; border-radius: 8px; padding: 12px 15px;
            font-size: 13px; line-height: 1.5;
        }
        .result-card.success { border-left: 4px solid #27ae60; background: #f0faf4; }
        .result-card.error { border-left: 4px solid #e74c3c; background: #fef5f5; }
        .result-card .url { font-size: 11px; color: #666; word-break: break-all; margin-bottom: 5px; }
        .result-card .price { font-weight: bold; }
        .modal-close {
            padding: 10px 20px; background: #3498db; color: white; border: none;
            border-radius: 6px; cursor: pointer; font-size: 14px; align-self: flex-end;
        }
        .modal-close:hover { background: #2980b9; }

        .spinner {
            display: inline-block; width: 16px; height: 16px; border: 3px solid #fff;
            border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite;
            margin-right: 8px; vertical-align: middle;
        }
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
        <button onclick="addCookie()">Добавить</button>
        <span id="addStatus"></span>
    </div>

    <div class="box">
        <h3>Отправить предложения</h3>
        <label>Выбрать куку:</label>
        <select id="cookieSelect">
            <option value="">-- Загружаю список... --</option>
        </select>
        <label>Ссылки на товары (до 20, по одной на строку):</label>
        <textarea id="offerUrls" rows="6" placeholder="https://www.olx.pl/d/oferta/...&#10;https://www.olx.pl/d/oferta/...&#10;..."></textarea>
        <div class="hint">Максимум 20 ссылок. Задержка 10 сек между отправками.</div>
        <button id="sendBtn" onclick="sendProposals()">🚀 Отправить все</button>
    </div>

    <!-- Модальное окно -->
    <div class="modal-overlay" id="modalOverlay">
        <div class="modal">
            <h2>📋 Результаты отправки</h2>
            <div class="modal-results" id="modalResults"></div>
            <button class="modal-close" onclick="closeModal()">Закрыть</button>
        </div>
    </div>

    <script>
        async function loadCookies() {
            const resp = await fetch('/api/cookies');
            const list = await resp.json();
            const select = document.getElementById('cookieSelect');
            select.innerHTML = '<option value="">-- Выберите куку --</option>';
            list.forEach(name => {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                select.appendChild(opt);
            });
        }
        loadCookies();

        async function addCookie() {
            const name = document.getElementById('cookieName').value.trim();
            const cookie = document.getElementById('cookieValue').value.trim();
            const status = document.getElementById('addStatus');
            if (!name || !cookie) {
                status.innerHTML = '<span class="error">Заполните оба поля</span>';
                return;
            }
            const resp = await fetch('/api/cookies', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name, cookie })
            });
            const data = await resp.json();
            if (data.success) {
                status.innerHTML = '<span class="success">Кука добавлена!</span>';
                loadCookies();
                document.getElementById('cookieName').value = '';
                document.getElementById('cookieValue').value = '';
            } else {
                status.innerHTML = `<span class="error">Ошибка: ${data.error}</span>`;
            }
        }

        function closeModal() {
            document.getElementById('modalOverlay').classList.remove('active');
        }

        async function sendProposals() {
            const cookie_name = document.getElementById('cookieSelect').value;
            const urlsText = document.getElementById('offerUrls').value.trim();
            const sendBtn = document.getElementById('sendBtn');
            const modalOverlay = document.getElementById('modalOverlay');
            const modalResults = document.getElementById('modalResults');

            if (!cookie_name || !urlsText) {
                alert('Выберите куку и введите ссылки');
                return;
            }

            const urls = urlsText.split('\\n').map(u => u.trim()).filter(u => u.length > 0);
            if (urls.length === 0) {
                alert('Нет ссылок');
                return;
            }
            if (urls.length > 20) {
                alert('Максимум 20 ссылок');
                return;
            }

            sendBtn.disabled = true;
            sendBtn.innerHTML = '<span class="spinner"></span>Отправка...';

            modalResults.innerHTML = urls.map((url, i) => 
                `<div class="result-card" id="card-${i}">
                    <div class="url">${url}</div>
                    <div>⏳ Ожидание (задержка 10 сек)...</div>
                </div>`
            ).join('');
            modalOverlay.classList.add('active');

            try {
                const resp = await fetch('/api/propose_batch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ cookie_name, urls })
                });
                const data = await resp.json();

                if (data.results) {
                    data.results.forEach((r, i) => {
                        const card = document.getElementById(`card-${i}`);
                        if (!card) return;
                        if (r.success) {
                            card.className = 'result-card success';
                            card.innerHTML = `
                                <div class="url">🔗 ${r.url}</div>
                                <div>💰 Оригинальная цена: <span class="price">${r.original_price} zł</span></div>
                                <div>📉 Предложено: <span class="price">${r.proposed_price} zł</span></div>
                                <div>🆔 ID: ${r.numeric_id}</div>
                                <div>✅ Успешно отправлено</div>
                            `;
                        } else {
                            card.className = 'result-card error';
                            card.innerHTML = `
                                <div class="url">🔗 ${r.url}</div>
                                <div>❌ ${r.error}</div>
                            `;
                        }
                    });
                }
            } catch (e) {
                modalResults.innerHTML = `<div class="result-card error">Ошибка соединения: ${e}</div>`;
            } finally {
                sendBtn.disabled = false;
                sendBtn.innerHTML = '🚀 Отправить все';
            }
        }
    </script>
</body>
</html>"""

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
            name = data.get("name", "").strip()
            cookie_str = data.get("cookie", "").strip()
            if not name or not cookie_str:
                return jsonify({"success": False, "error": "Имя и строка кук обязательны"}), 400
            cookies = load_cookies()
            cookies[name] = cookie_str
            save_cookies(cookies)
            return jsonify({"success": True})
    except Exception as e:
        err = traceback.format_exc()
        logger.error(err)
        return jsonify({"success": False, "error": f"Исключение: {err}"}), 500

@app.route("/api/propose_batch", methods=["POST"])
def propose_batch():
    try:
        data = request.get_json(force=True)
        cookie_name = data.get("cookie_name", "").strip()
        urls = data.get("urls", [])

        if not cookie_name:
            return jsonify({"success": False, "error": "Имя куки обязательно"}), 400
        if not urls or len(urls) == 0:
            return jsonify({"success": False, "error": "Нет ссылок"}), 400
        if len(urls) > MAX_URLS:
            return jsonify({"success": False, "error": f"Максимум {MAX_URLS} ссылок"}), 400

        cookies = load_cookies()
        cookie_str = cookies.get(cookie_name)
        if not cookie_str:
            return jsonify({"success": False, "error": "Кука не найдена"}), 404

        cookie_dict = cookie_str_to_dict(cookie_str)

        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_single_url, url, cookie_dict, DELAY_BETWEEN): url for url in urls}
            for future in as_completed(futures):
                results.append(future.result())

        # Восстанавливаем порядок
        url_order = {url: i for i, url in enumerate(urls)}
        results.sort(key=lambda r: url_order.get(r["url"], 999))

        return jsonify({"success": True, "results": results})
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
