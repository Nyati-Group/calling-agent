
import os
import re
import json
import uuid
import time
import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Lock
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

load_dotenv()

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("twilio_sarvam_agent")

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "").strip()

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"

SARVAM_STT_MODEL = "saaras:v4"
SARVAM_TTS_MODEL = "bulbul:v3"
SARVAM_CHAT_MODEL = os.getenv(
    "SARVAM_CHAT_MODEL",
    "sarvam-105b-conversations"
).strip()

# Reasoning is ON by default for Sarvam chat models ("medium"), which adds
# hidden chain-of-thought before every reply. None disables it entirely.
# Set SARVAM_REASONING_EFFORT=low|high|max in .env to turn it back on.
SARVAM_REASONING_EFFORT = os.getenv("SARVAM_REASONING_EFFORT", "").strip() or None

# Project details Aditya may talk about. Edit this file to change the project;
# it is small enough to send in full with every request (no search needed).
KNOWLEDGE_BASE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "knowledge_base",
    "sunrise_residency.md"
)

with open(KNOWLEDGE_BASE_PATH, encoding="utf-8") as kb_file:
    KNOWLEDGE_BASE = kb_file.read().strip()

# Keep answers short to reduce generation and playback time.
SYSTEM_PROMPT = """
You are Aditya, a friendly real estate agent from SyncAI Consultancy. You are
on an outbound phone call about Sunrise Residency, a project of 2 BHK and
3 BHK apartments in Whitefield, Bengaluru. You have already introduced
yourself and asked if it is a good time to talk, so do not repeat that.

Conversation plan (follow in order, one question per turn; never mention
the plan or a "script" to the caller):
1. If it is a good time: in one sentence, mention Sunrise Residency offers
   2 BHK and 3 BHK apartments in Whitefield, Bengaluru, then ask if they are
   looking to buy a home.
2. Ask whether they prefer a 2 BHK or a 3 BHK.
3. Ask their approximate budget.
4. Once you know the type and budget, call the find_flats tool and suggest
   the flats it returns, in one short sentence each: flat number, size and
   indicative price, exactly as returned. Never pick flats yourself. Say once
   that prices are indicative. If none is within budget, say so and mention
   the cheapest option it returns.
- Skip any step the caller has already answered, even if they answered
  several at once or in a different language or numerals.
5. Ask whether they are buying to live in or as an investment.
6. Ask when they plan to buy.
7. Offer a site visit or a call from a sales specialist, and ask for a
   convenient day and time. Confirm it back to them as a request that the
   team will confirm, not as a booking.
8. Thank them and close politely.
- Answer the caller's questions at any point using only the knowledge base,
  then continue with the next unanswered step.
- If it is not a good time: ask when to call back, thank them, and end.
- If they are not interested: thank them and end politely without pushing.

Knowledge rules:
- Use ONLY the knowledge base below for project facts. If something is not
  in it, say you do not have that detail right now and offer to have a sales
  specialist follow up.
- Never say a flat is available, sold, booked or reserved.
- Never state or imply RERA registration, approvals, a possession date, the
  developer's name, or an exact address.
- Never calculate a final payable amount, discuss what charges are included,
  or promise a discount.
- Do not give legal, loan, or tax advice.
- If asked whether the project is real, say honestly that these are sample
  project details used for testing.

Speaking style (your words are converted to speech):
- Write every number as plain digits without commas, exactly as in the
  knowledge base, for example "63 lakh rupees", "1 crore 6 lakh rupees",
  "1000 square feet", "flat A 101". Never spell numbers out in words and
  never use the rupee symbol. The speech engine reads digits aloud correctly
  in every language.
- Say "square feet", not "sq ft".
- Always reply in the same language the caller is speaking, written in
  that language's own script (Devanagari for Hindi and Marathi). If the
  caller switches language, switch with them.
- You are male: in Hindi, Marathi and other gendered languages, always use
  masculine forms when referring to yourself (for example "main bol raha
  hoon", never "bol rahi hoon").
- Keep replies concise: normally one or two short sentences.
- Open each reply with a brief acknowledgement of two or three words that
  ends with a full stop, such as "Sure." or "Got it.", then continue. Vary
  it and do not repeat the same one twice in a row.
- Use natural language suitable for speaking over a phone.
- Ask exactly one question per reply, never two or more.
- Do not use markdown, bullet points, emojis, or lengthy explanations.
- If you cannot understand the caller, ask them to repeat.
- Never claim to have completed an action you did not perform.

KNOWLEDGE BASE:
""" + KNOWLEDGE_BASE

