"""S95.1 RED — LoopAiPayloadMapper: WP field-guessing, images, byte-stable HTML."""
import base64
from importlib import import_module

import pytest

mapper_module = import_module("plugins.loopai-adapter.loopai_adapter.mapper")
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


def test_category_fallback_prefers_main_then_sub_then_category(mapper):
    result = _map(
        mapper,
        {
            "title": "T",
            "main_category": "Main",
            "sub_category": "Sub",
            "category": "Plain",
        },
    )
    assert result.ingest_payload["categories"] == ["Main"]

    result = _map(mapper, {"title": "T", "sub_category": "Sub", "category": "Plain"})
    assert result.ingest_payload["categories"] == ["Sub"]

    result = _map(mapper, {"title": "T", "category": "Plain"})
    assert result.ingest_payload["categories"] == ["Plain"]


def test_category_comma_split_and_no_category_is_empty(mapper):
    result = _map(mapper, {"title": "T", "category": "News, Tech ,  AI"})
    assert result.ingest_payload["categories"] == ["News", "Tech", "AI"]

    result = _map(mapper, {"title": "T"})
    assert result.ingest_payload["categories"] == []


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
