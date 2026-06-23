import json
import re
import tempfile
import uuid
from pathlib import Path

import edge_tts
from mutagen.mp3 import MP3
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

TEMP_DIR = Path(tempfile.gettempdir()) / "script_reader"
TEMP_DIR.mkdir(exist_ok=True)

# edge-ttsの日本語Neural音声は Nanami / Keita の2つのみ。
# pitch / rate を変えてナレーターのバリエーションを増やす。
# (voice_id, rate, pitch)
VOICES = {
    "nanami":        ("ja-JP-NanamiNeural", "+0%",  "+0Hz"),
    "nanami_soft":   ("ja-JP-NanamiNeural", "-8%",  "-12Hz"),
    "nanami_bright": ("ja-JP-NanamiNeural", "+8%",  "+18Hz"),
    "keita":         ("ja-JP-KeitaNeural",  "+0%",  "+0Hz"),
    "keita_low":     ("ja-JP-KeitaNeural",  "-6%",  "-18Hz"),
    "keita_bright":  ("ja-JP-KeitaNeural",  "+8%",  "+12Hz"),
}

SAMPLE_TEXT = "こんにちは。これはナレーターの試聴サンプルです。本日もよろしくお願いいたします。"

SECTION_RE = re.compile(r"^Section\s+\d+")
EXCLUDE_RE = re.compile(
    r"^[▶►▷]"
    r"|^[★☆]"
    r"|⏱"
    r"|^【スライド\d+】"
)


def parse_script(text: str) -> list[dict]:
    lines = text.splitlines()
    chapters: list[dict] = []
    current_title: str | None = None
    current_lines: list[str] = []
    skip_next_nonempty = False

    def flush():
        nonlocal current_title, current_lines
        content = "\n".join(l for l in current_lines if l.strip()).strip()
        if content:
            chapters.append({"title": current_title or "台本", "text": content})
        current_lines = []

    for line in lines:
        stripped = line.strip()
        if SECTION_RE.match(stripped):
            flush()
            current_title = re.sub(r"（合計.*）$", "", stripped).strip()
            skip_next_nonempty = False
            continue
        if re.match(r"^[▶►▷]", stripped):
            if re.match(r"^[▶►▷]\s*$", stripped):
                skip_next_nonempty = True
            continue
        if skip_next_nonempty and stripped:
            skip_next_nonempty = False
            continue
        if not stripped:
            skip_next_nonempty = False
        if stripped and EXCLUDE_RE.search(stripped):
            continue
        current_lines.append(line)

    flush()
    return chapters


class ConvertRequest(BaseModel):
    text: str
    voice: str = "nanami"


@app.post("/api/convert")
async def convert(req: ConvertRequest):
    voice_id, rate, pitch = VOICES.get(req.voice, VOICES["nanami"])
    chapters = parse_script(req.text)
    if not chapters:
        raise HTTPException(status_code=400, detail="読み上げるテキストが見つかりませんでした")

    session_id = str(uuid.uuid4())
    session_dir = TEMP_DIR / session_id
    session_dir.mkdir(exist_ok=True)

    async def generate():
        yield f"data: {json.dumps({'type': 'start', 'total': len(chapters), 'session_id': session_id})}\n\n"
        for i, chapter in enumerate(chapters):
            try:
                audio_path = session_dir / f"chapter_{i}.mp3"
                communicate = edge_tts.Communicate(
                    chapter["text"], voice_id, rate=rate, pitch=pitch
                )
                await communicate.save(str(audio_path))
                duration = round(MP3(str(audio_path)).info.length)
                payload = {
                    "type": "chapter",
                    "index": i,
                    "title": chapter["title"],
                    "text": chapter["text"],
                    "audio_url": f"/api/audio/{session_id}/{i}",
                    "duration": duration,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                return
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/audio/{session_id}/{chapter_index}")
async def get_audio(session_id: str, chapter_index: int):
    if not re.match(r"^[a-f0-9\-]+$", session_id):
        raise HTTPException(status_code=400, detail="不正なセッションID")
    audio_path = TEMP_DIR / session_id / f"chapter_{chapter_index}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="音声ファイルが見つかりません")
    return FileResponse(str(audio_path), media_type="audio/mpeg")


@app.get("/api/preview/{voice}")
async def preview(voice: str):
    cfg = VOICES.get(voice)
    if not cfg:
        raise HTTPException(status_code=404, detail="音声が見つかりません")
    voice_id, rate, pitch = cfg
    sample_path = TEMP_DIR / f"preview_{voice}.mp3"
    if not sample_path.exists():
        communicate = edge_tts.Communicate(
            SAMPLE_TEXT, voice_id, rate=rate, pitch=pitch
        )
        await communicate.save(str(sample_path))
    return FileResponse(str(sample_path), media_type="audio/mpeg")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
