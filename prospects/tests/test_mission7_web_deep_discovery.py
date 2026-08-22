"""Mission 7B — Web Deep Discovery : priorisation d'URLs étendue (blog,
actualités, presse, pages auteurs) et découverte via sitemap.xml (avec
lastmod, quand fourni), en réutilisant robots.txt/httpx du crawler existant —
aucun deuxième moteur de crawl."""
from unittest.mock import Mock, patch

import httpx
from django.test import TestCase

from prospects.services import crawler
from prospects.services import structured_data


class FakePolicy:
    def __init__(self, allow=True, sitemap_urls=None):
        self.allow = allow
        self._sitemaps = sitemap_urls

    def allowed(self, url):
        return self.allow

    def sitemaps(self):
        return self._sitemaps


def _xml_response(content, status_code=200, url="https://exemple.fr/sitemap.xml"):
    r = Mock(spec=httpx.Response)
    r.status_code = status_code
    r.content = content.encode("utf-8")
    r.headers = {"content-type": "application/xml"}
    r.url = url
    r.history = []
    r.iter_bytes = lambda: iter([r.content])
    return r


class _StreamCtx:
    """Audit correctif round 2, §2 — safe_get() lit via client.stream()."""

    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *args):
        return False


class FakeXmlClient:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, *args, **kwargs):
        self.requested.append(url)
        if url in self.pages:
            return self.pages[url]
        raise httpx.HTTPError("not found")

    def stream(self, method, url, **kwargs):
        self.requested.append(url)
        if url in self.pages:
            return _StreamCtx(self.pages[url])
        raise httpx.HTTPError("not found")


class ImportantPathsCoverageTests(TestCase):
    def test_important_page_terms_cover_blog_press_and_author_pages(self):
        for term in ["blog", "actualites", "presse", "auteur"]:
            self.assertIn(term, crawler.IMPORTANT_PAGE_TERMS)

    def test_is_important_url_recognizes_a_press_page(self):
        self.assertTrue(crawler.is_important_url("https://exemple.fr/presse/nouveau-produit"))

    def test_is_important_url_recognizes_a_blog_article(self):
        self.assertTrue(crawler.is_important_url("https://exemple.fr/blog/notre-actualite"))

    def test_is_important_url_does_not_flag_an_unrelated_page(self):
        self.assertFalse(crawler.is_important_url("https://exemple.fr/produits/widget-bleu"))


class SitemapUrlsTests(TestCase):
    def test_reads_a_plain_urlset_with_lastmod(self):
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://exemple.fr/blog/article-1</loc><lastmod>2026-08-10</lastmod></url>
          <url><loc>https://exemple.fr/carrieres/growth</loc><lastmod>2026-08-20</lastmod></url>
        </urlset>"""
        client = FakeXmlClient({"https://exemple.fr/sitemap.xml": _xml_response(xml)})
        with patch.object(crawler.httpx, "Client", return_value=client):
            entries = crawler.sitemap_urls("https://exemple.fr", policy=FakePolicy())

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["url"], "https://exemple.fr/blog/article-1")
        self.assertEqual(entries[0]["lastmod"], "2026-08-10")

    def test_follows_a_one_level_sitemap_index(self):
        index_xml = """<?xml version="1.0"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://exemple.fr/sitemap-blog.xml</loc></sitemap>
        </sitemapindex>"""
        child_xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://exemple.fr/blog/article-2</loc><lastmod>2026-08-15</lastmod></url>
        </urlset>"""
        client = FakeXmlClient({
            "https://exemple.fr/sitemap.xml": _xml_response(index_xml),
            "https://exemple.fr/sitemap-blog.xml": _xml_response(child_xml, url="https://exemple.fr/sitemap-blog.xml"),
        })
        with patch.object(crawler.httpx, "Client", return_value=client):
            entries = crawler.sitemap_urls("https://exemple.fr", policy=FakePolicy())

        self.assertEqual(entries, [{"url": "https://exemple.fr/blog/article-2", "lastmod": "2026-08-15"}])

    def test_respects_robots_txt_disallow(self):
        client = FakeXmlClient({"https://exemple.fr/sitemap.xml": _xml_response("<urlset></urlset>")})
        with patch.object(crawler.httpx, "Client", return_value=client):
            entries = crawler.sitemap_urls("https://exemple.fr", policy=FakePolicy(allow=False))

        self.assertEqual(entries, [])
        self.assertEqual(client.requested, [])

    def test_never_raises_on_malformed_xml(self):
        client = FakeXmlClient({"https://exemple.fr/sitemap.xml": _xml_response("not xml at all")})
        with patch.object(crawler.httpx, "Client", return_value=client):
            entries = crawler.sitemap_urls("https://exemple.fr", policy=FakePolicy())
        self.assertEqual(entries, [])

    def test_never_raises_on_network_error(self):
        client = FakeXmlClient({})
        with patch.object(crawler.httpx, "Client", return_value=client):
            entries = crawler.sitemap_urls("https://exemple.fr", policy=FakePolicy())
        self.assertEqual(entries, [])

    def test_ignores_off_domain_urls_in_sitemap(self):
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://autre-domaine.fr/page</loc><lastmod>2026-08-10</lastmod></url>
        </urlset>"""
        client = FakeXmlClient({"https://exemple.fr/sitemap.xml": _xml_response(xml)})
        with patch.object(crawler.httpx, "Client", return_value=client):
            entries = crawler.sitemap_urls("https://exemple.fr", policy=FakePolicy())
        self.assertEqual(entries, [])

    def test_uses_sitemap_declared_in_robots_txt_when_present(self):
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://exemple.fr/custom-location</loc><lastmod>2026-08-01</lastmod></url>
        </urlset>"""
        client = FakeXmlClient({"https://exemple.fr/custom/sitemap-xyz.xml": _xml_response(xml, url="https://exemple.fr/custom/sitemap-xyz.xml")})
        with patch.object(crawler.httpx, "Client", return_value=client):
            entries = crawler.sitemap_urls(
                "https://exemple.fr",
                policy=FakePolicy(sitemap_urls=["https://exemple.fr/custom/sitemap-xyz.xml"]),
            )
        self.assertEqual(entries, [{"url": "https://exemple.fr/custom-location", "lastmod": "2026-08-01"}])


