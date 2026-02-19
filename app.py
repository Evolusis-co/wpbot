# app.py - Production-ready Flask + OpenAI + Meta WhatsApp Cloud API
import os
import json
import logging
import traceback
import re
from datetime import datetime
from flask import Flask, request, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import requests
from openai import OpenAI
from prompts import SYSTEM_PROMPT_TEMPLATE

# --- Environment Setup ---
load_dotenv()

# Environment variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "").strip()
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "stepbot_verify")
QDRANT_URL = (os.getenv("QDRANT_URL") or os.getenv("QRANT_URL") or "").strip().rstrip("/")
QDRANT_API_KEY = (os.getenv("QDRANT_API_KEY") or os.getenv("QRANT_API_KEY") or "").strip()
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "bridgetext_scenarios").strip() or "bridgetext_scenarios"
QDRANT_TOP_K = 3
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip()
PORT = int(os.getenv("PORT", 10000))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
qdrant_collection_missing = False

# --- Logging Configuration (EXTREME for Render debugging) ---
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("whatsapp-bot")

# Log startup config
logger.info("=" * 80)
logger.info("WHATSAPP BOT STARTUP")
logger.info("=" * 80)
logger.info("OPENAI_API_KEY present: %s", bool(OPENAI_API_KEY))
logger.info("META_ACCESS_TOKEN present: %s", bool(META_ACCESS_TOKEN))
logger.info("META_PHONE_NUMBER_ID: %s", META_PHONE_NUMBER_ID)
logger.info("META_VERIFY_TOKEN present: %s", bool(META_VERIFY_TOKEN))
logger.info("QDRANT_URL present: %s", bool(QDRANT_URL))
logger.info("QDRANT_API_KEY present: %s", bool(QDRANT_API_KEY))
logger.info("QDRANT_COLLECTION: %s", QDRANT_COLLECTION)
logger.info("QDRANT_TOP_K: %s", QDRANT_TOP_K)
logger.info("OPENAI_EMBEDDING_MODEL: %s", OPENAI_EMBEDDING_MODEL)
logger.info("PORT: %s", PORT)
logger.info("LOG_LEVEL: %s", LOG_LEVEL)
logger.info("=" * 80)

