# app/db.py
import aiosqlite
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime, timedelta

DB_FILE = "app/db/tasks.db"

@dataclass
class Task:
    id: int
    api_key: str
    status: str
    progress: int
    filename: str
    created_at: str
    file_size: int = 0
    audio_duration: int = 0
    result_file: Optional[str] = None  # ✅ CHANGED: Store file path instead of content
    error: Optional[str] = None

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # Create table if it doesn't exist
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                filename TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_size INTEGER DEFAULT 0,
                audio_duration INTEGER DEFAULT 0,
                result_file TEXT,
                error TEXT
            )
        """)
        await db.commit()
        
        # Check if new columns exist (for migration)
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        # Add columns if they don't exist
        if "file_size" not in column_names:
            print("🔄 Migrating database schema...")
            await db.execute("ALTER TABLE tasks ADD COLUMN file_size INTEGER DEFAULT 0")
            await db.commit()
        
        if "audio_duration" not in column_names:
            await db.execute("ALTER TABLE tasks ADD COLUMN audio_duration INTEGER DEFAULT 0")
            await db.commit()
        
        # ✅ NEW: Migrate from 'result' to 'result_file'
        if "result_file" not in column_names and "result" in column_names:
            print("🔄 Migrating to file-based results...")
            await db.execute("ALTER TABLE tasks ADD COLUMN result_file TEXT")
            await db.commit()
            print("✅ Migration complete!")
        elif "result_file" not in column_names:
            await db.execute("ALTER TABLE tasks ADD COLUMN result_file TEXT")
            await db.commit()

async def create_task(api_key: str, filename: str, file_size: int = 0, audio_duration: int = 0) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (api_key, status, progress, filename, created_at, file_size, audio_duration) VALUES (?, 'pending', 0, ?, CURRENT_TIMESTAMP, ?, ?)",
            (api_key, filename, file_size, audio_duration)
        )
        await db.commit()
        return cursor.lastrowid

async def update_task(task_id: int, status: Optional[str] = None, progress: Optional[int] = None, result_file: Optional[str] = None, error: Optional[str] = None):
    """
    ✅ CHANGED: result_file instead of result
    """
    async with aiosqlite.connect(DB_FILE) as db:
        updates = []
        params = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if progress is not None:
            updates.append("progress = ?")
            params.append(progress)
        if result_file is not None:
            updates.append("result_file = ?")
            params.append(result_file)
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        
        if updates:
            query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"
            params.append(task_id)
            await db.execute(query, params)
            await db.commit()

async def get_task(task_id: int) -> Optional[Task]:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT id, api_key, status, progress, filename, created_at, file_size, audio_duration, result_file, error FROM tasks WHERE id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        if row:
            return Task(*row)
        return None

async def get_tasks_for_key(api_key: str) -> List[Task]:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT id, api_key, status, progress, filename, created_at, file_size, audio_duration, result_file, error FROM tasks WHERE api_key = ? ORDER BY created_at DESC",
            (api_key,)
        )
        rows = await cursor.fetchall()
        return [Task(*row) for row in rows]

async def cleanup_old_tasks(days_old: int = 7):
    """Delete tasks older than N days to save memory"""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "DELETE FROM tasks WHERE created_at < datetime('now', '-' || ? || ' days')",
            (days_old,)
        )
        await db.commit()
        deleted_count = cursor.rowcount
        if deleted_count > 0:
            print(f"🧹 Cleaned up {deleted_count} old tasks")
        
        # Vacuum to reclaim space
        await db.execute("VACUUM")
        await db.commit()