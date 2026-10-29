"""The media lookups the tool loops and voice routes call, and the media
library routes (/api/media/list, /api/media/delete).

Part of the chat routes (audit rank 74): see Orchestrator/routes/chat_routes.py,
the namespace every importer and test uses.
"""
from __future__ import annotations

from Orchestrator.utils.paths import (
    UploadNotFoundError,
    UploadPathError,
    blackbox_root,
    resolve_upload_path,
)
from fastapi import HTTPException
from Orchestrator.checkpoint import app
from Orchestrator.config import UPLOADS_DIR


def execute_get_media(url: str = None, task_id: str = None) -> dict:
    """Execute get_media tool - retrieve media file info by URL or task_id.

    Returns dict with file info, or error message.
    """
    from Orchestrator.tasks import task_db

    # If task_id provided, look up URL from task
    if task_id and not url:
        task = task_db.get_task(task_id)
        if task and task.result_url:
            url = task.result_url
        else:
            return {"error": f"Task {task_id} not found or has no result_url"}

    if not url:
        return {"error": "No URL or task_id provided"}

    # The url is model-supplied (get_media on the chat, voice and CU surfaces):
    # only media inside Portal/uploads may be looked up, so this cannot be used
    # to probe the rest of the box.
    try:
        file_path = resolve_upload_path(url)
    except UploadNotFoundError:
        return {"error": f"File not found: {url}"}
    except UploadPathError as e:
        return {"error": f"Invalid URL: {e}"}
    try:
        shown_path = file_path.relative_to(blackbox_root().resolve()).as_posix()
    except ValueError:
        shown_path = file_path.name

    # Detect media type
    suffix = file_path.suffix.lower()
    if suffix in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
        media_type = "image"
    elif suffix in ['.mp4', '.webm', '.mov']:
        media_type = "video"
    elif suffix in ['.wav', '.mp3', '.ogg', '.m4a']:
        media_type = "audio"
    else:
        media_type = "unknown"

    result = {
        "status": "found",
        "url": url,
        "media_type": media_type,
        "file_size_bytes": file_path.stat().st_size,
        "file_path": shown_path
    }

    # Get metadata from task if task_id provided
    if task_id:
        task = task_db.get_task(task_id)
        if task:
            result["metadata"] = {
                "prompt": task.prompt,
                "task_type": task.task_type.value if task.task_type else None,
                "created_at": task.created_at
            }

    return result


def execute_list_media(media_type: str = None, limit: int = 20) -> dict:
    """Execute list_media tool - list available media files in uploads folder.

    Returns dict with list of media files and their metadata.
    """
    from Orchestrator.media_index import list_media, sync_uploads_folder, get_index_stats

    # Sync uploads folder first to catch any unindexed files
    sync_uploads_folder()

    # Cap limit at 50
    limit = min(limit or 20, 50)

    # Get media list
    entries = list_media(media_type=media_type, limit=limit)

    # Get stats
    stats = get_index_stats()

    # Format entries for model consumption
    formatted = []
    for entry in entries:
        formatted.append({
            "url": entry.get("url"),
            "type": entry.get("type"),
            "description": entry.get("description") or entry.get("prompt", "")[:100],
            "filename": entry.get("filename"),
            "created_at": entry.get("created_at"),
            "file_size_kb": round(entry.get("file_size", 0) / 1024, 1) if entry.get("file_size") else None
        })

    return {
        "count": len(formatted),
        "total_in_index": stats.get("total_files", 0),
        "filter": media_type or "all",
        "media": formatted,
        "usage_hint": "Use URLs with generate_video(image_url=...) for image-to-video, or gemini_image(reference_images=[...]) for image-to-image."
    }


def execute_search_media(query: str, media_type: str = None, limit: int = 10) -> dict:
    """Execute search_media tool - search for media by description/prompt/filename.

    Returns dict with matching media files.
    """
    from Orchestrator.media_index import search_media, sync_uploads_folder

    if not query:
        return {"error": "No search query provided"}

    # Sync uploads folder first to catch any unindexed files
    sync_uploads_folder()

    # Cap limit at 20
    limit = min(limit or 10, 20)

    # Search
    results = search_media(query=query, media_type=media_type, limit=limit)

    # Format results for model consumption
    formatted = []
    for entry in results:
        formatted.append({
            "url": entry.get("url"),
            "type": entry.get("type"),
            "description": entry.get("description") or entry.get("prompt", "")[:100],
            "filename": entry.get("filename"),
            "created_at": entry.get("created_at"),
            "file_size_kb": round(entry.get("file_size", 0) / 1024, 1) if entry.get("file_size") else None
        })

    return {
        "query": query,
        "filter": media_type or "all",
        "count": len(formatted),
        "results": formatted,
        "usage_hint": "Use URLs with generate_video(image_url=...) for image-to-video, or gemini_image(reference_images=[...]) for image-to-image."
    }


# -----------------------------------------------------------------------------
# Media Management Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/media/list")
async def list_media_files(page: int = 1, per_page: int = 50):
    """List all media files in the uploads directory with pagination"""
    try:
        if not UPLOADS_DIR.exists():
            return {"files": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

        # Get all files
        all_files = []
        for file_path in UPLOADS_DIR.iterdir():
            if file_path.is_file():
                stat = file_path.stat()
                file_info = {
                    "filename": file_path.name,
                    "url": f"/ui/uploads/{file_path.name}",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "type": "image" if file_path.suffix.lower() in ['.png', '.jpg', '.jpeg', '.gif', '.webp']
                           else "video" if file_path.suffix.lower() in ['.mp4', '.webm', '.mov']
                           else "audio" if file_path.suffix.lower() in ['.mp3', '.wav', '.ogg', '.m4a']
                           else "other"
                }
                all_files.append(file_info)

        # Sort by modified time (newest first)
        all_files.sort(key=lambda x: x['modified'], reverse=True)

        # Pagination
        total = len(all_files)
        total_pages = (total + per_page - 1) // per_page
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_files = all_files[start_idx:end_idx]

        return {
            "files": page_files,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages
        }
    except Exception as e:
        print(f"[ERROR] Failed to list media files: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/media/delete/{filename}")
async def delete_media_file(filename: str):
    """Delete a specific media file"""
    try:
        file_path = UPLOADS_DIR / filename

        # Security check - ensure file is within uploads directory
        if not file_path.resolve().is_relative_to(UPLOADS_DIR.resolve()):
            raise HTTPException(status_code=400, detail="Invalid filename")

        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        file_path.unlink()
        print(f"[MEDIA] Deleted file: {filename}")

        return {"success": True, "message": f"Deleted {filename}"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Failed to delete file {filename}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
