# BridgeText — WhatsApp Workplace Coaching Chatbot

This repository contains a WhatsApp chatbot built with Flask that provides short, framework-guided workplace coaching (STEP + 4Rs). It accepts text and voice messages (Twilio and Meta/WhatsApp Cloud), can transcribe audio via OpenAI, and optionally performs retrieval-augmented generation (RAG) using a FAISS vector store created from PDF documents.

This README explains what the project uses, how it works, how to run it locally, environment variables, the ingestion flow (building the vector store), and troubleshooting tips.

## Table of contents

- Overview
- Architecture & flow
- Files and responsibilities
- Dependencies
- Environment variables
- Setup & run (local)
- Ingestion: building the vector store
- How messages are handled (Twilio + Meta)
- Audio handling and transcription
- RAG / embeddings (optional)
- Testing
- Troubleshooting and tips
- Security & privacy notes
- Next steps

## Overview

BridgeText is a lightweight Flask app that exposes webhooks for WhatsApp through two common integration paths:

- Twilio (incoming messages sent to `/whatsapp-webhook`) — supports text and Twilio-hosted media URLs. The app validates Twilio signatures when configured.
- Meta/WhatsApp Cloud (incoming messages to `/meta-webhook`) — supports text, interactive replies (buttons), and cloud-hosted media via the Graph API.

The core chat behaviour is a short coaching assistant using an explicit prompt that encodes two frameworks:

- STEP (Spot–Think–Engage–Perform) — for adaptability/flexibility
- 4Rs (Recognize–Regulate–Respect–Reflect) — for emotional intelligence

Responses are intentionally concise (text-like, 1–2 sentences). The app optionally uses a RAG retrieval chain if embeddings and FAISS are available.

## Architecture & flow

High-level flow:

1. Incoming webhook (Twilio or Meta) -> Flask route.
2. If the message has media (voice note), the app downloads the media with a robust multi-strategy downloader.
3. If audio, attempt transcription using OpenAI's audio transcription APIs (primary: gpt-4o-transcribe, fallback: whisper-1).
4. Generate reply via one of the following (in order):
	 - Retrieval-augmented chain (RAG) if LangChain+FAISS+embeddings+LLM chain are all available.
	 - Direct OpenAI chat completion if RAG isn't available but OpenAI SDK is configured.
	 - Fallback message when assistant/unavailable.
5. Send reply back using Twilio's response for Twilio integration or Graph API POST for Meta.

The code is structured to be robust when optional dependencies (langchain, FAISS, Google embeddings, etc.) are missing—app will still accept messages and use a simple OpenAI chat flow if possible.

## Files and responsibilities

- `app.py` — Main Flask application. Contains webhook endpoints (`/whatsapp-webhook`, `/meta-webhook`, `/whatsapp-status`, `/health`), media download helpers, audio conversion and transcription helpers, the prompt template, LLM/chain wiring, and logic for replying and tone preferences.
- `ingestion.py` — Script to create/searchable vector store using `langchain_community` and `GoogleGenerativeAIEmbeddings`. It reads PDFs from `./LEGAL-DATA`, splits text into chunks, embeds them, produces FAISS vector store, and saves it to `my_vector_store`.
- `requirements.txt` — Project dependency list (packages used or recommended).
- `test_openai.py` — Minimal script to validate OpenAI API access and a simple chat call.
- `my_vector_store/` — Where FAISS index files are persisted (produced by `ingestion.py`).

## Dependencies

Primary Python packages referenced in the code and in `requirements.txt`:

- flask
- python-dotenv
- twilio
- requests
- langchain (optional)
- langchain-core / langchain-openai / langchain_community (optional for RAG)
- langchain_google_genai (optional — embeddings)
- faiss-cpu (optional for FAISS vectorstore)
- pypdf / PyPDF2 (for ingestion)
- ffmpeg (external binary, required for audio conversions)
- openai (OpenAI Python SDK, used by code via `from openai import OpenAI`)

Note: Many components are optional. The app can run without langchain/FAISS if you only want simple text-based OpenAI responses. If you plan to transcribe audio, make sure ffmpeg is installed and reachable from PATH.

## Environment variables

The app reads its configuration from environment variables. Key variables (and typical usage):