GREETING_TEXT = (
    "Hi, my name is Aditya. I am calling from SyncAI Consultancy "
    "about Sunrise Residency in Whitefield, Bengaluru. "
    "Is this a good time to talk?"
)
GREETING_LANGUAGE = "en-IN"
STILL_THERE_TEXT = "Are you still there?"
GOODBYE_TEXT = "I can't hear you, so I will end the call now. Thank you, goodbye."
RETRY_TEXT = "Sorry, I couldn't hear you clearly. Please try again."
ERROR_TEXT = "Sorry, I am having a technical issue. Please try again."

# Twilio's Record timeout covers BOTH the wait for the caller to start
# speaking and pauses mid-sentence. 1 second was too short: the caller had
# no time to answer and the call ended. The caller can press # to finish early.
RECORD_MAX_SECONDS = 15
RECORD_SILENCE_SECONDS = 2

# How many times to ask "Are you still there?" before hanging up.
MAX_SILENT_PROMPTS = 2

# Recent user/assistant messages sent to the chat model.
HISTORY_MESSAGES = 8

# Short timeouts so a slow API fails fast instead of leaving dead air.
DOWNLOAD_TIMEOUT_SECONDS = 10
STT_TIMEOUT_SECONDS = 15
CHAT_TIMEOUT_SECONDS = 15
TTS_TIMEOUT_SECONDS = 15

# Generated audio is kept this long before being evicted from memory.
AUDIO_TTL_SECONDS = 600

# bulbul:v3 voice; set TTS_SPEAKER in .env to pick a voice that suits Aditya.
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "shubh").strip()

# A first sentence longer than this is split at its first comma so the
# caller starts hearing the reply sooner.
FIRST_CHUNK_MAX_CHARS = 50

# Speaking speed; 1.0 is normal, higher is faster. Set TTS_PACE in .env to tune.
TTS_PACE = float(os.getenv("TTS_PACE", "1.2"))

LANGUAGE_NAMES = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "mr-IN": "Marathi",
    "bn-IN": "Bengali",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "gu-IN": "Gujarati",
    "pa-IN": "Punjabi",
    "od-IN": "Odia"
}

SUPPORTED_TTS_LANGUAGES = {
    "en-IN", "hi-IN", "mr-IN", "bn-IN",
    "ta-IN", "te-IN", "kn-IN", "ml-IN",
    "gu-IN", "pa-IN", "od-IN"
}

# ---------------------------------------------------------
# Shared state
# ---------------------------------------------------------

storage_lock = Lock()

conversation_history = {}

# call_id -> consecutive turns where the caller said nothing.
silent_prompts = {}

# call_id -> last language detected from the caller's speech.
call_language = {}

# audio_id -> (created_at, Future[bytes]). TTS runs in the background and the
# /audio endpoint waits on the future, so TwiML can be returned before the
# audio is ready.
audio_store = {}

# (text, language) -> audio_id for fixed phrases that are reused forever.
static_audio = {}

tts_executor = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="tts"
)

# Reused keep-alive connections avoid a new TLS handshake per request.
http_session = requests.Session()
http_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=32)
http_session.mount("https://", http_adapter)

twilio_client = None

if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN
    )


# ---------------------------------------------------------
# Startup: pre-generate fixed phrases and warm connections
# ---------------------------------------------------------

@asynccontextmanager
async def lifespan(app):
    if SARVAM_API_KEY:
        # Runs in the background; the greeting is ready before the first call.
        static_audio_id(GREETING_TEXT, GREETING_LANGUAGE)
        static_audio_id(RETRY_TEXT, "en-IN")
        static_audio_id(ERROR_TEXT, "en-IN")
        static_audio_id(STILL_THERE_TEXT, "en-IN")
        static_audio_id(GOODBYE_TEXT, "en-IN")

    yield

    tts_executor.shutdown(wait=False)


app = FastAPI(
    title="Twilio Sarvam AI Calling Agent",
    lifespan=lifespan
)


# ---------------------------------------------------------
# Models
# ---------------------------------------------------------

