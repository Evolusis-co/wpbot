# app.py - Production-ready Flask + OpenAI + Meta WhatsApp Cloud API
import os
import json
import logging
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import requests
from openai import OpenAI

# --- Environment Setup ---
load_dotenv()

# Environment variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "").strip()
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "stepbot_verify")
PORT = int(os.getenv("PORT", 10000))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

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

# --- OpenAI Response Generation ---
def generate_response(phone_number: str, user_message: str) -> str:
    """Generate a response using OpenAI with conversation history."""
    logger.debug(f"generate_response called for {phone_number}: {user_message}")
    
    if not openai_client:
        logger.error("OpenAI client not initialized")
        return "Sorry, I'm temporarily unavailable. Please try again later."
    
    try:
        # Build messages list with system prompt and history
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful WhatsApp bot powered by OpenAI. "
                    "You provide concise, friendly responses to user messages. "
                    "Keep responses brief and practical. "
                    "Always be respectful and professional."
                )
            }
        ]
        
        # Add conversation history
        history = get_conversation_history(phone_number)
        messages.extend(history)
        
        # Add current user message
        messages.append({"role": "user", "content": user_message})
        
        logger.debug(f"Sending {len(messages)} messages to OpenAI (including system prompt and history)")
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
    
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "text",
        "text": {
            "body": message_text
        }
    }
    
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
                    
                    # Handle text messages
                    if msg_type == "text":
                        text_body = msg.get("text", {}).get("body", "")
                        logger.info(f"Text message received: '{text_body}'")
                        
                        if not text_body:
                            logger.warning("Empty text body")
                            continue
                        
                        # Generate response
                        logger.info(f"Generating response for {sender}...")
                        response_text = generate_response(sender, text_body)
                        logger.info(f"Generated response: '{response_text}'")
                        
                        # Send response
                        logger.info(f"Sending response back to {sender}...")
                        success = send_message_to_meta(sender, response_text)
                        
                        if success:
                            logger.info(f"✅ Message sent successfully")
                        else:
                            logger.error(f"❌ Failed to send message")
                    
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
