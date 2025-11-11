# app/youtube_downloader.py
import os
import yt_dlp
import tempfile
import logging
import asyncio
from typing import Tuple

logger = logging.getLogger(__name__)

YT_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "yt_downloads")
os.makedirs(YT_DOWNLOAD_DIR, exist_ok=True)

async def extract_title_only(url: str) -> str:
    """
    Extract only the video title without downloading
    Fast operation - just gets metadata
    """
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': 15,
            'skip_download': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            },
            'extractor_args': {
                'youtube': {
                    'player_client': ['web'],
                    'player_skip': ['js', 'configs'],
                }
            },
        }
        
        loop = asyncio.get_event_loop()
        
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info
        
        info = await loop.run_in_executor(None, extract)
        
        if info and 'title' in info:
            title = info['title'].strip()
            logger.info(f"📺 Extracted title: {title}")
            return title
        
        return None
    
    except Exception as e:
        logger.warning(f"Failed to extract title: {e}")
        return None

async def download_audio_from_url(url: str, task_id: int) -> Tuple[str, str, int]:
    """
    Download audio from YouTube/video URL using yt-dlp
    Mono 128kbps MP3 format
    Returns: (file_path, title, duration_seconds)
    """
    output_template = os.path.join(YT_DOWNLOAD_DIR, f"yt_{task_id}_%(title)s.%(ext)s")
    
    ydl_opts = {
        # ✅ FIXED: Use format that works with SABR videos
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
            'nopostoverwrites': False,
        }],
        'outtmpl': output_template,
        'quiet': False,
        'no_warnings': False,
        'extract_flat': False,
        'socket_timeout': 30,
        'postprocessor_args': [
            '-ar', '44100',
            '-ac', '1',
            '-b:a', '128k',
            '-q:a', '4',
        ],
        'prefer_ffmpeg': True,
        'keepvideo': False,
        'progress_hooks': [],
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
        # ✅ FIXED: Use android client which doesn't require SABR decryption
        'extractor_args': {
            'youtube': {
                'player_client': ['android'],  # Changed from 'web' to 'android'
                'player_skip': ['js', 'configs'],
            }
        },
        'retries': 5,  # Increased retries
        'fragment_retries': 5,
        'skip_unavailable_fragments': True,  # Skip unavailable fragments
    }
    
    try:
        logger.info(f"📥 Downloading audio from: {url}")
        
        loop = asyncio.get_event_loop()
        
        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info
        
        # Run in executor to avoid blocking
        info = await loop.run_in_executor(None, download)
        
        if info is None:
            raise ValueError("Failed to extract video information")
        
        title = info.get('title', 'Unknown')
        duration = int(info.get('duration', 0))
        
        logger.info(f"📺 Title: {title}, Duration: {duration}s")
        
        # Find the downloaded file
        import glob
        pattern = os.path.join(YT_DOWNLOAD_DIR, f"yt_{task_id}_*.mp3")
        files = glob.glob(pattern)
        
        if not files:
            raise FileNotFoundError(f"Downloaded file not found for pattern: {pattern}")
        
        file_path = files[0]
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Downloaded file not found: {file_path}")
        
        file_size = os.path.getsize(file_path)
        logger.info(f"✅ Downloaded: {file_path} ({file_size/1024/1024:.2f}MB)")
        
        return file_path, title, duration
    
    except Exception as e:
        logger.error(f"❌ Download failed: {e}")
        raise ValueError(f"Failed to download from URL: {str(e)}")

def validate_video_url(url: str) -> bool:
    """Check if URL is supported by yt-dlp"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'socket_timeout': 10,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            },
            'extractor_args': {
                'youtube': {
                    'player_client': ['android'],
                }
            },
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info is not None
    except Exception as e:
        logger.warning(f"URL validation failed: {e}")
        return False