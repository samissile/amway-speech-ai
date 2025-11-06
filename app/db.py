# app/db.py
import aiosqlite
from dataclasses import dataclass
from typing import Optional, List

DB_FILE = "tasks.db"

@dataclass
class Task:
    id: int
    api_key: str
    status: str
    progress: int
    filename: str
    created_at: str
    file_size: int = 0  # ✅ NEW
    audio_duration: int = 0  # ✅ NEW (in seconds)
    result: Optional[str] = None
    error: Optional[str] = None

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # Check if new columns exist
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        if "file_size" not in column_names:
            print("🔄 Migrating database schema...")
            await db.execute("ALTER TABLE tasks ADD COLUMN file_size INTEGER DEFAULT 0")
            await db.execute("ALTER TABLE tasks ADD COLUMN audio_duration INTEGER DEFAULT 0")
            await db.commit()
            print("✅ Migration complete!")
        else:
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
                    result TEXT,
                    error TEXT
                )
            """)
            await db.commit()

async def create_task(api_key: str, filename: str, file_size: int = 0, audio_duration: int = 0) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (api_key, status, progress, filename, created_at, file_size, audio_duration) VALUES (?, 'pending', 0, ?, CURRENT_TIMESTAMP, ?, ?)",
            (api_key, filename, file_size, audio_duration)
        )
        await db.commit()
        return cursor.lastrowid

async def update_task(task_id: int, status: Optional[str] = None, progress: Optional[int] = None, result: Optional[str] = None, error: Optional[str] = None):
    async with aiosqlite.connect(DB_FILE) as db:
        updates = []
        params = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if progress is not None:
            updates.append("progress = ?")
            params.append(progress)
        if result is not None:
            updates.append("result = ?")
            params.append(result)
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
            "SELECT id, api_key, status, progress, filename, created_at, file_size, audio_duration, result, error FROM tasks WHERE id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        if row:
            return Task(*row)
        return None

async def get_tasks_for_key(api_key: str) -> List[Task]:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT id, api_key, status, progress, filename, created_at, file_size, audio_duration, result, error FROM tasks WHERE api_key = ? ORDER BY created_at DESC",
            (api_key,)
        )
        rows = await cursor.fetchall()
        return [Task(*row) for row in rows]