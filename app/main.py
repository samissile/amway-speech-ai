# app/main.py
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Request, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import io
import secrets
import os
import logging
from dotenv import load_dotenv
from app.transcriber import transcribe_audio_file, summarize_with_gemini
from app.db import create_task, update_task, get_task, get_tasks_for_key, init_db

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI(title="Amway Speech AI")

# === CONFIG: Load API key from environment ===
VALID_API_KEY = os.getenv("VALID_API_KEY")
if not VALID_API_KEY:
    raise ValueError("❌ VALID_API_KEY not set in .env")

# Get the absolute path to the static directory
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Debug print to verify directory
print(f"Static Directory: {STATIC_DIR}")
print(f"Directory Exists: {os.path.exists(STATIC_DIR)}")

# Mount static + templates
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=STATIC_DIR)

# Add startup event for database initialization
@app.on_event("startup")
async def startup_event():
    await init_db()

# === Security: API Key Check ===
async def verify_api_key(api_key: str = Query(...)):
    logger.debug(f"Verifying API key: {api_key}")
    if not secrets.compare_digest(api_key, VALID_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
async def verify_api_key_form(api_key: str = Form(...)):
    logger.debug(f"Verifying API key (form): {api_key}")
    if not secrets.compare_digest(api_key, VALID_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

# === Login Page ===
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

# === Authenticate and redirect to home ===
@app.post("/auth")
async def auth(api_key: str = Depends(verify_api_key_form)):
    logger.debug(f"Authenticated with api_key: {api_key}")
    return RedirectResponse(url=f"/?api_key={api_key}")

# === Home: Status page, requires api_key query ===
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, api_key: str = Depends(verify_api_key)):
    logger.debug(f"Accessing home with api_key: {api_key}")
    tasks = await get_tasks_for_key(api_key)
    return templates.TemplateResponse("status.html", {"request": request, "tasks": tasks, "api_key": api_key})

# === Upload and start task ===
# In the transcribe endpoint, add file_size and duration capture
@app.post("/transcribe")
async def transcribe(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    summarize: str = Form("true"),
    api_key: str = Depends(verify_api_key_form)
):
    logger.debug(f"Received file: {file.filename}, summarize: {summarize}, api_key: {api_key}")
    summarize_bool = summarize.lower() in ("true", "on", "1")
    if not file.filename.lower().endswith(('.mp3', '.m4a', '.wav')):
        raise HTTPException(400, "Only MP3, M4A, WAV supported")
    content = await file.read()
    file_size = len(content)  # ✅ NEW
    logger.debug(f"File size: {file_size} bytes")
    if file_size > 500 * 1024 * 1024:
        raise HTTPException(400, "File too large (>500MB)")
    
    # ✅ NEW: Get audio duration
    try:
        from pydub import AudioSegment
        import io
        audio = AudioSegment.from_file(io.BytesIO(content))
        audio_duration = len(audio) // 1000  # Convert to seconds
    except:
        audio_duration = 0
    
    task_id = await create_task(api_key, file.filename, file_size, audio_duration)
    logger.debug(f"Created task_id: {task_id}")
    background_tasks.add_task(process_audio, content, file.filename, summarize_bool, task_id)
    return {"task_id": task_id, "message": "Task started"}

async def process_audio(content: bytes, filename: str, summarize: bool, task_id: int):
    try:
        transcript = await transcribe_audio_file(content, filename, task_id)
        
        if not summarize:
            result = transcript
        else:
            # ✅ PASS FILENAME TO LLM
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
        
        await update_task(task_id, status='done', progress=100, result=result)
    
    except Exception as e:
        await update_task(task_id, status='error', progress=100, error=str(e))
# === Get task status ===
@app.get("/status/{task_id}")
async def get_status(task_id: int, api_key: str = Depends(verify_api_key)):
    task = await get_task(task_id)
    if not task or task.api_key != api_key:
        raise HTTPException(404, "Task not found")
    return {
        "status": task.status,
        "progress": task.progress,
        "error": task.error,
        "has_result": bool(task.result),
        "file_size": task.file_size,  # ✅ NEW
        "audio_duration": task.audio_duration  # ✅ NEW
    }

# === Download result ===
@app.get("/download/{task_id}")
async def download(task_id: int, api_key: str = Depends(verify_api_key)):
    task = await get_task(task_id)
    if not task or task.api_key != api_key:
        raise HTTPException(404, "Task not found")
    if task.status != 'done' or not task.result:
        raise HTTPException(400, "Task not ready")

    output = io.BytesIO(task.result.encode('utf-8'))
    
    # ✅ FIX: Use RFC 5987 encoding for non-ASCII filenames
    filename = task.filename.rsplit('.', 1)[0] + "_result.txt"
    
    # Encode filename for Content-Disposition header (RFC 5987)
    # Format: filename*=UTF-8''<url-encoded-filename>
    from urllib.parse import quote
    filename_utf8 = quote(filename.encode('utf-8'), safe='')
    
    return StreamingResponse(
        output,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_utf8}"
        }
    )