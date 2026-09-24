import time
import uuid
from enum import Enum
from typing import Dict, Any, Optional, List
from pydantic import BaseModel


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class TaskInfo(BaseModel):
    task_id: str
    status: TaskStatus
    item_type: str  # track, album, playlist, query
    query: str
    total_tracks: int = 1
    completed_tracks: int = 0
    current_track: Optional[str] = None
    created_at: float
    updated_at: float
    result_files: List[str] = []
    error_message: Optional[str] = None


class TaskManager:
    def __init__(self):
        self._tasks: Dict[str, TaskInfo] = {}

    def create_task(self, query: str, item_type: str = "track", total_tracks: int = 1) -> TaskInfo:
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        info = TaskInfo(
            task_id=task_id,
            status=TaskStatus.PENDING,
            item_type=item_type,
            query=query,
            total_tracks=total_tracks,
            completed_tracks=0,
            created_at=now,
            updated_at=now
        )
        self._tasks[task_id] = info
        return info

    def get_task(self, task_id: str) -> Optional[TaskInfo]:
        return self._tasks.get(task_id)

    def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        completed_tracks: Optional[int] = None,
        total_tracks: Optional[int] = None,
        current_track: Optional[str] = None,
        added_file: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        task = self._tasks.get(task_id)
        if not task:
            return

        if status:
            task.status = status
        if completed_tracks is not None:
            task.completed_tracks = completed_tracks
        if total_tracks is not None:
            task.total_tracks = total_tracks
        if current_track is not None:
            task.current_track = current_track
        if added_file:
            task.result_files.append(added_file)
        if error_message:
            task.error_message = error_message
        
        task.updated_at = time.time()

    def list_tasks(self, limit: int = 20) -> List[TaskInfo]:
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)[:limit]


task_manager = TaskManager()
