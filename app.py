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
# We track which user IDs have already received a welcome message using a
# local JSON file so the state survives app restarts.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()


def _load_state() -> tuple:
    """Returns (bot_start_time: datetime, welcomed_ids: set)."""
    try:
        if os.path.exists(WELCOMED_USERS_FILE):
            with open(WELCOMED_USERS_FILE, "r") as fh:
                data = json.load(fh)
            start_time = datetime.fromisoformat(data["start_time"])
            ids = set(data.get("ids", []))
            logger.info("Loaded state: start_time=%s, welcomed=%d", start_time.isoformat(), len(ids))
            return start_time, ids
    except Exception as exc:
        logger.warning("Could not load state file (%s) – treating as fresh start", exc)
    now = datetime.utcnow()
    return now, set()


def _save_state(start_time: datetime, ids: set):
    try:
        with open(WELCOMED_USERS_FILE, "w") as fh:
            json.dump({"start_time": start_time.isoformat(), "ids": list(ids)}, fh)
    except Exception as exc:
        logger.error("Could not save state: %s", exc)


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

def send_whatsapp_message(phone: str, text: str) -> bool:
    """Send a plain-text WhatsApp message via Meta Cloud API."""
    if not META_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        logger.error("Meta credentials not configured")
        return False

    # Normalize: digits only, no leading +
    phone = re.sub(r"\D", "", phone)
    if not phone:
        logger.error("Invalid phone number after normalization")
        return False

    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
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
            # Meta can return 200 with an error object — check for it
            if isinstance(resp_body, dict) and resp_body.get("error"):
                logger.error("❌ Meta returned 200 but with error: %s", resp_body["error"])
                return False
            logger.info("✅ Message sent to %s", phone)
            return True
        logger.error("❌ Meta API %s: %s", resp.status_code, resp_body)
        return False
    except Exception as exc:
        logger.error("❌ send_whatsapp_message exception: %s", exc)
        return False

# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------

WELCOME_MESSAGE = """\
👋 Hey {first_name}, welcome to Evolusis!

You've just joined a platform built to help sales professionals perform at their best.

Here's what's in store for you:
• 📚 A personalised playbook modelled on your top performers
• 🤖 An AI coach ready to help with real workplace challenges
• 🎯 AI roleplay so you can practise before the pressure is on
• 📲 Nudges & updates delivered right here on WhatsApp

Your growth journey starts now. Visit *evolusis.com* to explore your dashboard. 🚀"""


def send_welcome(user: dict) -> bool:
    phone = (user.get("phone") or "").strip()
    if not phone:
        logger.info("User id=%s has no phone – skipping welcome", user.get("id"))
        return False
    first_name = (user.get("first_name") or "there").strip()
    message = WELCOME_MESSAGE.format(first_name=first_name)
    logger.info("Sending welcome to user id=%s phone=%s", user.get("id"), phone)
    return send_whatsapp_message(phone, message)

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