- OPENAI_API_KEY — OpenAI API key (used by `OpenAI` client and for transcription). Required to call OpenAI.
- GOOGLE_API_KEY — Google API key (used by `langchain_google_genai` embeddings if you use that provider).
- TWILIO_ACCOUNT_SID — Twilio account SID (optional; used for diagnostics and to fetch Twilio media endpoints that require account scoping).
- TWILIO_AUTH_TOKEN — Twilio auth token (used to validate Twilio webhook signatures and for authenticated media downloads).
- TWILIO_VALIDATE — 'true'/'false' — enable Twilio signature validation (default: true).
- DEBUG_SAVE_MEDIA — 'true'/'false' — when true, saves incoming media locally for debugging.
- META_VERIFY_TOKEN — token used for Meta webhook verification (GET) — choose a secret here.
- META_PHONE_NUMBER_ID — Meta phone number id (used to send messages through Graph API)
- META_ACCESS_TOKEN — Meta / Facebook Graph API access token used to send messages and fetch media.
- PORT — Flask port to run on (default 5000)
- FLASK_DEBUG — 'true'/'false' — run Flask in debug mode when running locally.
- LOG_LEVEL — logging level (e.g., INFO, DEBUG)

Set these in a `.env` file for local development or export them in your environment.

Example `.env` snippet:

```
OPENAI_API_KEY=sk-xxxx
GOOGLE_API_KEY=xxxx
TWILIO_ACCOUNT_SID=ACxxxx
TWILIO_AUTH_TOKEN=xxxx
META_ACCESS_TOKEN=EAA...
META_PHONE_NUMBER_ID=101234567890123
META_VERIFY_TOKEN=stepbot_verify
TWILIO_VALIDATE=true
DEBUG_SAVE_MEDIA=false
PORT=5000
FLASK_DEBUG=false
LOG_LEVEL=INFO
```

## Setup & run (local)

1. Create a virtual environment and install dependencies (example using pip):

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

2. Install ffmpeg (needed to convert voice files to MP3/16k sample rate). On Windows, add ffmpeg to PATH.

3. Create a `.env` file with the environment variables described above.

4. Run the Flask app locally:

```powershell
set FLASK_DEBUG=true
python app.py
```

5. To receive webhooks locally, expose your server with a tunnel (ngrok) or deploy to a cloud provider and configure Twilio/Meta webhooks to point to the appropriate public URL.

## Ingestion: building the vector store (optional)

If you want RAG, create a vector store using `ingestion.py`:

1. Prepare a folder `LEGAL-DATA` with the PDF documents you want embedded.
2. Ensure `GOOGLE_API_KEY` is set (the script uses `GoogleGenerativeAIEmbeddings`) and the `langchain_community` + `faiss-cpu` packages are installed.
3. Run:

```powershell
python ingestion.py
```

This script will:

- Load PDFs using `PyPDFDirectoryLoader`.
- Split content into chunks with `RecursiveCharacterTextSplitter`.
- Embed the chunks using `GoogleGenerativeAIEmbeddings`.
- Build one or more FAISS vector stores in batches and merge them.
- Save the merged FAISS index to `my_vector_store`.

Once `my_vector_store` exists, `app.py` will attempt to load it at startup and wire up a retriever used for RAG.

If you already have a pre-built vector store
------------------------------------------------

If a `my_vector_store` directory already exists in the repository (for example it contains files such as `index.faiss` and `index.pkl`), the application will attempt to load that index at startup and you can skip running `ingestion.py`. Typical workflows:

- Skip ingestion: leave the `my_vector_store` folder in place and start the app; `app.py` will try to load the FAISS index automatically.
- Rebuild from scratch: remove or move the `my_vector_store` folder (or rename it) and then run `python ingestion.py` to create a fresh index.
- Inspect files: the key files you mentioned (`index.faiss`, `index.pkl`) are the main FAISS index plus metadata/pickle; keep them together in `my_vector_store` for successful load.

If you want, I can add a short script to validate the existing `my_vector_store` contents and report whether `app.py` will be able to load it (checks for presence of the expected files). Let me know if you want that validation script added.

## How messages are handled (Twilio + Meta)

Twilio flow (`/whatsapp-webhook`):

- The endpoint optionally validates the Twilio signature (if `TWILIO_VALIDATE` is true and `TWILIO_AUTH_TOKEN` is set).
- It inspects `NumMedia`. If media is present it attempts to download the first media URL and transcribe audio.
- It calls `generate_reply_for_input(from_number, user_input)` and returns a Twilio MessagingResponse with the reply text.

