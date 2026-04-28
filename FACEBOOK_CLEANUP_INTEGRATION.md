# Facebook Image Cleanup Integration Guide

## نظرة عامة
لتفعيل تنظيف الصور المحلية تلقائيًا بعد نشر Facebook، أضف السطور التالية إلى `facebook_publisher.py`.

## الخطوة 1: استيراد Cleanup Function
أضف هذا السطر في رأس `facebook_publisher.py`:

```python
from image_cleanup import cleanup_old_facebook_images
```

## الخطوة 2: استدعاء Cleanup بعد النشر
في دالة `publish_facebook_articles()` أو النقطة التي تنشر بها على Facebook، أضف:

```python
# After successful Facebook post
if facebook_post_result.get("ok"):
    # Clean up old images to prevent storage bloat
    cleanup_result = cleanup_old_facebook_images(max_keep=20)
    log_event(
        "facebook_images_cleanup_after_post",
        **cleanup_result
    )
```

## الخطوة 3: مثال كامل
```python
def _publish_facebook_post(article, blueprint):
    # ... existing code ...
    
    # Publish the post
    image_url = _main_image_url(article)
    image_result = generate_facebook_image(...)
    
    if image_result.get("ok") and Path(image_result["path"]).exists():
        payload = {...}
        data = _post_photo_file(f"{FACEBOOK_PAGE_ID}/photos", payload, image_result["path"])
        post_id = data.get("post_id") or data.get("id") or ""
        
        # NEW: Clean up old images after successful post
        if post_id:
            cleanup_result = cleanup_old_facebook_images(max_keep=20)
            log_event(
                "facebook_post_with_cleanup",
                post_id=post_id,
                cleanup_deleted=cleanup_result.get("deleted_count", 0),
                cleanup_freed_mb=cleanup_result.get("total_size_freed_mb", 0),
            )
    
    # ... rest of function ...
```

## الخطوة 4: Monitoring Status
يمكنك التحقق من حالة صور Facebook في أي وقت:

```python
from image_cleanup import get_facebook_images_status

status = get_facebook_images_status()
print(status)
# Output:
# {
#     'image_count': 18,
#     'total_size_mb': 45.2,
#     'oldest_image': 'facebook_abc123.png',
#     'oldest_image_age_hours': 72.5,
#     'newest_image': 'facebook_xyz789.png',
#     'max_keep': 20
# }
```

## متغيرات التكوين
يمكن تخصيص عدد الصور التي يتم الاحتفاظ بها:

```python
from image_cleanup import cleanup_old_facebook_images

# Keep last 30 images instead of 20
result = cleanup_old_facebook_images(max_keep=30)

# Keep last 10 images only
result = cleanup_old_facebook_images(max_keep=10)
```

## Logging Events
يتم تسجيل جميع عمليات التنظيف:

```
[2026-04-28T16:15:00] image_cleanup_completed | deleted_count=2 | kept_count=18 | total_size_freed_mb=1.23
[2026-04-28T16:15:00] image_status_check | image_count=18 | total_size_mb=45.2 | oldest_age_hours=72.5
```

## الفوائد
- ✅ توفير مساحة التخزين
- ✅ تنظيف تلقائي بدون تدخل يدوي
- ✅ الاحتفاظ بآخر 20 صورة (قابل للتخصيص)
- ✅ logging كامل للعمليات
- ✅ معلومات status عند الحاجة

## ملاحظات
- يتم الحفظ فقط على الصور التي تطابق `facebook_*.png`
- الصور القديمة يتم حذفها أولاً
- لا يتأثر الأداء (فقط حذف ملفات محلية)
