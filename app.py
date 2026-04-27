# app.py - Evolusis WhatsApp Nudge & Notification Bot
import os
import json
import logging
import traceback
import re
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import requests
import psycopg2
import psycopg2.extras

# --- Environment Setup ---
load_dotenv()

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "").strip()
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "stepbot_verify")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", 10000))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))   # seconds between DB polls
WELCOMED_USERS_FILE = os.getenv("WELCOMED_USERS_FILE", "welcomed_users.json")

# --- Logging ---
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("evolusis-notifier")

logger.info("=" * 60)
logger.info("EVOLUSIS NOTIFIER STARTUP")
logger.info("META configured: %s", bool(META_ACCESS_TOKEN and META_PHONE_NUMBER_ID))
logger.info("DATABASE_URL present: %s", bool(DATABASE_URL))
logger.info("POLL_INTERVAL: %ds", POLL_INTERVAL)
logger.info("=" * 60)

# --- Flask App ---
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# ---------------------------------------------------------------------------
# Welcomed-users persistence
# Bot state is persisted in a single PostgreSQL table `bot_messages` that
# records every message sent — template name, params, meta message ID and
# timestamp.  On startup we derive bot_start_time and welcomed_ids from it
# so the state survives Render restarts with zero local files needed.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS wpbot_messages (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER,
    phone           TEXT,
    template_name   TEXT        NOT NULL,
    params          JSONB,
    meta_message_id TEXT,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

def _ensure_table():
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_INIT_SQL)
        conn.close()
    except Exception as exc:
        logger.warning("Could not create bot_messages table: %s", exc)


