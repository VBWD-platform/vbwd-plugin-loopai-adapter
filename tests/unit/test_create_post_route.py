"""S95.2 RED — create-post handler: WP-shaped 200 / 422 / 409 / 500 envelopes.

The handler core (``create_post_from_payload``) takes its cms collaborators as
arguments so it is unit-testable with mocks — no HTTP, no DB.
"""
import base64
from importlib import import_module
from unittest.mock import MagicMock

from plugins.cms.src.services.post_service import PostSlugConflictError

routes_module = import_module("plugins.loopai-adapter.loopai_adapter.routes")
create_post_from_payload = routes_module.create_post_from_payload

_CONFIG = {"default_status": "published", "default_post_type": "post"}


def _call(payload, *, ingest=None, image=None, post=None):
    ingest_service = ingest or MagicMock()
    image_service = image or MagicMock()
    post_service = post or MagicMock()
    return create_post_from_payload(
        payload,
        user_id="user-123",
        ingest_service=ingest_service,
        image_service=image_service,
        post_service=post_service,
        config=_CONFIG,
    )


def test_happy_path_returns_wp_success_envelope():
    ingest = MagicMock()
    ingest.ingest.return_value = {"id": "post-7", "slug": "headline"}
    image = MagicMock()
    image.upload_image.return_value = {"id": "img-9", "url_path": "/uploads/x.png"}
    post = MagicMock()

    payload = {
        "title": "Headline",
        "summary": "S",
        "article_body": "B",
        "image_base64": base64.b64encode(b"png-bytes").decode(),
        "image_file": "hero.png",
    }
    body, status = _call(payload, ingest=ingest, image=image, post=post)

    assert status == 200
    assert body["status"] == "success"
    assert body["post_id"] == "post-7"
    assert body["featured_image_id"] == "img-9"
    assert body["post_content_length"] > 0

    # ingest called exactly once, authored as the key's user, with the mapper's
    # payload; the featured image is uploaded via the image service and linked to
    # the created post via update_post (ContentIngestService ignores a preset URL).
    ingest.ingest.assert_called_once()
    _, kwargs = ingest.ingest.call_args
    assert kwargs["user_id"] == "user-123"
    sent_payload = ingest.ingest.call_args.args[0]
    assert sent_payload["title"] == "Headline"
    image.upload_image.assert_called_once()
    post.update_post.assert_called_once_with(
        "post-7", {"featured_image_url": "/uploads/x.png"}
    )


def test_missing_title_returns_422_and_does_not_ingest():
    ingest = MagicMock()
    body, status = _call({"summary": "no title"}, ingest=ingest)

    assert status == 422
    assert body == {"status": "error", "message": "Missing required fields"}
    ingest.ingest.assert_not_called()


def test_ingest_failure_returns_500():
    ingest = MagicMock()
    ingest.ingest.side_effect = RuntimeError("boom")
    body, status = _call({"title": "T"}, ingest=ingest)

    assert status == 500
    assert body["status"] == "error"
    assert "boom" in body["message"]


def test_slug_conflict_returns_409_not_500():
    ingest = MagicMock()
    ingest.ingest.side_effect = PostSlugConflictError("dup slug")
    body, status = _call({"title": "T"}, ingest=ingest)

    assert status == 409
    assert body["status"] == "error"


def test_no_image_reports_featured_image_id_zero():
    ingest = MagicMock()
    ingest.ingest.return_value = {"id": "post-1"}
    image = MagicMock()
    body, status = _call({"title": "T"}, ingest=ingest, image=image)

    assert status == 200
    assert body["featured_image_id"] == 0
    image.upload_image.assert_not_called()
