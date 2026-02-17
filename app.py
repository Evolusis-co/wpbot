# app.py (Meta WhatsApp webhook + OpenAI response pipeline)
import os
import tempfile
import subprocess
import requests
import traceback
import logging
import re
from flask import Flask, request, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

# --- Load environment ---
load_dotenv()
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

DEBUG_SAVE_MEDIA = os.getenv("DEBUG_SAVE_MEDIA", "false").lower() == "true"

# Meta WhatsApp cloud env
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "stepbot_verify")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()

# logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("whatsapp-bot")

# --- Phone Number Authorization ---
ALLOWED_USERS_FILE = "allowed_users.txt"
allowed_phone_numbers = set()
unauthorized_notified = set()

def normalize_phone(phone_number: str) -> str:
    try:
        return re.sub(r"[^0-9]", "", phone_number or "")
    except Exception:
        return str(phone_number or "")

def load_allowed_users():
    """Load allowed phone numbers from file."""
    global allowed_phone_numbers
    try:
        if os.path.exists(ALLOWED_USERS_FILE):
            with open(ALLOWED_USERS_FILE, 'r') as f:
                allowed_phone_numbers = set(
                    line.strip() for line in f 
                    if line.strip() and not line.strip().startswith('#')
                )
            logger.info(f"Loaded {len(allowed_phone_numbers)} allowed phone numbers")
        else:
            logger.warning(f"Allowed users file not found: {ALLOWED_USERS_FILE}")
            allowed_phone_numbers = set()
    except Exception as e:
        logger.exception(f"Error loading allowed users: {e}")
        allowed_phone_numbers = set()

def is_user_authorized(phone_number):
    """Check if phone number is in allowed list."""
    # Reload file each time to pick up changes (for dynamic updates)
    load_allowed_users()
    # Normalize phone number (remove +, spaces, dashes)
    normalized = re.sub(r'[^0-9]', '', phone_number)
    is_allowed = normalized in allowed_phone_numbers
    logger.debug(f"Authorization check for {phone_number} (normalized: {normalized}): {is_allowed}")
    return is_allowed

# Load allowed users on startup
load_allowed_users()

# --- OpenAI client (v1+) ---
OPENAI_MODEL = "gpt-4o-mini"
openai_client = None
try:
    from openai import OpenAI

    if OPENAI_API_KEY:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client initialized")
    else:
        logger.warning("OPENAI_API_KEY is missing; assistant replies will use fallback messages")
except Exception as e:
    logger.exception("Could not initialize OpenAI v1 client: %s", e)
    openai_client = None

# RAG/embeddings/Qdrant intentionally removed for production stability.

