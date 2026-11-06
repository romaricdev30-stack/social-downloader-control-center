"""Multi-bot Social Downloader: Flask dashboard + Telegram webhooks + secured API."""
import os, re, uuid, hmac, hashlib, secrets, logging, html
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg
import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, request, redirect, url_for, session, render_template, flash
from psycopg.rows import dict_row

from drift import get_fb_video, download_insta_reel, download_snapchat, get_pinterest_download_links, download_youtube

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("social-downloader")
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY", ""),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    MAX_CONTENT_LENGTH=256 * 1024,
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
FERNET_KEY = os.getenv("FERNET_KEY", "").strip()
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "SocialDownloaderControl/2.0"})

PLATFORMS = {
    "facebook": {"hosts": {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"}, "label": "Facebook", "route": "/facebook"},
    "instagram": {"hosts": {"instagram.com", "www.instagram.com"}, "label": "Instagram", "route": "/Instagram"},
    "snapchat": {"hosts": {"snapchat.com", "www.snapchat.com", "story.snapchat.com"}, "label": "Snapchat", "route": "/snap"},
    "pinterest": {"hosts": {"pinterest.com", "www.pinterest.com", "pin.it"}, "label": "Pinterest", "route": "/Pinterest"},
    "youtube": {"hosts": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}, "label": "YouTube", "route": "/youtube"},
}


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL manquant : configure une base PostgreSQL persistante.")
    conn = psycopg.connect(DATABASE_URL, connect_timeout=8, row_factory=dict_row)
    conn.execute("""CREATE TABLE IF NOT EXISTS managed_bots (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL,
        token_enc TEXT NOT NULL, webhook_secret_enc TEXT NOT NULL,
        webhook_secret_hash TEXT NOT NULL, api_key_hash TEXT NOT NULL,
        api_key_prefix TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
        downloads BIGINT NOT NULL DEFAULT 0, last_seen TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    conn.commit()
    return conn


def cipher():
    if not FERNET_KEY:
        raise RuntimeError("FERNET_KEY manquant.")
    return Fernet(FERNET_KEY.encode())


def encrypt(value): return cipher().encrypt(value.encode()).decode()
def decrypt(value): return cipher().decrypt(value.encode()).decode()
def digest(value): return hashlib.sha256(value.encode()).hexdigest()


def platform_for(raw_url):
    if not isinstance(raw_url, str) or len(raw_url) > 2048: return None
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname: return None
        host = parsed.hostname.lower().rstrip(".")
        for key, cfg in PLATFORMS.items():
            if host in cfg["hosts"]: return key
    except Exception: return None
    return None


def extract(raw_url, platform):
    funcs = {"facebook": get_fb_video, "instagram": download_insta_reel,
             "snapchat": download_snapchat, "pinterest": get_pinterest_download_links,
             "youtube": download_youtube}
    return funcs[platform](raw_url)


def normalize_result(result):
    items = []
    raw_items = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
    title = "Média détecté"
    if isinstance(result, dict):
        title = result.get("title") or "Média détecté"
        video = result.get("video")
        if isinstance(video, dict):
            title = video.get("title") or title
            raw_items = [video]
        elif isinstance(video, list):
            raw_items = video
    for item in raw_items:
        if not isinstance(item, dict): continue
        candidates = [
            (item.get("video"), "video", item.get("quality") or "Vidéo"),
            (item.get("photo"), "photo", "Photo"), (item.get("image"), "photo", "Image"),
            (item.get("Download Link"), "video", item.get("Quality") or item.get("ext") or "Vidéo"),
            (item.get("url"), "video", item.get("quality") or item.get("format_note") or "Vidéo"),
            (item.get("hd"), "video", "HD"), (item.get("sd"), "video", "SD"),
        ]
        for media_url, kind, quality in candidates:
            if isinstance(media_url, str) and media_url.startswith(("https://", "http://")) and media_url != "Not available":
                if not any(x["url"] == media_url for x in items):
                    items.append({"url": media_url, "kind": kind, "quality": str(quality), "title": item.get("Title") or item.get("title") or title})
    return title, items[:5]


def telegram(token, method, payload):
    r = HTTP.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
    data = r.json()
    if not r.ok or not data.get("ok"):
        raise RuntimeError(data.get("description", f"Telegram HTTP {r.status_code}"))
    return data.get("result")


def send_text(token, chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    return telegram(token, "sendMessage", payload)


def send_media(token, chat_id, media):
    caption = f"<b>{html.escape(str(media['quality']))}</b>\n{html.escape(str(media['title'])[:700])}"
    try:
        method = "sendPhoto" if media["kind"] == "photo" else "sendVideo"
        field = "photo" if method == "sendPhoto" else "video"
        payload = {"chat_id": chat_id, field: media["url"], "caption": caption, "parse_mode": "HTML"}
        if method == "sendVideo": payload["supports_streaming"] = True
        telegram(token, method, payload)
    except Exception as exc:
        log.info("Telegram remote media send failed: %s", exc)
        safe_url = html.escape(str(media["url"]), quote=True)
        send_text(token, chat_id, f"{caption}\n\n🔗 <a href=\"{safe_url}\">Ouvrir le média</a>")


def process_message(bot, message):
    token = decrypt(bot["token_enc"])
    chat_id = (message.get("chat") or {}).get("id")
    body = (message.get("text") or message.get("caption") or "").strip()
    if not chat_id: return
    if body.startswith("/start"):
        keyboard = {"inline_keyboard": [[{"text": "✨ Comment utiliser", "callback_data": "help"}, {"text": "🌐 Plateformes", "callback_data": "platforms"}]]}
        welcome = (
            "🚀 <b>WELCOME TO SOCIAL DOWNLOADER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🎬 <b>Ton hub multimédia intelligent</b>\n"
            "Un seul lien. Une analyse. Le média disponible à portée de main.\n\n"
            "⚡ <b>5 plateformes prises en charge</b>\n"
            "📘 Facebook  •  📸 Instagram\n"
            "👻 Snapchat  •  📌 Pinterest\n"
            "▶️ YouTube\n\n"
            "<b>Comment commencer ?</b>\n"
            "① Copie un lien public.\n② Colle-le simplement ici.\n③ Je détecte la plateforme et je lance l'analyse.\n\n"
            "🛡️ <i>Respecte les droits d'auteur et la vie privée. Utilise uniquement les contenus que tu es autorisé à enregistrer.</i>\n\n"
            "✨ <b>Prêt ? Envoie ton premier lien !</b>"
        )
        send_text(token, chat_id, welcome, keyboard); return
    if body.startswith("/help"):
        send_text(token, chat_id, "🧭 <b>GUIDE RAPIDE</b>\n\n1. Envoie un lien public Facebook, Instagram, Snapchat, Pinterest ou YouTube.\n2. Patiente pendant l'analyse.\n3. Reçois le média ou un lien direct lorsqu'il est disponible.\n\n⚠️ Les contenus privés, protégés ou non accessibles ne peuvent pas être récupérés.")
        return
    if body.startswith("/platforms"):
        send_text(token, chat_id, "🌐 <b>PLATEFORMES DISPONIBLES</b>\n\n📘 Facebook\n📸 Instagram\n👻 Snapchat\n📌 Pinterest\n▶️ YouTube\n\nColle un lien pour commencer.")
        return
    match = re.search(r"https?://[^\s<>]+", body)
    if not match:
        send_text(token, chat_id, "👋 Envoie simplement un lien public d'une des cinq plateformes prises en charge. Tape /help pour voir le guide.")
        return
    media_url = match.group(0).rstrip(".,!?)]}")
    platform = platform_for(media_url)
    if not platform:
        send_text(token, chat_id, "❌ Je ne reconnais pas ce lien. Essaie Facebook, Instagram, Snapchat, Pinterest ou YouTube."); return
    telegram(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
    send_text(token, chat_id, f"🔎 <b>Analyse {PLATFORMS[platform]['label']} en cours…</b>\n⚙️ Vérification des médias disponibles.")
    try:
        result = extract(media_url, platform)
        if isinstance(result, dict) and result.get("status") == "error":
            send_text(token, chat_id, "⚠️ Le service source n'a pas pu traiter ce lien. Réessaie plus tard."); return
        title, items = normalize_result(result)
        if not items:
            send_text(token, chat_id, "🛰️ Aucun média exploitable n'a été trouvé. Le contenu peut être indisponible ou le service externe temporairement défaillant."); return
        with db() as conn:
            conn.execute("UPDATE managed_bots SET downloads=downloads+1,last_seen=NOW() WHERE id=%s", (bot["id"],))
        send_text(token, chat_id, f"✅ <b>Analyse terminée</b>\n🌐 {PLATFORMS[platform]['label']}\n📦 {len(items)} média(s) disponible(s)\n📝 {title[:220]}")
        for item in items: send_media(token, chat_id, item)
    except Exception:
        log.exception("Extraction failed")
        send_text(token, chat_id, "⚠️ Une erreur est survenue pendant l'analyse. Le service peut être momentanément indisponible.")


def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin_ok"):
            if request.path.startswith("/admin/api/"): return jsonify({"ok": False, "error": "Authentification requise"}), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def csrf_ok():
    return hmac.compare_digest(session.get("csrf", ""), request.headers.get("X-CSRF-Token", ""))


def get_bot(bot_id):
    with db() as conn:
        return conn.execute("SELECT * FROM managed_bots WHERE id=%s", (bot_id,)).fetchone()


def api_bot_from_key():
    key = request.headers.get("X-API-Key", "")
    if not key: return None
    with db() as conn:
        return conn.execute("SELECT * FROM managed_bots WHERE api_key_hash=%s AND enabled=TRUE", (digest(key),)).fetchone()


@app.get("/")
def home():
    return jsonify({"service": "Social Downloader Control", "status": "online", "dashboard": "/dashboard", "api": "/v1/download", "platforms": list(PLATFORMS)})


@app.get("/login")
def login():
    return render_template("login.html")


@app.post("/login")
def login_post():
    if not DASHBOARD_PASSWORD or not app.secret_key:
        return "Dashboard auth is not configured", 503
    supplied = request.form.get("password", "")
    if hmac.compare_digest(supplied, DASHBOARD_PASSWORD):
        session.clear(); session["admin_ok"] = True; session["csrf"] = secrets.token_urlsafe(32)
        return redirect(url_for("dashboard"))
    return render_template("login.html", error="Mot de passe incorrect."), 401


@app.post("/logout")
@admin_required
def logout():
    if not csrf_ok(): return "CSRF check failed", 403
    session.clear(); return redirect(url_for("login"))


@app.get("/dashboard")
@admin_required
def dashboard():
    if not session.get("csrf"): session["csrf"] = secrets.token_urlsafe(32)
    return render_template("dashboard.html", csrf=session["csrf"])


@app.get("/admin/api/overview")
@admin_required
def overview():
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS bots, COUNT(*) FILTER (WHERE enabled) AS active, COALESCE(SUM(downloads),0) AS downloads FROM managed_bots").fetchone()
    return jsonify(row)


@app.get("/admin/api/bots")
@admin_required
def list_bots():
    with db() as conn:
        rows = conn.execute("SELECT id,name,username,api_key_prefix,enabled,downloads,last_seen,created_at FROM managed_bots ORDER BY created_at DESC").fetchall()
    return jsonify([dict(x) for x in rows])


@app.post("/admin/api/bots")
@admin_required
def add_bot():
    if not csrf_ok(): return jsonify({"error": "CSRF invalide"}), 403
    data = request.get_json(silent=True) or {}
    name, token = (data.get("name") or "Mon bot").strip()[:80], (data.get("token") or "").strip()
    if not token or len(token) > 200: return jsonify({"error": "Token Telegram invalide"}), 400
    try:
        me = HTTP.get(f"https://api.telegram.org/bot{token}/getMe", timeout=12).json()
        if not me.get("ok"): return jsonify({"error": "Telegram n'a pas reconnu ce token."}), 400
        user = me["result"]
        bot_id = str(uuid.uuid4())
        webhook_secret = secrets.token_urlsafe(30).replace(".", "_")[:48]
        api_key = "sda_" + secrets.token_urlsafe(32)
        with db() as conn:
            conn.execute("INSERT INTO managed_bots(id,name,username,token_enc,webhook_secret_enc,webhook_secret_hash,api_key_hash,api_key_prefix) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (bot_id, name or user.get("first_name", "Bot"), user.get("username", ""), encrypt(token), encrypt(webhook_secret), digest(webhook_secret), digest(api_key), api_key[:12]))
        webhook_ok = False; webhook_error = None
        if PUBLIC_BASE_URL:
            try:
                telegram(token, "setWebhook", {"url": f"{PUBLIC_BASE_URL}/telegram/webhook/{bot_id}", "secret_token": webhook_secret, "allowed_updates": ["message", "callback_query"]})
                webhook_ok = True
            except Exception as exc: webhook_error = str(exc)[:250]
        return jsonify({"ok": True, "bot": {"id": bot_id, "name": name, "username": user.get("username")}, "api_key": api_key, "webhook_configured": webhook_ok, "webhook_error": webhook_error}), 201
    except Exception as exc:
        log.exception("Could not add bot")
        return jsonify({"error": str(exc)[:250]}), 500


@app.post("/admin/api/bots/<bot_id>/webhook")
@admin_required
def refresh_webhook(bot_id):
    if not csrf_ok(): return jsonify({"error": "CSRF invalide"}), 403
    bot = get_bot(bot_id)
    if not bot: return jsonify({"error": "Bot introuvable"}), 404
    if not PUBLIC_BASE_URL: return jsonify({"error": "PUBLIC_BASE_URL non configurée"}), 503
    secret = decrypt(bot["webhook_secret_enc"])
    result = telegram(decrypt(bot["token_enc"]), "setWebhook", {"url": f"{PUBLIC_BASE_URL}/telegram/webhook/{bot_id}", "secret_token": secret, "allowed_updates": ["message", "callback_query"]})
    return jsonify({"ok": bool(result), "message": "Webhook configuré"})


@app.post("/admin/api/bots/<bot_id>/toggle")
@admin_required
def toggle_bot(bot_id):
    if not csrf_ok(): return jsonify({"error": "CSRF invalide"}), 403
    with db() as conn:
        row = conn.execute("UPDATE managed_bots SET enabled=NOT enabled WHERE id=%s RETURNING enabled", (bot_id,)).fetchone()
    if not row: return jsonify({"error": "Bot introuvable"}), 404
    return jsonify({"ok": True, "enabled": row["enabled"]})


@app.post("/admin/api/bots/<bot_id>/rotate-key")
@admin_required
def rotate_key(bot_id):
    if not csrf_ok(): return jsonify({"error": "CSRF invalide"}), 403
    key = "sda_" + secrets.token_urlsafe(32)
    with db() as conn:
        row = conn.execute("UPDATE managed_bots SET api_key_hash=%s,api_key_prefix=%s WHERE id=%s RETURNING id", (digest(key), key[:12], bot_id)).fetchone()
    if not row: return jsonify({"error": "Bot introuvable"}), 404
    return jsonify({"ok": True, "api_key": key})


@app.delete("/admin/api/bots/<bot_id>")
@admin_required
def delete_bot(bot_id):
    if not csrf_ok(): return jsonify({"error": "CSRF invalide"}), 403
    bot = get_bot(bot_id)
    if not bot: return jsonify({"error": "Bot introuvable"}), 404
    try: telegram(decrypt(bot["token_enc"]), "deleteWebhook", {"drop_pending_updates": False})
    except Exception: log.warning("Could not delete remote webhook for %s", bot_id)
    with db() as conn: conn.execute("DELETE FROM managed_bots WHERE id=%s", (bot_id,))
    return jsonify({"ok": True})


@app.post("/telegram/webhook/<bot_id>")
def telegram_webhook(bot_id):
    bot = get_bot(bot_id)
    if not bot or not bot["enabled"]: return jsonify({"ok": True})
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(digest(received), bot["webhook_secret_hash"]): return jsonify({"ok": False}), 401
    update = request.get_json(silent=True) or {}
    try:
        if update.get("message"):
            process_message(bot, update["message"])
        elif update.get("callback_query"):
            cb = update["callback_query"]; token = decrypt(bot["token_enc"])
            telegram(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            if chat_id and cb.get("data") == "help":
                send_text(token, chat_id, "🧭 <b>GUIDE RAPIDE</b>\nEnvoie un lien public, je détecte la plateforme, puis je cherche les médias disponibles. Tape /platforms pour la liste.")
            elif chat_id and cb.get("data") == "platforms":
                send_text(token, chat_id, "🌐 Facebook • Instagram • Snapchat • Pinterest • YouTube")
        return jsonify({"ok": True})
    except Exception:
        log.exception("Webhook processing error")
        return jsonify({"ok": True})


@app.post("/v1/download")
def v1_download():
    bot = api_bot_from_key()
    if not bot: return jsonify({"ok": False, "error": "Clé API absente ou invalide"}), 401
    data = request.get_json(silent=True) or {}
    raw_url = (data.get("url") or "").strip()
    platform = platform_for(raw_url)
    if not platform: return jsonify({"ok": False, "error": "URL non prise en charge"}), 400
    try:
        result = extract(raw_url, platform)
        title, items = normalize_result(result)
        with db() as conn: conn.execute("UPDATE managed_bots SET downloads=downloads+1,last_seen=NOW() WHERE id=%s", (bot["id"],))
        return jsonify({"ok": bool(items), "platform": platform, "title": title, "items": items})
    except Exception:
        log.exception("API download failed")
        return jsonify({"ok": False, "error": "Extraction indisponible pour le moment"}), 502


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store" if request.path.startswith(("/admin", "/dashboard", "/login")) else "no-cache"
    return response


@app.errorhandler(413)
def too_large(_): return jsonify({"ok": False, "error": "Requête trop volumineuse"}), 413
