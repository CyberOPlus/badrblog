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
            "تحمل شهادة الماستر أو ما يعادلها؟ مباراة لمتصرف بسوس ماسة.\n\n"
            "غرفة التجارة والصناعة والخدمات لجهة سوس ماسة تعلن عن منصب واحد "
            "لمتصرف من الدرجة الثالثة (سلم 10).\n\n"
            "آخر أجل للترشيح: 20 أكتوبر 2026.\n\n"
            "راجع الشروط والوثائق وخطوات التسجيل وإرسال الملف عبر رابط المقال "
            "في أول تعليق 👇"
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
        self.assertEqual(blueprint["hashtags"], [])
        self.assertEqual(blueprint["cta"], self.caption.split("\n\n")[-1])
        self.assertNotIn("http", blueprint["caption"])
        self.assertTrue(all(
            line.startswith("\u200f")
            for line in blueprint["caption"].splitlines() if line.strip()
        ))
        self.assertIn(
            self.article["blogger_post_url"],
            facebook._first_comment_text(self.article["blogger_post_url"]),
        )
        prompt = generate.call_args.args[0]
        self.assertIn("منصب واحد", prompt)
        self.assertIn("التسجيل الإلكتروني", prompt)

    def test_old_hashtag_copy_is_repaired_before_delivery(self):
        old = json.dumps({"facebook_post_text": self.caption + "\n#وظائف #المغرب #توظيف"})
        new = json.dumps({"facebook_post_text": self.caption})
        with patch.object(
            social_ai, "_generate_ai_article",
            side_effect=[(old, "gemini:test"), (new, "gemini:test")],
        ) as generate:
            result = social_ai.generate_jobs_facebook_post(self.article)

        self.assertEqual(result["attempts"], 2)
        self.assertNotIn("fallback", result)
        self.assertEqual(result["facebook_post_text"], self.caption)
        self.assertIn("must not contain hashtags", generate.call_args.args[0])

    def test_foreign_words_and_unreadable_block_are_rejected(self):
        for invalid in (
            self.caption.replace("غرفة التجارة", "Chamber of Commerce"),
            self.caption.replace("\n\n", " "),
        ):
            with self.subTest(caption=invalid):
                with self.assertRaises(social_ai.SocialAIQualityError):
                    social_ai.validate_jobs_facebook_post(invalid, article=self.article)

    def test_results_cta_and_connected_sentences_survive_formatting(self):
        raw = (
            "نتائج مباراة المتصرفين صدرت. يمكنك مراجعة وضعية ترشيحك.\n\n"
            "النتائج الرسمية منشورة لدى الجهة المنظمة.\n\n"
            "للاطلاع على لائحة النتائج، تجد رابط المقال في أول تعليق 👇\n"
            "#نتائج #وظائف #المغرب"
        )
        formatted = facebook._format_jobs_facebook_caption(raw)
        self.assertEqual(formatted.split("\n\n")[0], raw.split("\n\n")[0])
        self.assertTrue(formatted.endswith(
            "للاطلاع على لائحة النتائج، تجد رابط المقال في أول تعليق 👇"
        ))
        self.assertNotIn("شروط التقديم", formatted)
        self.assertNotIn("#", formatted)

    def test_formatter_preserves_verified_decimal_salary(self):
        raw = (
            "وظيفة متاحة لمهندس شبكات بأجر 12.500 درهم.\n"
            "راجع تفاصيل المهام وشروط الترشيح في المقال.\n"
            "رابط المقال في أول تعليق 👇"
        )
        self.assertIn("12.500 درهم", facebook._format_jobs_facebook_caption(raw))

    def test_fallback_mentions_deadline_once_and_omits_unknown_count(self):
        caption = social_ai._deterministic_jobs_facebook_post(self.article)
        self.assertEqual(caption.count("20 أكتوبر 2026"), 1)
        self.assertNotIn("عدد المناصب 0", caption)
        self.assertNotIn("#", caption)
        self.assertNotIn("الوثائق", caption)  # The fallback does not invent article contents.

    def test_fallback_keeps_notice_stage_without_reopening_applications(self):
        for stage in ("candidate_list", "results", "final_results", "update"):
            with self.subTest(stage=stage):
                article = dict(self.article, job_notice_type=stage,
                               seo_title="مستجدات المباراة لدى غرفة التجارة")
                caption = social_ai._deterministic_jobs_facebook_post(article)
                self.assertNotIn("آخر أجل للترشيح", caption)
                self.assertNotIn("قبل تقديم ترشيحك", caption)
                self.assertNotIn("فرصة توظيف", caption)
                self.assertIn(social_ai._NOTICE_STAGE_LABELS[stage], caption)

    def test_arabic_fallback_preserves_numeric_facts_without_guessing_foreign_names(self):
        article = dict(self.article,
                       job_company="مكتب التكوين المهني وإنعاش الشغل (OFPPT)",
                       seo_title="مباراة مكون في النسيج التقليدي (1)",
                       job_location="Errachidia")
        caption = social_ai._deterministic_jobs_facebook_post(article)
        self.assertIn("(1)", caption)
        self.assertIn("مكتب التكوين المهني وإنعاش الشغل", caption)
        self.assertIsNone(re.search(r"[A-Za-z]", caption))
        self.assertNotIn("الرشيدية", caption)  # No guessed translation in the fallback.


if __name__ == "__main__":
    unittest.main()
