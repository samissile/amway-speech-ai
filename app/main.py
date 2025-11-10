# app/main.py
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Request, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import io
import secrets
import os
import logging
import aiofiles
import asyncio
import glob
import atexit
import tempfile
from datetime import datetime, timedelta
from dotenv import load_dotenv
from app.transcriber import transcribe_audio_file_streaming, summarize_with_gemini, get_audio_duration
from app.db import create_task, update_task, get_task, get_tasks_for_key, init_db, cleanup_old_tasks

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI(title="Amway Speech AI")

# === CONFIG ===
VALID_API_KEY = os.getenv("VALID_API_KEY")
if not VALID_API_KEY:
    raise ValueError("❌ VALID_API_KEY not set in .env")

# ✅ Directories
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
RESULTS_DIR = os.path.join(os.path.dirname(__file__), 'results')  # ✅ NEW
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "audio_uploads")
SEGMENT_DIR = os.path.join(tempfile.gettempdir(), "segments")

# Create directories
os.makedirs(RESULTS_DIR, exist_ok=True)  # ✅ NEW
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SEGMENT_DIR, exist_ok=True)

logger.info(f"📁 Results directory: {RESULTS_DIR}")
logger.info(f"📁 Upload directory: {UPLOAD_DIR}")
logger.info(f"📁 Segment directory: {SEGMENT_DIR}")

# Mount static + templates
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=STATIC_DIR)

# === CLEANUP FUNCTIONS ===
def cleanup_temp_files():
    """Cleanup on shutdown"""
    logger.info("🧹 Cleaning up temporary files...")
    for directory in [UPLOAD_DIR, SEGMENT_DIR]:
        pattern = os.path.join(directory, "*")
        for file in glob.glob(pattern):
            try:
                os.remove(file)
                logger.debug(f"Deleted: {file}")
            except Exception as e:
                logger.warning(f"Failed to delete {file}: {e}")

async def cleanup_old_temp_files():
    """Delete temp files older than 2 hours"""
    now = datetime.now()
    cutoff = now - timedelta(hours=2)
    
    for directory in [UPLOAD_DIR, SEGMENT_DIR]:
        pattern = os.path.join(directory, "*")
        for file in glob.glob(pattern):
            try:
                file_time = datetime.fromtimestamp(os.path.getmtime(file))
                if file_time < cutoff:
                    os.remove(file)
                    logger.info(f"🧹 Deleted old temp file: {file}")
            except Exception as e:
                logger.warning(f"Failed to cleanup {file}: {e}")

async def cleanup_old_result_files(days_old: int = 7):
    """✅ NEW: Delete result files older than N days"""
    now = datetime.now()
    cutoff = now - timedelta(days=days_old)
    
    pattern = os.path.join(RESULTS_DIR, "result_*.txt")
    for file in glob.glob(pattern):
        try:
            file_time = datetime.fromtimestamp(os.path.getmtime(file))
            if file_time < cutoff:
                os.remove(file)
                logger.info(f"🧹 Deleted old result file: {file}")
        except Exception as e:
            logger.warning(f"Failed to cleanup result file {file}: {e}")

async def periodic_cleanup():
    """Run cleanup every 30 minutes"""
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        await cleanup_old_temp_files()
        await cleanup_old_result_files(days_old=7)  # ✅ NEW
        
        # Also cleanup old database tasks
        try:
            await cleanup_old_tasks(days_old=7)
        except Exception as e:
            logger.error(f"Database cleanup failed: {e}")

# Register cleanup on exit
atexit.register(cleanup_temp_files)

# === STARTUP/SHUTDOWN ===
@app.on_event("startup")
async def startup_event():
    await init_db()
    await cleanup_old_tasks(days_old=7)
    await cleanup_old_result_files(days_old=7)  # ✅ NEW
    asyncio.create_task(periodic_cleanup())
    logger.info("✅ App started with memory optimizations + file-based results")

@app.on_event("shutdown")
async def shutdown_event():
    cleanup_temp_files()
    import gc
    gc.collect()
    logger.info("👋 App shutdown complete")

