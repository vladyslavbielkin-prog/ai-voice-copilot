import os
import json
import base64
import asyncio
import audioop

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse, Dial, Start, Stream

from google.cloud.speech_v2 import SpeechAsyncClient
from google.cloud.speech_v2.types import cloud_speech
from google.oauth2 import service_account as gsa

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_API_KEY = os.getenv("TWILIO_API_KEY")
TWILIO_API_SECRET = os.getenv("TWILIO_API_SECRET")
TWILIO_TWIML_APP_SID = os.getenv("TWILIO_TWIML_APP_SID")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")

# Активні WebSocket клієнти для realtime транскриптів у браузер
transcript_clients: list[WebSocket] = []


@app.get("/health")
def health():
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return HTMLResponse(content=f.read())


@app.get("/token")
def get_token():
    """Генерує Access Token для Twilio Voice SDK v2"""
    token = AccessToken(
        TWILIO_ACCOUNT_SID,
        TWILIO_API_KEY,
        TWILIO_API_SECRET,
        identity="sales-agent",
        ttl=3600,
    )
    grant = VoiceGrant(
        outgoing_application_sid=TWILIO_TWIML_APP_SID,
        incoming_allow=True,
    )
    token.add_grant(grant)
    return JSONResponse({"token": token.to_jwt()})


@app.post("/twiml/outbound")
async def twiml_outbound(request: Request):
    """TwiML для вихідного дзвінка — запускає стрім і дзвонить клієнту"""
    form = await request.form()
    to_number = form.get("To") or form.get("to") or ""
    print(f"📲 /twiml/outbound → To={to_number!r}, всі поля: {dict(form)}", flush=True)

    response = VoiceResponse()

    start = Start()
    start.stream(url="wss://ai-voice-copilot.fly.dev/ws", track="both_tracks")
    response.append(start)

    if to_number:
        dial = Dial(caller_id=TWILIO_FROM_NUMBER)
        dial.number(to_number)
        response.append(dial)
    else:
        response.say("Номер клієнта не вказано.", language="uk-UA")

    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/transcript")
async def transcript_ws(websocket: WebSocket):
    """WebSocket для передачі транскриптів у браузер sales-агента"""
    await websocket.accept()
    transcript_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        transcript_clients.remove(websocket)


async def broadcast_transcript(text: str, speaker: str = "unknown", interim: bool = False):
    """Надсилає транскрипт всім підключеним браузерам"""
    dead = []
    for client in transcript_clients:
        try:
            await client.send_text(json.dumps({"text": text, "speaker": speaker, "interim": interim}))
        except Exception:
            dead.append(client)
    for d in dead:
        transcript_clients.remove(d)


@app.websocket("/ws")
async def twilio_ws(websocket: WebSocket):
    """Приймає аудіо від Twilio Media Streams і транскрибує через Google STT v2"""
    await websocket.accept()
    print("📞 Дзвінок розпочато", flush=True)

    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    project_id = os.getenv("GOOGLE_PROJECT_ID")
    if not creds_json or not project_id:
        print("❌ GOOGLE_CREDENTIALS_JSON або GOOGLE_PROJECT_ID не задано", flush=True)
        await websocket.close(code=1011)
        return

    credentials = gsa.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )

    # Черги аудіо для кожного спікера
    inbound_queue: asyncio.Queue = asyncio.Queue()
    outbound_queue: asyncio.Queue = asyncio.Queue()

    # Стан конвертатора частоти дискретизації (mulaw 8kHz → linear16 16kHz)
    ratecv_states: dict[str, object] = {"inbound": None, "outbound": None}

    async def stream_to_google(audio_queue: asyncio.Queue, speaker: str):
        """Стрімить аудіо в Google STT v2 і пушить транскрипти в браузер"""
        client = SpeechAsyncClient(credentials=credentials)
        recognizer = f"projects/{project_id}/locations/global/recognizers/_"

        config = cloud_speech.RecognitionConfig(
            explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
                encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,
                audio_channel_count=1,
            ),
            language_codes=["uk-UA"],
            model="chirp_2",
            features=cloud_speech.RecognitionFeatures(
                enable_automatic_punctuation=True,
            ),
        )
        streaming_config = cloud_speech.StreamingRecognitionConfig(
            config=config,
            streaming_features=cloud_speech.StreamingRecognitionFeatures(
                interim_results=True,
            ),
        )

        async def audio_gen():
            yield cloud_speech.StreamingRecognizeRequest(
                recognizer=recognizer,
                streaming_config=streaming_config,
            )
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    return
                yield cloud_speech.StreamingRecognizeRequest(audio=chunk)

        try:
            print(f"🧠 Google STT v2 підключено [{speaker}]", flush=True)
            async for response in await client.streaming_recognize(requests=audio_gen()):
                for result in response.results:
                    if not result.alternatives:
                        continue
                    text = result.alternatives[0].transcript.strip()
                    if not text:
                        continue
                    is_final = result.is_final
                    if is_final:
                        label = "🟢 Sales" if speaker == "sales" else "🔵 Client"
                        print(f"{label}: {text}", flush=True)
                    asyncio.create_task(
                        broadcast_transcript(text, speaker, interim=not is_final)
                    )
        except Exception as e:
            print(f"⚠️ Google STT [{speaker}] помилка: {e}", flush=True)

    inbound_task = asyncio.create_task(stream_to_google(inbound_queue, "client"))
    outbound_task = asyncio.create_task(stream_to_google(outbound_queue, "sales"))

    try:
        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)
            event = data.get("event")

            if event == "media":
                media = data.get("media", {})
                payload_b64 = media.get("payload")
                track = media.get("track", "inbound")
                if payload_b64:
                    # mulaw 8kHz → linear16 8kHz → linear16 16kHz
                    mulaw_bytes = base64.b64decode(payload_b64)
                    lin8 = audioop.ulaw2lin(mulaw_bytes, 2)
                    lin16, ratecv_states[track] = audioop.ratecv(
                        lin8, 2, 1, 8000, 16000, ratecv_states[track]
                    )
                    if track == "outbound":
                        await outbound_queue.put(lin16)
                    else:
                        await inbound_queue.put(lin16)

            elif event == "stop":
                print("📵 Дзвінок завершено", flush=True)
                break

    except WebSocketDisconnect:
        print("📵 Дзвінок завершено", flush=True)

    finally:
        await inbound_queue.put(None)
        await outbound_queue.put(None)
        inbound_task.cancel()
        outbound_task.cancel()
        print("-" * 40, flush=True)
