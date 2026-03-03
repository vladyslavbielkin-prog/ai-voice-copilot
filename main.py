import os
import json
import base64
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from deepgram import AsyncDeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1.types import ListenV1Results

app = FastAPI()


@app.get("/health")
def health():
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def twilio_ws(websocket: WebSocket):
    await websocket.accept()
    print("🔌 Twilio connected")

    dg_key = os.getenv("DEEPGRAM_API_KEY")
    if not dg_key:
        print("❌ DEEPGRAM_API_KEY not set")
        await websocket.close(code=1011)
        return

    # ✅ deepgram-sdk v6: AsyncDeepgramClient з api_key=
    dg = AsyncDeepgramClient(api_key=dg_key)

    # Буфер + таймер — збираємо речення в один рядок
    speaker_buffer: list[str] = []
    flush_task: asyncio.Task | None = None
    FLUSH_DELAY_SEC = 3.0

    # Черга для передачі транскриптів між потоками
    transcript_queue: asyncio.Queue = asyncio.Queue()

    async def flush_buffer():
        await asyncio.sleep(FLUSH_DELAY_SEC)
        if speaker_buffer:
            print(f"🗣 Спікер: {' '.join(speaker_buffer)}")
            speaker_buffer.clear()

    async def process_transcripts():
        nonlocal flush_task
        while True:
            item = await transcript_queue.get()
            speaker_buffer.append(item["text"])
            if item["speech_final"]:
                if flush_task and not flush_task.done():
                    flush_task.cancel()
                flush_task = asyncio.create_task(flush_buffer())

    # ✅ deepgram-sdk v6: async context manager, всі параметри — рядки
    async with dg.listen.v1.connect(
        model="nova-2",
        language="uk",
        encoding="mulaw",     # ✅ Twilio надсилає mulaw за замовчуванням
        sample_rate="8000",
        channels="1",
        interim_results="true",
        punctuate="true",
        smart_format="true",
    ) as dg_socket:
        print("🧠 Deepgram connected")

        # ✅ Підписуємось на транскрипти через EventType.MESSAGE
        async def on_message(event_type, result, **kwargs):
            if not isinstance(result, ListenV1Results):
                return
            try:
                text = (result.channel.alternatives[0].transcript or "").strip()
                if not text or not result.is_final:
                    return
                transcript_queue.put_nowait({
                    "text": text,
                    "speech_final": result.speech_final or False,
                })
            except Exception as e:
                print(f"⚠️ Transcript parse error: {e}")

        dg_socket.on(EventType.MESSAGE, on_message)

        # Запускаємо listener та processor паралельно
        listener_task = asyncio.create_task(dg_socket.start_listening())
        processor_task = asyncio.create_task(process_transcripts())

        try:
            while True:
                msg = await websocket.receive_text()
                data = json.loads(msg)
                event = data.get("event")

                if event == "start":
                    print("🎬 Stream started")

                elif event == "media":
                    payload_b64 = data.get("media", {}).get("payload")
                    if payload_b64:
                        audio_bytes = base64.b64decode(payload_b64)
                        await dg_socket.send_media(audio_bytes)

                elif event == "stop":
                    print("🔴 Stream stopped")
                    break

        except WebSocketDisconnect:
            print("🔌 Twilio disconnected")

        finally:
            # Флашимо залишок буфера
            if flush_task and not flush_task.done():
                flush_task.cancel()
            if speaker_buffer:
                print(f"🗣 Спікер: {' '.join(speaker_buffer)}")
                speaker_buffer.clear()

            listener_task.cancel()
            processor_task.cancel()
            print("✅ Session closed")