def _load_state() -> tuple:
    """Returns (bot_start_time: datetime, welcomed_ids: set) derived from bot_messages."""
    _ensure_table()
    if not DATABASE_URL:
        return datetime.utcnow(), set()
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            # bot_start_time = earliest BOT_START marker, or now if first run
            cur.execute(
                "SELECT params FROM wpbot_messages WHERE template_name = 'BOT_START' ORDER BY sent_at ASC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                start_time = datetime.fromisoformat(row[0]["start_time"])
            else:
                start_time = datetime.utcnow()
                cur.execute(
                    "INSERT INTO wpbot_messages (template_name, params) VALUES ('BOT_START', %s)",
                    (psycopg2.extras.Json({"start_time": start_time.isoformat()}),)
                )
            # welcomed_ids = all user_ids that received the welcome template
            cur.execute(
                "SELECT DISTINCT user_id FROM wpbot_messages WHERE user_id IS NOT NULL AND template_name <> 'BOT_START'"
            )
            ids = set(r[0] for r in cur.fetchall())
        conn.close()
        logger.info("Loaded state from DB: start_time=%s, welcomed=%d", start_time.isoformat(), len(ids))
        return start_time, ids
    except Exception as exc:
        logger.warning("Could not load state from DB (%s) – treating as fresh start", exc)
    return datetime.utcnow(), set()


def _log_sent_message(user_id, phone: str, template_name: str, params: list, meta_message_id: str):
    """Insert a row into bot_messages for every successfully sent message."""
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO wpbot_messages (user_id, phone, template_name, params, meta_message_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (user_id, phone, template_name,
                 psycopg2.extras.Json(params), meta_message_id)
            )
        conn.close()
    except Exception as exc:
        logger.error("Could not log sent message: %s", exc)


def _save_state(start_time: datetime, ids: set):
    """No-op: state is written per-message via _log_sent_message."""
    pass


bot_start_time, welcomed_ids = _load_state()
logger.info("Bot start time (UTC): %s", bot_start_time.isoformat())

# ---------------------------------------------------------------------------
# Database helpers  (public.users schema)
# ---------------------------------------------------------------------------

def _db_connect():
    return psycopg2.connect(DATABASE_URL, connect_timeout=5)


def fetch_users_with_phone() -> list:
    """Return all users that have a phone number."""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set – skipping DB query")
        return []
    try:
        conn = _db_connect()
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, first_name, last_name, global_role,
                       is_verified, created_at, updated_at, phone
                FROM public.users
                WHERE phone IS NOT NULL AND trim(phone) <> ''
                ORDER BY created_at ASC
            """)
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("fetch_users_with_phone failed: %s", exc)
        return []


def get_user_by_id(user_id: int) -> dict:
    if not DATABASE_URL:
        return {}
    try:
        conn = _db_connect()
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, first_name, last_name, global_role,
                       is_verified, created_at, updated_at, phone
                FROM public.users WHERE id = %s
            """, (user_id,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as exc:
        logger.error("get_user_by_id failed: %s", exc)
        return {}


def get_user_by_email(email: str) -> dict:
    if not DATABASE_URL:
        return {}
    try:
        conn = _db_connect()
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, first_name, last_name, global_role,
                       is_verified, created_at, updated_at, phone
                FROM public.users WHERE email = %s
            """, (email,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as exc:
        logger.error("get_user_by_email failed: %s", exc)
        return {}

# ---------------------------------------------------------------------------
# WhatsApp sending
# ---------------------------------------------------------------------------

WELCOME_TEMPLATE_NAME = os.getenv("WELCOME_TEMPLATE_NAME", "wellcome")
WELCOME_TEMPLATE_LANG = os.getenv("WELCOME_TEMPLATE_LANG", "en")


def _post_to_meta(phone: str, payload: dict) -> str | None:
    """Send a payload to Meta WhatsApp Cloud API. Returns meta_message_id on success, None on failure."""
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text
        logger.info("Meta API status=%s phone=%s body=%s", resp.status_code, phone, resp_body)
        if resp.status_code == 200:
            if isinstance(resp_body, dict) and resp_body.get("error"):
                logger.error("❌ Meta returned 200 but with error: %s", resp_body["error"])
                return None
            meta_id = None
            if isinstance(resp_body, dict):
                messages = resp_body.get("messages", [])
                if messages:
                    meta_id = messages[0].get("id")
            logger.info("✅ Message sent to %s (id=%s)", phone, meta_id)
            return meta_id or "accepted"
        logger.error("❌ Meta API %s: %s", resp.status_code, resp_body)
        return None
    except Exception as exc:
        logger.error("❌ _post_to_meta exception: %s", exc)
        return None


def send_template_message(phone: str, template_name: str, lang: str, params: list, user_id=None) -> bool:
    """Send an approved WhatsApp message template with body parameters."""
    if not META_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        logger.error("Meta credentials not configured")
        return False
    phone = re.sub(r"\D", "", phone)
    if not phone:
        logger.error("Invalid phone number after normalization")
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in params]
                }
            ]
        }
    }
    meta_id = _post_to_meta(phone, payload)
    if meta_id:
        _log_sent_message(user_id, phone, template_name, params, meta_id)
        return True
    return False


# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------

def send_welcome(user: dict) -> bool:
    phone = (user.get("phone") or "").strip()
    if not phone:
        logger.info("User id=%s has no phone – skipping welcome", user.get("id"))
        return False
    first_name = (user.get("first_name") or "there").strip()
    logger.info("Sending welcome to user id=%s phone=%s", user.get("id"), phone)
    return send_template_message(phone, WELCOME_TEMPLATE_NAME, WELCOME_TEMPLATE_LANG, [first_name], user_id=user.get("id"))

# ---------------------------------------------------------------------------
# Poller – detect new sign-ups and welcome them
# ---------------------------------------------------------------------------

def check_and_welcome_new_users():
    """Welcome users created after bot_start_time who haven't been welcomed yet."""
    global welcomed_ids, bot_start_time
    users = fetch_users_with_phone()
    if not users:
        return

    new_users = []
    for u in users:
        if u["id"] in welcomed_ids:
            continue
        # Only welcome users created AFTER this bot instance started.
        # created_at comes back as a timezone-aware datetime from psycopg2.
        created = u.get("created_at")
        if created is None:
            continue
        # Normalise to UTC naive for comparison
        if hasattr(created, "utcoffset") and created.utcoffset() is not None:
            created = created.replace(tzinfo=None) - created.utcoffset()
        if created >= bot_start_time:
            new_users.append(u)

    if not new_users:
        logger.debug("No new users to welcome")
        return

    logger.info("Found %d new user(s) to welcome", len(new_users))
    sent = set()
    for user in new_users:
        if send_welcome(user):
            sent.add(user["id"])
        else:
            logger.warning("Failed to send welcome to user id=%s", user.get("id"))

    if sent:
        with _state_lock:
            welcomed_ids |= sent
            _save_state(bot_start_time, welcomed_ids)


def _poller_loop():
    logger.info("Poller thread started (every %ds)", POLL_INTERVAL)
    while True:
        try:
            check_and_welcome_new_users()
        except Exception as exc:
            logger.error("Poller error: %s", exc)
            logger.debug(traceback.format_exc())
        time.sleep(POLL_INTERVAL)

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "HEAD"])
def root():
    if request.method == "HEAD":
        return "", 200
    return jsonify({"status": "ok", "service": "evolusis-notifier"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "welcomed_users_count": len(welcomed_ids),
        "meta_configured": bool(META_ACCESS_TOKEN and META_PHONE_NUMBER_ID),
        "db_configured": bool(DATABASE_URL)
    }), 200


