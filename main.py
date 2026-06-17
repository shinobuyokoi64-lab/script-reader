import asyncio
import json
import re
import tempfile
import uuid
from pathlib import Path

import edge_tts
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

TEMP_DIR = Path(tempfile.gettempdir()) / "script_reader"
TEMP_DIR.mkdir(exist_ok=True)

VOICES = {
    "nanami": "ja-JP-NanamiNeural",
    "keita": "ja-JP-KeitaNeural",
}

SECTION_RE = re.compile(r"^Section\s+\d+")

# Lines to always exclude
EXCLUDE_RE = re.compile(
    r"^[▶►▷]\s*$"          # ▶ alone on a line (direction follows on next line)
    r"|^[★☆]"              # ★ inline direction
    r"|⏱"                  # timing notation
    r"|^【スライド\d+】"    # slide number header
)


def parse_script(text: str) -> list[dict]:
    """Split text into chapters, filtering stage directions."""
    lines = text.splitlines()
    chapters: list[dict] = []
    current_title: str | None = None
    current_lines: list[str] = []
    skip_next_nonempty = False  # True after a bare ▶ line

    def flush():
        nonlocal current_title, current_lines
        content = "\n".join(l for l in current_lines if l.strip()).strip()
        if content:
            chapters.append({"title": current_title or "台本", "text": content})
        current_lines = []

    for line in lines:
        stripped = line.strip()

        # New chapter
        if SECTION_RE.match(stripped):
            flush()
            # Remove timing suffix like（合計 525秒 / 8分45秒）for cleaner title
            current_title = re.sub(r"（合計.*）$", "", stripped).strip()
            skip_next_nonempty = False
            continue

        # ▶ line (with or without text after): always skip
        # If bare ▶, also skip the NEXT non-empty line (direction follows)
        if re.match(r"^[▶►▷]", stripped):
            if re.match(r"^[▶►▷]\s*$", stripped):
                skip_next_nonempty = True
            continue

        # Skip the direction line that follows ▶
        if skip_next_nonempty and stripped:
            skip_next_nonempty = False
            continue

        if not stripped:
            skip_next_nonempty = False

        # Skip always-excluded patterns
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
    voice_id = VOICES.get(req.voice, VOICES["nanami"])
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
                communicate = edge_tts.Communicate(chapter["text"], voice_id)
                await communicate.save(str(audio_path))
                payload = {
                    "type": "chapter",
                    "index": i,
                    "title": chapter["title"],
                    "preview": chapter["text"][:120].replace("\n", " ") + ("…" if len(chapter["text"]) > 120 else ""),
                    "audio_url": f"/api/audio/{session_id}/{i}",
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
