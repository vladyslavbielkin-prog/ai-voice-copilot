import os
import json
import base64
import asyncio
from contextlib import AsyncExitStack

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse, Dial, Start, Stream

from deepgram import AsyncDeepgramClient
from deepgram.listen.v1.types import ListenV1Results

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
    """Генерує Access Token для Twilio Voice SDK"""
    token = AccessToken(
        TWILIO_ACCOUNT_SID,
        TWILIO_API_KEY,
        TWILIO_API_SECRET,
        identity="sales-agent",
    )
    grant = VoiceGrant(outgoing_application_sid=TWILIO_TWIML_APP_SID)
    token.add_grant(grant)
    return JSONResponse({"token": token.to_jwt()})


@app.post("/twiml/outbound")
async def twiml_outbound(request: Request):
    """TwiML для вихідного дзвінка — запускає стрім і дзвонить клієнту"""
    form = await request.form()
    to_number = form.get("To", "")

    response = VoiceResponse()

    start = Start()
    start.stream(url="wss://ai-voice-copilot.fly.dev/ws", track="both_tracks")
    response.append(start)

    dial = Dial(caller_id=TWILIO_FROM_NUMBER)
    dial.number(to_number)
    response.append(dial)

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


async def broadcast_transcript(text: str):
    """Надсилає транскрипт всім підключеним браузерам"""
    dead = []
    for client in transcript_clients:
        try:
            await client.send_text(json.dumps({"text": text}))
        except Exception:
            dead.append(client)
    for d in dead:
        transcript_clients.remove(d)


@app.websocket("/ws")
async def twilio_ws(websocket: WebSocket):
    """Приймає аудіо від Twilio Media Streams"""
    await websocket.accept()
    print("📞 Дзвінок розпочато", flush=True)

    dg_key = os.getenv("DEEPGRAM_API_KEY")
    if not dg_key:
        print("❌ DEEPGRAM_API_KEY not set", flush=True)
        await websocket.close(code=1011)
        return

    dg = AsyncDeepgramClient(api_key=dg_key)

    speaker_buffer: list[str] = []
    flush_task: asyncio.Task | None = None
    FLUSH_DELAY_SEC = 1.0

    def do_flush():
        if speaker_buffer:
            text = " ".join(speaker_buffer)
            print(f"🗣 Спікер: {text}", flush=True)
            speaker_buffer.clear()
            asyncio.create_task(broadcast_transcript(text))

    async def flush_buffer():
        await asyncio.sleep(FLUSH_DELAY_SEC)
        do_flush()

    async def read_transcripts(dg_socket, label: str):
        nonlocal flush_task
        async for message in dg_socket:
            if not isinstance(message, ListenV1Results):
                continue
            try:
                text = (message.channel.alternatives[0].transcript or "").strip()
                if not text or not message.is_final:
                    continue
                speaker_buffer.append(text)
                if message.speech_final:
                    if flush_task and not flush_task.done():
                        flush_task.cancel()
                    do_flush()
                else:
                    if flush_task and not flush_task.done():
                        flush_task.cancel()
                    flush_task = asyncio.create_task(flush_buffer())
            except Exception as e:
                print(f"⚠️ [{label}] Помилка парсингу: {e}", flush=True)

    dg_params = dict(
        model="nova-2",
        language="uk",
        encoding="mulaw",
        sample_rate="8000",
        channels="1",
        interim_results="true",
        punctuate="true",
        smart_format="true",
    )

    try:
        async with AsyncExitStack() as stack:
            inbound_socket = await stack.enter_async_context(
                dg.listen.v1.connect(**dg_params)
            )
            outbound_socket = await stack.enter_async_context(
                dg.listen.v1.connect(**dg_params)
            )

            print("🧠 Deepgram підключено (x2)", flush=True)

            inbound_task = asyncio.create_task(read_transcripts(inbound_socket, "IN"))
            outbound_task = asyncio.create_task(read_transcripts(outbound_socket, "OUT"))

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
                            audio_bytes = base64.b64decode(payload_b64)
                            if track == "outbound":
                                await outbound_socket.send_media(audio_bytes)
                            else:
                                await inbound_socket.send_media(audio_bytes)

                    elif event == "stop":
                        print("📵 Дзвінок завершено", flush=True)
                        break

            except WebSocketDisconnect:
                print("📵 Дзвінок завершено", flush=True)

            finally:
                inbound_task.cancel()
                outbound_task.cancel()
                if flush_task and not flush_task.done():
                    flush_task.cancel()
                do_flush()
                print("-" * 40, flush=True)

    except Exception as e:
        print(f"❌ Критична помилка: {e}", flush=True)
