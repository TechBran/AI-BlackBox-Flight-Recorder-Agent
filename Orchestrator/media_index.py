#!/usr/bin/env python3
"""
media_index.py - Media file index for uploads folder

Provides searchable index of all media files (images, videos, audio) in the uploads folder.
Each entry includes metadata like prompt, type, created_at, file_size for easy discovery.
"""

import contextlib
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

from Orchestrator.config import UPLOADS_DIR
from Orchestrator.utils import atomic_json

# Index file location
MEDIA_INDEX_FILE = Path("Manifest/media_index.json")


def _valid_index(data) -> bool:
    return isinstance(data, dict)


def load_media_index() -> Dict[str, Any]:
    """Load the media index from disk. Never raises: a file that does not
    parse is quarantined (media_index.json.corrupt-<ts>) and reads as empty."""
    return atomic_json.read_json(MEDIA_INDEX_FILE, {}, valid=_valid_index)


def save_media_index(index: Dict[str, Any]):
    """Replace the media index on disk, atomically."""
    try:
        atomic_json.write_json(MEDIA_INDEX_FILE, index)
    except Exception as e:
        print(f"[MEDIA INDEX] Error saving index: {e}")


@contextlib.contextmanager
def _editing_index():
    """Locked read-modify-write of the index (audit O05-ledger-persistence-12):
    the worker thread and request threads add entries concurrently, and each
    add used to load, modify and save with no lock, losing entries. The write
    is atomic, so a crash mid-save cannot truncate the catalog. When the file
    on disk is corrupt it is quarantined and the index is rebuilt from the
    files in Portal/uploads before the edit, instead of the edit's single
    entry replacing the whole catalog."""
    with atomic_json.locked(MEDIA_INDEX_FILE):
        existed = MEDIA_INDEX_FILE.exists()
        with atomic_json.update_json(MEDIA_INDEX_FILE, {}, valid=_valid_index) as index:
            if existed and not MEDIA_INDEX_FILE.exists():
                rebuilt = _index_uploads_into(index)
                print(f"[MEDIA INDEX] Index did not parse (quarantined); rebuilt {rebuilt} entries from {UPLOADS_DIR}")
            yield index


def add_media_entry(
    url: str,
    media_type: str,
    prompt: Optional[str] = None,
    task_id: Optional[str] = None,
    filename: Optional[str] = None,
    file_size: Optional[int] = None,
    extra_metadata: Optional[Dict] = None
):
    """
    Add or update a media entry in the index.

    Args:
        url: The URL path (e.g., /ui/uploads/image.png)
        media_type: Type of media (image, video, audio)
        prompt: The prompt used to generate this media (if generated)
        task_id: The task ID that generated this media
        filename: Original filename
        file_size: File size in bytes
        extra_metadata: Additional metadata (aspect_ratio, resolution, duration, etc.)
    """
    # Extract filename from URL if not provided
    if not filename:
        filename = url.split('/')[-1]

    # Get file size if not provided
    if file_size is None:
        file_path = UPLOADS_DIR / filename
        if file_path.exists():
            file_size = file_path.stat().st_size

    # Create entry
    entry = {
        "url": url,
        "type": media_type,
        "filename": filename,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "file_size": file_size,
    }

    if prompt:
        entry["prompt"] = prompt
        # Also create a searchable description from the prompt
        entry["description"] = prompt[:200] if len(prompt) > 200 else prompt

    if task_id:
        entry["task_id"] = task_id

    if extra_metadata:
        entry.update(extra_metadata)

    # Use URL as the key. Best-effort: the media is already saved, so an
    # index write failure is logged rather than failing its generation.
    try:
        with _editing_index() as index:
            index[url] = entry
    except Exception as e:
        print(f"[MEDIA INDEX] Error saving index: {e}")
        return entry

    print(f"[MEDIA INDEX] Added: {url} ({media_type})")
    return entry


def remove_media_entry(url: str) -> bool:
    """Remove a media entry from the index."""
    with _editing_index() as index:
        if url not in index:
            return False
        del index[url]
    print(f"[MEDIA INDEX] Removed: {url}")
    return True


def list_media(
    media_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_desc: bool = True
) -> List[Dict[str, Any]]:
    """
    List media files with optional filtering.

    Args:
        media_type: Filter by type (image, video, audio) or None for all
        limit: Maximum number of results
        offset: Skip this many results (for pagination)
        sort_by: Field to sort by (created_at, filename, file_size)
        sort_desc: Sort descending if True

    Returns:
        List of media entries
    """
    index = load_media_index()

    # Filter by type if specified
    entries = list(index.values())
    if media_type:
        entries = [e for e in entries if e.get("type") == media_type]

    # Sort
    def get_sort_key(entry):
        val = entry.get(sort_by, "")
        if val is None:
            return ""
        return val

    entries.sort(key=get_sort_key, reverse=sort_desc)

    # Paginate
    entries = entries[offset:offset + limit]

    return entries