class MakeCallRequest(BaseModel):
    to_number: str


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def log_timing(stage, started_at):
    elapsed = time.perf_counter() - started_at
    logger.info("[LATENCY] %s: %.2f seconds", stage, elapsed)


def require_sarvam():
    if not SARVAM_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="SARVAM_API_KEY is missing from .env"
        )


def require_twilio():
    if not twilio_client:
        raise HTTPException(
            status_code=500,
            detail="Twilio credentials are missing from .env"
        )

    if not TWILIO_PHONE_NUMBER:
        raise HTTPException(
            status_code=500,
            detail="TWILIO_PHONE_NUMBER is missing from .env"
        )


def public_url(path):
    if not PUBLIC_BASE_URL:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL is missing from .env"
        )

    parsed = urlparse(PUBLIC_BASE_URL)

    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL must be an HTTPS URL"
        )

    return PUBLIC_BASE_URL + path


def new_conversation():
    # Include the spoken greeting so the model knows what the caller is
    # answering (e.g. "yes" to "Is this a good time to talk?").
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        },
        {
            "role": "assistant",
            "content": GREETING_TEXT
        }
    ]


def get_conversation(call_id):
    with storage_lock:
        if call_id not in conversation_history:
            conversation_history[call_id] = new_conversation()

        return list(conversation_history[call_id])


def clear_conversation(call_id):
    with storage_lock:
        conversation_history.pop(call_id, None)


def audio_url(audio_id):
    return public_url("/audio/" + audio_id)


def split_sentences(text):
    # Split after sentence-ending punctuation (including the Devanagari danda)
    # so each sentence is synthesised in parallel and the first one plays
    # as soon as it is ready.
    parts = re.split(r"(?<=[.!?।])\s+", text.strip())
    sentences = []

    for part in parts:
        part = part.strip()

        if not part:
            continue

        # Merge very short fragments into the previous sentence.
        if sentences and len(part) < 12:
            sentences[-1] += " " + part
        else:
            sentences.append(part)

    # TTS time grows with text length and the caller hears nothing until the
    # first clip is ready, so speak a long first sentence's opening clause
    # on its own.
    if sentences and len(sentences[0]) > FIRST_CHUNK_MAX_CHARS:
        head, comma, tail = sentences[0].partition(",")
        if comma and 12 <= len(head) <= FIRST_CHUNK_MAX_CHARS and tail.strip():
            sentences[0:1] = [head + comma, tail.strip()]

    return sentences or [text.strip()]


# ---------------------------------------------------------
# Sarvam speech-to-text
# ---------------------------------------------------------

def transcribe_audio(audio_bytes):
    require_sarvam()
    started_at = time.perf_counter()

    headers = {
        "api-subscription-key": SARVAM_API_KEY
    }

    files = {
        "file": (
            "recording.wav",
            audio_bytes,
            "audio/wav"
        )
    }

    data = {
        "model": SARVAM_STT_MODEL,
        "language_code": "unknown"
    }

    try:
        result = http_session.post(
            SARVAM_STT_URL,
            headers=headers,
            files=files,
            data=data,
            timeout=STT_TIMEOUT_SECONDS
        )

        result.raise_for_status()
        body = result.json()

    except requests.RequestException:
        logger.exception("Sarvam STT request failed")
        raise

    finally:
        log_timing("Sarvam STT request", started_at)

    transcript = (
        body.get("transcript")
        or body.get("text")
        or ""
    ).strip()

    # None when Sarvam cannot tell; the caller falls back to the call's
    # previous language instead of assuming English.
    language_code = body.get("language_code")

    logger.info(
        "STT language=%s transcript_length=%d",
        language_code,
        len(transcript)
    )

    return transcript, language_code


# ---------------------------------------------------------
# Sarvam conversational AI
# ---------------------------------------------------------

def parse_lakh(price_text):
    # "63 lakh" -> 63, "1 crore 5 lakh" -> 105
    match = re.match(r"(?:(\d+) crore ?)?(?:(\d+) lakh)?", price_text.strip())
    return int(match.group(1) or 0) * 100 + int(match.group(2) or 0)


# Inventory parsed from the knowledge base so there is a single source of
# truth. Budget matching is done here in code because the model often mixes
# up rows or misjudges which prices are within a budget.
FLATS = [
    {
        "flat": flat,
        "bhk": int(bhk),
        "floor": int(floor),
        "area": int(area),
        "price_text": price,
        "price_lakh": parse_lakh(price)
    }
    for flat, bhk, floor, area, price in re.findall(
        r"Flat (A-\d+): (\d) BHK, floor (\d+), (\d+) sq ft, "
        r"indicative price (.+?) rupees",
        KNOWLEDGE_BASE
    )
]


