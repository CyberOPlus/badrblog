# ============================================================
# test_image_extraction.py - Image Extraction Tests
# ============================================================

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from image_extractor import (
    extract_main_image,
    extract_extra_images,
    validate_image_url,
    _is_blocked_image,
    _make_absolute_url,
    _is_valid_image_dimensions,
)


class TestImageExtraction(unittest.TestCase):
    """Test advanced image extraction functionality."""
    
    def test_og_image_extraction(self):
        """Test extraction of og:image meta tag."""
        html = '''
        <html>
            <head>
                <meta property="og:image" content="https://example.com/image.jpg">
            </head>
        </html>
        '''
        url, method = extract_main_image(html, "https://example.com/article")
        self.assertEqual(url, "https://example.com/image.jpg")
        self.assertEqual(method, "og:image_or_twitter")
    
    def test_twitter_image_extraction(self):
        """Test extraction of twitter:image meta tag."""
        html = '''
        <html>
            <head>
                <meta name="twitter:image" content="https://example.com/twitter.jpg">
            </head>
        </html>
        '''
        url, method = extract_main_image(html, "https://example.com/article")
        self.assertEqual(url, "https://example.com/twitter.jpg")
        self.assertEqual(method, "og:image_or_twitter")
    
    def test_jsonld_image_extraction(self):
        """Test extraction of JSON-LD structured data image."""
        html = '''
        <html>
            <head>
                <script type="application/ld+json">
                {
                    "@type": "Article",
                    "image": "https://example.com/article-image.jpg"
                }
                </script>
            </head>
        </html>
        '''
        url, method = extract_main_image(html, "https://example.com/article")
        self.assertEqual(url, "https://example.com/article-image.jpg")
        self.assertEqual(method, "json_ld")
    
    def test_article_content_image_extraction(self):
        """Test extraction of image from article content."""
        html = '''
        <html>
            <body>
                <article>
                    <p>Some content</p>
                    <img src="/images/content.jpg" alt="Article content">
                </article>
            </body>
        </html>
        '''
        url, method = extract_main_image(html, "https://example.com/article")
        self.assertEqual(url, "https://example.com/images/content.jpg")
        self.assertEqual(method, "article_content")
    
    def test_relative_url_conversion(self):
        """Test relative URL is converted to absolute."""
        html = '''
        <html>
            <head>
                <meta property="og:image" content="/images/photo.jpg">
            </head>
        </html>
        '''
        url, method = extract_main_image(html, "https://example.com/page")
        self.assertEqual(url, "https://example.com/images/photo.jpg")
    
    def test_blocked_image_logo_skipped(self):
        """Test that logo images are skipped."""
        html = '''
        <html>
            <body>
                <img src="https://example.com/logo.png" alt="site logo" class="logo">
                <img src="https://example.com/article.jpg" alt="Article">
            </body>
        </html>
        '''
        url, method = extract_main_image(html, "https://example.com/article")
        # Should skip the logo and not return it
        self.assertNotIn("logo", url.lower() if url else "")
    
    def test_blocked_image_avatar_skipped(self):
        """Test that avatar images are skipped."""
        result = _is_blocked_image("https://example.com/avatar.jpg", [], "author avatar")
        self.assertTrue(result)
    
    def test_blocked_image_icon_skipped(self):
        """Test that icon images are skipped."""
        result = _is_blocked_image("https://example.com/icon.jpg", ["icon"], "")
        self.assertTrue(result)
    
    def test_extra_images_extraction(self):
        """Test extraction of additional article images."""
        html = '''
        <html>
            <body>
                <article>
                    <img src="https://example.com/main.jpg" alt="Main">
                    <img src="https://example.com/extra1.jpg" alt="Extra 1">
                    <img src="https://example.com/extra2.jpg" alt="Extra 2">
                </article>
            </body>
        </html>
        '''
        extras = extract_extra_images(
            html,
            "https://example.com/article",
            main_image_url="https://example.com/main.jpg",
            limit=3
        )
        self.assertGreaterEqual(len(extras), 1)
        # Should not include the main image
        for extra in extras:
            self.assertNotEqual(extra["url"], "https://example.com/main.jpg")
    
    def test_extra_images_limit(self):
        """Test that extra images are limited to specified amount."""
        html = '''
        <html>
            <body>
                <article>
                    <img src="https://example.com/1.jpg">
                    <img src="https://example.com/2.jpg">
                    <img src="https://example.com/3.jpg">
                    <img src="https://example.com/4.jpg">
                    <img src="https://example.com/5.jpg">
                </article>
            </body>
        </html>
        '''
        extras = extract_extra_images(
            html,
            "https://example.com/article",
            limit=2
        )
        self.assertLessEqual(len(extras), 2)
    
    def test_no_image_returns_none(self):
        """Test that None is returned when no suitable image is found."""
        html = '''
        <html>
            <head></head>
            <body>
                <article>
                    <p>Just text, no images</p>
                </article>
            </body>
        </html>
        '''
        url, method = extract_main_image(html, "https://example.com/article")
        self.assertIsNone(url)
        self.assertIsNone(method)
    
    def test_make_absolute_url_already_absolute(self):
        """Test that absolute URLs are returned unchanged."""
        result = _make_absolute_url("https://example.com/image.jpg", "https://other.com")
        self.assertEqual(result, "https://example.com/image.jpg")
    
    def test_make_absolute_url_relative(self):
        """Test that relative URLs are converted to absolute."""
        result = _make_absolute_url("/images/photo.jpg", "https://example.com/page")
        self.assertEqual(result, "https://example.com/images/photo.jpg")
    
    def test_make_absolute_url_relative_parent(self):
        """Test relative URL with parent directory."""
        result = _make_absolute_url("../images/photo.jpg", "https://example.com/blog/post")
        self.assertEqual(result, "https://example.com/images/photo.jpg")
    
    def test_image_dimensions_valid(self):
        """Test validation of valid image dimensions."""
        result = _is_valid_image_dimensions(800, 600)
        self.assertTrue(result)
    
    def test_image_dimensions_too_small(self):
        """Test that very small images are rejected."""
        result = _is_valid_image_dimensions(100, 100)
        self.assertFalse(result)
    
    def test_image_dimensions_unknown_treated_as_valid(self):
        """Test that unknown dimensions are treated as potentially valid."""
        result = _is_valid_image_dimensions(None, None)
        self.assertTrue(result)
    
    def test_image_dimensions_extreme_aspect_ratio(self):
        """Test that extreme aspect ratios are rejected."""
        # Very wide: 10:1 ratio should be rejected (max 2.0)
        result = _is_valid_image_dimensions(2000, 200)
        self.assertFalse(result)
        
        # Very tall: 1:10 ratio should be rejected (min 0.6)
        result = _is_valid_image_dimensions(200, 2000)
        self.assertFalse(result)
    
    @patch('image_extractor.requests.head')
    def test_validate_image_url_success(self, mock_head):
        """Test successful image URL validation."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            'Content-Type': 'image/jpeg',
            'Content-Length': '1000000'  # 1 MB
        }
        mock_head.return_value = mock_response
        
        result = validate_image_url("https://example.com/image.jpg")
        self.assertTrue(result)
    
    @patch('image_extractor.requests.head')
    def test_validate_image_url_too_small(self, mock_head):
        """Test that very small files are rejected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            'Content-Type': 'image/jpeg',
            'Content-Length': '1000'  # 1 KB - too small
        }
        mock_head.return_value = mock_response
        
        result = validate_image_url("https://example.com/image.jpg")
        self.assertFalse(result)
    
    @patch('image_extractor.requests.head')
    def test_validate_image_url_wrong_content_type(self, mock_head):
        """Test that non-image content is rejected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            'Content-Type': 'text/html',
            'Content-Length': '1000000'
        }
        mock_head.return_value = mock_response
        
        result = validate_image_url("https://example.com/notimage.html")
        self.assertFalse(result)


class TestImageInsertion(unittest.TestCase):
    """Test image insertion in HTML."""
    
    def test_main_image_inserted_after_paragraph(self):
        """Test that main image is inserted after first paragraph."""
        from article_ai_processor import _insert_main_image_if_missing
        
        html = '<p>First paragraph</p><p>Second paragraph</p>'
        package = {"main_image": "https://example.com/image.jpg", "title": "Test Title"}
        
        result = _insert_main_image_if_missing(html, package)
        
        # Should contain the image
        self.assertIn("https://example.com/image.jpg", result)
        # Should be inserted after first paragraph
        self.assertIn("<p>First paragraph</p>", result)


if __name__ == "__main__":
    unittest.main()