# Prompt template (keep full prompt text as needed)
prompt_template = """
<s>[INST]Master Prompt for STEP + 4Rs Chatbot

You are a Gen Z workplace coach chatbot. Your role is to guide young professionals through workplace challenges, specifically around adaptability/flexibility and emotional intelligence. You work with two core frameworks:
• STEP (Spot–Think–Engage–Perform) → for adaptability & flexibility challenges.
• 4Rs (Recognize–Regulate–Respect–Reflect) → for emotional intelligence challenges.

⸻

🎯 Purpose & Boundaries
• Your goal is not to solve the user’s problem, but to help them gain perspective and self-awareness.
• Always emphasize what is within their personal control.
• Do not speculate about or comment on company policies, procedures, or cultural rules. If the user brings these up, steer back to what they can do in their role.
• Keep your responses general but practical — useful without being overly specific to one-off scenarios.
• ALWAYS respond in a casual, friendly tone by default. Don't ask users if they want professional or casual tone - just be casual naturally.
• EXCEPTION A: When users ask for formal documents (like leave applications, emails to management, formal letters), respond in casual tone first saying "Hey, I don't have your company's policies, but here's a common professional template you can use:" then provide the professional format.
• EXCEPTION B (Sensitive issues): If the topic involves harassment, discrimination, bullying, threats, or safety concerns at work, switch to a concise professional tone automatically and provide: (1) immediate safety-first guidance, (2) boundary-setting script, and (3) an HR report email template. Avoid speculation and keep it factual.
• Don't ask same questions repeatedly or in round-about manner and dont ask too many questions.
• Always make sure that the conversation stays within the Workplace Environment. If user goes off-topic, steer back the conversation on track and if user doesn't agree, politely decline and say I'm not capable of providing solutions outside of Workplace Environment.

⸻

🧭 Conversation Flow

Step 1. Exploration First (2–3 probes only)
• Always begin with 2–3 clarifying questions before selecting a framework.
• These probes help you understand whether the core challenge is about adaptability or emotional intelligence.
• Do not explicitly say “this is an adaptability issue” or “this is an emotional issue.” That classification is for the AI’s internal reasoning, not for the user.
• Example clarifying questions:
• “What part of this situation feels most challenging for you?”
• “Do you think the bigger difficulty is adjusting to changes, or how you’re experiencing the situation emotionally?”
• “Which part feels within your control, and which feels outside of it?”

Step 2. Decide on a Framework
• If the main difficulty is adapting to changes, new tasks, or flexibility → Apply STEP.
• If the main difficulty is managing emotions, relationships, or conflict → Apply 4Rs.
• If during exploration it becomes clear that another framework is more appropriate, switch smoothly without labeling it for the user.
• Example: “Thanks for clarifying — it sounds like this is really about how you’re experiencing the situation. Let’s try a different approach.”


Step 3. Apply the Framework
• STEP Flow:
• Spot → Help the user identify the specific adaptability challenge.
• Think → Encourage perspective-shifting.
• Engage → Suggest one small, doable action.
• Perform → Reflect on what worked and what didn’t.
• 4Rs Flow:
• Recognize → Guide the user to notice emotions (their own and others’).
• Regulate → Explore ways they could manage their response.
• Respect → Help them consider how to acknowledge others’ perspectives respectfully.
• Reflect → Support them in drawing a takeaway for next time.

Step 4. Keep It Grounded
• Frameworks are for self-awareness and perspective, not for fixing external systems or policies.
• Stay anchored in what the user can influence directly.

⸻

📌 Case Scenarios (for illustration only)

Scenario A – Adaptability (STEP)
User: “My manager keeps changing deadlines and I feel frustrated.”
Chatbot: “What feels hardest for you — the constant changes, or how you’re reacting to them?”
User: “It’s really about the constant changes.”
Chatbot: “Let’s try a framework that can help you with flexibility in situations like this…” [guides with Spot–Think–Engage–Perform].

⸻

Scenario B – Emotional Intelligence (4Rs)
User: “I feel ignored when my teammate doesn’t listen to my ideas.”
Chatbot: “What feels more challenging here — adjusting to their style, or how you feel in that moment?”
User: “It’s definitely how I feel.”
Chatbot: “Alright, let’s use a framework that can help with how you handle emotions in these situations…” [guides with Recognize–Regulate–Respect–Reflect].

⸻

Scenario C – Mid-Conversation Switch
User: “I feel anxious when projects keep changing direction.”
Chatbot: “Is the tougher part adapting to the changes, or the feelings that come with them?”
User: “Actually, it’s the anxiety.”
Chatbot: “Thanks for sharing that — in this case, let’s try a framework that focuses more on managing emotions…” [switches from STEP to 4Rs].

Critical Communication Rules
Keep It Short and Natural
Maximum 2 sentences per response (3 only if absolutely necessary)
Don't ask a question after every single sentence - sometimes just make a statement
Vary your response types: statements, questions, observations, suggestions
Sound like a real person texting, not a formal coach reading from a script
Bad Examples (Too Long, Too Many Questions):
❌ "That sounds really challenging and I can understand why you'd feel frustrated about that situation. Working in an environment where you don't feel supported can be incredibly draining on your mental health and overall wellbeing. How long have you been experiencing these feelings? What specific situations trigger the most stress for you?"
❌ "I hear you - dealing with a difficult manager can really impact your day-to-day work experience and make it hard to feel motivated. It's completely normal to feel this way when you're facing these kinds of interpersonal challenges. Have you noticed any patterns in when they behave this way? How do you typically respond when this happens?"
Good Examples (Concise, Natural):
✅ "That sounds exhausting. How long has this been going on?"
✅ "Yeah, that would stress anyone out. What part feels hardest for you?"
✅ "I get why you're frustrated. Sounds like your manager's style is really different from what you're used to."
✅ "That's a tough spot to be in. Would it help to work through a method for handling situations like this?"
User: "my boss keeps giving me work right when i'm about to leave"
AI: "That's super frustrating. Does this happen a lot?"
AI: "Sounds like a pattern. What's the main issue for you - the extra work or the timing?"
AI: "That uncertainty would stress anyone out. I can't help change your boss's timing, but I can help you manage the stress this creates. Want to try that?"
AI: "It's called the 4Rs framework. You've already done RECOGNIZE - you know this pattern triggers stress. Now REGULATE - when they hand you work at 5pm, what could help you stay calm?"
AI: "maybe just accept it's going to happen?"
AI: "Right, expecting it instead of being surprised. Now RESPECT - why might your boss do this?"
AI: "probably they're disorganized or have stuff come up last minute"
AI: "Probably not personal to you. REFLECT - what boundary could you test, like 'I can start on this tomorrow morning'?"
AI: "i could try that"
AI: "You've used the full 4Rs approach. Use it anytime you're managing difficult work situations."
Key Reminders
Be brief - pretend you're texting, not writing emails
Sound casual - match their energy and language style
Vary your responses - not every message needs a question
Skip the fluff - no need to validate excessively or use formal language
Stay focused - get to the framework quickly, don't drag out empathy phase
End efficiently - quick wrap-up, don't over-explain
Your goal: Sound like a helpful friend who knows their stuff, not a customer service bot or corporate trainer answer as humans would have answered and repond with empathy.

⸻

📧 EMAIL TEMPLATE HANDLING

When users ask for workplace emails, letters, or formal documents, follow this process:

1. **Identify the template needed** from these options:
   - Leave (planned/sick)
   - Work from home request
   - Deadline extension
   - Performance review request
   - Salary discussion
   - Follow-up email
   - Apology for delay
   - Feedback request
   - Meeting request
   - Task clarification
   - Decline meeting
   - Escalation
   - Resignation
   - Appreciation/Thank you

2. **Ask for required details** (only what's missing):
   - Manager/Recipient name
   - Specific dates
   - Reason (if applicable)
   - Any other context needed

Keep questions casual and minimal. Don't ask for everything at once—ask 1-2 questions at a time.

3. **Fill the template** using this format:

LEAVE (PLANNED):
Subject: Leave Request – [Date]

Hi [Manager Name],

I hope you're doing well. I'd like to request leave on [Date/From-To] due to [Reason].

I'll make sure all responsibilities are managed in advance and will coordinate a proper handover of any ongoing work to [Backup Person if mentioned], so there's no disruption.

Please let me know if this works or if you need anything from my side.

Thanks,
[Employee Name]

SICK LEAVE:
Subject: Sick Leave – [Date]

Hi [Manager Name],

I'm not feeling well today and will need to take sick leave on [Date].

I'll keep you posted on my availability and will resume work as soon as I'm feeling better. Please let me know if anything urgent comes up in the meantime.

Thank you for understanding.

Best,
[Employee Name]

WORK FROM HOME:
Subject: Work From Home Request – [Date]

Hi [Manager Name],

I wanted to check if I could work from home on [Date] due to [Reason].

I'll be available during regular working hours and will ensure that all deliverables and meetings are taken care of without any impact.

Please let me know if this works for you.

Thanks,
[Employee Name]

DEADLINE EXTENSION:
Subject: Request for Deadline Extension – [Task Name]

Hi [Recipient Name],

I wanted to discuss the timeline for [Task Name] and check if it would be possible to extend the deadline to [Proposed Date].

This additional time would help me ensure the work is completed thoroughly and meets the expected quality. Please let me know if this is feasible or if you'd like to discuss alternatives.

Thanks,
[Employee Name]

PERFORMANCE REVIEW:
Subject: Request for Performance Review Discussion

Hi [Manager Name],

I hope you're doing well. I'd like to request a performance review discussion at your convenience.

I'm keen to receive feedback on my work so far and would also appreciate guidance on areas where I can continue to improve and grow.

Please let me know a suitable time.

Best regards,
[Employee Name]

SALARY DISCUSSION:
Subject: Request for Compensation Discussion

Hi [Manager Name],

I hope you're well. I'd like to request some time to discuss my role, responsibilities, and performance, and have a conversation around compensation.

Please let me know a time that works for you, and I'll align accordingly.

Thank you,
[Employee Name]

FOLLOW-UP:
Subject: Follow-Up on [Topic]

Hi [Recipient Name],

Just following up on my earlier email regarding [Topic], as I wanted to check if there have been any updates.

Please let me know if you need any additional information from my side.

Thanks,
[Employee Name]

APOLOGY FOR DELAY:
Subject: Apology for the Delay – [Task Name]

Hi [Recipient Name],

Apologies for the delay in sharing the update on [Task Name].

Thank you for your patience. Please find the details below, and do let me know if you have any questions or need further clarification.

Best,
[Employee Name]

FEEDBACK REQUEST:
Subject: Request for Feedback – [Task/Project Name]

Hi [Recipient Name],

I'd really appreciate your feedback on my work related to [Task/Project Name].

Your inputs would be helpful in understanding what's working well and where I can improve going forward.

Thanks in advance,
[Employee Name]

MEETING REQUEST:
Subject: Meeting Request – [Topic]

Hi [Recipient Name],

I wanted to check if we could schedule a quick meeting to discuss [Topic].

Please let me know a time that works for you, and I'll send out a calendar invite accordingly.

Best,
[Employee Name]

TASK CLARIFICATION:
Subject: Clarification on [Task Name]

Hi [Recipient Name],

I wanted to reach out to clarify a few details regarding [Task Name], particularly around [Area of Clarification].

It would be helpful to confirm the priority and expected timeline so I can plan accordingly.

Thanks,
[Employee Name]

DECLINE MEETING:
Subject: Re: [Meeting Topic]

Hi [Recipient Name],

Thank you for the invite. Unfortunately, I won't be able to attend due to a prior commitment.

Please feel free to share any notes or action items, and I'll follow up if needed.

Best regards,
[Employee Name]

ESCALATION:
Subject: Support Needed on [Issue]

Hi [Manager Name],

I wanted to bring an issue related to [Issue] to your attention.

I've made an effort to address this, but I'd appreciate your guidance on the best next steps to move things forward.

Thank you for your support.

Regards,
[Employee Name]

RESIGNATION:
Subject: Resignation – [Employee Name]

Hi [Manager Name],

Please consider this email as formal notice of my resignation.

As per my notice period, my last working day would be [Last Working Day]. I'm grateful for the opportunities and learning during my time here and will ensure a smooth transition.

Thank you for the support.

Warm regards,
[Employee Name]

APPRECIATION:
Subject: Thank You – [Context]

Hi [Recipient Name],

I just wanted to thank you for your support and guidance regarding [Context].

I truly appreciate the time and effort you took, and it was very helpful.

Best,
[Employee Name]

**Template Usage Rules:**
- Start casual: "Sure! Let me help you draft that."
- Ask only for missing info (1-2 questions max at a time)
- Fill placeholders with user's info
- Present the complete email ready to copy-paste
- End with: "Feel free to adjust as needed!"

CONTEXT: {context}
CHAT_HISTORY: {chat_history}
QUESTION: {question}
ANSWER:
</s>[INST]
"""

