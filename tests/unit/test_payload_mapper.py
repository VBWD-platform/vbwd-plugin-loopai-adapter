"""S95.1 RED — LoopAiPayloadMapper: WP field-guessing, images, byte-stable HTML."""
import base64
from importlib import import_module

import pytest

mapper_module = import_module("plugins.loopai_adapter.loopai_adapter.mapper")
LoopAiPayloadMapper = mapper_module.LoopAiPayloadMapper


@pytest.fixture
def mapper():
    return LoopAiPayloadMapper()


def _map(mapper, payload):
    return mapper.map(payload, default_status="published", default_post_type="post")


@pytest.mark.parametrize("messy_key", ["article__body", "ARTICLE_BODY", "articleBody"])
def test_guesses_article_body_from_messy_key(mapper, messy_key):
    result = _map(mapper, {"title": "T", messy_key: "the body text"})
    assert "<div>the body text</div>" in result.ingest_payload["content_html"]


def test_categories_hierarchy_subcat_from_sub_category(mapper):
    """New contract: parent 'blog' + a subcategory child taken from sub_category."""
    result = _map(mapper, {"title": "T", "sub_category": "AI", "category": "Plain"})
    assert result.ingest_payload["categories"] == [
        {"name": "blog"},
        {"name": "AI", "parent": "blog"},
    ]


def test_categories_hierarchy_falls_back_to_category(mapper):
    """No sub_category → the subcategory is the first comma-split of ``category``."""
    result = _map(mapper, {"title": "T", "category": "News, Tech"})
    assert result.ingest_payload["categories"] == [
        {"name": "blog"},
        {"name": "News", "parent": "blog"},
    ]


def test_categories_empty_sub_category_falls_back_to_category(mapper):
    result = _map(mapper, {"title": "T", "sub_category": "  ", "category": "Tech"})
    assert result.ingest_payload["categories"] == [
        {"name": "blog"},
        {"name": "Tech", "parent": "blog"},
    ]


def test_categories_blog_only_when_no_subcategory(mapper):
    result = _map(mapper, {"title": "T"})
    assert result.ingest_payload["categories"] == [{"name": "blog"}]


def test_categories_parent_configurable(mapper):
    result = mapper.map(
        {"title": "T", "sub_category": "AI"},
        default_status="published",
        default_post_type="post",
        default_parent_category="journal",
    )
    assert result.ingest_payload["categories"] == [
        {"name": "journal"},
        {"name": "AI", "parent": "journal"},
    ]


def test_excerpt_from_summary_html_stripped(mapper):
    result = _map(mapper, {"title": "T", "summary": "<p>Hello <b>world</b></p>"})
    assert result.ingest_payload["excerpt"] == "Hello world"


def test_excerpt_falls_back_to_lead_paragraph(mapper):
    result = _map(mapper, {"title": "T", "lead_paragraph": "Lead text here"})
    assert result.ingest_payload["excerpt"] == "Lead text here"


def test_excerpt_truncated_on_word_boundary(mapper):
    long_summary = "word " * 100  # ~500 chars once collapsed
    result = _map(mapper, {"title": "T", "summary": long_summary})
    excerpt = result.ingest_payload["excerpt"]
    assert len(excerpt) <= 300
    # No mid-word cut: every token is the whole word.
    assert set(excerpt.split()) == {"word"}


def test_no_excerpt_when_no_source_text(mapper):
    result = _map(mapper, {"title": "T"})
    assert "excerpt" not in result.ingest_payload


def test_seo_titles_equal_the_title(mapper):
    result = _map(mapper, {"title": "My Headline", "summary": "S"})
    seo = result.ingest_payload["seo"]
    assert seo["meta_title"] == "My Headline"
    assert seo["og_title"] == "My Headline"


def test_seo_description_derived_from_summary_and_truncated(mapper):
    long_summary = "alpha " * 60  # ~360 chars once collapsed
    result = _map(mapper, {"title": "T", "summary": long_summary})
    seo = result.ingest_payload["seo"]
    assert seo["meta_description"] == seo["og_description"]
    assert len(seo["meta_description"]) <= 160
    assert set(seo["meta_description"].split()) == {"alpha"}


def test_seo_description_falls_back_to_lead_paragraph_and_strips_html(mapper):
    result = _map(mapper, {"title": "T", "lead_paragraph": "Lead <i>text</i>"})
    seo = result.ingest_payload["seo"]
    assert seo["meta_description"] == "Lead text"
    assert seo["og_description"] == "Lead text"


def test_seo_description_omitted_when_no_source_but_titles_present(mapper):
    result = _map(mapper, {"title": "Only title"})
    seo = result.ingest_payload["seo"]
    assert seo["meta_title"] == "Only title"
    assert "meta_description" not in seo
    assert "og_description" not in seo


def test_seo_does_not_set_canonical_or_robots(mapper):
    result = _map(mapper, {"title": "T", "summary": "S"})
    seo = result.ingest_payload["seo"]
    assert "canonical_url" not in seo
    assert "robots" not in seo


def test_recursive_image_extraction_in_document_order(mapper):
    payload = {
        "title": "T",
        "wrap": {
            "first": {"image_base64": "AAAA", "image_file": "a.png"},
            "deeper": [
                {"image_base64": "BBBB", "image_file": "b.png"},
            ],
        },
        "tail": {"image_base64": "CCCC", "image_file": "c.png"},
    }
    result = _map(mapper, payload)
    # Featured image is the first node found (depth-first, document order).
    assert result.featured_image == {"base64": "AAAA", "filename": "a.png"}
    html = result.ingest_payload["content_html"]
    # The featured image (index 0) is NOT embedded; the others are, by filename.
    assert "a.png" not in html
    assert "b.png" in html
    assert "c.png" in html


def test_node_missing_image_file_is_not_extracted(mapper):
    result = _map(mapper, {"title": "T", "image_base64": "AAAA"})
    assert result.featured_image is None


def test_zero_images_means_no_featured_and_no_img_tag(mapper):
    result = _map(mapper, {"title": "T", "summary": "S", "article_body": "B"})
    assert result.featured_image is None
    assert "<img" not in result.ingest_payload["content_html"]


def test_html_byte_equals_wp_template(mapper):
    payload = {
        "title": "Hello World",
        "summary": "The summary",
        "lead_paragraph": "The lead",
        "article_body": "<p>Body</p>",
        "images": [
            {"image_base64": "AAAA", "image_file": "featured.jpg"},
            {"image_base64": "BBBB", "image_file": "second.jpg"},
        ],
    }
    result = _map(mapper, payload)
    expected = (
        "<strong>The summary</strong>"
        "<p>The lead</p>"
        "<div><p>Body</p></div>"
        '<img src="second.jpg" alt="Hello World" '
        'style="max-width:100%; height:auto;" />'
    )
    assert result.ingest_payload["content_html"] == expected


def test_missing_title_signals_invalid(mapper):
    result = _map(mapper, {"summary": "no title here"})
    assert result.valid is False


def test_valid_payload_carries_status_type_and_tags(mapper):
    result = _map(mapper, {"title": "T", "tags": "saas, ai , "})
    assert result.valid is True
    assert result.ingest_payload["type"] == "post"
    assert result.ingest_payload["status"] == "published"
    assert result.ingest_payload["tags"] == ["saas", "ai"]


def test_featured_image_base64_is_preserved(mapper):
    raw = base64.b64encode(b"hello").decode()
    result = _map(
        mapper,
        {"title": "T", "image_base64": raw, "image_file": "hero.png"},
    )
    assert result.featured_image == {"base64": raw, "filename": "hero.png"}