# --- OpenAI Client Initialization ---
openai_client = None
if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized successfully")
    except Exception as e:
        logger.error("❌ Failed to initialize OpenAI client: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        openai_client = None
else:
    logger.warning("⚠️ OPENAI_API_KEY not set - bot will not be able to generate responses")

# --- Flask App Setup ---
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# --- Conversation Memory (for multi-turn context) ---
conversation_memory = {}  # Maps phone_number -> [{"role": "user"|"assistant", "content": "..."}, ...]
MAX_HISTORY_TURNS = 10
QUICK_REPLY_PATTERN = re.compile(r"\[\s*QUICK[_ ]REPLIES\s*:\s*([^\]]+)\]", re.IGNORECASE)

def get_conversation_history(phone_number: str) -> list:
    """Retrieve conversation history for a user."""
    return conversation_memory.get(phone_number, [])

def save_message_to_history(phone_number: str, role: str, content: str):
    """Save a message to conversation history."""
    if phone_number not in conversation_memory:
        conversation_memory[phone_number] = []
    
    conversation_memory[phone_number].append({
        "role": role,
        "content": content
    })
    
    # Keep only recent history to avoid token overflow
    if len(conversation_memory[phone_number]) > MAX_HISTORY_TURNS * 2:
        conversation_memory[phone_number] = conversation_memory[phone_number][-(MAX_HISTORY_TURNS * 2):]
    
    logger.debug(f"Saved message to history for {phone_number}: role={role}, len(history)={len(conversation_memory[phone_number])}")


def _format_chat_history_for_prompt(history: list) -> str:
    if not history:
        return "No previous messages."
    lines = []
    for message in history[-(MAX_HISTORY_TURNS * 2):]:
        role = message.get("role", "user").upper()
        content = str(message.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "No previous messages."


def _build_knowledge_context(results: list) -> str:
    if not results:
        return "No relevant knowledge base context retrieved."

    sections = []
    for idx, item in enumerate(results, start=1):
        payload = item.get("payload") or {}
        title = payload.get("scenario_title", "Untitled Scenario")
        category = payload.get("category", "Unknown Category")
        tags = payload.get("tags") or []
        when_to_use = payload.get("when_to_use", "")
        signals = payload.get("signals", "")
        recommended_action = payload.get("recommended_action", "")
        content = str(payload.get("content", "")).strip()
        snippet = content[:1200]

        signals_text = ", ".join(signals) if isinstance(signals, list) else str(signals or "")

        sections.append(
            f"[{idx}] Title: {title}\n"
            f"Category: {category}\n"
            f"Tags: {', '.join(tags) if tags else 'none'}\n"
            f"when_to_use: {when_to_use or 'n/a'}\n"
            f"signals: {signals_text or 'n/a'}\n"
            f"recommended_action: {recommended_action or 'n/a'}\n"
            f"Content:\n{snippet}"
        )

    return "\n\n---\n\n".join(sections)


def _embed_user_query(query: str):
    if not openai_client:
        logger.warning("Skipping embedding because OpenAI client is not initialized")
        return None

    try:
        embedding_response = openai_client.embeddings.create(
            model=OPENAI_EMBEDDING_MODEL,
            input=query
        )
        vector = embedding_response.data[0].embedding
        logger.debug("Created query embedding with %s dimensions", len(vector))
        return vector
    except Exception as e:
        logger.error("❌ Failed to create query embedding: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        return None


def _keyword_score(query: str, payload: dict) -> int:
    query_tokens = {token for token in query.lower().split() if len(token) > 2}
    if not query_tokens:
        return 0

    text_fields = [
        str(payload.get("scenario_title", "")),
        str(payload.get("category", "")),
        str(payload.get("content", "")),
        str(payload.get("when_to_use", "")),
        str(payload.get("recommended_action", "")),
        " ".join(payload.get("tags", []) if isinstance(payload.get("tags"), list) else []),
    ]
    corpus = " ".join(text_fields).lower()

    return sum(1 for token in query_tokens if token in corpus)


def _search_qdrant_lexical_fallback(query: str, limit: int = 3) -> list:
    global qdrant_collection_missing

    if not QDRANT_URL or not QDRANT_API_KEY:
        return []

    if qdrant_collection_missing:
        return []

    headers = {
        "api-key": QDRANT_API_KEY,
        "Content-Type": "application/json"
    }
    scroll_url = f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/scroll"
    scroll_payload = {
        "limit": 200,
        "with_payload": True,
        "with_vector": False
    }

    try:
        logger.debug("Qdrant lexical fallback request URL: %s", scroll_url)
        resp = requests.post(scroll_url, headers=headers, json=scroll_payload, timeout=30)
        if resp.status_code == 404:
            qdrant_collection_missing = True
            logger.warning("Qdrant collection '%s' not found. Disabling retrieval until restart.", QDRANT_COLLECTION)
            return []
        if resp.status_code != 200:
            logger.error("Lexical fallback failed with status %s", resp.status_code)
            logger.error("Lexical fallback response: %s", resp.text)
            return []

        body = resp.json()
        points = body.get("result", {}).get("points", [])
        ranked = sorted(
            points,
            key=lambda point: _keyword_score(query, point.get("payload") or {}),
            reverse=True,
        )
        top_points = [point for point in ranked if _keyword_score(query, point.get("payload") or {}) > 0][:limit]
        logger.info("✅ Qdrant lexical fallback returned %s ranked results", len(top_points))
        return top_points
    except Exception as e:
        logger.error("❌ Qdrant lexical fallback failed: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        return []


def _search_qdrant(query: str, limit: int = 3) -> list:
    global qdrant_collection_missing

    if not QDRANT_URL or not QDRANT_API_KEY:
        logger.info("Qdrant is not configured. Skipping retrieval.")
        return []

    if qdrant_collection_missing:
        logger.debug("Skipping Qdrant retrieval because collection is marked missing")
        return []

    vector = _embed_user_query(query)
    if not vector:
        return []

    headers = {
        "api-key": QDRANT_API_KEY,
        "Content-Type": "application/json"
    }

    search_url = f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search"
    search_payload = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "with_vector": False
    }

    try:
        logger.debug("Qdrant search request URL: %s", search_url)
        search_response = requests.post(search_url, headers=headers, json=search_payload, timeout=30)
        if search_response.status_code == 404:
            qdrant_collection_missing = True
            logger.warning("Qdrant collection '%s' not found. Disabling retrieval until restart.", QDRANT_COLLECTION)
            return []
        if search_response.status_code == 200:
            body = search_response.json()
            results = body.get("result", [])
            logger.info("✅ Qdrant search success. Results: %s", len(results))
            if results:
                return results

            logger.warning("Qdrant vector search returned 0 results; trying lexical fallback")
            return _search_qdrant_lexical_fallback(query, limit)

        logger.warning("Qdrant /points/search returned %s; trying /points/query fallback", search_response.status_code)
    except Exception as e:
        logger.warning("Qdrant /points/search request failed: %s", e)

    query_url = f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/query"
    query_payload = {
        "query": vector,
        "limit": limit,
        "with_payload": True,
        "with_vector": False
    }

    try:
        logger.debug("Qdrant query request URL: %s", query_url)
        query_response = requests.post(query_url, headers=headers, json=query_payload, timeout=30)
        if query_response.status_code == 404:
            qdrant_collection_missing = True
            logger.warning("Qdrant collection '%s' not found. Disabling retrieval until restart.", QDRANT_COLLECTION)
            return []
        if query_response.status_code != 200:
            logger.error("❌ Qdrant query failed with status %s", query_response.status_code)
            logger.error("Qdrant response: %s", query_response.text)
            return _search_qdrant_lexical_fallback(query, limit)

        body = query_response.json()
        result = body.get("result", {})
        points = result.get("points", []) if isinstance(result, dict) else []
        logger.info("✅ Qdrant query success. Results: %s", len(points))
        if points:
            return points

        logger.warning("Qdrant query returned 0 results; trying lexical fallback")
        return _search_qdrant_lexical_fallback(query, limit)
    except Exception as e:
        logger.error("❌ Qdrant query request failed: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        return _search_qdrant_lexical_fallback(query, limit)


def _parse_quick_replies(message_text: str):
    text = str(message_text or "")
    match = QUICK_REPLY_PATTERN.search(text)
    if not match:
        return text.strip(), []

    options_raw = match.group(1)
    options = [option.strip().strip('"\'') for option in options_raw.split("|") if option.strip()]
    cleaned_text = QUICK_REPLY_PATTERN.sub("", text).strip()
    return cleaned_text, options


def _build_button_id(phone_number: str, option_text: str, idx: int) -> str:
    compact = re.sub(r"[^a-zA-Z0-9]+", "_", option_text).strip("_").lower()[:24] or f"opt_{idx}"
    phone_suffix = str(phone_number)[-6:]
    return f"qr_{phone_suffix}_{idx}_{compact}"[:256]

# --- OpenAI Response Generation ---
def generate_response(phone_number: str, user_message: str) -> str:
    """Generate a response using OpenAI with conversation history."""
    logger.debug(f"generate_response called for {phone_number}: {user_message}")
    
    if not openai_client:
        logger.error("OpenAI client not initialized")
        return "Sorry, I'm temporarily unavailable. Please try again later."
    
    try:
        history = get_conversation_history(phone_number)
        qdrant_results = _search_qdrant(user_message, QDRANT_TOP_K)
        knowledge_context = _build_knowledge_context(qdrant_results)
        history_text = _format_chat_history_for_prompt(history)

        system_prompt = (
            f"{SYSTEM_PROMPT_TEMPLATE.strip()}\n\n"
            f"CONTEXT:\n{knowledge_context}\n\n"
            f"CHAT_HISTORY:\n{history_text}\n\n"
            "INSTRUCTION: Use the context when relevant. If context is not relevant, respond naturally without fabricating facts."
        )

        # Build messages list with prompt and history
        messages = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]
        
        # Add conversation history
        messages.extend(history)
        
        # Add current user message
        messages.append({"role": "user", "content": user_message})
        
        logger.debug(f"Sending {len(messages)} messages to OpenAI (including system prompt and history)")
        logger.debug("Qdrant context docs used: %s", len(qdrant_results))
        logger.debug(f"Messages summary: {[m['role'] for m in messages]}")
        
        # Call OpenAI
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )
        
        assistant_message = response.choices[0].message.content
        logger.info(f"✅ OpenAI response generated for {phone_number}: {len(assistant_message)} chars")
        logger.debug(f"OpenAI response: {assistant_message}")
        
        # Save to history
        save_message_to_history(phone_number, "user", user_message)
        save_message_to_history(phone_number, "assistant", assistant_message)
        
        return assistant_message
        
    except Exception as e:
        logger.error(f"❌ OpenAI API error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return "Sorry, I encountered an error generating a response. Please try again."

# --- Meta WhatsApp API Helpers ---
def send_message_to_meta(phone_number: str, message_text: str) -> bool:
    """Send a message via Meta WhatsApp Cloud API."""
    logger.debug(f"send_message_to_meta called: phone={phone_number}, text={message_text}")
    
    if not META_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        logger.error("❌ META_ACCESS_TOKEN or META_PHONE_NUMBER_ID not configured")
        return False
    
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    
    clean_text, quick_replies = _parse_quick_replies(message_text)

    if quick_replies:
        logger.info("Quick replies detected: %s", quick_replies)
        buttons = []
        for idx, option in enumerate(quick_replies[:3], start=1):
            button_title = option[:20]
            buttons.append({
                "type": "reply",
                "reply": {
                    "id": _build_button_id(phone_number, button_title, idx),
                    "title": button_title
                }
            })

        body_text = clean_text[:1024] if clean_text else "Please choose an option:"
        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": buttons}
            }
        }
        logger.info("Sending interactive button message to %s", phone_number)
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "type": "text",
            "text": {
                "body": clean_text or "..."
            }
        }
        logger.info("Sending plain text message to %s", phone_number)
    
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    logger.debug(f"Meta API Request:")
    logger.debug(f"  URL: {url}")
    logger.debug(f"  Headers: Authorization=Bearer [HIDDEN], Content-Type=application/json")
    logger.debug(f"  Payload: {json.dumps(payload, indent=2)}")
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        logger.info(f"Meta API Response Status: {response.status_code}")
        logger.debug(f"Meta API Response Headers: {dict(response.headers)}")
        
        response_json = None
        try:
            response_json = response.json()
            logger.debug(f"Meta API Response Body: {json.dumps(response_json, indent=2)}")
        except Exception as e:
            logger.debug(f"Could not parse response as JSON: {e}")
            response_json = response.text
            logger.debug(f"Meta API Response Text: {response_json}")
        
        if response.status_code == 200:
            logger.info(f"✅ Message sent successfully to {phone_number}")
            return True
        else:
            logger.error(f"❌ Meta API returned {response.status_code}")
            logger.error(f"Response: {response_json}")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request failed: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error sending message: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False

