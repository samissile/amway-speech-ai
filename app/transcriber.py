# app/transcriber.py - BULLETPROOF VERSION with Progress Tracking and Updated Summarization
import io
import json
from pydub import AudioSegment
from pydub.exceptions import CouldntDecodeError
import requests
from typing import Tuple, List
from tqdm import tqdm  # For progress (optional)
from app.db import update_task  # Ensure this import matches
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# === CONFIG ===
API_ENDPOINT = "https://api.laozhang.ai/v1/audio/transcriptions"
API_AUTH_HEADER = os.getenv("API_AUTH_HEADER")
if not API_AUTH_HEADER:
    raise ValueError("❌ API_AUTH_HEADER not set in .env")
API_MODEL = "gpt-4o-transcribe"
SEGMENT_MS = 5 * 60 * 1000  # 5 minutes

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not set in .env")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

async def safe_load_audio(file_bytes: bytes, filename: str) -> AudioSegment:
    """Load audio with proper format detection and validation."""
    file_ext = filename.lower().split('.')[-1]
    
    # Try the actual file extension FIRST
    formats_to_try = [file_ext] + ['mp3', 'm4a', 'wav', 'aac']
    formats_to_try = list(dict.fromkeys(formats_to_try))  # Remove duplicates
    
    for fmt in formats_to_try:
        try:
            # Handle m4a -> mp4 pydub quirk
            pydub_fmt = 'mp4' if fmt == 'm4a' else fmt
            
            audio = AudioSegment.from_file(io.BytesIO(file_bytes), format=pydub_fmt)
            
            # ✅ VALIDATION: Check if audio actually loaded
            if len(audio) == 0:
                print(f"⚠️  Format {fmt} loaded 0 seconds, trying next format...")
                continue
            
            print(f"✅ Loaded as {fmt} ({len(audio)/1000:.1f}s)")
            return audio
            
        except (CouldntDecodeError, Exception) as e:
            print(f"❌ Failed to load as {fmt}: {str(e)[:50]}")
            continue
    
    raise ValueError(
        "❌ **UNSUPPORTED/BROKEN FILE**. Tried all formats. "
        "Try: Re-export in Audacity → MP3 128kbps CBR"
    )

async def transcribe_audio_file(file_bytes: bytes, filename: str, task_id: int) -> str:
    """Main transcription - bulletproof with progress tracking."""
    try:
        audio = await safe_load_audio(file_bytes, filename)
    except ValueError as e:
        await update_task(task_id, status='error', error=str(e))
        raise ValueError(str(e))
    
    total_ms = len(audio)
    num_segments = (total_ms // SEGMENT_MS) + (1 if total_ms % SEGMENT_MS else 0)
    full_text: List[str] = []
    
    print(f"📁 Total: {total_ms/60000:.1f}min → {num_segments} segments")
    
    await update_task(task_id, status='processing', progress=0)
    
    for i in tqdm(range(num_segments), desc="Transcribing"):
        try:
            start_ms = i * SEGMENT_MS
            end_ms = min((i + 1) * SEGMENT_MS, total_ms)
            segment = audio[start_ms:end_ms]
            
            # EXPORT with FFmpeg-safe params
            segment_bytes = io.BytesIO()
            segment.export(
                segment_bytes,
                format="mp3",
                bitrate="128k",
                parameters=["-ar", "44100", "-ac", "2", "-y"]
            )
            segment_bytes.seek(0)
            
            # API call
            files = {'file': (f"seg_{i+1}.mp3", segment_bytes.getvalue(), 'audio/mp3')}
            data = {'model': API_MODEL}
            headers = {'Authorization': API_AUTH_HEADER}
            
            resp = requests.post(API_ENDPOINT, headers=headers, files=files, data=data, timeout=300)
            resp.raise_for_status()
            
            result = resp.json()
            text = result.get('text', f"[EMPTY SEGMENT {i+1}]")
            full_text.append(f"=== SEGMENT {i+1} ({(start_ms/1000)/60:.1f}-{ (end_ms/1000)/60:.1f}min ===\n{text}")
            
            # Update progress
            progress = int(((i + 1) / num_segments) * 100)
            await update_task(task_id, progress=progress)
            
        except requests.RequestException as e:
            full_text.append(f"[API ERROR {i+1}]: {str(e)}")
            await update_task(task_id, progress=int(((i + 1) / num_segments) * 100))
        except Exception as e:
            full_text.append(f"[SEGMENT ERROR {i+1}]: {str(e)[:100]}")
            await update_task(task_id, progress=int(((i + 1) / num_segments) * 100))
    
    return "\n\n".join(full_text)

async def summarize_with_gemini(transcript: str, task_id: int, filename: str = "") -> Tuple[str, str]:
    """Gemini summarization - returns summary and info separately."""
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json"
    }

    async def call_gemini(prompt: str) -> str:
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception as e:
            await update_task(task_id, error=f"[GEMINI ERROR]: {str(e)}")
            return f"[GEMINI ERROR]: {str(e)}"

    await update_task(task_id, progress=100)

    # ✅ IMPROVED: Include filename in context
    filename_context = f"音檔名稱: {filename}\n" if filename else ""
    
    # Combined prompt for info extraction
    prompt = (
        "請根據以下演講稿進行分析，並以繁體中文回答：\n\n"
        f"{filename_context}"
        "1. 回答以下問題並以 a, b, c 格式列點：\n"
        "   a. 講者是否為安利的領袖？(回答：是/否)\n"
        "   b. 講者的名字 (若未提及，則回答：未提及)\n"
        "   c. 演講的主題 (若未提及，則回答：未提及)\n"
        "2. 根據上述分析，判斷講者是否為安利領袖。若是，則在總結中使用「安利領袖」稱呼講者；若否，則僅使用「講者」或講者姓名（若已知）。請詳細歸納演講內容，提供結構化的總結，包含主題和主要觀點。\n\n"
        f"演講稿:\n{transcript}"
    )
    
    response = await call_gemini(prompt)
    
    # Parse response - extract info and summary
    info = response
    summary = ""
    
    try:
        # Try to split info and summary
        if "2. 根據上述分析" in response:
            parts = response.split("2. 根據上述分析", 1)
            info = parts[0].strip()
            summary = "2. 根據上述分析" + parts[1] if len(parts) > 1 else ""
        else:
            summary = response
    except Exception as e:
        summary = response
        await update_task(task_id, error=f"[Parsing Error]: {str(e)}")

    return summary, info