def search_media(
    query: str,
    media_type: Optional[str] = None,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """
    Search media by prompt/description/filename.

    Args:
        query: Search query (searches prompt, description, filename)
        media_type: Filter by type (image, video, audio) or None for all
        limit: Maximum number of results

    Returns:
        List of matching media entries, sorted by relevance
    """
    index = load_media_index()
    query_lower = query.lower()
    query_terms = query_lower.split()

    results = []

    for url, entry in index.items():
        # Filter by type if specified
        if media_type and entry.get("type") != media_type:
            continue

        # Build searchable text from multiple fields
        searchable = " ".join([
            entry.get("prompt", ""),
            entry.get("description", ""),
            entry.get("filename", ""),
            entry.get("task_id", "")
        ]).lower()

        # Calculate relevance score
        score = 0
        for term in query_terms:
            if term in searchable:
                score += 1
                # Bonus for exact phrase match
                if query_lower in searchable:
                    score += 2
                # Bonus for match in prompt (most relevant)
                if term in entry.get("prompt", "").lower():
                    score += 1

        if score > 0:
            results.append({**entry, "_score": score})

    # Sort by score descending
    results.sort(key=lambda x: x.get("_score", 0), reverse=True)

    # Remove score from results and limit
    for r in results:
        r.pop("_score", None)

    return results[:limit]


def get_media_info(url: str) -> Optional[Dict[str, Any]]:
    """Get info for a specific media file by URL."""
    index = load_media_index()
    return index.get(url)


def sync_uploads_folder():
    """
    Scan the uploads folder and add any unindexed files to the index.
    Useful for bootstrapping or recovering the index.
    """
    if not UPLOADS_DIR.exists():
        return 0
    with _editing_index() as index:
        added = _index_uploads_into(index)

    if added > 0:
        print(f"[MEDIA INDEX] Synced {added} files from uploads folder")

    return added


def _index_uploads_into(index: Dict[str, Any]) -> int:
    """Add an entry to `index` for every media file in Portal/uploads it does
    not list yet. Returns how many were added."""
    added = 0

    if not UPLOADS_DIR.exists():
        return added

    # Map extensions to types
    ext_type_map = {
        # Images
        '.png': 'image', '.jpg': 'image', '.jpeg': 'image',
        '.gif': 'image', '.webp': 'image', '.bmp': 'image',
        # Videos
        '.mp4': 'video', '.webm': 'video', '.mov': 'video', '.avi': 'video',
        # Audio
        '.mp3': 'audio', '.wav': 'audio', '.ogg': 'audio',
        '.m4a': 'audio', '.flac': 'audio',
    }

    for file_path in UPLOADS_DIR.iterdir():
        if not file_path.is_file():
            continue

        ext = file_path.suffix.lower()
        if ext not in ext_type_map:
            continue

        url = f"/ui/uploads/{file_path.name}"

        # Skip if already indexed
        if url in index:
            continue

        media_type = ext_type_map[ext]

        # Try to extract description from filename
        # Slug format: description-words_taskid.ext
        filename_base = file_path.stem
        description = None
        task_id = None

        # Check for slug format (has underscore followed by UUID-like string)
        if '_' in filename_base:
            parts = filename_base.rsplit('_', 1)
            if len(parts) == 2 and len(parts[1]) >= 8:
                # Looks like slug_taskid format
                description = parts[0].replace('-', ' ').replace('_', ' ')
                task_id = parts[1]

        if not description:
            description = filename_base.replace('-', ' ').replace('_', ' ')

        # Add to index
        entry = {
            "url": url,
            "type": media_type,
            "filename": file_path.name,
            "created_at": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat() + "Z",
            "file_size": file_path.stat().st_size,
            "description": description,
        }

        if task_id:
            entry["task_id"] = task_id

        index[url] = entry
        added += 1

    return added


def clear_media_index():
    """Clear the entire media index. Use with caution."""
    save_media_index({})
    print("[MEDIA INDEX] Index cleared")


def get_index_stats() -> Dict[str, Any]:
    """Get statistics about the media index."""
    index = load_media_index()

    stats = {
        "total_files": len(index),
        "by_type": {},
        "total_size_bytes": 0
    }

    for entry in index.values():
        media_type = entry.get("type", "unknown")
        stats["by_type"][media_type] = stats["by_type"].get(media_type, 0) + 1
        stats["total_size_bytes"] += entry.get("file_size", 0) or 0

    stats["total_size_mb"] = round(stats["total_size_bytes"] / (1024 * 1024), 2)

    return stats
