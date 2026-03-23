# app.py - Production-ready Flask + OpenAI + Meta WhatsApp Cloud API
import os
import json
import logging
import traceback
import re
import base64
import io
from datetime import datetime
from flask import Flask, request, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import requests
import psycopg2
import psycopg2.extras
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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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
logger.info("DATABASE_URL present: %s", bool(DATABASE_URL))
logger.info("PORT: %s", PORT)
logger.info("LOG_LEVEL: %s", LOG_LEVEL)
logger.info("=" * 80)

# --- Database: Fetch User by Phone Number ---

def get_user_by_phone(whatsapp_number: str) -> dict:
    """
    Look up a user in public.users by phone_number.
    WhatsApp sends numbers like '919321503773' (no +).
    We try an exact match first, then strip the leading country code.
    Returns a dict with user fields or an empty dict if not found.
    """
    if not whatsapp_number:
        logger.debug("No WhatsApp number provided for user lookup")
        return {}

    if not DATABASE_URL:
        logger.debug("DATABASE_URL not set – skipping user lookup")
        return {}

    # Normalize incoming number to digits and try common suffix lengths.
    normalized = re.sub(r"\D", "", str(whatsapp_number))
    if not normalized:
        return {}

    candidates = {normalized}
    for length in (10, 11, 12):
        if len(normalized) >= length:
            candidates.add(normalized[-length:])

    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            candidate_list = list(candidates)
            placeholders = ",".join(["%s"] * len(candidate_list))
            cur.execute(
                f"""
                SELECT user_id, email, first_name, last_name,
                       phone_number, user_type, training_role, company_id
                FROM public.users
                WHERE regexp_replace(coalesce(phone_number, ''), '\\D', '', 'g') IN ({placeholders})
                LIMIT 1
                """,
                candidate_list,
            )
            row = cur.fetchone()
        conn.close()

        if row:
            user = dict(row)
            logger.info("✅ User found in DB: %s %s (id=%s)",
                        user.get("first_name"), user.get("last_name"), user.get("user_id"))
            return user

        logger.info("No DB user found for number %s", whatsapp_number)
        return {}

    except Exception as exc:
        logger.error("❌ DB lookup failed: %s", exc)
        return {}



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


def _normalize_human_punctuation(text: str) -> str:
    normalized = str(text or "")
    normalized = normalized.replace("—", " - ").replace("–", " - ")
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    return normalized.strip()


def _build_button_id(phone_number: str, option_text: str, idx: int) -> str:
    compact = re.sub(r"[^a-zA-Z0-9]+", "_", option_text).strip("_").lower()[:24] or f"opt_{idx}"
    phone_suffix = str(phone_number)[-6:]
    return f"qr_{phone_suffix}_{idx}_{compact}"[:256]

# --- Image Analysis with Vision API ---