def describe_flat(flat):
    return (
        "Flat {flat}: {bhk} BHK, floor {floor}, {area} square feet, "
        "indicative price {price_text} rupees"
    ).format(**flat)


def find_flats(bhk, max_budget_lakh, limit=2):
    of_type = sorted(
        (flat for flat in FLATS if flat["bhk"] == int(bhk)),
        key=lambda flat: flat["price_lakh"]
    )

    if not of_type:
        return "There are no {} BHK flats in Sunrise Residency.".format(bhk)

    # Closest to the budget first, without going over it.
    within = [
        flat for flat in of_type
        if flat["price_lakh"] <= float(max_budget_lakh)
    ][::-1][:limit]

    if within:
        return (
            "Flats within the budget, closest to the budget first. "
            "Mention these exactly:\n"
            + "\n".join(describe_flat(flat) for flat in within)
        )

    return (
        "No {} BHK flat is within the budget. "
        "The cheapest {} BHK is:\n".format(bhk, bhk)
        + describe_flat(of_type[0])
    )


CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_flats",
            "description": (
                "Find Sunrise Residency flats of a given type within the "
                "caller's budget. Always call this before suggesting flats."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bhk": {
                        "type": "integer",
                        "enum": [2, 3],
                        "description": "Number of bedrooms the caller wants."
                    },
                    "max_budget_lakh": {
                        "type": "number",
                        "description": (
                            "Caller's maximum budget in lakh rupees. "
                            "1 crore = 100 lakh."
                        )
                    }
                },
                "required": ["bhk", "max_budget_lakh"]
            }
        }
    }
]


def run_tool(tool_call):
    function = tool_call.get("function") or {}

    try:
        arguments = json.loads(function.get("arguments") or "{}")

        if function.get("name") == "find_flats":
            result = find_flats(
                arguments["bhk"],
                arguments["max_budget_lakh"]
            )
        else:
            result = "Unknown tool."

    except Exception as exc:
        result = "Tool error: " + str(exc)

    logger.info(
        "Tool %s(%s) -> %s",
        function.get("name"),
        function.get("arguments"),
        result.replace("\n", " | ")
    )

    return result


def chat_completion(messages):
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "model": SARVAM_CHAT_MODEL,
        "messages": messages,
        "tools": CHAT_TOOLS,
        "temperature": 0.2,
        "max_tokens": 200,
        # Must be sent explicitly; omitting it means "medium" reasoning.
        "reasoning_effort": SARVAM_REASONING_EFFORT
    }

    started_at = time.perf_counter()

    try:
        result = http_session.post(
            SARVAM_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=CHAT_TIMEOUT_SECONDS
        )

        result.raise_for_status()
        body = result.json()

    except requests.RequestException:
        logger.exception("Sarvam chat request failed")
        raise

    finally:
        log_timing("Sarvam chat request", started_at)

    choices = body.get("choices", [])

    if not choices:
        raise RuntimeError(
            "Sarvam chat returned no response choices"
        )

    return choices[0].get("message") or {}


def clean_for_speech(text):
    # Strip markdown and list markers the model sometimes adds; they would
    # be read aloud or break sentence splitting.
    text = re.sub(r"[*#_`]", "", text)
    text = re.sub(r"(?m)^\s*(?:\d+[.)]|[-\u2022])\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def get_sarvam_response(call_id, user_text, language_code=None):
    require_sarvam()

    messages = get_conversation(call_id)

    messages.append({
        "role": "user",
        "content": user_text
    })

    # Retain recent turns to limit prompt size.
    system_message = messages[0]
    recent_messages = messages[1:][-HISTORY_MESSAGES:]
    messages_to_send = [system_message] + recent_messages

    # A per-turn instruction is far more reliable than the system prompt
    # alone; it is not stored in the history.
    language_name = LANGUAGE_NAMES.get(language_code)

    if language_name:
        messages_to_send.append({
            "role": "system",
            "content": (
                "The caller is speaking " + language_name + ". "
                "Reply only in " + language_name + ", using its native script."
            )
        })

    reply = ""

    # At most one tool round per turn keeps latency to two model calls.
    for _ in range(2):
        message = chat_completion(messages_to_send)
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            reply = clean_for_speech(message.get("content") or "")
            break

        messages_to_send.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls
        })

        for tool_call in tool_calls:
            messages_to_send.append({
                "role": "tool",
                "tool_call_id": tool_call.get("id"),
                "content": run_tool(tool_call)
            })

    if not reply:
        raise RuntimeError(
            "Sarvam chat returned an empty response"
        )

    with storage_lock:
        if call_id not in conversation_history:
            conversation_history[call_id] = new_conversation()

        conversation_history[call_id].append({
            "role": "user",
            "content": user_text
        })

        conversation_history[call_id].append({
            "role": "assistant",
            "content": reply
        })

    return reply


