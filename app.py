# app.py (full updated: robust Twilio/Meta media handling, auth sanitization, detailed logging)
import os
import tempfile
import subprocess
import requests
import traceback
import logging
import base64
import re
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

# --- Load environment ---
load_dotenv()
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Accept multiple env names and sanitize (strip quotes/spaces)
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("TWILIO_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_AUTH") or os.getenv("TWILIO_AUTH_TOKEN".upper()) or "").strip()

# Backwards-compat aliases
TWILIO_SID = TWILIO_ACCOUNT_SID
TWILIO_AUTH = TWILIO_AUTH_TOKEN

TWILIO_VALIDATE = os.getenv("TWILIO_VALIDATE", "true").lower() == "true"
DEBUG_SAVE_MEDIA = os.getenv("DEBUG_SAVE_MEDIA", "false").lower() == "true"

# Meta WhatsApp cloud env
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "stepbot_verify")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()

# logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("whatsapp-bot")

# quick runtime debug about Twilio env presence (does not log secrets)
logger.info("TWILIO_ACCOUNT_SID present: %s", bool(TWILIO_ACCOUNT_SID))
logger.info("TWILIO_AUTH_TOKEN present: %s", bool(TWILIO_AUTH_TOKEN))
logger.info("TWILIO_AUTH_TOKEN length: %s", len(TWILIO_AUTH_TOKEN or ""))

# --- Phone Number Authorization ---
ALLOWED_USERS_FILE = "allowed_users.txt"
allowed_phone_numbers = set()

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
openai_client = None
try:
    from openai import OpenAI

    if OPENAI_API_KEY:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    else:
        openai_client = OpenAI()
    logger.info("OpenAI client initialized")
except Exception as e:
    logger.exception("Could not initialize OpenAI v1 client: %s", e)
    openai_client = None

# --- RAG pipeline setup (langchain + vectorstore) + ChatPromptTemplate-based prompts ---
ChatPromptTemplateImpl = None
MessagesPlaceholderImpl = None
FAISS = None
GoogleGenerativeAIEmbeddings = None
ChatOpenAI = None
create_history_aware_retriever = None
create_retrieval_chain = None
create_stuff_documents_chain = None

embeddings = None
db = None
db_retriever = None