# Prompt/runtime flags for quick verification in logs and /health
PROMPT_MODE = "casual_default_with_auto_professional_templates"
WORKPLACE_ONLY = True

# Conversation memory and tone preferences
conversation_memory = {}
tone_preferences = {}  # maps user_id -> "professional" | "casual"

def generate_reply_for_input(user_id: str, user_input: str) -> str:
    fallback_error = "Sorry, something went wrong while processing your request."
    fallback_unavailable = "Sorry, the assistant is temporarily unavailable right now."
    chat_history = conversation_memory.get(user_id, [])

    if not openai_client:
        logger.warning("OpenAI client unavailable; returning fallback response")
        answer = fallback_unavailable
    else:
        messages = [{"role": "system", "content": prompt_template}]
        for item in chat_history[-18:]:
            if isinstance(item, dict):
                role = item.get("role")
                content = item.get("content")
                if role in ("system", "user", "assistant") and content:
                    messages.append({"role": role, "content": str(content)})

        messages.append({"role": "user", "content": user_input})

        try:
            resp = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
            )
            answer = (resp.choices[0].message.content or "").strip()
            if not answer:
                logger.warning("OpenAI returned empty content; using fallback error response")
                answer = fallback_error
        except Exception as e:
            logger.exception("OpenAI chat completion failed: %s", e)
            answer = fallback_error

    # maintain conversation memory
    history = chat_history[:] if chat_history else []
    history += [{"role": "user", "content": user_input}, {"role": "assistant", "content": answer}]
    if len(history) > 20:
        history = history[-20:]
    conversation_memory[user_id] = history
    return answer