@app.route("/meta-webhook", methods=["GET"])
def meta_webhook_verify():
    """Meta webhook verification handshake."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("✅ Webhook verified")
        return challenge, 200
    logger.warning("❌ Webhook verification failed")
    return jsonify({"error": "Verification failed"}), 403


@app.route("/meta-webhook", methods=["POST"])
def meta_webhook_incoming():
    """Receive incoming WhatsApp messages – acknowledged but not processed
    (this is a notification bot, not a chatbot)."""
    return jsonify({"status": "ok"}), 200


@app.route("/notify-welcome", methods=["POST"])
def notify_welcome():
    """Trigger a welcome message for a specific user.

    Call this from your signup flow as soon as a user registers.
    Body (JSON): {"user_id": 5}  OR  {"email": "user@example.com"}
    """
    global welcomed_ids
    body = request.get_json(silent=True) or {}

    user_id = body.get("user_id")
    email = body.get("email")

    if user_id:
        user = get_user_by_id(int(user_id))
    elif email:
        user = get_user_by_email(str(email))
    else:
        return jsonify({"error": "Provide user_id or email"}), 400

    if not user:
        return jsonify({"error": "User not found"}), 404

    uid = user["id"]

    with _state_lock:
        if uid in welcomed_ids:
            return jsonify({"status": "already_welcomed", "user_id": uid}), 200

    success = send_welcome(user)
    if success:
        with _state_lock:
            welcomed_ids.add(uid)
            _save_state(bot_start_time, welcomed_ids)
        return jsonify({"status": "welcome_sent", "user_id": uid}), 200

    return jsonify({"error": "Failed to send welcome message"}), 500


@app.route("/force-welcome", methods=["POST"])
def force_welcome():
    """Force-send a welcome message regardless of welcomed state (for testing).

    Body (JSON): {"user_id": 5}  OR  {"phone": "919321503773"}
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    phone = body.get("phone")

    if user_id:
        user = get_user_by_id(int(user_id))
    elif phone:
        # Build a minimal user dict for direct phone test
        user = {"id": 0, "first_name": "there", "phone": str(phone)}
    else:
        return jsonify({"error": "Provide user_id or phone"}), 400

    if not user:
        return jsonify({"error": "User not found"}), 404

    success = send_welcome(user)
    if success:
        return jsonify({"status": "welcome_sent"}), 200
    return jsonify({"error": "Failed to send welcome message"}), 500


@app.route("/trigger-check", methods=["POST"])
def trigger_check():
    """Manually trigger a new-user check (useful for testing)."""
    try:
        check_and_welcome_new_users()
        return jsonify({
            "status": "ok",
            "welcomed_users_count": len(welcomed_ids),
            "bot_start_time": bot_start_time.isoformat()
        }), 200
    except Exception as exc:
        logger.error("trigger-check error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/debug-send", methods=["POST"])
def debug_send():
    """Return the raw Meta API response – use for diagnosing template errors.

    Body (JSON): {"phone": "919321503773", "lang": "en_US"}
    """
    body = request.get_json(silent=True) or {}
    phone = re.sub(r"\D", "", str(body.get("phone", "919321503773")))
    lang = body.get("lang", WELCOME_TEMPLATE_LANG)
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": WELCOME_TEMPLATE_NAME,
            "language": {"code": lang},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": "Suyash"}]}
            ]
        }
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    try:
        resp_body = resp.json()
    except Exception:
        resp_body = resp.text
    return jsonify({"http_status": resp.status_code, "meta_response": resp_body}), 200


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    logger.error("500 error: %s", e)
    return jsonify({"error": "Internal server error"}), 500

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Start background poller
_poller_thread = threading.Thread(target=_poller_loop, daemon=True)
_poller_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)