# ---------------------------------------------------------
# Sarvam text-to-speech
# ---------------------------------------------------------

def generate_speech(text, language_code="en-IN"):
    require_sarvam()

    if language_code not in SUPPORTED_TTS_LANGUAGES:
        language_code = "en-IN"

    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "text": text[:2000],
        "target_language_code": language_code,
        "model": SARVAM_TTS_MODEL,
        "speaker": TTS_SPEAKER,
        "pace": TTS_PACE,
        "speech_sample_rate": 8000,
        "output_audio_codec": "wav"
    }

    started_at = time.perf_counter()

    try:
        result = http_session.post(
            SARVAM_TTS_URL,
            headers=headers,
            json=payload,
            timeout=TTS_TIMEOUT_SECONDS
        )

        result.raise_for_status()
        body = result.json()

    except requests.RequestException:
        logger.exception("Sarvam TTS request failed")
        raise

    finally:
        log_timing("Sarvam TTS request", started_at)

    audios = body.get("audios", [])

    if not audios:
        raise RuntimeError(
            "Sarvam TTS returned no audio"
        )

    return base64.b64decode(audios[0])


def prune_audio_store():
    cutoff = time.time() - AUDIO_TTL_SECONDS

    with storage_lock:
        keep = set(static_audio.values())
        expired = [
            audio_id
            for audio_id, (created_at, _) in audio_store.items()
            if created_at < cutoff and audio_id not in keep
        ]

        for audio_id in expired:
            audio_store.pop(audio_id, None)


def schedule_speech(text, language_code="en-IN"):
    """Start TTS in the background and return an audio id immediately."""
    prune_audio_store()

    audio_id = str(uuid.uuid4())
    future = tts_executor.submit(generate_speech, text, language_code)

    with storage_lock:
        audio_store[audio_id] = (time.time(), future)

    return audio_id


def static_audio_id(text, language_code="en-IN"):
    """Audio id for a fixed phrase, generated once and reused for every call."""
    key = (text, language_code)

    with storage_lock:
        audio_id = static_audio.get(key)
        entry = audio_store.get(audio_id) if audio_id else None

    if entry:
        future = entry[1]

        # Retry a phrase whose earlier generation failed.
        if not (future.done() and future.exception() is not None):
            return audio_id

    audio_id = schedule_speech(text, language_code)

    with storage_lock:
        static_audio[key] = audio_id

    return audio_id


def static_audio_ready(text, language_code="en-IN"):
    with storage_lock:
        audio_id = static_audio.get((text, language_code))
        entry = audio_store.get(audio_id) if audio_id else None

    if not entry:
        return False

    future = entry[1]
    return future.done() and future.exception() is None


# ---------------------------------------------------------
# Recording configuration
# ---------------------------------------------------------

def add_recording(response):
    response.record(
        action=public_url("/process-recording"),
        method="POST",
        max_length=RECORD_MAX_SECONDS,
        finish_on_key="#",
        play_beep=False,
        timeout=RECORD_SILENCE_SECONDS
    )

    # If the caller stays silent, Twilio skips the action URL and runs the
    # next verb; without this the call would simply hang up.
    response.redirect(public_url("/no-answer"), method="POST")


def play_static(response, text, language_code="en-IN"):
    if static_audio_ready(text, language_code):
        response.play(audio_url(static_audio_id(text, language_code)))
    else:
        response.say(text, language=language_code)


def add_error_reply(response):
    play_static(response, ERROR_TEXT)
    add_recording(response)


def twiml(response):
    return Response(
        content=str(response),
        media_type="application/xml"
    )


