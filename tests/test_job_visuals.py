import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import article_ai_processor
import article_draft_publisher
import facebook_publisher
import job_visual_policy as visual_policy
import utils.facebook_image_generator as visuals


class JobVisualTests(unittest.TestCase):
    def test_firjar_font_is_loaded(self):
        font = visuals._font(42)
        family = " ".join(str(x) for x in getattr(font, "getname", lambda: ("", ""))())
        self.assertIn("Firjar", family)

    def test_four_facebook_templates_use_new_named_assets(self):
        self.assertEqual(
            [path.name for path in visuals.JOB_TEMPLATE_FILES],
            [
                "job-new-orange.png",
                "job-deadline-yellow.png",
                "job-alert-blue.png",
                "job-apply-red.png",
            ],
        )
        for path in visuals.JOB_TEMPLATE_FILES:
            self.assertTrue(path.exists(), str(path))
            with Image.open(path) as image:
                self.assertEqual(image.size, (1254, 1254))

    def test_visual_policy_uses_job_facts_not_random_icons(self):
        now = __import__("datetime").datetime(2026, 9, 29, 12, tzinfo=__import__("datetime").timezone.utc)

        keys, _ = visual_policy.semantic_template_candidates(
            {"job_notice_type": "candidate_list"},
            now=now,
        )
        self.assertEqual(keys, ("alert",))

        keys, _ = visual_policy.semantic_template_candidates(
            {
                "job_notice_type": "vacancy",
                "job_deadline": "2026-09-30",
                "job_application_url": "https://example.com/apply/42",
            },
            now=now,
        )
        self.assertEqual(keys, ("deadline",))

        keys, _ = visual_policy.semantic_template_candidates(
            {
                "job_notice_type": "vacancy",
                "job_application_url": "https://example.com/apply/42",
                "job_application_link_kind": "direct_apply",
            },
            now=now,
        )
        self.assertEqual(keys, ("new", "apply"))

    def test_visual_template_is_pinned_for_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "visual.json"
            article = {
                "id": "job-42",
                "job_notice_type": "vacancy",
                "job_application_url": "https://example.com/apply/42",
                "job_application_link_kind": "direct_apply",
            }
            first = visual_policy.choose_job_template(article, state)
            second = visual_policy.choose_job_template(article, state)
            self.assertEqual(first["key"], second["key"])
            self.assertTrue(second["pinned"])
            self.assertEqual(article["facebook_template_key"], first["key"])

    def test_facebook_renderer_handles_varied_job_titles(self):
        titles = (
            "مطلوب تقنيو صيانة بالدار البيضاء",
            "Maroc Telecom recrute des Techniciens Réseaux",
            "فرص توظيف مهندسين ومطورين لدى شركة دولية في الرباط والدار البيضاء",
        )
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            with patch.object(visuals, "JOB_VISUAL_STATE_PATH", temp / "visual_state.json"):
                for index, title in enumerate(titles):
                    result = visuals._generate_job_facebook_image(
                        title,
                        "",
                        temp / f"facebook-{index}.jpg",
                        hook_text="OCP Group",
                        template_key="apply",
                    )
                    self.assertTrue(result["ok"], result.get("error"))
                    self.assertEqual(result.get("template_key"), "apply")
                    with Image.open(result["path"]) as image:
                        self.assertEqual(image.size, (1080, 1350))

    def test_jobs_html_keeps_exactly_one_generated_cover(self):
        html = (
            "<p>مقدمة قصيرة عن الوظيفة.</p>"
            "<img src='https://source.example.com/hero.jpg' alt='source'/>"
            "<h2>التفاصيل</h2><p>تفاصيل موثقة.</p>"
        )
        package = {
            "title": "Technical Lead ServiceNow",
            "cover_alt": "Technical Lead ServiceNow - inwi",
            "main_image": "https://example.com/assets/generated/job-articles/servicenow-inwi.jpg",
            "cover_width": 1200,
            "cover_height": 675,
            "extra_article_images": [
                {"url": "https://source.example.com/extra.jpg", "alt": "extra"}
            ],
        }
        output = article_ai_processor.format_phase3_article_html(html, package)
        self.assertEqual(output.count("<img"), 1)
        self.assertIn(package["main_image"], output)
        self.assertNotIn("source.example.com", output)
        self.assertIn("Technical Lead ServiceNow - inwi", output)
        self.assertNotIn("<figcaption>", output)

    def test_prepare_job_cover_updates_article_with_one_cover(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            with patch.object(article_draft_publisher, "JOB_ARTICLE_COVER_DIR", temp), \
                 patch.object(article_draft_publisher, "_persist_generated_job_cover", return_value=False):
                article = {
                    "id": "job-1",
                    "desired_slug": "servicenow-inwi",
                    "job_title": "Technical Lead ServiceNow",
                    "seo_title": "وظيفة مدير تقني ServiceNow لدى inwi في الدار البيضاء",
                    "job_company": "inwi",
                    "job_location": "Casablanca",
                    "company_logo_url": "",
                    "ai_input_package": {
                        "job_title": "Technical Lead ServiceNow",
                        "job_company": "inwi",
                        "job_location": "Casablanca",
                    },
                }
                url = article_draft_publisher._prepare_job_article_cover(article)
                self.assertTrue(url.endswith("/servicenow-inwi.jpg"))
                self.assertEqual(len(article["article_images"]), 1)
                self.assertEqual(article["main_image"], url)
                self.assertEqual(article["extra_article_images"], [])
                self.assertIn("مدير تقني ServiceNow", article["article_images"][0]["alt"])
                self.assertIn("مدير تقني ServiceNow", article["ai_input_package"]["cover_alt"])
                self.assertTrue((temp / "servicenow-inwi.jpg").exists())


    def test_jobs_facebook_never_uses_article_cover_as_employer_logo(self):
        article = {
            "company_logo_url": "",
            "company_logo_verified": False,
            "main_image": "https://example.com/generated-job-cover.jpg",
        }
        self.assertEqual(facebook_publisher._main_image_url(article), "")

        article["company_logo_url"] = "https://example.com/verified-logo.png"
        article["company_logo_verified"] = True
        self.assertEqual(
            facebook_publisher._main_image_url(article),
            "https://example.com/verified-logo.png",
        )

    def test_jobs_facebook_uses_translated_reader_facing_title(self):
        article = {
            "job_title": "Technical Lead ServiceNow",
            "seo_title": "وظيفة مدير تقني ServiceNow لدى inwi في الدار البيضاء",
            "job_company": "inwi",
            "job_location": "الدار البيضاء",
        }
        blueprint = facebook_publisher._jobs_facebook_blueprint(
            article,
            "https://example.blogspot.com/test.html",
        )
        self.assertIn("مدير تقني ServiceNow", blueprint["caption"])
        self.assertNotIn("💼 الوظيفة: Technical Lead ServiceNow", blueprint["caption"])

    def test_article_logo_trims_transparent_padding_and_scales_up(self):
        image = Image.new("RGBA", (600, 300), (0, 0, 0, 0))
        for x in range(260, 340):
            for y in range(120, 180):
                image.putpixel((x, y), (180, 0, 120, 255))

        prepared = visuals._prepare_article_job_logo(image, (360, 120))
        self.assertIsNotNone(prepared)
        self.assertGreater(prepared.width, 80)
        self.assertGreater(prepared.height, 60)
        self.assertLessEqual(prepared.width, 360)
        self.assertLessEqual(prepared.height, 120)

    def test_article_renderer_handles_landscape_template_and_mixed_title(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            template = temp / "template.png"
            Image.new("RGB", (1200, 675), "white").save(template)

            result = visuals.generate_job_article_cover(
                "فرصة توظيف Développeur Full Stack بمدينة Casablanca",
                "",
                temp / "article-cover.jpg",
                employer_name="Example Company",
                template_path=template,
            )
            self.assertTrue(result["ok"], result.get("error"))
            with Image.open(result["path"]) as image:
                self.assertEqual(image.size, (1200, 675))


if __name__ == "__main__":
    unittest.main()
