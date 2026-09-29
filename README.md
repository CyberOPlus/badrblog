# Cybero Plus Jobs Bot

هذا المستودع **هو البوت نفسه**. لا توجد نسخة Jobs داخل بوت آخر، ولا يحتاج التشغيل إلى مجلد فرعي مستقل.

## الهدف

جمع فرص التوظيف المناسبة للمغاربة من المصادر المعتمدة، التحقق منها، منع التكرار، اختيار الأفضل، إنشاء مقال عربي دقيق، نشره في Blogger، ثم نشر Facebook في التوقيت المناسب مع رابط المقال/القناة في أول تعليق.

## خط السير الحالي

```
مصادر الوظائف
→ discovery / scraping
→ استخراج حقائق الوظيفة
→ source trust + eligibility
→ quality score
→ campaign identity / dedup / update detection
→ AI article with fact guard
→ stable SEO slug
→ Blogger publish/update
→ JobPosting JSON-LD
→ Facebook timing
→ Facebook image + employer logo when available
→ first comment
→ durable state + long-term job memory
```

## قواعد الوظائف المحفوظة

- المصدر الرسمي هو المرجع الأعلى.
- الوظيفة غير المؤكدة للمغاربة لا تنشر.
- إعلان منتهي الأجل لا يعامل كوظيفة مفتوحة.
- تحديث عدد المناصب أو الموعد أو رابط الترشيح يحدث **نفس الحملة/المقال** عندما تكون الهوية مؤكدة.
- إعادة نفس المسمى في حملة جديدة لاحقاً يمكن أن تصبح صفحة جديدة.
- الـslug لا يحتوي على تاريخ أو عدد مناصب أو موعد نهائي قابل للتغيير.
- تاريخ الحملات محفوظ في `data/job_memory/` بشكل sharded حتى لا يكبر ملف JSON واحد بلا حدود.
- Labels: `jobs` دائماً، ثم `jobs-morocco` أو `remote-jobs` أو `jobs-abroad`، و`visa-sponsorship` عند التأكد.

## التوقيت — المغرب

المنطق موجود في `job_core.py` ويستعمل `Africa/Casablanca`.

حد Blogger اليومي يراعي **اليوم + الشهر**، وFacebook له slots مستقلة حسب أيام الأسبوع. الفرص الرسمية العاجلة جداً يمكنها استعمال urgent override محدود.

GitHub Actions يمكنه الفحص المتكرر، لكن قرار النشر نفسه يبقى داخل `job_core.py` حتى لا ينشر لمجرد أن Workflow اشتغل.

## Blogger التجريبي

الهدف الحالي للاختبار:

`https://cyberopluss.blogspot.com/`

يوجد host lock لمنع توجيه اختبار بالخطأ إلى الدومين الحقيقي.

## الملفات الأساسية

- `main.py` — orchestration الرئيسي.
- `job_core.py` — الهوية، الذاكرة، الجودة، الـslug، limits والتوقيت.
- `job_extractor.py` — استخراج حقائق الوظيفة.
- `jobposting.py` — JobPosting structured data.
- `scraper.py` — discovery وجلب الصفحات.
- `article_queue.py` — queue الدائمة.
- `article_ai_processor.py` — صياغة المقال مع حقائق الوظيفة.
- `article_draft_publisher.py` / `blogger_client.py` — Blogger.
- `facebook_publisher.py` — Facebook، الصور والتعليق الأول.
- `assets/facebook/job-new-orange.png`, `job-deadline-yellow.png`, `job-alert-blue.png`, `job-apply-red.png` — قوالب Facebook الأربعة الحالية.
- `jobs_sources.json` / `sources.json` — سجل المصادر.
- `.github/workflows/auto-cycle.yml` — التشغيل الآلي.

## أوامر مفيدة

```bash
python main.py queue-maintenance
python main.py auto-cycle
python main.py post-facebook
python main.py health
```

## مبدأ التطوير

نطوّر نفس البوت تدريجياً. لا ننشئ bot داخل bot، ولا نهدم منطقاً شغالاً لإعادة بنائه من الصفر.