# ---------------------------------------------------------
# Health
# ---------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Twilio Sarvam AI Calling Agent",
        "sarvam_configured": bool(SARVAM_API_KEY),
        "twilio_configured": bool(twilio_client),
        "public_url_configured": bool(PUBLIC_BASE_URL),
        "chat_model": SARVAM_CHAT_MODEL,
        "reasoning_effort": SARVAM_REASONING_EFFORT
    }


# ---------------------------------------------------------
# Serve generated audio
# ---------------------------------------------------------

@app.get("/audio/{audio_id}")
def serve_audio(audio_id: str):
    # Sync endpoint: runs in a worker thread, so waiting here does not block
    # the event loop.
    with storage_lock:
        entry = audio_store.get(audio_id)

    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="Audio not found; the server may have restarted"
        )

    started_at = time.perf_counter()

    try:
        content = entry[1].result(timeout=TTS_TIMEOUT_SECONDS + 5)

    except Exception:
        logger.exception("Audio generation failed for %s", audio_id)

        if static_audio_ready(ERROR_TEXT):
            with storage_lock:
                error_entry = audio_store[static_audio[(ERROR_TEXT, "en-IN")]]
            content = error_entry[1].result()
        else:
            raise HTTPException(
                status_code=502,
                detail="Audio generation failed"
            )

    log_timing("Twilio waited for audio", started_at)

    return Response(
        content=content,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"}
    )


# ---------------------------------------------------------
# Make outbound call
# ---------------------------------------------------------

@app.post("/make-call")
def make_call(request: MakeCallRequest):
    require_twilio()

    try:
        call = twilio_client.calls.create(
            to=request.to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=public_url("/voice"),
            method="POST"
        )

        logger.info(
            "Outbound call created: call_sid=%s",
            call.sid
        )

        return {
            "success": True,
            "call_sid": call.sid,
            "status": call.status,
            "to": request.to_number
        }

    except Exception as exc:
        logger.exception("Failed to create outbound call")

        raise HTTPException(
            status_code=500,
            detail="Could not create Twilio call: " + str(exc)
        )


# ---------------------------------------------------------
# Call status and transcript (polled by the web UI)
# ---------------------------------------------------------

@app.get("/calls/{call_sid}")
def call_status(call_sid: str):
    require_twilio()

    try:
        call = twilio_client.calls(call_sid).fetch()
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail="Could not fetch call: " + str(exc)
        )

    with storage_lock:
        history = list(conversation_history.get(call_sid, []))

    return {
        "sid": call.sid,
        "to": call.to,
        "status": call.status,
        "duration": call.duration,
        "transcript": [
            {"role": message["role"], "content": message["content"]}
            for message in history
            if message["role"] in ("user", "assistant")
        ]
    }


# ---------------------------------------------------------
# Call answered
# ---------------------------------------------------------

@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    call_id = str(form.get("CallSid", "unknown-call"))

    with storage_lock:
        conversation_history[call_id] = new_conversation()
        silent_prompts.pop(call_id, None)
        call_language.pop(call_id, None)

    response = VoiceResponse()

    if SARVAM_API_KEY:
        # Pre-generated at startup, so this is served instantly.
        response.play(
            audio_url(static_audio_id(GREETING_TEXT, GREETING_LANGUAGE))
        )
    else:
        response.say(
            "Hello! How can I help you?",
            language="en-IN"
        )

    add_recording(response)

    return twiml(response)


# ---------------------------------------------------------
# Caller stayed silent
# ---------------------------------------------------------

@app.post("/no-answer")
async def no_answer(request: Request):
    form = await request.form()
    call_id = str(form.get("CallSid", "unknown-call"))

    with storage_lock:
        count = silent_prompts.get(call_id, 0) + 1
        silent_prompts[call_id] = count

    logger.info("No answer from %s (prompt %d)", call_id, count)

    response = VoiceResponse()

    if count > MAX_SILENT_PROMPTS:
        play_static(response, GOODBYE_TEXT)
        response.hangup()

        with storage_lock:
            silent_prompts.pop(call_id, None)
        clear_conversation(call_id)
    else:
        play_static(response, STILL_THERE_TEXT)
        add_recording(response)

    return twiml(response)


# ---------------------------------------------------------
# Process each caller turn
# ---------------------------------------------------------

