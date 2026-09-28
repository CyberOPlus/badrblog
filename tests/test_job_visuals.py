import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import article_ai_processor
import article_draft_publisher
import utils.facebook_image_generator as visuals


class JobVisualTests(unittest.TestCase):
    def test_cairo_font_is_loaded(self):
        font = visuals._font(42)
        family = " ".join(str(x) for x in getattr(font, "getname", lambda: ("", ""))())
        self.assertIn("Cairo", family)

    def test_four_facebook_templates_keep_portrait_size(self):
        self.assertEqual(len(visuals.JOB_TEMPLATE_FILES), 4)
        for path in visuals.JOB_TEMPLATE_FILES:
            self.assertTrue(path.exists(), str(path))
            with Image.open(path) as image:
                self.assertEqual(image.size, (1080, 1350))

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
                    )
                    self.assertTrue(result["ok"], result.get("error"))
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
            "main_image": "https://raw.githubusercontent.com/CyberOPlus/badrblog/main/assets/generated/job-articles/servicenow-inwi.jpg",
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
                self.assertEqual(
                    article["article_images"][0]["alt"],
                    "وظيفة Technical Lead ServiceNow لدى inwi في Casablanca",
                )
                self.assertEqual(
                    article["ai_input_package"]["cover_alt"],
                    "وظيفة Technical Lead ServiceNow لدى inwi في Casablanca",
                )
                self.assertTrue((temp / "servicenow-inwi.jpg").exists())


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
