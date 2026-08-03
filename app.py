import json
import re
import logging
import os
import traceback
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

COOKIES_FILE = "cookies.json"

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
        for meta_name in ("csrf-token", "_csrf", "csrf"):
            meta = soup.find("meta", attrs={"name": meta_name})
            if meta and meta.get("content"):
                return meta["content"].strip()
        meta = soup.find("meta", attrs={"property": "csrf-token"})
        if meta and meta.get("content"):
            return meta["content"].strip()
        for inp in soup.find_all("input", type="hidden"):
            if "csrf" in (inp.get("name") or "").lower() and inp.get("value"):
                return inp["value"].strip()
        for pat in (r'window\.CSRF_TOKEN\s*=\s*["\']([^"\']+)["\']',
                    r'csrfToken\s*=\s*["\']([^"\']+)["\']',
                    r'"_csrf"\s*:\s*"([^"]+)"'):
            m = re.search(pat, html)
            if m:
                return m.group(1).strip()
        for tag in soup.find_all(attrs={"data-csrf": True}):
            return tag["data-csrf"].strip()
        body = soup.find("body")
        if body and body.get("data-csrf-token"):
            return body["data-csrf-token"].strip()
        for cname in ("csrftoken", "XSRF-TOKEN", "_csrf", "csrf"):
            val = session_obj.cookies.get(cname)
            if val:
                return val
        return None
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

    # ========== ПОИСК ЧИСЛОВОГО OFFER_ID (ОСНОВНОЕ ИСПРАВЛЕНИЕ) ==========
    numeric_offer_id = None
    soup = BeautifulSoup(html, "html.parser")

    # 1. Ищем в JS-объектах adId / ad_id / adid как число
    js_patterns = [
        r'"adId"\s*:\s*"?(\d{7,})"?',
        r'"ad_id"\s*:\s*"?(\d{7,})"?',
        r'"adid"\s*:\s*"?(\d{7,})"?',
        r"'adId'\s*:\s*'?(\d{7,})'?",
        r'"id"\s*:\s*(\d{7,})',  # может быть в контексте объявления, но осторожно
    ]
    for pat in js_patterns:
        m = re.search(pat, html)
        if m:
            numeric_offer_id = m.group(1)
            logger.info(f"Найден числовой adId через JS: {numeric_offer_id}")
            break

    # 2. data-offerid (часто содержит число)
    if not numeric_offer_id:
        elem = soup.find(attrs={"data-offerid": True})
        if elem:
            raw = elem["data-offerid"]
            if raw.isdigit():
                numeric_offer_id = raw
                logger.info(f"Найден data-offerid: {numeric_offer_id}")

    # 3. meta[itemprop="productID"] (иногда число)
    if not numeric_offer_id:
        meta = soup.find("meta", itemprop="productID")
        if meta and meta.get("content"):
            raw = meta["content"].strip()
            if raw.isdigit():
                numeric_offer_id = raw
                logger.info(f"Найден productID: {numeric_offer_id}")

    # 4. Из og:url (может содержать числовой ID в конце)
    if not numeric_offer_id:
        meta_og = soup.find("meta", property="og:url")
        if meta_og and meta_og.get("content"):
            og_url = meta_og.get("content", "")
            m = re.search(r'/(\d{7,})(?:\.html)?$', og_url)
            if m:
                numeric_offer_id = m.group(1)
                logger.info(f"Найден числовой ID в og:url: {numeric_offer_id}")

    # 5. Если ничего не нашли – фоллбэк на буквенный ID из URL (старый метод)
    if not numeric_offer_id:
        fallback_id = None
        m = re.search(r'/[^/]*[_-][iI][dD](\w+)\.html', url)
        if m:
            fallback_id = m.group(1)
        else:
            m = re.search(r'/(\d{7,})(?:\.html)?$', url)
            if m:
                fallback_id = m.group(1)
        if fallback_id:
            logger.warning(f"Числовой ID не найден, используется буквенный: {fallback_id}")
            numeric_offer_id = fallback_id
        else:
            return {"success": False, "error": "Не удалось извлечь ID объявления. Проверьте ссылку."}

    logger.info(f"Итоговый offer_id для API: {numeric_offer_id}")

    # ========== ЦЕНА И ПРЕДЛОЖЕНИЕ ==========
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

    api_url = f"https://www.olx.pl/api/v1/offers/{numeric_offer_id}/propose-price/"
    payload = {"price": str(proposal)}
    api_headers = {
        **headers,
        "Content-Type": "application/json",
        "Referer": url,
        "Origin": "https://www.olx.pl",
        "X-Requested-With": "XMLHttpRequest",
    }
    if csrf_token:
        api_headers["X-CSRF-Token"] = csrf_token

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
            err_text = api_resp.text[:300]
            logger.warning(f"API ответ {api_resp.status_code}: {err_text}")
            return {"success": False, "error": f"Ошибка API (код {api_resp.status_code}): {err_text}"}
    except Exception as e:
        return {"success": False, "error": f"Ошибка отправки предложения: {e}"}

# ========== HTML-страница ==========
HTML_PAGE = """<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>ROCKET OLX Sender</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; }
        h1 { color: #2c3e50; }
        label { display: block; margin-top: 10px; font-weight: bold; }
        input[type="text"], textarea { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; }
        button { padding: 10px 20px; margin-top: 10px; background: #27ae60; color: white; border: none; cursor: pointer; }
        button:hover { background: #219150; }
        .box { border: 1px solid #ccc; padding: 15px; margin: 20px 0; border-radius: 6px; }
        .error { color: red; }
        .success { color: green; }
    </style>
</head>
<body>
    <h1>🚀 ROCKET OLX Price Proposer</h1>

    <div class="box">
        <h3>Добавить куку</h3>
        <label>Имя куки:</label>
        <input type="text" id="cookieName" placeholder="Например: main">
        <label>Строка кук (из браузера):</label>
        <textarea id="cookieValue" rows="3" placeholder="sessionid=...; csrftoken=...;"></textarea>
        <button onclick="addCookie()">Добавить</button>
        <span id="addStatus"></span>
    </div>

    <div class="box">
        <h3>Отправить предложение</h3>
        <label>Выбрать куку:</label>
        <select id="cookieSelect">
            <option value="">-- Загружаю список... --</option>
        </select>
        <label>Ссылка на товар OLX:</label>
        <input type="text" id="offerUrl" placeholder="https://www.olx.pl/d/oferta/...">
        <button onclick="sendProposal()">Отправить предложение</button>
        <pre id="resultBox"></pre>
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

        async function sendProposal() {
            const cookie_name = document.getElementById('cookieSelect').value;
            const url = document.getElementById('offerUrl').value.trim();
            const resultBox = document.getElementById('resultBox');
            resultBox.textContent = '⏳ Отправка...';
            if (!cookie_name || !url) {
                resultBox.textContent = '❌ Выберите куку и укажите ссылку';
                return;
            }
            const resp = await fetch('/api/propose', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ cookie_name, url })
            });
            const data = await resp.json();
            if (data.success) {
                resultBox.textContent = `✅ Предложение отправлено!\\nОригинальная цена: ${data.original_price} zł\\nПредложено: ${data.proposed_price} zł\\nСсылка: ${data.url}`;
            } else {
                resultBox.textContent = `❌ Ошибка: ${data.error}`;
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
