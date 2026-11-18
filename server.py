#!/usr/bin/env python3
"""
Локальный сервер портфолио + API для админки.

Запуск:   python3 server.py
Сайт:     http://localhost:8000
Админка:  http://localhost:8000/admin/   (логин admin, пароль из ADMIN_PASSWORD, по умолчанию admin)

Только стандартная библиотека Python, ничего ставить не нужно.
"""
import base64
import json
import os
import re
import shutil
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(ROOT, "content.json")
BACKUPS = os.path.join(ROOT, "backups")
UPLOADS = os.path.join(ROOT, "covers")
PORT = int(os.environ.get("PORT", "8000"))
USER = os.environ.get("ADMIN_USER", "admin")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
MAX_UPLOAD = 50 * 1024 * 1024
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".mp4", ".webm"}
HIDDEN = ("/server.py", "/backups")

# Если установлен Pillow (pip3 install pillow), PNG/JPG при загрузке
# автоматически сжимаются в WebP. Без него файлы сохраняются как есть.
try:
    from PIL import Image
except ImportError:
    Image = None


def to_webp(raw):
    """PNG/JPG → WebP. Длинные скриншоты сайтов ужимаются до 1440 px по ширине."""
    import io
    im = Image.open(io.BytesIO(raw))
    w, h = im.size
    long_shot = h > 3000
    if long_shot and w > 1440:
        im = im.resize((1440, round(h * 1440 / w)), Image.LANCZOS)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")
    out = io.BytesIO()
    im.save(out, "WEBP", quality=80 if long_shot else 85, method=6)
    return out.getvalue()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # ---------- helpers ----------
    def _authorized(self):
        expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        return self.headers.get("Authorization", "") == f"Basic {expected}"

    def _ask_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Portfolio admin", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def end_headers(self):
        # json и админку не кэшируем, чтобы правки были видны сразу
        if self.path.split("?")[0].endswith(".json") or self.path.startswith("/admin"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith(HIDDEN):
            self.send_error(404)
            return
        if path.startswith("/admin") and not self._authorized():
            self._ask_auth()
            return
        super().do_GET()

    # ---------- POST ----------
    def do_POST(self):
        if not self._authorized():
            self._ask_auth()
            return
        url = urlparse(self.path)
        if url.path == "/admin/api/content":
            self._save_content()
        elif url.path == "/admin/api/upload":
            self._upload(parse_qs(url.query).get("name", [""])[0])
        else:
            self.send_error(404)

    def _save_content(self):
        raw = self._read_body(5 * 1024 * 1024)
        if raw is None:
            return self._json(400, {"error": "Пустой или слишком большой запрос"})
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            return self._json(400, {"error": f"Некорректный JSON: {e}"})
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list) or not isinstance(data.get("sites"), list):
            return self._json(400, {"error": "Нет списков projects / sites"})
        keys = [i.get("key") for i in data["projects"] + data["sites"]]
        if any(not k for k in keys) or len(keys) != len(set(keys)):
            return self._json(400, {"error": "У каждой записи должен быть уникальный ключ (key)"})

        # разделы, которых нет в запросе (например, из старой вкладки админки), не стираем
        if os.path.exists(CONTENT):
            try:
                with open(CONTENT, encoding="utf-8") as f:
                    existing = json.load(f)
                for key, value in existing.items():
                    data.setdefault(key, value)
            except (ValueError, OSError):
                pass
        data.pop("status", None)  # устаревшее поле, теперь статус живёт в footer

        # резервная копия предыдущей версии
        os.makedirs(BACKUPS, exist_ok=True)
        if os.path.exists(CONTENT):
            shutil.copy2(CONTENT, os.path.join(BACKUPS, time.strftime("content-%Y%m%d-%H%M%S.json")))
        tmp = CONTENT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONTENT)
        self._json(200, {"ok": True, "savedAt": time.strftime("%H:%M:%S")})

    def _upload(self, name):
        base, ext = os.path.splitext(os.path.basename(name))
        ext = ext.lower()
        if ext not in ALLOWED_EXT:
            return self._json(400, {"error": f"Формат {ext or '?'} не поддерживается"})
        base = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-") or "file"
        raw = self._read_body(MAX_UPLOAD)
        if raw is None:
            return self._json(400, {"error": "Файл пустой или больше 50 МБ"})
        if Image is not None and ext in (".png", ".jpg", ".jpeg"):
            try:
                raw, ext = to_webp(raw), ".webp"
            except Exception as e:  # битый файл и т.п. — сохраняем как есть
                print("WebP conversion skipped:", e)
        os.makedirs(UPLOADS, exist_ok=True)
        filename, n = base + ext, 1
        while os.path.exists(os.path.join(UPLOADS, filename)):
            n += 1
            filename = f"{base}-{n}{ext}"
        with open(os.path.join(UPLOADS, filename), "wb") as f:
            f.write(raw)
        self._json(200, {"ok": True, "path": f"covers/{filename}"})


if __name__ == "__main__":
    os.chdir(ROOT)
    print(f"Сайт:     http://localhost:{PORT}")
    print(f"Админка:  http://localhost:{PORT}/admin/   (логин: {USER})")
    if Image is None:
        print("ℹ️  Pillow не установлен: картинки загружаются без сжатия (pip3 install pillow).")
    if PASSWORD == "admin":
        print("⚠️  Пароль по умолчанию «admin». Перед выкладкой на сервер задай ADMIN_PASSWORD.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
