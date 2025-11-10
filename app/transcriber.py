# app/transcriber.py - WINDOWS COMPATIBLE VERSION
import os
import json
import subprocess
import requests
from typing import Tuple, List
from app.db import update_task
from dotenv import load_dotenv
import asyncio
import logging
import tempfile

logger = logging.getLogger(__name__)
load_dotenv()

# === CONFIG ===
API_ENDPOINT = "https://api.laozhang.ai/v1/audio/transcriptions"
API_AUTH_HEADER = os.getenv("API_AUTH_HEADER")
if not API_AUTH_HEADER:
    raise ValueError("❌ API_AUTH_HEADER not set in .env")
API_MODEL = "gpt-4o-transcribe"
SEGMENT_DURATION = 5 * 60  # 5 minutes in seconds

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not set in .env")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# ✅ FIXED: Use proper temp directory for Windows/Linux
SEGMENT_DIR = os.path.join(tempfile.gettempdir(), "segments")
os.makedirs(SEGMENT_DIR, exist_ok=True)

async def get_audio_duration(file_path: str) -> int:
    """
    Get audio duration WITHOUT loading file into memory
    ✅ FIXED: Windows encoding handling
    """
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'quiet', 
                '-print_format', 'json', 
                '-show_format', 
                file_path
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',  # ✅ FIXED: Explicit UTF-8 encoding
            errors='ignore',    # ✅ FIXED: Ignore decode errors
            timeout=30
        )
        
        if result.returncode != 0:
            logger.warning(f"FFprobe failed: {result.stderr}")
            return 0
        
        # ✅ FIXED: Check if stdout is empty
        if not result.stdout or result.stdout.strip() == '':
            logger.error("FFprobe returned empty output")
            return 0
        
        data = json.loads(result.stdout)
        duration = int(float(data['format']['duration']))
        logger.info(f"📊 Audio duration: {duration}s ({duration/60:.1f}min)")
        return duration
    
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse FFprobe JSON: {e}")
        logger.debug(f"FFprobe output: {result.stdout[:200]}")
        return 0
    except Exception as e:
        logger.error(f"Failed to get duration: {e}")
        return 0

