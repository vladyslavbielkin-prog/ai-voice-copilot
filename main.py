import os
import json
import base64
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from deepgram import AsyncDeepgramClient
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

    async def flush_buffer():
        await asyncio.sleep(FLUSH_DELAY_SEC)
        if speaker_buffer:
            print(f"🗣 Спікер: {' '.join(speaker_buffer)}")
            speaker_buffer.clear()

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

        # ✅ Читаємо транскрипти напряму через async for — надійніше ніж on()
        async def read_transcripts():
            nonlocal flush_task
            async for message in dg_socket:
                if not isinstance(message, ListenV1Results):
                    continue
                try:
                    text = (message.channel.alternatives[0].transcript or "").strip()
                    print(f"[DG RAW] is_final={message.is_final} speech_final={message.speech_final} text={repr(text)}")
                    if not text or not message.is_final:
                        continue
                    speaker_buffer.append(text)
                    if message.speech_final:
                        if flush_task and not flush_task.done():
                            flush_task.cancel()
                        flush_task = asyncio.create_task(flush_buffer())
                except Exception as e:
                    print(f"⚠️ Transcript parse error: {e}")

        reader_task = asyncio.create_task(read_transcripts())

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
            reader_task.cancel()
            if flush_task and not flush_task.done():
                flush_task.cancel()
            if speaker_buffer:
                print(f"🗣 Спікер: {' '.join(speaker_buffer)}")
                speaker_buffer.clear()
            print("✅ Session closed")
