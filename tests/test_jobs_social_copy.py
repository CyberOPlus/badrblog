"""Jobs caption quality and formatting regressions; no remote publishing."""

import json
import re
import unittest
from unittest.mock import patch

import facebook_publisher as facebook
import social_ai_processor as social_ai


class JobsSocialCopyTests(unittest.TestCase):
    def setUp(self):
        self.article = {
            "id": "social-competition",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/job.html",
            "seo_title": "مباراة توظيف متصرف من الدرجة الثالثة بغرفة التجارة لسوس ماسة",
            "seo_description": "مباراة توظيف، آخر أجل هو 20 أكتوبر 2026.",
            "job_notice_type": "competition",
            "job_company": "غرفة التجارة والصناعة والخدمات لجهة سوس ماسة",
            "job_deadline_display": "20 أكتوبر 2026",
            "job_number_of_positions": 0,
            "final_html": (
                "<p>منصب واحد لمتصرف من الدرجة الثالثة (سلم 10).</p>"
                "<p>تقبل شهادة الماستر أو الشهادات المعادلة المحددة في الإعلان.</p>"
                "<p>ملف الترشيح يتضمن نسخة من الدبلوم والبطاقة الوطنية.</p>"
                "<p>التسجيل الإلكتروني ثم إرسال الملف بالبريد المضمون.</p>"
            ),
        }
        self.caption = (
            "🎓 تحمل شهادة الماستر أو ما يعادلها؟ هذه المباراة بسوس ماسة قد تهمك.\n\n"
            "💼 غرفة التجارة والصناعة والخدمات تعلن عن منصب متصرف من الدرجة الثالثة.\n\n"
            "⏳ آخر أجل للترشيح: 20 أكتوبر 2026.\n\n"
            "👇 راجع الشروط والوثائق وخطوات التسجيل وإرسال الملف في أول تعليق.\n\n"
            "#CyberoPlus #مباريات_التوظيف"
        )

    def test_arabic_caption_passes_generation_and_publisher_with_first_comment_link(self):
        response = json.dumps({"facebook_post_text": self.caption}, ensure_ascii=False)
        with patch.object(
            social_ai, "_generate_ai_article", return_value=(response, "gemini:test")
        ) as generate, patch.object(facebook, "_load_style_memory", return_value={}):
            blueprint = facebook._prepare_facebook_post(
                self.article, [], self.article["blogger_post_url"]
            )

        generate.assert_called_once()
        self.assertEqual(
            blueprint["hashtags"],
            ["#CyberoPlus", "#مباريات_التوظيف"],
        )
        self.assertIn("أول تعليق", blueprint["cta"])
        self.assertNotIn("http", blueprint["caption"])
        self.assertTrue(all(
            line.startswith("\u200f")
            for line in blueprint["caption"].splitlines() if line.strip()
        ))
        first_comment = facebook._first_comment_text(
            self.article["blogger_post_url"], article=self.article
        )
        self.assertIn(self.article["blogger_post_url"], first_comment)
        self.assertIn("شروط المباراة", first_comment)
        prompt = generate.call_args.args[0]
        self.assertIn("منصب واحد", prompt)
        self.assertIn("التسجيل الإلكتروني", prompt)
        self.assertIn("#CyberoPlus #مباريات_التوظيف", prompt)

    def test_hashtag_stuffing_is_repaired_before_delivery(self):
        old = json.dumps({
            "facebook_post_text": self.caption.replace(
                "#CyberoPlus #مباريات_التوظيف",
                "#CyberoPlus #مباريات_التوظيف #وظائف #المغرب",
            )
        }, ensure_ascii=False)
        new = json.dumps({"facebook_post_text": self.caption}, ensure_ascii=False)
        with patch.object(
            social_ai,
            "_generate_ai_article",
            side_effect=[(old, "gemini:test"), (new, "gemini:test")],
        ) as generate:
            result = social_ai.generate_jobs_facebook_post(self.article)

        self.assertEqual(result["attempts"], 2)
        self.assertNotIn("fallback", result)
        self.assertEqual(result["facebook_post_text"], self.caption)
        self.assertIn("exactly #CyberoPlus", generate.call_args.args[0])

    def test_arabic_only_retry_repairs_the_rejected_copy_without_reexposing_url(self):
        rejected_caption = self.caption.replace(
            "غرفة التجارة والصناعة والخدمات",
            "Chamber of Commerce",
        )
        rejected_caption = rejected_caption.replace(
            "راجع الشروط والوثائق وخطوات التسجيل وإرسال الملف في أول تعليق.",
            "راجع https://example.com/apply في أول تعليق.",
        )
        first = json.dumps(
            {"facebook_post_text": rejected_caption},
            ensure_ascii=False,
        )
        second = json.dumps(
            {"facebook_post_text": self.caption},
            ensure_ascii=False,
        )
        prompts = []

        def fake_generate(prompt, **kwargs):
            prompts.append(prompt)
            return (
                (first, "cloudflare:test")
                if len(prompts) == 1
                else (second, "cloudflare:test")
            )

        with patch.object(social_ai, "_generate_ai_article", side_effect=fake_generate):
            result = social_ai.generate_jobs_facebook_post(self.article)

        self.assertEqual(result["attempts"], 2)
        self.assertIn("ARABIC-ONLY REPAIR", prompts[1])
        self.assertIn("Chamber of Commerce", prompts[1])
        self.assertNotIn("https://example.com/apply", prompts[1])
        self.assertIn("[رابط محذوف]", prompts[1])
        self.assertEqual(result["facebook_post_text"], self.caption)

    def test_cached_legacy_hashtags_are_normalized_not_regenerated(self):
        article = dict(
            self.article,
            facebook_post_source="social_ai",
            facebook_post_text=(
                "📢 فرصة جديدة لمهندس نظم في الدار البيضاء.\n\n"
                "💼 التفاصيل الكاملة والشروط موضحة في الإعلان.\n\n"
                "👇 التفاصيل في أول تعليق.\n"
                "#وظائف #المغرب #تقنية"
            ),
        )
        with patch.object(facebook, "generate_jobs_facebook_post") as generate, \
                patch.object(facebook, "_load_style_memory", return_value={}):
            first = facebook._prepare_facebook_post(
                article, [], article["blogger_post_url"]
            )
            second = facebook._prepare_facebook_post(
                article, [], article["blogger_post_url"]
            )
        generate.assert_not_called()
        self.assertEqual(first["caption"], second["caption"])
        self.assertEqual(
            first["hashtags"],
            ["#CyberoPlus", "#مباريات_التوظيف"],
        )
        self.assertNotIn("#وظائف ", first["caption"])

    def test_foreign_words_and_unreadable_block_are_rejected(self):
        for invalid in (
            self.caption.replace("غرفة التجارة", "Chamber of Commerce"),
            self.caption.replace("\n\n", " "),
        ):
            with self.subTest(caption=invalid):
                with self.assertRaises(social_ai.SocialAIQualityError):
                    social_ai.validate_jobs_facebook_post(
                        invalid, article=self.article
                    )

    def test_notice_type_controls_hashtag_and_first_comment(self):
        expected = {
            "vacancy": ("#وظائف_المغرب", "التفاصيل وشروط وطريقة التقديم"),
            "competition": ("#مباريات_التوظيف", "شروط المباراة والوثائق"),
            "candidate_list": ("#لوائح_المترشحين", "لائحة المترشحين"),
            "results": ("#نتائج_المباريات", "النتائج والتفاصيل"),
            "final_results": ("#النتائج_النهائية", "النتائج النهائية"),
            "update": ("#مستجدات_التوظيف", "تفاصيل المستجد"),
        }
        for stage, (hashtag, comment_label) in expected.items():
            with self.subTest(stage=stage):
                article = dict(self.article, job_notice_type=stage)
                formatted = facebook._format_jobs_facebook_caption(
                    self.caption, article=article
                )
                self.assertTrue(
                    formatted.endswith(f"#CyberoPlus {hashtag}")
                )
                comment = facebook._first_comment_text(
                    article["blogger_post_url"], article=article
                )
                self.assertIn(comment_label, comment)

    def test_results_cta_and_connected_sentences_survive_formatting(self):
        article = dict(self.article, job_notice_type="results")
        raw = (
            "📋 نتائج مباراة المتصرفين صدرت ويمكن مراجعة وضعية الترشيح.\n\n"
            "✅ النتائج الرسمية منشورة لدى الجهة المنظمة.\n\n"
            "👇 للاطلاع على لائحة النتائج والتفاصيل، راجع أول تعليق.\n"
            "#نتائج #وظائف #المغرب"
        )
        formatted = facebook._format_jobs_facebook_caption(raw, article=article)
        self.assertEqual(formatted.split("\n\n")[0], raw.split("\n\n")[0])
        self.assertTrue(
            formatted.endswith("#CyberoPlus #نتائج_المباريات")
        )
        self.assertNotIn("شروط التقديم", formatted)
        self.assertNotIn("#نتائج ", formatted)

    def test_formatter_preserves_verified_decimal_salary(self):
        raw = (
            "📢 وظيفة متاحة لمهندس شبكات بأجر 12.500 درهم.\n"
            "💼 راجع تفاصيل المهام وشروط الترشيح.\n"
            "👇 التفاصيل في أول تعليق."
        )
        self.assertIn(
            "12.500 درهم",
            facebook._format_jobs_facebook_caption(
                raw, article=dict(self.article, job_notice_type="vacancy")
            ),
        )

    def test_fallback_mentions_deadline_once_and_omits_unknown_count(self):
        caption = social_ai._deterministic_jobs_facebook_post(self.article)
        self.assertEqual(caption.count("20 أكتوبر 2026"), 1)
        self.assertNotIn("عدد المناصب المؤكد: 0", caption)
        self.assertTrue(
            caption.endswith("#CyberoPlus #مباريات_التوظيف")
        )
        self.assertNotIn("الوثائق", caption)

    def test_fallback_keeps_notice_stage_without_reopening_applications(self):
        for stage in ("candidate_list", "results", "final_results", "update"):
            with self.subTest(stage=stage):
                article = dict(
                    self.article,
                    job_notice_type=stage,
                    seo_title="مستجدات المباراة لدى غرفة التجارة",
                )
                caption = social_ai._deterministic_jobs_facebook_post(article)
                self.assertNotIn("آخر أجل للترشيح", caption)
                self.assertNotIn("قبل الترشيح", caption)
                self.assertNotIn("فرصة توظيف", caption)
                self.assertIn(social_ai._NOTICE_STAGE_LABELS[stage], caption)
                self.assertTrue(
                    caption.endswith(
                        "#CyberoPlus "
                        + social_ai.jobs_contextual_hashtag(stage)
                    )
                )

    def test_arabic_fallback_preserves_numeric_facts_without_guessing_foreign_names(self):
        article = dict(
            self.article,
            job_company="مكتب التكوين المهني وإنعاش الشغل (OFPPT)",
            seo_title="مباراة مكون في النسيج التقليدي (1)",
            job_location="Errachidia",
        )
        caption = social_ai._deterministic_jobs_facebook_post(article)
        body = re.sub(r"#[\w\u0600-\u06FF_]+", "", caption)
        self.assertIn("(1)", caption)
        self.assertIn("مكتب التكوين المهني وإنعاش الشغل", caption)
        self.assertIsNone(re.search(r"[A-Za-z]", body))
        self.assertNotIn("الرشيدية", caption)

    def test_recent_hook_is_rejected_for_regeneration(self):
        article = dict(
            self.article,
            _facebook_recent_hooks=[
                social_ai._copy_memory_key(self.caption.split("\n\n")[0])
            ],
        )
        with self.assertRaisesRegex(
            social_ai.SocialAIQualityError, "recent opening"
        ):
            social_ai.validate_jobs_facebook_post(
                self.caption, article=article
            )

    def test_comment_reconciliation_accepts_legacy_comment_text(self):
        article = {
            "id": "legacy-comment",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/job.html",
            "facebook_status": "posted_comment_uncertain",
            "facebook_post_id": "page_123",
            "job_notice_type": "results",
        }
        legacy = facebook._legacy_first_comment_text(
            article["blogger_post_url"]
        )
        with patch.object(facebook, "FACEBOOK_PAGE_ACCESS_TOKEN", "token"), \
                patch.object(
                    facebook,
                    "_get_from_graph",
                    return_value={
                        "data": [{"id": "comment-1", "message": legacy}]
                    },
                ), \
                patch.object(facebook, "_persist_jobs_social_state"):
            result = facebook.reconcile_uncertain_facebook_comment(article)

        self.assertTrue(result["resolved"])
        self.assertEqual(article["facebook_comment_id"], "comment-1")


if __name__ == "__main__":
    unittest.main()