async def transcribe_audio_file_streaming(file_path: str, filename: str, task_id: int) -> str:
    """
    ✅ MEMORY-OPTIMIZED: Process audio from disk without loading into RAM
    ✅ WINDOWS COMPATIBLE: Proper encoding and path handling
    """
    try:
        # Get duration without loading file
        duration_seconds = await get_audio_duration(file_path)
        
        if duration_seconds == 0:
            raise ValueError("Unable to determine audio duration. Check if FFmpeg is installed.")
        
        num_segments = (duration_seconds // SEGMENT_DURATION) + (
            1 if duration_seconds % SEGMENT_DURATION else 0
        )
        
        full_text: List[str] = []
        logger.info(f"📁 Processing {duration_seconds/60:.1f}min → {num_segments} segments")
        
        await update_task(task_id, status='processing', progress=0)
        
        for i in range(num_segments):
            try:
                start_sec = i * SEGMENT_DURATION
                end_sec = min((i + 1) * SEGMENT_DURATION, duration_seconds)
                
                # ✅ FIXED: Proper cross-platform path
                segment_path = os.path.join(SEGMENT_DIR, f"seg_{task_id}_{i}.mp3")
                
                logger.info(f"🔄 Extracting segment {i+1}/{num_segments} ({start_sec}s - {end_sec}s)")
                
                # ✅ FIXED: Add encoding to subprocess
                result = subprocess.run([
                    'ffmpeg', '-y',
                    '-ss', str(start_sec),
                    '-t', str(SEGMENT_DURATION),
                    '-i', file_path,
                    '-ar', '44100',
                    '-ac', '2',
                    '-b:a', '128k',
                    '-vn',  # No video
                    segment_path
                ], 
                    capture_output=True, 
                    timeout=300,
                    encoding='utf-8',  # ✅ FIXED
                    errors='ignore'     # ✅ FIXED
                )
                
                if result.returncode != 0:
                    error_msg = result.stderr[:200] if result.stderr else "Unknown error"
                    logger.error(f"FFmpeg failed for segment {i+1}: {error_msg}")
                    full_text.append(f"[FFMPEG ERROR {i+1}]: Extraction failed")
                    continue
                
                # Check if segment was created
                if not os.path.exists(segment_path):
                    logger.error(f"Segment file not created: {segment_path}")
                    full_text.append(f"[ERROR {i+1}]: Segment file not created")
                    continue
                
                # Read ONLY the small segment file (~5-10MB)
                with open(segment_path, 'rb') as f:
                    segment_bytes = f.read()
                
                logger.info(f"📤 Uploading segment {i+1} ({len(segment_bytes)/1024/1024:.1f}MB) to API")
                
                # API call
                files = {'file': (f"seg_{i+1}.mp3", segment_bytes, 'audio/mp3')}
                data = {'model': API_MODEL}
                headers = {'Authorization': API_AUTH_HEADER}
                
                resp = requests.post(
                    API_ENDPOINT, 
                    headers=headers, 
                    files=files, 
                    data=data, 
                    timeout=300
                )
                resp.raise_for_status()
                
                result_json = resp.json()
                text = result_json.get('text', f"[EMPTY SEGMENT {i+1}]")
                full_text.append(
                    f"=== SEGMENT {i+1} ({start_sec/60:.1f}-{end_sec/60:.1f}min ===\n{text}"
                )
                
                logger.info(f"✅ Segment {i+1} transcribed successfully")
                
                # ✅ IMMEDIATE CLEANUP to free memory/disk
                try:
                    os.remove(segment_path)
                    del segment_bytes
                except Exception as e:
                    logger.warning(f"Cleanup warning: {e}")
                
                # Update progress
                progress = int(((i + 1) / num_segments) * 100)
                await update_task(task_id, progress=progress)
                
                # Small delay to avoid API rate limits
                await asyncio.sleep(0.5)
                
            except subprocess.TimeoutExpired:
                logger.error(f"Segment {i+1} extraction timeout")
                full_text.append(f"[TIMEOUT {i+1}]: FFmpeg extraction took too long")
            
            except requests.RequestException as e:
                logger.error(f"API error for segment {i+1}: {e}")
                full_text.append(f"[API ERROR {i+1}]: {str(e)[:100]}")
            
            except Exception as e:
                logger.error(f"Segment {i+1} error: {e}")
                full_text.append(f"[ERROR {i+1}]: {str(e)[:100]}")
        
        return "\n\n".join(full_text)
    
    finally:
        # Cleanup: Delete original uploaded file
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"🧹 Deleted original file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete original file: {e}")

async def summarize_with_gemini(transcript: str, task_id: int, filename: str = "") -> Tuple[str, str]:
    """
    Generate AI summary using Gemini
    """
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
            error_msg = f"[GEMINI ERROR]: {str(e)}"
            logger.error(error_msg)
            await update_task(task_id, error=error_msg)
            return error_msg

    await update_task(task_id, progress=100)

    filename_context = f"音檔名稱: {filename}\n" if filename else ""
    
    prompt = (
        "請根據以下演講稿進行分析，並以繁體中文回答：\n\n"
        "1. 回答以下問題並以 a, b, c 格式列點：\n"
        "   a. 講者是否為安利的領袖？(回答：是/否)\n"
        f"   b. 講者的名字 (若{filename}和演講稿未提及，則回答：未提及)\n"
        f"   c. 演講的主題 (若{filename}和演講稿未提及，則回答：未提及)\n"
        "2. 根據上述分析，判斷講者是否為安利領袖。若是，則在總結中使用「安利領袖」稱呼講者；若否，則僅使用「講者」或講者姓名（若已知）。請詳細歸納演講內容，提供結構化的總結，包含主題和主要觀點。\n\n"
        f"{filename_context}"
        f"演講稿:\n{transcript}"
    )
    
    response = await call_gemini(prompt)
    
    info = response
    summary = ""
    
    try:
        if "2. 根據上述分析" in response:
            parts = response.split("2. 根據上述分析", 1)
            info = parts[0].strip()
            summary = "2. 根據上述分析" + parts[1] if len(parts) > 1 else ""
        else:
            summary = response
    except Exception as e:
        logger.error(f"Parsing error: {e}")
        summary = response

    return summary, info