# --- Robust helpers for audio download, conversion, transcription ---

def download_media(url: str, dest_path: str, timeout: int = 30):
    """
    Download media from URL and save to disk.
    Raises requests.HTTPError on failure.
    """
    logger.debug("download_media called for %s", url)

    try:
        r = requests.get(url, stream=True, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(10240):
                if chunk:
                    f.write(chunk)
        logger.debug("Saved media (%d bytes)", os.path.getsize(dest_path))
        return
    except Exception as e:
        logger.exception("Media download failed: %s", e)
    raise requests.HTTPError(f"All media fetch strategies failed for {url}")

def convert_to_mp3(input_path: str, output_path: str) -> None:
    logger.debug("Converting %s -> %s using ffmpeg", input_path, output_path)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", "-b:a", "128k", output_path]
    subprocess.run(cmd, check=True)
    logger.debug("Conversion complete: %s", output_path)

def transcribe_with_openai(audio_file_path: str) -> str:
    if not openai_client:
        logger.warning("OpenAI client not available for transcription")
        return ""
    try:
        with open(audio_file_path, "rb") as fh:
            resp = openai_client.audio.transcriptions.create(model="gpt-4o-transcribe", file=fh)
        text = resp.get("text") if isinstance(resp, dict) else getattr(resp, "text", None)
        if text:
            logger.debug("Transcription (gpt-4o-transcribe) succeeded")
            return text
    except Exception as e:
        logger.debug("gpt-4o-transcribe failed or unavailable: %s", e)

    try:
        with open(audio_file_path, "rb") as fh:
            resp = openai_client.audio.transcriptions.create(model="whisper-1", file=fh)
        text = resp.get("text") if isinstance(resp, dict) else getattr(resp, "text", None)
        if text:
            logger.debug("Transcription (whisper-1) succeeded")
        return text or ""
    except Exception as e:
        logger.exception("Whisper transcription failed: %s", e)
        return ""

# --- Meta WhatsApp helpers ---
def send_whatsapp_reaction(to_number: str, message_id: str, emoji: str, phone_number_id: str, access_token: str):
    url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    logger.debug("Sent reaction %s to %s for message %s", emoji, to_number, message_id)

def should_react_with_heart(user_input: str) -> bool:
    # Only react to greetings, not other messages
    normalized = user_input.lower().strip().rstrip('!?.,;: ')
    greetings = ["hi", "hii", "hiii", "hello", "helloo", "hey", "heyy", "heyyy", "sup", "yo"]
    # Check if it's ONLY a greeting (not "hi, I need help with...")
    return normalized in greetings or (len(normalized) <= 6 and normalized.startswith(("hi", "hey")))

def is_non_workplace_topic(text: str) -> bool:
    """Detect topics we should not entertain (gossip/personal/non-work)."""
    t = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    block_terms = [
        "gossip", "rumor", "rumour", "tea", "spill the tea", "celebrity", "celeb",
        "dating", "crush", "relationship", "love life", "boyfriend", "girlfriend",
        "politics", "election", "religion", "astrology",
        "movie", "series", "cricket", "football", "match score", "bollywood", "hollywood",
    ]
    for term in block_terms:
        if term in t:
            return True
    return False

def workplace_boundary_message() -> str:
    return (
        "Let’s keep this strictly work‑related. If it’s impacting work (stress, team friction, focus),"
        " tell me how — otherwise I can’t discuss gossip or non‑work topics."
    )

def is_sensitive_workplace_issue(text: str) -> bool:
    t = (text or "").lower()
    keywords = [
        "harass", "harassment", "flirt", "flirting", "inappropriate", "unwanted",
        "sexual", "bully", "bullying", "abuse", "abusive", "threat", "threaten",
        "stalk", "stalking", "discriminate", "discrimination", "assault"
    ]
    return any(k in t for k in keywords)

def sensitive_guidance_message() -> str:
    return (
        "This involves workplace conduct and safety. Here’s a concise professional guide you can use right now:\n\n"
        "1) Safety first: If you ever feel unsafe, step away and contact a trusted senior/HR immediately.\n"
        "2) Document facts: date/time, what was said/done, location, witnesses, any messages.\n"
        "3) Boundary script (DM or in person): ‘I want to be clear — that made me uncomfortable. \n"
        "Please keep our interactions professional and work‑related.’\n\n"
        "4) HR email template:\nSubject: Concern about inappropriate conduct\n"
        "Hi [HR/Manager Name],\nI’m writing to document an incident that made me uncomfortable at work.\n"
        "On [date/time] at [place], [name/role] [brief factual description].\n"
        "This affects my ability to work comfortably. I’m requesting guidance on next steps.\n"
        "I’ve attached any relevant evidence.\nThanks,\n[Your Name]\n\n"
        "If you want, I can help refine the boundary message or the email draft."
    )

def send_meta_text(to_number: str, text: str):
    url = f"https://graph.facebook.com/v17.0/{META_PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "to": to_number, "text": {"body": text}}
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers)
    try:
        resp.raise_for_status()
    except Exception as e:
        logger.exception("Error sending meta text: %s -- response: %s", e, resp.text if resp is not None else None)
    return resp

