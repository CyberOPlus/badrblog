# ============================================================
# image_cleanup.py - Facebook Image Cleanup and Management
# ============================================================

import time
from pathlib import Path
from typing import Dict, List

from config import FACEBOOK_IMAGE_OUTPUT_DIR
from production_logging import log_event

# Keep only the last N generated Facebook images to prevent storage bloat
MAX_FACEBOOK_IMAGES_TO_KEEP = 20


def cleanup_old_facebook_images(max_keep: int = MAX_FACEBOOK_IMAGES_TO_KEEP) -> Dict[str, int]:
    """
    Clean up old Facebook images, keeping only the most recent ones.
    
    Returns:
        Dict with 'deleted_count', 'kept_count', and 'total_size_freed' (in MB)
    """
    if not FACEBOOK_IMAGE_OUTPUT_DIR.exists():
        return {"deleted_count": 0, "kept_count": 0, "total_size_freed_mb": 0}
    
    try:
        # Get all PNG files sorted by modification time (oldest first)
        image_files = sorted(
            FACEBOOK_IMAGE_OUTPUT_DIR.glob("facebook_*.png"),
            key=lambda p: p.stat().st_mtime
        )
        
        if len(image_files) <= max_keep:
            log_event(
                "image_cleanup_no_action",
                total_images=len(image_files),
                max_keep=max_keep,
            )
            return {
                "deleted_count": 0,
                "kept_count": len(image_files),
                "total_size_freed_mb": 0
            }
        
        # Delete oldest images
        images_to_delete = image_files[:-max_keep]
        deleted_count = 0
        total_size_freed = 0
        
        for image_file in images_to_delete:
            try:
                size_bytes = image_file.stat().st_size
                image_file.unlink()
                deleted_count += 1
                total_size_freed += size_bytes
            except Exception as error:
                log_event(
                    "image_cleanup_delete_failed",
                    file_path=str(image_file),
                    error=error.__class__.__name__,
                )
        
        total_size_freed_mb = total_size_freed / (1024 * 1024)
        
        log_event(
            "image_cleanup_completed",
            deleted_count=deleted_count,
            kept_count=len(image_files) - deleted_count,
            total_size_freed_mb=round(total_size_freed_mb, 2),
        )
        
        return {
            "deleted_count": deleted_count,
            "kept_count": len(image_files) - deleted_count,
            "total_size_freed_mb": round(total_size_freed_mb, 2)
        }
    
    except Exception as error:
        log_event(
            "image_cleanup_failed",
            error=error.__class__.__name__,
            details=str(error)[:200],
        )
        return {
            "deleted_count": 0,
            "kept_count": 0,
            "total_size_freed_mb": 0,
            "error": str(error)[:200]
        }


def get_facebook_images_status() -> Dict:
    """Get current status of Facebook images directory."""
    if not FACEBOOK_IMAGE_OUTPUT_DIR.exists():
        return {
            "image_count": 0,
            "total_size_mb": 0,
            "oldest_image": None,
            "newest_image": None,
        }
    
    try:
        image_files = list(FACEBOOK_IMAGE_OUTPUT_DIR.glob("facebook_*.png"))
        
        if not image_files:
            return {
                "image_count": 0,
                "total_size_mb": 0,
                "oldest_image": None,
                "newest_image": None,
            }
        
        # Sort by modification time
        sorted_files = sorted(image_files, key=lambda p: p.stat().st_mtime)
        
        total_size = sum(f.stat().st_size for f in image_files)
        
        return {
            "image_count": len(image_files),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "oldest_image": sorted_files[0].name if sorted_files else None,
            "oldest_image_age_hours": round(
                (time.time() - sorted_files[0].stat().st_mtime) / 3600, 1
            ) if sorted_files else None,
            "newest_image": sorted_files[-1].name if sorted_files else None,
            "max_keep": MAX_FACEBOOK_IMAGES_TO_KEEP,
        }
    
    except Exception as error:
        log_event(
            "image_status_check_failed",
            error=error.__class__.__name__,
        )
        return {"error": str(error)[:200]}