Meta/WhatsApp Cloud flow (`/meta-webhook`):

- On GET, the endpoint responds to the webhook verification challenge (check `hub.verify_token`).
- On POST, it inspects incoming messages via the Graph API payload. It supports text, audio/voice, and interactive button replies.
- When a button reply selects tone (Professional or Casual), the app records a per-user tone preference in memory and acknowledges it.

Conversation state:

- Conversation history is stored in memory in `conversation_memory` (per user phone number). This keeps the last ~20 messages and is reset on process restart. For persistence across restarts, you would wire up a DB or Redis.

## Audio handling and transcription

Key helpers in `app.py`:

- `download_media(url, dest_path, auth)` — A multi-strategy downloader with these strategies in order:
	1. Unauthenticated GET (public CDN links).
 2. GET with Basic Authorization header.
 3. Manual redirect-follow with `auth` applied to each hop.
 4. Twilio `/Content` endpoint fallback if the URL is Twilio-hosted.

- `convert_to_mp3(input_path, output_path)` — Uses the `ffmpeg` binary to normalize audio to a single-channel 16 kHz MP3 for better transcription.
- `transcribe_with_openai(audio_file_path)` — Attempts transcription with OpenAI's `gpt-4o-transcribe` then falls back to `whisper-1`.

If transcription fails, the bot returns a short placeholder message indicating the voice message could not be transcribed.

## RAG / embeddings (optional behavior)

The app attempts to import and wire up LangChain modules, embeddings, and FAISS at startup. If any piece is missing, the app logs the missing parts and continues. RAG components include:

- `GoogleGenerativeAIEmbeddings` to create embeddings (requires `GOOGLE_API_KEY`).
- `FAISS` via `langchain_community.vectorstores` to load a local `my_vector_store` directory.
- Chain helpers to create history-aware retriever and a retrieval chain.

When all are present, the app uses the retrieval chain for better grounded answers. When missing, it falls back to the plain OpenAI chat path.

## Testing

- `test_openai.py` is a tiny script that validates your `OPENAI_API_KEY` and runs a single chat completion to ensure API access.

Run it like:

```powershell
python test_openai.py
```

If it prints an assistant reply, your OpenAI key is working.

## Troubleshooting & tips

- If audio transcription fails:
	- Ensure `ffmpeg` is installed and in PATH.
	- Check environment `OPENAI_API_KEY` is set and valid.
	- Set `LOG_LEVEL=DEBUG` to see download/transcription logs.

- If Twilio media URLs fail to download:
	- Verify `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set. The downloader will attempt authenticated fetches.
	- For Twilio-hosted media, a trailing `/Content` path may be necessary — the downloader handles this.

- If RAG doesn't boot:
	- Confirm `my_vector_store` exists and contains FAISS files.
	- Confirm `faiss-cpu`, `langchain_community`, and `langchain_google_genai` are installed.

- Webhook validation issues:
	- For Twilio signature validation, make sure the URL Twilio calls matches `request.url` (including scheme/path) and `TWILIO_AUTH_TOKEN` is correct.

## Security & privacy notes

- Secrets: Keep `OPENAI_API_KEY`, `TWILIO_AUTH_TOKEN`, and `META_ACCESS_TOKEN` private and store them securely (use environment variables or a secrets manager).
- Audio and chat transcripts: The app may persist debug audio files only if `DEBUG_SAVE_MEDIA=true`. Otherwise, audio is processed in temporary directories and not retained.
- Conversation memory is stored in-process memory (not persisted). If you need to store chat logs, implement explicit storage and inform users of retention.

## Next steps & enhancements

- Persist conversation history in a durable store (Redis or DB) for longer context.
- Add authentication/verification for Meta webhook beyond token (e.g., app secret proof).
- Rate limiting and guardrails for OpenAI calls.
- Add unit tests and CI checks for critical flows.
- Add a small Dockerfile and a deployment guide.

## Quick summary

- `app.py` is the main webhook/assistant server.
- `ingestion.py` builds the FAISS vector store from PDFs in `LEGAL-DATA`.
- Place required secrets in `.env` and run `python app.py` to start the server.

If you want, I can now:

1. Add a sample `.env.example` file with the exact variables used.
2. Add a small `docker-compose` or `Dockerfile` for quick deployment.
3. Implement an optional persistent conversation store (Redis) and an admin endpoint to inspect memory.

Tell me which follow-up you prefer and I'll implement it next.