# === SECURITY ===
async def verify_api_key(api_key: str = Query(...)):
    if not secrets.compare_digest(api_key, VALID_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

async def verify_api_key_form(api_key: str = Form(...)):
    if not secrets.compare_digest(api_key, VALID_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

# === ROUTES ===
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/auth")
async def auth(api_key: str = Depends(verify_api_key_form)):
    return RedirectResponse(url=f"/?api_key={api_key}", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, api_key: str = Query(None)):
    if not api_key:
        return RedirectResponse(url="/login")
    
    try:
        await verify_api_key(api_key)
    except HTTPException:
        return RedirectResponse(url="/login")
    
    tasks = await get_tasks_for_key(api_key)
    return templates.TemplateResponse("status.html", {
        "request": request, 
        "tasks": tasks, 
        "api_key": api_key
    })

@app.post("/transcribe")
async def transcribe(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    summarize: str = Form("true"),
    api_key: str = Depends(verify_api_key_form)
):
    """
    ✅ OPTIMIZED: Streams upload to disk instead of loading into RAM
    ✅ WINDOWS COMPATIBLE: Proper path handling
    """
    summarize_bool = summarize.lower() in ("true", "on", "1")
    
    if not file.filename.lower().endswith(('.mp3', '.m4a', '.wav')):
        raise HTTPException(400, "Only MP3, M4A, WAV supported")
    
    temp_filename = f"upload_{secrets.token_hex(8)}_{file.filename}"
    temp_path = os.path.join(UPLOAD_DIR, temp_filename)
    file_size = 0
    
    try:
        logger.info(f"📥 Streaming upload to disk: {temp_path}")
        async with aiofiles.open(temp_path, 'wb') as f:
            while chunk := await file.read(8192):
                await f.write(chunk)
                file_size += len(chunk)
        
        logger.info(f"✅ Upload complete: {file_size / 1024 / 1024:.2f}MB")
        
        if file_size > 500 * 1024 * 1024:
            os.remove(temp_path)
            raise HTTPException(400, "File too large (>500MB)")
        
        audio_duration = await get_audio_duration(temp_path)
        task_id = await create_task(api_key, file.filename, file_size, audio_duration)
        
        background_tasks.add_task(
            process_audio_from_file, 
            temp_path, 
            file.filename, 
            summarize_bool, 
            task_id
        )
        
        return {"task_id": task_id, "message": "Task started"}
    
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"Upload failed: {e}")
        raise HTTPException(500, f"Upload failed: {str(e)}")

async def process_audio_from_file(file_path: str, filename: str, summarize: bool, task_id: int):
    """
    ✅ UPDATED: Save result to file instead of database
    """
    result_file_path = None
    
    try:
        # Transcribe from disk (streaming)
        transcript = await transcribe_audio_file_streaming(file_path, filename, task_id)
        
        if not summarize:
            result = transcript
        else:
            # Generate summary
            summary, info = await summarize_with_gemini(transcript, task_id, filename)
            result = (
                "================================================\n"
                "AI 演講內容總結:\n"
                "================================================\n"
                f"{summary}\n\n"
                "================================================\n"
                "完整轉錄文字稿:\n"
                "================================================\n"
                f"{transcript}"
            )
        
        # ✅ NEW: Save result to file
        result_file_path = os.path.join(RESULTS_DIR, f"result_{task_id}.txt")
        async with aiofiles.open(result_file_path, 'w', encoding='utf-8') as f:
            await f.write(result)
        
        logger.info(f"💾 Result saved to: {result_file_path}")
        
        # ✅ CHANGED: Store file path instead of content
        await update_task(task_id, status='done', progress=100, result_file=result_file_path)
        logger.info(f"✅ Task {task_id} completed successfully")
    
    except Exception as e:
        logger.error(f"❌ Task {task_id} failed: {e}")
        await update_task(task_id, status='error', progress=100, error=str(e))
    
    finally:
        # Cleanup: delete uploaded file
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"🧹 Deleted uploaded file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete {file_path}: {e}")

@app.get("/status/{task_id}")
async def get_status(task_id: int, api_key: str = Depends(verify_api_key)):
    task = await get_task(task_id)
    if not task or task.api_key != api_key:
        raise HTTPException(404, "Task not found")
    
    # ✅ CHANGED: Check if result file exists
    has_result = task.result_file and os.path.exists(task.result_file)
    
    return {
        "status": task.status,
        "progress": task.progress,
        "error": task.error,
        "has_result": has_result,
        "file_size": task.file_size,
        "audio_duration": task.audio_duration
    }

@app.get("/download/{task_id}")
async def download(task_id: int, api_key: str = Depends(verify_api_key)):
    """
    ✅ UPDATED: Stream file from disk instead of database
    """
    task = await get_task(task_id)
    if not task or task.api_key != api_key:
        raise HTTPException(404, "Task not found")
    
    if task.status != 'done' or not task.result_file:
        raise HTTPException(400, "Task not ready")
    
    # ✅ NEW: Check if result file exists
    if not os.path.exists(task.result_file):
        raise HTTPException(404, "Result file not found")
    
    # Generate download filename
    original_name = task.filename.rsplit('.', 1)[0]
    download_filename = f"{original_name}_result.txt"
    
    # ✅ NEW: Stream file directly from disk (memory efficient!)
    from urllib.parse import quote
    filename_utf8 = quote(download_filename.encode('utf-8'), safe='')
    
    return FileResponse(
        path=task.result_file,
        media_type="text/plain; charset=utf-8",
        filename=download_filename,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_utf8}"
        }
    )

# Health check endpoint
@app.get("/health")
async def health():
    return {
        "status": "healthy", 
        "memory_optimized": True,
        "file_based_results": True
    }