# --- Flask Routes ---

@app.route("/", methods=["GET", "HEAD"])
def root():
    """Root endpoint for platform probes."""
    if request.method == "HEAD":
        return "", 200
    return jsonify({"status": "ok", "service": "whatsapp-bot"}), 200

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    logger.debug("Health check requested")
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "openai_configured": bool(openai_client),
        "meta_configured": bool(META_ACCESS_TOKEN and META_PHONE_NUMBER_ID)
    }), 200

@app.route("/meta-webhook", methods=["GET"])
def meta_webhook_get():
    """Webhook verification for Meta WhatsApp Cloud API."""
    logger.info("=" * 80)
    logger.info("GET /meta-webhook - VERIFICATION REQUEST")
    logger.info("=" * 80)
    logger.info(f"Request timestamp: {datetime.utcnow().isoformat()}")
    logger.debug(f"Query parameters: {dict(request.args)}")
    
    try:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        logger.info(f"hub.mode: {mode}")
        logger.info(f"hub.verify_token present: {bool(token)}")
        logger.info(f"hub.challenge: {challenge}")
        logger.info(f"Expected token: {META_VERIFY_TOKEN}")
        
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            logger.info("✅ Webhook verification SUCCESSFUL")
            logger.info(f"Returning challenge: {challenge}")
            return challenge, 200
        else:
            logger.warning("❌ Webhook verification FAILED")
            if mode != "subscribe":
                logger.warning(f"  - hub.mode mismatch: got '{mode}', expected 'subscribe'")
            if token != META_VERIFY_TOKEN:
                logger.warning(f"  - hub.verify_token mismatch")
            return jsonify({"error": "Verification failed"}), 403
            
    except Exception as e:
        logger.error(f"❌ Exception in webhook verification: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

@app.route("/meta-webhook", methods=["POST"])
def meta_webhook_post():
    """Incoming webhook from Meta WhatsApp Cloud API."""
    print("🔔 Webhook received")  # Also print to stdout for visibility
    logger.info("=" * 80)
    logger.info("POST /meta-webhook - INCOMING MESSAGE")
    logger.info("=" * 80)
    logger.info(f"Request timestamp: {datetime.utcnow().isoformat()}")
    
    try:
        # Log headers
        logger.debug("Request headers:")
        for key, value in request.headers:
            if key.lower() in ["content-type", "user-agent", "authorization"]:
                masked_value = "[HIDDEN]" if key.lower() == "authorization" else value
                logger.debug(f"  {key}: {masked_value}")
        
        # Get request body
        data = request.get_json(silent=True)
        logger.debug(f"Request body: {json.dumps(data, indent=2)}")
        
        if not data:
            logger.warning("No JSON data in request body")
            return jsonify({"status": "ok"}), 200
        
        # Extract structure
        logger.debug(f"Data object keys: {list(data.keys())}")
        
        # Iterate through entries
        entries = data.get("entry", [])
        logger.info(f"Processing {len(entries)} entry/entries")
        
        for entry in entries:
            changes = entry.get("changes", [])
            logger.debug(f"Entry has {len(changes)} change(s)")
            
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                logger.info(f"Processing {len(messages)} message(s)")
                
                for msg in messages:
                    msg_id = msg.get("id")
                    msg_type = msg.get("type")
                    sender = msg.get("from")
                    timestamp = msg.get("timestamp")
                    
                    logger.info(f"Message ID: {msg_id}")
                    logger.info(f"Sender: {sender}")
                    logger.info(f"Type: {msg_type}")
                    logger.info(f"Timestamp: {timestamp}")

                    user_input = ""

                    if msg_type == "text":
                        user_input = msg.get("text", {}).get("body", "").strip()
                        logger.info(f"Text message received: '{user_input}'")

                    elif msg_type == "interactive":
                        interactive = msg.get("interactive", {})
                        button_reply = interactive.get("button_reply", {})
                        user_input = (button_reply.get("title") or button_reply.get("id") or "").strip()
                        logger.info(f"Interactive button selected: '{user_input}'")

                    elif msg_type == "button":
                        button = msg.get("button", {})
                        user_input = (button.get("text") or button.get("payload") or "").strip()
                        logger.info(f"Button message received: '{user_input}'")

                    if user_input:
                        logger.info(f"Generating response for {sender}...")
                        response_text = generate_response(sender, user_input)
                        logger.info(f"Generated response: '{response_text}'")

                        logger.info(f"Sending response back to {sender}...")
                        success = send_message_to_meta(sender, response_text)

                        if success:
                            logger.info("✅ Message sent successfully")
                        else:
                            logger.error("❌ Failed to send message")
                    
                    else:
                        logger.debug(f"Ignoring non-text message type: {msg_type}")
        
        logger.info("✅ Webhook processing completed successfully")
        logger.info("=" * 80)
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"❌ Exception in webhook processing: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.info("=" * 80)
        return jsonify({"error": str(e)}), 500

@app.route("/test-send", methods=["POST"])
def test_send():
    """Test endpoint to send a message to a specific number."""
    logger.info("=" * 80)
    logger.info("POST /test-send - TEST MESSAGE")
    logger.info("=" * 80)
    
    try:
        phone_number = request.args.get("phone") or request.json.get("phone") if request.is_json else None
        
        if not phone_number:
            phone_number = "919321503773"  # Default test number
        
        logger.info(f"Sending test message to: {phone_number}")
        
        test_text = "Bot test successful ✅"
        success = send_message_to_meta(phone_number, test_text)
        
        if success:
            logger.info(f"✅ Test message sent")
            logger.info("=" * 80)
            return jsonify({"status": "ok", "message": "Test message sent", "phone": phone_number}), 200
        else:
            logger.error(f"❌ Test message failed")
            logger.info("=" * 80)
            return jsonify({"status": "error", "message": "Failed to send test message"}), 500
            
    except Exception as e:
        logger.error(f"❌ Exception in test-send: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.info("=" * 80)
        return jsonify({"error": str(e)}), 500

@app.route("/test-openai", methods=["POST"])
def test_openai():
    """Test endpoint to verify OpenAI connectivity."""
    logger.info("=" * 80)
    logger.info("POST /test-openai - OPENAI TEST")
    logger.info("=" * 80)
    
    if not openai_client:
        logger.error("❌ OpenAI client not initialized")
        logger.info("=" * 80)
        return jsonify({"status": "error", "message": "OpenAI client not initialized"}), 500
    
    try:
        logger.info("Sending test query to OpenAI...")
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say 'OpenAI integration successful' in one sentence."}],
            max_tokens=100
        )
        
        result = response.choices[0].message.content
        logger.info(f"✅ OpenAI response: {result}")
        logger.info("=" * 80)
        return jsonify({"status": "ok", "message": result}), 200
        
    except Exception as e:
        logger.error(f"❌ OpenAI test failed: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.info("=" * 80)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors."""
    logger.warning(f"404 Not Found: {request.method} {request.path}")
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors."""
    logger.error(f"500 Server Error: {e}")
    logger.error(f"Traceback: {traceback.format_exc()}")
    return jsonify({"error": "Internal server error"}), 500

# --- Main Entry Point ---
if __name__ == "__main__":
    logger.info("=" * 80)
    logger.info("STARTING FLASK SERVER")
    logger.info("=" * 80)
    logger.info(f"Listening on 0.0.0.0:{PORT}")
    logger.info(f"Debug mode: OFF")
    logger.info(f"Environment: production")
    logger.info("=" * 80)
    
    try:
        app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            threaded=True,
            use_reloader=False
        )
    except Exception as e:
        logger.error(f"❌ Failed to start server: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
