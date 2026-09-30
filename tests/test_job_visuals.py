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

    def test_visual_template_reselects_when_job_facts_change(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "visual.json"
            article = {
                "id": "job-changing",
                "job_notice_type": "vacancy",
                "facebook_template_key": "apply",
                "facebook_template_file": "job-apply-red.png",
                "facebook_template_reason": "active-vacancy-with-direct-apply",
            }
            article["job_notice_type"] = "candidate_list"
            selected = visual_policy.choose_job_template(article, state)
            self.assertEqual(selected["key"], "alert")
            self.assertFalse(selected["pinned"])
            self.assertEqual(article["facebook_template_key"], "alert")
            self.assertEqual(article["facebook_template_reselected_from"], "apply")

    def test_facebook_renderer_stress_tests_all_templates_and_title_shapes(self):
        titles = (
            "مدير استشارات الأمن السيبراني",
            "مهندس بنية تحتية لافتراضية الشبكات",
            "Développeur Fullstack Java Angular Confirmé",
            "Salesforce/CRM Business Analyst",
            "خبير الأمن السيبراني IAM وإدارة الهوية والوصول",
            "Administrative Associate G-6 Temporary Appointment 364 days",
            "النتائج النهائية لمباراة توظيف مهندسين وتقنيين من عدة تخصصات",
            "لوائح المدعوين لاجتياز الاختبار الكتابي لمباراة توظيف تقنيين متخصصين",
        )
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            logo_image = Image.new("RGBA", (480, 160), (20, 80, 170, 255))
            with patch.object(visuals, "JOB_VISUAL_STATE_PATH", temp / "visual_state.json"), \
                 patch.object(visuals, "_load_job_logo", return_value=logo_image):
                render_index = 0
                for template_key in visual_policy.JOB_TEMPLATE_KEYS:
                    for title in titles:
                        result = visuals._generate_job_facebook_image(
                            title,
                            "https://example.com/verified-logo.png",
                            temp / f"facebook-{render_index}.jpg",
                            employer_name="وزارة الانتقال الرقمي وإصلاح الإدارة",
                            template_key=template_key,
                        )
                        render_index += 1
                        self.assertTrue(
                            result["ok"],
                            f"{template_key}: {title}: {result.get('error')}",
                        )
                        self.assertTrue(result.get("layout_valid"))
                        self.assertEqual(result.get("template_key"), template_key)
                        self.assertGreaterEqual(
                            int(result.get("title_font_size") or 0),
                            visuals.JOB_TITLE_MIN_SIZE,
                        )
                        self.assertLessEqual(
                            int(result.get("title_lines") or 0),
                            visuals.JOB_TITLE_MAX_LINES,
                        )
                        left, top, right, bottom = result["title_bbox"]
                        self.assertGreaterEqual(left, visuals.JOB_CONTENT_LEFT)
                        self.assertLessEqual(right, visuals.JOB_CONTENT_RIGHT)
                        self.assertGreaterEqual(top, visuals.JOB_TITLE_TOP)
                        self.assertLessEqual(bottom, visuals.JOB_TITLE_BOTTOM)
                        self.assertEqual(result.get("logo_kind"), "logo")
                        with Image.open(result["path"]) as image:
                            self.assertEqual(image.size, (1080, 1350))

    def test_job_visual_title_removes_duplicate_company_and_location(self):
        article = {
            "job_notice_type": "vacancy",
            "seo_title": "Orange Business توظف مديرًا لاستشارات الأمن السيبراني بالدار البيضاء",
            "job_title": "Manager - Cybersecurity Consulting (GRC)",
            "job_company": "Orange Business",
            "job_location": "Casablanca",
        }
        visual_title = facebook_publisher._job_visual_title(article)
        self.assertEqual(visual_title, "مديرًا لاستشارات الأمن السيبراني")
        self.assertNotIn("Orange Business", visual_title)
        self.assertNotIn("الدار البيضاء", visual_title)

        article = {
            "job_notice_type": "vacancy",
            "seo_title": "وظيفة مدير تقني ServiceNow لدى inwi في الدار البيضاء",
            "job_title": "Technical Lead ServiceNow",
            "job_company": "inwi",
            "job_location": "Casablanca",
        }
        self.assertEqual(
            facebook_publisher._job_visual_title(article),
            "مدير تقني ServiceNow",
        )

    def test_job_visual_title_compacts_long_administrative_suffix(self):
        article = {
            "job_notice_type": "vacancy",
            "seo_title": (
                "Administrative Associate, G-6, Temporary Appointment, "
                "364 days, Cox's Bazar, Bangladesh"
            ),
            "job_title": "Administrative Associate",
            "job_company": "UNICEF",
            "job_location": "Cox's Bazar",
        }
        self.assertEqual(
            facebook_publisher._job_visual_title(article),
            "Administrative Associate",
        )

    def test_facebook_logo_scaling_uses_visible_mark_not_source_padding(self):
        horizontal = Image.new("RGBA", (1200, 500), (0, 0, 0, 0))
        for x in range(350, 850):
            for y in range(205, 295):
                horizontal.putpixel((x, y), (20, 80, 170, 255))
        prepared = visuals._prepare_facebook_job_logo(horizontal)
        self.assertIsNotNone(prepared)
        self.assertGreaterEqual(prepared.width, 540)
        self.assertLessEqual(prepared.width, 610)
        self.assertLessEqual(prepared.height, 205)

        square = Image.new("RGBA", (700, 700), (0, 0, 0, 0))
        for x in range(275, 425):
            for y in range(275, 425):
                square.putpixel((x, y), (160, 30, 80, 255))
        prepared_square = visuals._prepare_facebook_job_logo(square)
        self.assertIsNotNone(prepared_square)
        self.assertGreaterEqual(prepared_square.width, 280)
        self.assertLessEqual(prepared_square.width, 315)
        self.assertLessEqual(prepared_square.height, 305)

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
            logo_image = Image.new("RGBA", (480, 160), (20, 80, 170, 255))
            with patch.object(article_draft_publisher, "JOB_ARTICLE_COVER_DIR", temp), \
                 patch.object(article_draft_publisher, "_persist_generated_job_cover", return_value=False), \
                 patch.object(visuals, "_load_job_logo", return_value=logo_image):
                article = {
                    "id": "job-1",
                    "desired_slug": "servicenow-inwi",
                    "job_title": "Technical Lead ServiceNow",
                    "seo_title": "وظيفة مدير تقني ServiceNow لدى inwi في الدار البيضاء",
                    "job_company": "inwi",
                    "job_location": "Casablanca",
                    "company_logo_url": "https://example.com/verified-logo.png",
                    "company_logo_verified": True,
                    "company_logo_confidence": 99,
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
        self.assertEqual(
            facebook_publisher._job_visual_title(article),
            "مدير تقني ServiceNow",
        )

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

            logo_image = Image.new("RGBA", (480, 160), (20, 80, 170, 255))
            with patch.object(visuals, "_load_job_logo", return_value=logo_image):
                result = visuals.generate_job_article_cover(
                    "فرصة توظيف Développeur Full Stack بمدينة Casablanca",
                    "https://example.com/verified-logo.png",
                    temp / "article-cover.jpg",
                    employer_name="Example Company",
                    template_path=template,
                )
            self.assertTrue(result["ok"], result.get("error"))
            self.assertTrue(result.get("logo_loaded"))
            with Image.open(result["path"]) as image:
                self.assertEqual(image.size, (1200, 675))

    def test_job_visuals_refuse_missing_verified_logo(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            template = temp / "template.png"
            Image.new("RGB", (1200, 675), "white").save(template)

            article_result = visuals.generate_job_article_cover(
                "وظيفة اختبار",
                "",
                temp / "article-cover.jpg",
                employer_name="Example Company",
                template_path=template,
            )
            self.assertFalse(article_result["ok"])
            self.assertIn("verified employer logo", article_result.get("error", ""))

            facebook_result = visuals._generate_job_facebook_image(
                "وظيفة اختبار",
                "",
                temp / "facebook-cover.jpg",
                employer_name="Example Company",
                template_key=visual_policy.JOB_TEMPLATE_KEYS[0],
            )
            self.assertFalse(facebook_result["ok"])
            self.assertIn("verified employer logo", facebook_result.get("error", ""))

    def test_missing_verified_logo_is_optional_for_article_publishing(self):
        article = {
            "id": "job-no-logo",
            "job_title": "مهندس نظم",
            "job_company": "Unknown Employer",
            "seo_title": "فرصة توظيف مهندس نظم لدى Unknown Employer",
            "ai_input_package": {
                "job_title": "مهندس نظم",
                "job_company": "Unknown Employer",
            },
        }
        with (
            patch.object(article_draft_publisher, "JOBS_MODE", True),
            patch.object(
                article_draft_publisher,
                "verified_company_logo",
                return_value={},
            ),
            patch.object(
                article_draft_publisher,
                "refresh_company_logo",
                return_value={
                    "company_logo_url": "",
                    "company_logo_verified": False,
                },
            ),
        ):
            cover = article_draft_publisher._prepare_job_article_cover(article)

        self.assertEqual(cover, "")
        self.assertEqual(
            article["logo_resolution_status"],
            "unavailable_optional",
        )
        self.assertEqual(
            article["job_article_cover_status"],
            "skipped_missing_verified_logo",
        )
        self.assertFalse(article["article_logo_used"])
        self.assertNotIn("publish_block_reason", article)

    def test_verified_logo_render_failure_is_optional_for_article_publishing(self):
        article = {
            "id": "job-logo-render-fail",
            "job_title": "مهندس نظم",
            "job_company": "Example Company",
            "seo_title": "فرصة توظيف مهندس نظم لدى Example Company",
            "ai_input_package": {
                "job_title": "مهندس نظم",
                "job_company": "Example Company",
            },
        }
        with (
            patch.object(article_draft_publisher, "JOBS_MODE", True),
            patch.object(
                article_draft_publisher,
                "verified_company_logo",
                return_value={
                    "company_logo_url": "https://example.com/logo.png",
                    "company_logo_verified": True,
                },
            ),
            patch.object(
                article_draft_publisher,
                "generate_job_article_cover",
                return_value={
                    "ok": False,
                    "logo_loaded": False,
                    "error": "temporary render error",
                },
            ),
        ):
            cover = article_draft_publisher._prepare_job_article_cover(article)

        self.assertEqual(cover, "")
        self.assertEqual(
            article["job_article_cover_status"],
            "render_retry_optional",
        )
        self.assertTrue(article["logo_visual_retry_pending"])
        self.assertNotIn("publish_block_reason", article)

    def test_job_cover_git_push_retries_after_concurrent_commit(self):
        def completed(returncode=0, stdout="", stderr=""):
            return __import__("subprocess").CompletedProcess(
                args=[],
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

        responses = [
            completed(),  # git config name
            completed(),  # git config email
            completed(),  # git add
            completed(returncode=1),  # cached diff exists
            completed(),  # git commit
            completed(returncode=1, stderr="non-fast-forward"),  # first push
            completed(),  # pull --rebase with autostash
            completed(),  # second push
        ]
        with tempfile.TemporaryDirectory() as temp:
            cover = Path(temp) / "cover.jpg"
            cover.write_bytes(b"image")
            with patch.object(article_draft_publisher.os, "getenv", return_value="true"), \
                 patch.object(article_draft_publisher.subprocess, "run", side_effect=responses) as run:
                self.assertTrue(
                    article_draft_publisher._persist_generated_job_cover(cover)
                )

        self.assertEqual(run.call_count, 8)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            [
                "git",
                "-c", "rebase.autoStash=true",
                "pull", "--rebase", "origin", "main",
            ],
            commands,
        )



if __name__ == "__main__":
    unittest.main()