class StructuredDataJsonLdTests(TestCase):
    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml")

    def test_extracts_a_person_block(self):
        html = """<script type="application/ld+json">
        {"@type": "Person", "name": "Julie Martin", "jobTitle": "Directrice Marketing"}
        </script>"""
        blocks = structured_data.extract_json_ld_blocks(self._soup(html))
        persons = structured_data.find_persons(blocks)
        self.assertEqual(persons, [{
            "full_name": "Julie Martin", "job_title": "Directrice Marketing",
            "profile_url": "", "bio_url": "", "method": "json_ld_person",
        }])

    def test_extracts_employees_nested_in_an_organization(self):
        html = """<script type="application/ld+json">
        {"@type": "Organization", "name": "Acme",
         "employee": [{"@type": "Person", "name": "Marc Dupuis", "jobTitle": "CEO"}]}
        </script>"""
        blocks = structured_data.extract_json_ld_blocks(self._soup(html))
        persons = structured_data.find_persons(blocks)
        self.assertEqual(len(persons), 1)
        self.assertEqual(persons[0]["full_name"], "Marc Dupuis")

    def test_never_raises_on_invalid_json(self):
        html = '<script type="application/ld+json">{not valid json</script>'
        blocks = structured_data.extract_json_ld_blocks(self._soup(html))
        self.assertEqual(blocks, [])

    def test_person_without_a_name_is_ignored(self):
        html = """<script type="application/ld+json">
        {"@type": "Person", "jobTitle": "Growth"}
        </script>"""
        blocks = structured_data.extract_json_ld_blocks(self._soup(html))
        self.assertEqual(structured_data.find_persons(blocks), [])

    def test_dated_article_with_explicit_date_is_extracted(self):
        html = """<script type="application/ld+json">
        {"@type": "BlogPosting", "headline": "On recrute un Growth Manager",
         "datePublished": "2026-08-20"}
        </script>"""
        blocks = structured_data.extract_json_ld_blocks(self._soup(html))
        facts = structured_data.find_dated_content(blocks)
        self.assertEqual(facts, [{
            "content_type": "blogposting", "date_field": "datePublished",
            "date": "2026-08-20", "headline": "On recrute un Growth Manager",
        }])

    def test_article_without_a_date_produces_no_fact(self):
        html = """<script type="application/ld+json">
        {"@type": "Article", "headline": "Sans date"}
        </script>"""
        blocks = structured_data.extract_json_ld_blocks(self._soup(html))
        self.assertEqual(structured_data.find_dated_content(blocks), [])

    def test_meta_published_time_fallback(self):
        html = '<meta property="article:published_time" content="2026-08-18T10:00:00Z">'
        date = structured_data.find_meta_published_time(self._soup(html))
        self.assertEqual(date, "2026-08-18")

    def test_meta_published_time_absent_returns_empty(self):
        html = "<html><body>Rien ici.</body></html>"
        date = structured_data.find_meta_published_time(self._soup(html))
        self.assertEqual(date, "")
