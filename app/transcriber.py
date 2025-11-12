# app/transcriber.py
import os
import json
import subprocess
import requests
from typing import Tuple, List, Optional
from app.db import update_task
from dotenv import load_dotenv
import asyncio
import logging
import tempfile

logger = logging.getLogger(__name__)
load_dotenv()

# === CONFIG ===
API_ENDPOINT = "https://api.bltcy.ai/v1/audio/transcriptions"
API_AUTH_HEADER = os.getenv("API_AUTH_HEADER")
if not API_AUTH_HEADER:
    raise ValueError("❌ API_AUTH_HEADER not set in .env")
API_MODEL = "gpt-4o-transcribe"
SEGMENT_DURATION = 5 * 60  # 5 minutes

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not set in .env")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent"

SEGMENT_DIR = os.path.join(tempfile.gettempdir(), "segments")
os.makedirs(SEGMENT_DIR, exist_ok=True)

async def get_audio_duration(file_path: str) -> int:
    """Get audio duration without loading file into memory"""
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
            encoding='utf-8',
            errors='ignore',
            timeout=30
        )
        
        if result.returncode != 0 or not result.stdout or result.stdout.strip() == '':
            logger.warning(f"FFprobe failed or returned empty")
            return 0
        
        data = json.loads(result.stdout)
        duration = int(float(data['format']['duration']))
        logger.info(f"📊 Audio duration: {duration}s ({duration/60:.1f}min)")
        return duration
    
    except Exception as e:
        logger.error(f"Failed to get duration: {e}")
        return 0

async def transcribe_audio_file_streaming(
    file_path: str, 
    filename: str, 
    task_id: int, 
    initial_progress: int = 0
) -> str:
    """
    Process audio from disk without loading into RAM
    Optimized for accuracy, memory, and speed
    """
    try:
        duration_seconds = await get_audio_duration(file_path)
        
        if duration_seconds == 0:
            raise ValueError("Unable to determine audio duration")
        
        # Update duration in database
        await update_task(task_id, audio_duration=duration_seconds)
        
        num_segments = (duration_seconds // SEGMENT_DURATION) + (
            1 if duration_seconds % SEGMENT_DURATION else 0
        )
        
        full_text: List[str] = []
        logger.info(f"📁 Processing {duration_seconds/60:.1f}min → {num_segments} segments")
        
        progress_range = 90 - initial_progress
        
        for i in range(num_segments):
            try:
                start_sec = i * SEGMENT_DURATION
                end_sec = min((i + 1) * SEGMENT_DURATION, duration_seconds)
                
                segment_path = os.path.join(SEGMENT_DIR, f"seg_{task_id}_{i}.mp3")
                
                logger.info(f"🔄 Segment {i+1}/{num_segments} ({start_sec}s-{end_sec}s)")
                
                # Extract segment with optimized settings for accuracy
                result = subprocess.run([
                    'ffmpeg', '-y',
                    '-ss', str(start_sec),
                    '-t', str(SEGMENT_DURATION),
                    '-i', file_path,
                    '-ar', '44100',  # 44.1kHz for better quality
                    '-ac', '1',      # Mono for smaller size
                    '-b:a', '128k',  # 128kbps bitrate
                    '-acodec', 'libmp3lame',  # Explicit codec
                    '-q:a', '4',     # Quality level (lower = better)
                    '-vn',           # No video
                    segment_path
                ], 
                    capture_output=True, 
                    timeout=300,
                    encoding='utf-8',
                    errors='ignore'
                )
                
                if result.returncode != 0 or not os.path.exists(segment_path):
                    logger.error(f"FFmpeg failed for segment {i+1}")
                    full_text.append(f"[片段 {i+1} 錯誤: 提取失敗]")
                    continue
                
                # Read segment
                with open(segment_path, 'rb') as f:
                    segment_bytes = f.read()
                
                logger.info(f"📤 Uploading segment {i+1} ({len(segment_bytes)/1024/1024:.1f}MB)")
                
                # Transcribe with retry logic
                max_retries = 3
                text = None
                
                for attempt in range(max_retries):
                    try:
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
                        text = result_json.get('text', '')
                        
                        if text:
                            logger.info(f"✅ Segment {i+1} transcribed ({len(text)} chars)")
                            break
                        else:
                            logger.warning(f"Empty transcription for segment {i+1}")
                            text = f"[片段 {i+1}: 無法識別的音頻]"
                            break
                    
                    except requests.RequestException as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"Retry {attempt+1}/{max_retries} for segment {i+1}")
                            await asyncio.sleep(2 ** attempt)
                        else:
                            text = f"[片段 {i+1} 錯誤: API 請求失敗]"
                            logger.error(f"API error for segment {i+1}: {e}")
                
                if text:
                    full_text.append(
                        f"\n{text}"
                    )
                
                # Immediate cleanup
                try:
                    os.remove(segment_path)
                    del segment_bytes
                except Exception as e:
                    logger.warning(f"Cleanup warning: {e}")
                
                # Update progress
                segment_progress = int(initial_progress + ((i + 1) / num_segments) * progress_range)
                await update_task(task_id, progress=segment_progress)
                
                await asyncio.sleep(0.3)  # Rate limit
                
            except Exception as e:
                logger.error(f"Segment {i+1} error: {e}")
                full_text.append(f"[片段 {i+1} 錯誤: {str(e)[:100]}]")
        
        return "\n\n".join(full_text)
    
    finally:
        # Cleanup original file
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"🧹 Deleted: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete: {e}")

async def summarize_with_gemini(
    transcript: str, 
    task_id: int, 
    filename: str = ""
) -> Tuple[str, str]:
    """Generate AI summary using Gemini with optimized prompt"""
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json"
    }

    async def call_gemini(prompt: str) -> str:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.7,
                "topP": 0.9,
                "topK": 40,
                "maxOutputTokens": 8192,
            }
        }
        try:
            resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception as e:
            error_msg = f"[GEMINI ERROR]: {str(e)}"
            logger.error(error_msg)
            return error_msg

    filename_context = f"檔案名稱: {filename}\n" if filename else ""
    
    prompt = (
        "請根據以下演講稿進行分析，並以繁體中文回答：\n"
        "1. 回答以下問題並以 a, b, c 格式列點(只給答案)：\n"
        "   a. 講者是否為安利的領袖？(回答：是/否)\n"
        f"   b. 講者的名字 (若{filename}和演講稿未提及，則回答：未提及)\n"
        f"   c. 演講的主題 (若{filename}和演講稿未提及，則回答：未提及)\n"
        "2. 請詳細歸納演講內容，提供結構化的總結，包含主題和主要觀點。\n"
        f"演講稿:\n{transcript}"
    )
    
    response = await call_gemini(prompt)
    
    info = response
    summary = ""
    
    try:
        if "2. 演講內容總結" in response:
            parts = response.split("2. 演講內容總結", 1)
            info = parts[0].strip()
            summary = "2. 演講內容總結" + parts[1] if len(parts) > 1 else ""
        else:
            summary = response
    except Exception as e:
        logger.error(f"Parsing error: {e}")
        summary = response

    return summary, info