import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

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