# Guarded, granular imports so missing packages won't crash the app at startup
try:
    # Chat prompt template (try multiple possible import paths)
    try:
        from langchain_core.prompts import ChatPromptTemplate as ChatPromptTemplateImpl, MessagesPlaceholder as MessagesPlaceholderImpl
        logger.info("Imported ChatPromptTemplate from langchain_core.prompts")
    except Exception:
        try:
            from langchain.prompts.chat import ChatPromptTemplate as ChatPromptTemplateImpl, MessagesPlaceholder as MessagesPlaceholderImpl
            logger.info("Imported ChatPromptTemplate from langchain.prompts.chat")
        except Exception:
            ChatPromptTemplateImpl = None
            MessagesPlaceholderImpl = None
            logger.debug("ChatPromptTemplate not available (optional)")

    # vectorstore and embeddings (optional)
    try:
        from langchain_community.vectorstores import FAISS
        logger.info("Imported FAISS vectorstore")
    except Exception:
        FAISS = None
        logger.debug("FAISS not available (optional)")

    try:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        logger.info("Imported GoogleGenerativeAIEmbeddings")
    except Exception:
        GoogleGenerativeAIEmbeddings = None
        logger.debug("GoogleGenerativeAIEmbeddings not available (optional)")

    # langchain Chat LLM wrapper (optional)
    try:
        from langchain_openai import ChatOpenAI
        ChatOpenAI = ChatOpenAI
        logger.info("Imported langchain_openai.ChatOpenAI")
    except Exception:
        ChatOpenAI = None
        logger.debug("langchain_openai.ChatOpenAI not available (optional)")

    # chains helpers (optional) - try multiple paths and fallback to None
    try:
        # newer langchain organization may expose these in different modules
        from langchain.chains import create_history_aware_retriever, create_retrieval_chain
        from langchain.chains.combine_documents import create_stuff_documents_chain
        logger.info("Imported chain helpers from langchain.chains")
    except Exception:
        try:
            # fallback to older or alternate locations
            from langchain_community.chains import create_history_aware_retriever, create_retrieval_chain
            from langchain.chains.combine_documents import create_stuff_documents_chain
            logger.info("Imported chain helpers from fallback locations")
        except Exception:
            create_history_aware_retriever = None
            create_retrieval_chain = None
            create_stuff_documents_chain = None
            logger.debug("Chain helpers not available (optional)")

    # Attempt to initialize embeddings + Qdrant vector store
    if GoogleGenerativeAIEmbeddings:
        try:
            # Import Qdrant components
            try:
                from qdrant_client import QdrantClient
                from langchain_qdrant import QdrantVectorStore
                logger.info("Imported Qdrant components")
            except Exception as e:
                logger.warning("Qdrant not available, falling back to FAISS if available: %s", e)
                QdrantClient = None
                QdrantVectorStore = None
            
            embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
            
            # Try Qdrant first (prioritize cloud vector store)
            if QdrantClient and QdrantVectorStore:
                qdrant_url = os.getenv("QDRANT_URL")
                qdrant_api_key = os.getenv("QDRANT_API_KEY")
                
                if qdrant_url:
                    try:
                        # Connect to Qdrant Cloud
                        qdrant_client = QdrantClient(
                            url=qdrant_url,
                            api_key=qdrant_api_key
                        )
                        
                        # Initialize Qdrant vectorstore
                        db = QdrantVectorStore(
                            client=qdrant_client,
                            collection_name="bridgetext_scenarios",
                            embedding=embeddings
                        )
                        
                        db_retriever = db.as_retriever(
                            search_type="similarity",
                            search_kwargs={"k": 3}  # Retrieve top 3 relevant scenarios
                        )
                        
                        logger.info("✅ Qdrant vector store loaded successfully from %s", qdrant_url)
                    except Exception as e:
                        logger.exception("Failed to connect to Qdrant: %s", e)
                        db = None
                        db_retriever = None
                else:
                    logger.info("QDRANT_URL not set, skipping Qdrant initialization")
                    db = None
                    db_retriever = None
            
            # Fallback to FAISS if Qdrant not available
            elif FAISS:
                vs_path = "my_vector_store"
                if os.path.isdir(vs_path):
                    try:
                        db = FAISS.load_local(vs_path, embeddings, allow_dangerous_deserialization=True)
                        db_retriever = db.as_retriever(search_type="similarity", search_kwargs={"k": 4})
                        logger.info("Vector store loaded from local FAISS: %s", vs_path)
                    except Exception as e:
                        logger.exception("Failed to load local vector store at %s: %s", vs_path, e)
                        db = None
                        db_retriever = None
                else:
                    logger.info("Vector store path %s not found; skipping load", vs_path)
                    db = None
                    db_retriever = None
            else:
                logger.info("No vector store available (neither Qdrant nor FAISS)")
                db = None
                db_retriever = None
                
        except Exception as e:
            logger.exception("Error initializing embeddings or vector store: %s", e)
            embeddings = None
            db = None
            db_retriever = None
    else:
        logger.info("Skipping embeddings initialization (missing GoogleGenerativeAIEmbeddings)")

except Exception as e:
    # Catch-all safeguard: never let optional langchain failures crash the app
    logger.exception("Error initializing vector store or langchain modules: %s", e)
    ChatPromptTemplateImpl = None
    MessagesPlaceholderImpl = None
    FAISS = None
    GoogleGenerativeAIEmbeddings = None
    ChatOpenAI = None
    create_history_aware_retriever = None
    create_retrieval_chain = None
    create_stuff_documents_chain = None
    embeddings = None
    db = None
    db_retriever = None

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
• EXCEPTION: When users ask for formal documents (like leave applications, emails to management, formal letters), respond in casual tone first saying "Hey, I don't have your company's policies, but here's a common professional template you can use:" then provide the professional format.
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