def download_recording(recording_url):
    started_at = time.perf_counter()

    recording_url = str(recording_url).rstrip("/")
    if not recording_url.endswith(".wav"):
        recording_url += ".wav"

    # Twilio occasionally returns 404 for a moment right after the recording
    # callback; retry quickly instead of failing the turn.
    for attempt in range(3):
        result = http_session.get(
            recording_url,
            auth=(
                TWILIO_ACCOUNT_SID,
                TWILIO_AUTH_TOKEN
            ),
            timeout=DOWNLOAD_TIMEOUT_SECONDS
        )

        if result.status_code != 404 or attempt == 2:
            break

        time.sleep(0.25)

    result.raise_for_status()
    log_timing("Twilio recording download", started_at)

    if not result.content:
        raise RuntimeError(
            "Twilio returned an empty recording"
        )

    return result.content


def handle_turn(call_id, recording_url):
    """Blocking pipeline for one caller turn; runs in a worker thread."""
    overall_start = time.perf_counter()
    response = VoiceResponse()

    try:
        require_sarvam()

        audio_bytes = download_recording(recording_url)

        # Step 1: Transcribe caller.
        transcript, detected_language = transcribe_audio(audio_bytes)

        # Short replies like "ok" may not be detected reliably; fall back
        # to the language the caller used earlier in the call.
        with storage_lock:
            if detected_language in LANGUAGE_NAMES:
                call_language[call_id] = detected_language

            language_code = call_language.get(call_id, "en-IN")

        if not transcript:
            response.play(audio_url(static_audio_id(RETRY_TEXT)))
            add_recording(response)
            return response

        # Step 2: Generate AI response.
        assistant_text = get_sarvam_response(
            call_id,
            transcript,
            language_code
        )

        logger.info(
            "Assistant reply for %s: %s",
            call_id,
            assistant_text[:300]
        )

        # Step 3: Start TTS for every sentence in parallel and return the
        # TwiML right away. Twilio's audio fetch waits for each sentence,
        # so later sentences finish generating while earlier ones play.
        for sentence in split_sentences(assistant_text):
            response.play(
                audio_url(schedule_speech(sentence, language_code))
            )

        add_recording(response)

    except Exception:
        logger.exception(
            "Turn processing failed for call %s",
            call_id
        )

        response = VoiceResponse()
        add_error_reply(response)

    finally:
        log_timing(
            "Recording-to-TwiML processing",
            overall_start
        )

    return response


@app.post("/process-recording")
async def process_recording(request: Request):
    form = await request.form()

    call_id = str(form.get("CallSid", "unknown-call"))
    recording_url = form.get("RecordingUrl")
    recording_duration = form.get("RecordingDuration", "unknown")

    logger.info(
        "Recording received: call_sid=%s duration=%s",
        call_id,
        recording_duration
    )

    with storage_lock:
        silent_prompts.pop(call_id, None)

    if not recording_url:
        response = VoiceResponse()
        response.say(
            "Sorry, I did not receive the recording. Please try again.",
            language="en-IN"
        )
        add_recording(response)
        return twiml(response)

    # Blocking HTTP calls run in a thread so other calls and Twilio's
    # audio fetches are not stalled.
    response = await run_in_threadpool(
        handle_turn,
        call_id,
        recording_url
    )

    return twiml(response)


# ---------------------------------------------------------
# Test conversational memory without a phone call
# ---------------------------------------------------------

@app.get("/test-memory")
def test_memory():
    test_call_id = "test-" + str(uuid.uuid4())

    try:
        first = get_sarvam_response(
            test_call_id,
            "My name is Anvita."
        )

        second = get_sarvam_response(
            test_call_id,
            "What is my name?"
        )

        return {
            "success": True,
            "first_reply": first,
            "second_reply": second
        }

    except Exception as exc:
        logger.exception("Sarvam memory test failed")

        raise HTTPException(
            status_code=502,
            detail="Sarvam chat test failed: " + str(exc)
        )

    finally:
        clear_conversation(test_call_id)


# ---------------------------------------------------------
# Web UI (build it with: cd frontend && npm run build)
# ---------------------------------------------------------

# Mounted last so the API routes above take precedence.
FRONTEND_DIST = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "frontend",
    "dist"
)

if os.path.isdir(FRONTEND_DIST):
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIST, html=True),
        name="frontend"
    )