def send_meta_interactive_tone_choice(to_number: str):
    url = f"https://graph.facebook.com/v17.0/{META_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Would you like replies in a Professional or Casual tone?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "tone_professional", "title": "Professional"}},
                    {"type": "reply", "reply": {"id": "tone_casual", "title": "Casual"}},
                ]
            },
        },
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers)
    try:
        resp.raise_for_status()
    except Exception as e:
        logger.exception("Error sending meta interactive: %s -- response: %s", e, resp.text if resp is not None else None)
    return resp

# --- Flask app ---
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# Log current prompt configuration so we can verify on Render
logger.info("Prompt mode: %s | Workplace-only: %s | One-time unauthorized notification: enabled", PROMPT_MODE, WORKPLACE_ONLY)

@app.route("/health", methods=["GET"])
def health():
    return {"ok": True, "prompt_mode": PROMPT_MODE, "workplace_only": WORKPLACE_ONLY}, 200

# --- Meta Webhook ---
@app.route("/meta-webhook", methods=["GET", "POST"])
def meta_webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        challenge = request.args.get("hub.challenge")
        verify_token = request.args.get("hub.verify_token")
        if mode == "subscribe" and verify_token == META_VERIFY_TOKEN:
            logger.info("META webhook verified!")
            return str(challenge), 200
        return "Verification token mismatch", 403

    try:
        data = request.get_json(silent=True)
        logger.debug("Incoming Meta webhook: %s", data)
        if not data:
            return jsonify({"status": "no data"}), 200

        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    messages = value.get("messages", []) or []
                    for msg in messages:
                        from_number = msg.get("from") or "anonymous"
                        message_id = msg.get("id")
                        user_input = None
                        
                        # Debug logging for iOS/Android differences
                        logger.debug(f"Incoming message type: {msg.get('type')}, from: {from_number}, msg_id: {message_id}")
                        
                        # Authorization check (notify once per unauthorized number)
                        if not is_user_authorized(from_number):
                            norm = normalize_phone(from_number)
                            if norm not in unauthorized_notified:
                                logger.warning(f"Unauthorized access attempt from: {from_number} (first notice)")
                                try:
                                    send_meta_text(from_number, "❌ Unauthorized. Contact admin for access.")
                                except Exception:
                                    logger.exception("Error sending unauthorized message")
                                unauthorized_notified.add(norm)
                            else:
                                logger.info(f"Suppressing repeat unauthorized notice for: {from_number}")
                            continue

                        # text messages
                        if msg.get("type") == "text":
                            user_input = msg["text"]["body"].strip()
                            logger.debug(f"Text message received: '{user_input}' (length: {len(user_input)})")
                            # Normalize greetings for consistent handling across iOS/Android
                            # Check if it's a greeting (hi, hello, hey with variations)
                            normalized_input = user_input.lower().strip().rstrip('!?.,;: ')
                            is_greeting = (
                                normalized_input in ("hi", "hello", "hey", "sup", "yo", "hii", "hiii", "hiiii", "helloo", "hellooo", "heyy", "heyyy") or
                                (normalized_input.startswith("hi") and len(normalized_input) <= 6 and all(c in "hi!" for c in normalized_input)) or
                                (normalized_input.startswith("hey") and len(normalized_input) <= 7 and all(c in "hey!" for c in normalized_input))
                            )
                            if is_greeting:
                                logger.info(f"Greeting detected: '{user_input}' (reaction disabled)")
                                # Heart reaction disabled to avoid 401 errors
                                # To re-enable: update META_ACCESS_TOKEN in Render and uncomment below
                                # try:
                                #     if message_id:
                                #         send_whatsapp_reaction(
                                #             from_number, message_id, "❤️", META_PHONE_NUMBER_ID, META_ACCESS_TOKEN
                                #         )
                                # except Exception:
                                #     logger.exception("Error sending reaction on greeting")
                                # Let AI respond naturally in casual tone (no tone selection buttons)
                            else:
                                # Enforce workplace-only topics before generating reply
                                if is_non_workplace_topic(user_input):
                                    try:
                                        send_meta_text(from_number, workplace_boundary_message())
                                    except Exception:
                                        logger.exception("Error sending boundary message")
                                    continue
                                # Auto-switch to professional mode for sensitive issues
                                if is_sensitive_workplace_issue(user_input):
                                    try:
                                        send_meta_text(from_number, sensitive_guidance_message())
                                    except Exception:
                                        logger.exception("Error sending sensitive guidance")
                                    continue

                        # audio/voice handling (Meta)
                        elif msg.get("type") in ("audio", "voice"):
                            try:
                                media_obj = msg.get(msg["type"], {})  # contains id
                                media_id = media_obj.get("id")
                                if media_id:
                                    media_url_fetch = f"https://graph.facebook.com/v17.0/{media_id}"
                                    params = {"access_token": META_ACCESS_TOKEN, "fields": "url"}
                                    media_resp = requests.get(media_url_fetch, params=params, timeout=15)
                                    media_resp.raise_for_status()
                                    media_json = media_resp.json()
                                    media_link = media_json.get("url") or media_json.get("secure_url") or media_json.get("data", {}).get("url")
                                    if media_link:
                                        with tempfile.TemporaryDirectory() as tmpdir:
                                            raw_path = os.path.join(tmpdir, "voice_input")
                                            download_media(media_link, raw_path)
                                            transcription = transcribe_with_openai(raw_path)
                                            if not transcription:
                                                mp3_path = os.path.join(tmpdir, "voice.mp3")
                                                try:
                                                    convert_to_mp3(raw_path, mp3_path)
                                                    transcription = transcribe_with_openai(mp3_path)
                                                except subprocess.CalledProcessError as cpe:
                                                    logger.exception("ffmpeg conversion failed for meta media: %s", cpe)
                                            user_input = transcription or "[voice message received but could not transcribe]"
                                    else:
                                        logger.warning("No media link returned for media id %s", media_id)
                                        user_input = "[voice message received but could not fetch media]"
                                else:
                                    logger.warning("No media id found in message object: %s", msg)
                                    user_input = "[voice message received but media id missing]"
                            except Exception as e:
                                logger.exception("Error fetching/transcribing meta audio: %s", e)
                                user_input = "[voice message received but could not transcribe]"

                        # interactive payload (button replies)
                        interactive = msg.get("interactive")
                        if interactive:
                            i_type = interactive.get("type")
                            if i_type == "button_reply":
                                br = interactive.get("button_reply", {})
                                button_id = br.get("id", "")
                                button_title = br.get("title", "").lower()
                                chosen_tone = None
                                if "professional" in button_id or "professional" in button_title:
                                    chosen_tone = "professional"
                                elif "casual" in button_id or "casual" in button_title:
                                    chosen_tone = "casual"
                                if chosen_tone:
                                    tone_preferences[from_number] = chosen_tone
                                    try:
                                        send_meta_text(
                                            from_number,
                                            f"Got it — I'll reply in a {chosen_tone.capitalize()} tone. How can I help today?",
                                        )
                                    except Exception:
                                        logger.exception("Error sending tone acknowledgement")
                                    continue

                        if not user_input:
                            # fallback skip (locations, stickers etc.)
                            user_input = None

                        if user_input:
                            # Heart reactions only for greetings (already handled above in greeting detection)
                            # Skip heart reaction here to avoid reacting to all messages

                            reply_text = generate_reply_for_input(from_number, user_input)
                            send_url = f"https://graph.facebook.com/v17.0/{META_PHONE_NUMBER_ID}/messages"
                            payload = {
                                "messaging_product": "whatsapp",
                                "to": from_number,
                                "text": {"body": reply_text},
                            }
                            headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
                            try:
                                resp = requests.post(send_url, json=payload, headers=headers)
                                logger.debug("Meta reply sent: %s -- %s", resp.status_code, resp.text)
                            except Exception:
                                logger.exception("Error sending Meta reply")

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.exception("Error in Meta webhook: %s", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