CONTEXT: {context}
CHAT_HISTORY: {chat_history}
QUESTION: {question}
ANSWER:
</s>[INST]
"""

# Prompt/runtime flags for quick verification in logs and /health
PROMPT_MODE = "casual_default_with_auto_professional_templates"
WORKPLACE_ONLY = True

# Build ChatPromptTemplate-based prompts (fallback to simple shim if ChatPromptTemplate missing)
if ChatPromptTemplateImpl is not None and MessagesPlaceholderImpl is not None:
    contextualize_q_system_prompt = "Given the conversation so far and a follow-up question, rephrase the follow-up question to be a standalone question."
    contextualize_q_prompt = ChatPromptTemplateImpl.from_messages(
        [
            ("system", contextualize_q_system_prompt),
            ("placeholder", "{chat_history}"),
            ("human", "{input}"),
        ]
    )

    qa_chat_prompt = ChatPromptTemplateImpl.from_messages(
        [
            ("system", prompt_template),
            ("placeholder", "{chat_history}"),
            ("human", "{input}"),
        ]
    )
else:
    # minimal shim: simple wrapper that mimics .from_messages().invoke/format usage
    class _ShimChatPrompt:
        def __init__(self, messages):
            self.messages = messages

        @classmethod
        def from_messages(cls, messages):
            return cls(messages)

        def invoke(self, mapping):
            parts = []
            for role, content in self.messages:
                if role == "placeholder":
                    varname = content.strip().strip("{}")
                    chat_history = mapping.get(varname) or mapping.get("chat_history") or []
                    if isinstance(chat_history, list):
                        for m in chat_history:
                            if isinstance(m, dict):
                                parts.append(f"{m.get('role','user')}: {m.get('content','')}")
                            elif isinstance(m, (list, tuple)) and len(m) == 2:
                                parts.append(f"{m[0]}: {m[1]}")
                    else:
                        parts.append(str(chat_history))
                else:
                    try:
                        parts.append(content.format(**mapping))
                    except Exception:
                        parts.append(content)
            return {"text": "\n\n".join(parts)}

    contextualize_q_prompt = _ShimChatPrompt.from_messages(
        [("system", "Given the conversation so far and a follow-up question, rephrase the follow-up question to be a standalone question."), ("placeholder", "{chat_history}"), ("human", "{input}")]
    )
    qa_chat_prompt = _ShimChatPrompt.from_messages([("system", prompt_template), ("placeholder", "{chat_history}"), ("human", "{input}")])

# LLM + chains (guarded, retains existing behaviour)
try:
    if ChatOpenAI is None:
        try:
            from langchain_openai import ChatOpenAI
            ChatOpenAI = ChatOpenAI
        except Exception:
            ChatOpenAI = None

    llm = None
    if ChatOpenAI is not None:
        try:
            # Create a lightweight wrapper instance if possible
            llm = ChatOpenAI(api_key=OPENAI_API_KEY, model_name="gpt-4o-mini")
            logger.info("Langchain ChatOpenAI initialized")
        except Exception as e:
            logger.exception("Failed to initialize ChatOpenAI: %s", e)
            llm = None
    else:
        llm = None

    if llm and db_retriever and create_history_aware_retriever and create_retrieval_chain and create_stuff_documents_chain:
        try:
            history_aware_retriever = create_history_aware_retriever(llm, db_retriever, contextualize_q_prompt)
            question_answer_chain = create_stuff_documents_chain(llm, qa_chat_prompt)
            qa = create_retrieval_chain(history_aware_retriever, question_answer_chain)
            logger.info("LLM and retrieval chain initialized")
        except Exception as e:
            logger.exception("Failed to wire up retrieval chain: %s", e)
            qa = None
            history_aware_retriever = None
    else:
        qa = None
        history_aware_retriever = None
        logger.info("LLM/chain not initialized (missing optional dependencies); falling back to non-RAG behaviour")
except Exception as e:
    logger.exception("Error initializing LLM or chains: %s", e)
    llm = None
    qa = None

# --- Detailed RAG dependency logging ---
if not llm:
    logger.warning("RAG not enabled: llm (ChatOpenAI) is missing or failed to initialize.")
if not db_retriever:
    logger.warning("RAG not enabled: db_retriever (Qdrant retriever) is missing or failed to initialize.")
if not create_history_aware_retriever:
    logger.warning("RAG not enabled: create_history_aware_retriever is missing.")
if not create_retrieval_chain:
    logger.warning("RAG not enabled: create_retrieval_chain is missing.")
if not create_stuff_documents_chain:
    logger.warning("RAG not enabled: create_stuff_documents_chain is missing.")

# Conversation memory and tone preferences
conversation_memory = {}
tone_preferences = {}  # maps user_id -> "professional" | "casual"

def generate_reply_for_input(user_id: str, user_input: str) -> str:
    # No tone preference needed - AI automatically uses casual tone by default
    effective_input = user_input
    chat_history_for_chain = conversation_memory.get(user_id, [])
    try:
        if qa:
            # prefer the retrieval chain API which expects {"input": ..., "chat_history": ...}
            result = qa.invoke({"input": effective_input, "chat_history": chat_history_for_chain})
        else:
            # fallback: if no QA chain, try to use OpenAI client directly for a simple chat reply
            if openai_client:
                # build a simple chat-style prompt using the system prompt and history
                messages = []
                messages.append({"role": "system", "content": prompt_template})
                for h in chat_history_for_chain:
                    # accept both dict role/content and tuple forms
                    if isinstance(h, dict) and "role" in h and "content" in h:
                        messages.append({"role": h["role"], "content": h["content"]})
                    elif isinstance(h, (list, tuple)) and len(h) == 2:
                        messages.append({"role": "user", "content": h[1]})
                messages.append({"role": "user", "content": effective_input})
                try:
                    resp = openai_client.chat.completions.create(model="gpt-4o-mini", messages=messages)
                    text = None

                    # 1) dict-like responses
                    if isinstance(resp, dict):
                        choices = resp.get("choices") or []
                        if choices:
                            # handle both "message" and legacy "text"
                            text = choices[0].get("message", {}).get("content") or choices[0].get("text")
                        result = {"answer": text or "Sorry, I'm having trouble right now."}

                    else:
                        # 2) object-like responses (OpenAI SDK objects)
                        try:
                            # try attribute-style access (choices -> message -> content)
                            choices = getattr(resp, "choices", None)
                            if choices and len(choices) > 0:
                                first = choices[0]
                                # prefer .message.content
                                msg = getattr(first, "message", None)
                                if msg is not None:
                                    text = getattr(msg, "content", None)
                                # fallback to .text or .delta
                                if not text:
                                    text = getattr(first, "text", None)
                                    if not text:
                                        # sometimes content is nested under .message['content']-like
                                        try:
                                            text = first.get("message", {}).get("content")
                                        except Exception:
                                            text = None
                            # final fallback: stringification may include content; attempt regex
                            if not text:
                                s = str(resp)
                                m = re.search(r"message=ChatCompletionMessage\\(content='(.*?)'", s)
                                if not m:
                                    m = re.search(r"content='(.*?)'", s)
                                if m:
                                    text = m.group(1)
                            result = {"answer": text or str(resp)}
                        except Exception:
                            # absolute fallback to string
                            result = {"answer": str(resp)}
                except Exception as e:
                    logger.exception("Error calling OpenAI chat completions: %s", e)
                    result = {"answer": "Sorry, something went wrong while processing your request."}
            else:
                result = {"answer": "Sorry, the assistant is temporarily unavailable."}
    except Exception as e:
        logger.exception("Error invoking QA chain: %s", e)
        result = {"answer": "Sorry, something went wrong while processing your request."}

    # normalize result to a single answer string
    if isinstance(result, dict):
        answer = (
            result.get("answer")
            or result.get("output_text")
            or result.get("result")
            or result.get("output")
            or ""
        )
    else:
        answer = result if isinstance(result, str) else str(result)

    # Ensure answer is a plain string (no nested objects)
    if not isinstance(answer, str):
        try:
            answer = str(answer)
        except Exception:
            answer = "Sorry, I couldn't format the assistant response."

    # maintain conversation memory
    history = chat_history_for_chain[:] if chat_history_for_chain else []
    history += [{"role": "user", "content": user_input}, {"role": "assistant", "content": answer}]
    if len(history) > 20:
        history = history[-20:]
    conversation_memory[user_id] = history
    return answer

# --- Robust helpers for audio download, conversion, transcription ---

def _basic_auth_header(auth: tuple):
    if not auth:
        return {}
    user, passwd = auth
    token = base64.b64encode(f"{user}:{passwd}".encode()).decode()
    return {"Authorization": f"Basic {token}"}

def download_media(url: str, dest_path: str, auth: tuple = None, timeout: int = 30):
    """
    Multi-strategy media downloader:
     - try unauthenticated GET (some providers return public link)
     - try GET with Authorization header preserved across redirects
     - manual redirect-follow with requests.get(..., auth=auth) on each hop
     - try Twilio /Content endpoint fallback
    Raises requests.HTTPError on failure.
    """
    logger.debug("download_media called for %s (auth=%s)", url, bool(auth))

    # Strategy 1: simple unauthenticated GET (useful for Meta or public CDN links)
    try:
        r = requests.get(url, stream=True, timeout=10, allow_redirects=True)
        logger.debug("Unauthenticated fetch status=%s headers=%s", r.status_code, dict(list(r.headers.items())[:5]))
        if 200 <= r.status_code < 300 and (r.headers.get("content-length") or r.content):
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(10240):
                    if chunk:
                        f.write(chunk)
            logger.debug("Saved media via unauthenticated fetch (%d bytes)", os.path.getsize(dest_path))
            return
    except Exception as e:
        logger.debug("Unauthenticated fetch failed: %s", e)

    # Strategy 2: GET with Basic Authorization header attached (some CDNs accept header)
    if auth:
        headers = _basic_auth_header(auth)
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=15, allow_redirects=True)
            logger.debug("Auth-header fetch status=%s headers=%s", r.status_code, dict(list(r.headers.items())[:5]))
            if 200 <= r.status_code < 300 and (r.headers.get("content-length") or r.content):
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(10240):
                        if chunk:
                            f.write(chunk)
                logger.debug("Saved media via auth-header fetch (%d bytes)", os.path.getsize(dest_path))
                return
            else:
                logger.warning("Auth-header fetch returned non-2xx: %s. body-snippet=%s", r.status_code, (r.content[:256] if r.content else b""))
        except Exception as e:
            logger.debug("Auth-header fetch failed: %s", e)

    # Strategy 3: manual redirect-follow with auth on each hop (preserve HTTP Basic auth)
    if auth:
        try:
            current = url
            hops = 0
            max_hops = 8
            while hops < max_hops:
                logger.debug("Manual-auth hop %s -> %s", hops, current)
                r = requests.get(current, auth=auth, stream=True, timeout=15, allow_redirects=False)
                logger.debug("  status=%s headers=%s", r.status_code, dict(list(r.headers.items())[:5]))
                if 200 <= r.status_code < 300:
                    with open(dest_path, "wb") as f:
                        for chunk in r.iter_content(10240):
                            if chunk:
                                f.write(chunk)
                    logger.debug("Saved media via manual-auth fetch (%d bytes)", os.path.getsize(dest_path))
                    return
                if r.is_redirect or r.is_permanent_redirect:
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if not loc:
                        r.raise_for_status()
                    parsed = urlparse(loc)
                    if not parsed.scheme:
                        current = requests.compat.urljoin(current, loc)
                    else:
                        current = loc
                    hops += 1
                    continue
                logger.warning("Manual-auth fetch non-2xx and not redirect: status=%s body-snippet=%s", r.status_code, (r.content[:256] if r.content else b""))
                r.raise_for_status()
        except Exception as e:
            logger.exception("Manual-auth fetch failed: %s", e)

    # Strategy 4: Twilio media Content endpoint fallback (append /Content)
    try:
        parsed = urlparse(url)
        if "api.twilio.com" in parsed.netloc and auth:
            alt = url
            if not parsed.path.endswith("/Content"):
                alt = f"{url}/Content"
            logger.debug("Trying Twilio Content endpoint: %s", alt)
            r = requests.get(alt, auth=auth, stream=True, timeout=15, allow_redirects=True)
            logger.debug("Twilio Content fetch status=%s headers=%s", r.status_code, dict(list(r.headers.items())[:5]))
            if 200 <= r.status_code < 300 and (r.headers.get("content-length") or r.content):
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(10240):
                        if chunk:
                            f.write(chunk)
                logger.debug("Saved media via Twilio Content endpoint (%d bytes)", os.path.getsize(dest_path))
                return
            else:
                logger.warning("Twilio Content fetch failed status=%s body-snippet=%s", r.status_code, (r.content[:256] if r.content else b""))
    except Exception as e:
        logger.debug("Twilio Content endpoint attempt failed: %s", e)

    # All strategies exhausted -> raise
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
validator = None
try:
    validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None
except Exception:
    validator = None
    logger.debug("Twilio RequestValidator not available or failed to initialize")

# Log current prompt configuration so we can verify on Render
logger.info("Prompt mode: %s | Workplace-only: %s", PROMPT_MODE, WORKPLACE_ONLY)

@app.route("/health", methods=["GET"])
def health():
    return {"ok": True, "prompt_mode": PROMPT_MODE, "workplace_only": WORKPLACE_ONLY}, 200

# --- Twilio Webhook ---
@app.route("/whatsapp-webhook", methods=["POST"])
def whatsapp_webhook():
    try:
        if validator and TWILIO_VALIDATE:
            signature = request.headers.get("X-Twilio-Signature", "")
            try:
                if not validator.validate(request.url, request.form, signature):
                    logger.warning("Invalid Twilio signature")
                    return ("Invalid signature", 403)
            except Exception:
                logger.exception("Twilio signature validation failed; continuing (validator threw)")

        from_number = request.form.get("From", "anonymous")
        incoming_msg = (request.form.get("Body") or "").strip()
        num_media = int(request.form.get("NumMedia", "0"))

        user_input = None
        if num_media > 0:
            media_url = request.form.get("MediaUrl0")
            media_ct = request.form.get("MediaContentType0", "")  # e.g. audio/ogg, audio/mpeg
            logger.debug("Incoming Twilio media: url=%s content-type=%s", media_url, media_ct)

            # quick account-match diagnostic: ensure media_url account matches TWILIO_ACCOUNT_SID
            try:
                if media_url and TWILIO_ACCOUNT_SID and f"/Accounts/{TWILIO_ACCOUNT_SID}/" not in media_url:
                    logger.warning("Media URL account mismatch: media_url=%s, expected account sid=%s", media_url, TWILIO_ACCOUNT_SID)
            except Exception:
                logger.debug("Skipping account match check due to parse error")

            # map content-type to extension
            ext = ".bin"
            if "ogg" in media_ct or "opus" in media_ct:
                ext = ".ogg"
            elif "mpeg" in media_ct or "mp3" in media_ct:
                ext = ".mp3"
            elif "wav" in media_ct:
                ext = ".wav"

            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    raw_path = os.path.join(tmpdir, f"incoming_media{ext}")

                    acct = TWILIO_ACCOUNT_SID.strip() if TWILIO_ACCOUNT_SID else ""
                    token = TWILIO_AUTH_TOKEN.strip() if TWILIO_AUTH_TOKEN else ""
                    auth_tuple = (acct, token) if (acct and token) else None

                    logger.info("Media fetch will attempt auth=%s, account_sid_present=%s", bool(auth_tuple), bool(acct))

                    # Use robust multi-strategy downloader
                    download_media(media_url, raw_path, auth=auth_tuple, timeout=30)

                    # transcription attempts
                    transcription = transcribe_with_openai(raw_path)
                    if not transcription:
                        mp3_path = os.path.join(tmpdir, "converted.mp3")
                        try:
                            convert_to_mp3(raw_path, mp3_path)
                            transcription = transcribe_with_openai(mp3_path)
                        except subprocess.CalledProcessError as cpe:
                            logger.exception("ffmpeg conversion failed: %s", cpe)
                    user_input = transcription or "[voice message received but could not transcribe]"

                    if DEBUG_SAVE_MEDIA:
                        save_dest = os.path.join(os.getcwd(), f"debug_media_{os.path.basename(raw_path)}")
                        with open(raw_path, "rb") as rfh, open(save_dest, "wb") as wfh:
                            wfh.write(rfh.read())
                        logger.info("Saved debug media to %s", save_dest)
            except requests.HTTPError as http_err:
                logger.exception("HTTP error processing incoming media: %s", http_err)
                user_input = "[error processing voice message - media fetch failed]"
            except Exception as e:
                logger.exception("Error processing incoming media: %s", e)
                user_input = "[error processing voice message]"
        else:
            user_input = incoming_msg

        if not user_input:
            resp = MessagingResponse()
            resp.message("👋 I didn’t receive any text. Please send a message.")
            return str(resp), 200

        reply = generate_reply_for_input(from_number, user_input)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp), 200
    except Exception as e:
        logger.exception("Unhandled error in whatsapp_webhook: %s", e)
        resp = MessagingResponse()
        resp.message("Sorry, something went wrong processing your message.")
        return str(resp), 500

@app.route("/whatsapp-status", methods=["POST"])
def whatsapp_status():
    return ("", 204)

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
                        
                        # Authorization check
                        if not is_user_authorized(from_number):
                            logger.warning(f"Unauthorized access attempt from: {from_number}")
                            try:
                                send_meta_text(from_number, "❌ Unauthorized. Contact admin for access.")
                            except Exception:
                                logger.exception("Error sending unauthorized message")
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
                                logger.info(f"Greeting detected: '{user_input}' -> sending ❤️ reaction")
                                try:
                                    if message_id:
                                        send_whatsapp_reaction(
                                            from_number, message_id, "❤️", META_PHONE_NUMBER_ID, META_ACCESS_TOKEN
                                        )
                                except Exception:
                                    logger.exception("Error sending reaction on greeting")
                                # Let AI respond naturally in casual tone (no tone selection buttons)

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
                                            download_media(media_link, raw_path)  # Meta link usually doesn't need Twilio auth
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