def download_and_encode_image(image_url: str, media_id: str = "") -> str:
    """Download image from Meta and encode as base64 data URL."""
    logger.debug(f"download_and_encode_image called: url={image_url[:50] if image_url else 'N/A'}..., media_id={media_id}")
    
    if not image_url:
        logger.error("No image URL provided")
        return ""
    
    try:
        headers = {}
        if META_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {META_ACCESS_TOKEN}"
        
        logger.debug(f"Downloading image from: {image_url[:60]}...")
        response = requests.get(image_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            image_data = response.content
            content_type = response.headers.get('content-type', 'image/jpeg')
            
            # Encode as base64
            image_b64 = base64.b64encode(image_data).decode('utf-8')
            data_url = f"data:{content_type};base64,{image_b64}"
            
            logger.info(f"✅ Image downloaded and encoded: {len(image_data)} bytes")
            return data_url
        else:
            logger.error(f"❌ Failed to download image: status {response.status_code}")
            return ""
            
    except Exception as e:
        logger.error(f"❌ Error downloading/encoding image: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return ""


def download_media_bytes(media_url: str) -> tuple:
    """Download media from Meta and return bytes plus content type."""
    logger.debug(f"download_media_bytes called: url={media_url[:50] if media_url else 'N/A'}...")

    if not media_url:
        logger.error("No media URL provided")
        return b"", ""

    try:
        headers = {}
        if META_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {META_ACCESS_TOKEN}"

        response = requests.get(media_url, headers=headers, timeout=30)
        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            logger.info("✅ Media downloaded: %s bytes", len(response.content))
            return response.content, content_type

        logger.error("❌ Failed to download media: status %s", response.status_code)
        return b"", ""
    except Exception as e:
        logger.error("❌ Error downloading media: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        return b"", ""


def _guess_audio_extension(content_type: str) -> str:
    mapping = {
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/mp4": "m4a",
        "audio/amr": "amr",
        "audio/3gpp": "3gp",
        "audio/x-m4a": "m4a",
    }
    return mapping.get(content_type, "audio")


def transcribe_audio_from_meta(media_url: str) -> str:
    """Transcribe WhatsApp audio using OpenAI speech-to-text."""
    logger.debug("transcribe_audio_from_meta called")

    if not openai_client:
        logger.error("OpenAI client not initialized for transcription")
        return ""

    audio_bytes, content_type = download_media_bytes(media_url)
    if not audio_bytes:
        return ""

    try:
        ext = _guess_audio_extension(content_type)
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = f"audio.{ext}"

        response = openai_client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file
        )
        transcript = str(getattr(response, "text", "") or "").strip()
        logger.info("✅ Audio transcription complete: %s chars", len(transcript))
        return transcript
    except Exception as e:
        logger.error("❌ Audio transcription error: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        return ""

def analyze_image_with_vision(image_data_url: str, caption: str = "") -> str:
    """Analyze an image using OpenAI Vision API (gpt-4o)."""
    logger.debug(f"analyze_image_with_vision called: image_len={len(image_data_url) if image_data_url else 0}, caption={caption}")
    
    if not openai_client:
        logger.error("OpenAI client not initialized for vision analysis")
        return "Sorry, I couldn't analyze the image. Please try again."
    
    try:
        if caption:
            instruction = f"Answer this question using the image context: {caption}"
        else:
            instruction = "Please describe what's in this image concisely in 2-3 sentences."
        
        logger.info(f"Sending image to OpenAI Vision API with instruction: {instruction[:60]}...")
        
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": instruction
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_data_url
                            }
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        
        vision_result = _normalize_human_punctuation(response.choices[0].message.content)
        logger.info(f"✅ Vision API analysis successful: {len(vision_result)} chars")
        logger.debug(f"Vision result: {vision_result}")
        
        return vision_result
        
    except Exception as e:
        logger.error(f"❌ Vision API error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return "I had trouble analyzing that image. Please try again."

def get_media_url_from_meta(media_id: str) -> str:
    """Retrieve the download URL for a media file from Meta."""
    logger.debug(f"get_media_url_from_meta called: media_id={media_id}")
    
    if not META_ACCESS_TOKEN:
        logger.error("META_ACCESS_TOKEN not configured")
        return ""
    
    try:
        url = f"https://graph.facebook.com/v19.0/{media_id}"
        headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
        
        logger.debug(f"Fetching media URL from: {url}")
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            body = response.json()
            media_url = body.get("url", "")
            logger.info(f"✅ Media URL retrieved for download")
            return media_url
        else:
            logger.error(f"❌ Failed to get media URL: status {response.status_code}")
            logger.error(f"Response: {response.text}")
            return ""
            
    except Exception as e:
        logger.error(f"❌ Error retrieving media URL: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return ""

# --- OpenAI Response Generation ---
def generate_response(phone_number: str, user_message: str, image_url: str = "", image_media_id: str = "") -> str:
    """Generate a response using OpenAI with conversation history."""
    logger.debug(
        f"generate_response called for {phone_number}: {user_message}, image={bool(image_url)}, media_id={bool(image_media_id)}"
    )
    
    if not openai_client:
        logger.error("OpenAI client not initialized")
        return "Sorry, I'm temporarily unavailable. Please try again later."
    
    try:
        # If image provided, analyze it with vision
        image_analysis = ""
        if image_url:
            logger.info(f"Image URL detected. Downloading and analyzing with vision API...")
            # First download and encode the image
            image_data_url = download_and_encode_image(image_url, image_media_id)
            if image_data_url:
                image_analysis = analyze_image_with_vision(image_data_url, user_message)
                if not user_message or user_message.strip() == "":
                    user_message = f"[User sent an image] {image_analysis}"
                else:
                    user_message = f"{user_message}\n\n[Image context: {image_analysis}]"
                logger.debug(f"Updated user message with image analysis: {len(user_message)} chars")
            else:
                logger.error("Failed to download/encode image")
                # Continue without image analysis
                user_message = user_message or "I couldn't download the image to analyze."
        
        history = get_conversation_history(phone_number)
        qdrant_results = _search_qdrant(user_message, QDRANT_TOP_K)
        knowledge_context = _build_knowledge_context(qdrant_results)
        history_text = _format_chat_history_for_prompt(history)

        # Build user info block from database
        user_info = get_user_by_phone(phone_number)
        if user_info:
            first = (user_info.get("first_name") or "").strip()
            last = (user_info.get("last_name") or "").strip()
            full_name = f"{first} {last}".strip() or "User"
            user_info_text = (
                f"USER INFORMATION:\n"
                f"- Name: {full_name}\n"
                f"- Role: {user_info.get('training_role') or user_info.get('user_type') or 'Unknown'}\n"
                f"- Email: {user_info.get('email') or 'N/A'}\n"
            )
        else:
            user_info_text = ""

        system_prompt = (
            f"{SYSTEM_PROMPT_TEMPLATE.strip()}\n\n"
            + (f"{user_info_text}\n" if user_info_text else "")
            + f"CONTEXT:\n{knowledge_context}\n\n"
            f"CHAT_HISTORY:\n{history_text}\n\n"
            "INSTRUCTION: Keep your responses concise (under 150 words). Be direct and on-point. "
            "Use the context when relevant. If context is not relevant, respond naturally without fabricating facts."
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
            max_tokens=300
        )
        
        assistant_message = _normalize_human_punctuation(response.choices[0].message.content)
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
    message_text = _normalize_human_punctuation(message_text)
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
                    image_url = ""
                    image_media_id = ""

                    if msg_type == "text":
                        user_input = msg.get("text", {}).get("body", "").strip()
                        logger.info(f"Text message received: '{user_input}'")

                    elif msg_type == "image":
                        image_data = msg.get("image", {})
                        media_id = image_data.get("id", "")
                        caption = image_data.get("caption", "").strip()
                        
                        logger.info(f"Image message received: media_id={media_id}, caption='{caption}'")
                        
                        if media_id:
                            image_media_id = media_id
                            image_url = get_media_url_from_meta(media_id)
                            if image_url:
                                logger.info(f"Image URL retrieved: {image_url[:60]}...")
                                user_input = caption or "[Image sent]"
                            else:
                                logger.error("Failed to retrieve image URL")
                                user_input = "I couldn't download the image. Please try again."
                        else:
                            logger.warning("No media_id in image message")
                            user_input = "I didn't receive a valid image. Please try again."

                    elif msg_type == "audio":
                        audio_data = msg.get("audio", {})
                        media_id = audio_data.get("id", "")

                        logger.info("Audio message received: media_id=%s", media_id)

                        if media_id:
                            audio_url = get_media_url_from_meta(media_id)
                            if audio_url:
                                transcript = transcribe_audio_from_meta(audio_url)
                                if transcript:
                                    user_input = transcript
                                    logger.info("Audio transcript: %s chars", len(transcript))
                                else:
                                    user_input = "I couldn't transcribe that audio. Please try again."
                            else:
                                user_input = "I couldn't download the audio. Please try again."
                        else:
                            user_input = "I didn't receive a valid audio message. Please try again."

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
                        # Gate: only registered users may use the bot
                        if not get_user_by_phone(sender):
                            logger.info("Unregistered number %s – sending redirect message", sender)
                            send_message_to_meta(
                                sender,
                                "Sorry, you are not registered to use this service. "
                                "Please visit https://evolusis.com/ to get access."
                            )
                            continue

                        logger.info(f"Generating response for {sender}...")
                        response_text = generate_response(sender, user_input, image_url, image_media_id)
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
