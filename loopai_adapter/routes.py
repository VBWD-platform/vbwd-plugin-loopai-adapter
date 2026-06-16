"""loopai-adapter routes — the LoopAI ingest API on the classic vbwd namespace.

Two endpoints under ``/api/v1/loopai-adapter`` accepting the WordPress
``wp-loopai-adapter`` request/response shapes so a LoopAI pipeline pointed at a
vbwd instance keeps working (the caller-side changes: the classic URL plus the
vbwd token via ``X-API-Key`` instead of WP Basic-Auth):

- ``POST /api/v1/loopai-adapter/create-post`` — create a published cms post.
- ``GET  /api/v1/loopai-adapter/posts`` — a minimal WP-shaped published-post list.

This module is a protocol shim. The WP→cms translation lives in
``LoopAiPayloadMapper``; post/image/term/SEO creation is reused verbatim from
the cms plugin (its ``ContentIngestService`` / ``CmsImageService`` /
``PostService`` factories) — the adapter never touches cms models or storage.
"""
import logging
import mimetypes
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, current_app, g, jsonify, request

from vbwd.middleware.api_key_auth import require_api_key

from .mapper import LoopAiPayloadMapper

logger = logging.getLogger(__name__)

loopai_adapter_bp = Blueprint("loopai_adapter", __name__)

PLUGIN_NAME = "loopai-adapter"
CREATE_POST_SCOPE = "loopai:posts:create"

# Baseline config fallbacks (the plugin also ships config.json / admin-config.json).
DEFAULT_STATUS = "published"
DEFAULT_POST_TYPE = "post"

# cms post-status -> WordPress post-status for the read endpoint.
_WP_STATUS = {"published": "publish", "draft": "draft", "private": "private"}


# ── cms service reuse (DRY: post/image/term creation has ONE home, the cms plugin)


def _content_ingest_service():
    """Reuse the cms plugin's fully-wired content-ingestion composer."""
    from plugins.cms.src.routes import _content_ingest_service as cms_factory

    return cms_factory()


def _image_service():
    """Reuse the cms plugin's image service (the gallery's single owner)."""
    from plugins.cms.src.routes import _image_service as cms_factory

    return cms_factory()


def _post_service():
    """Reuse the cms plugin's fully-wired post service (read path)."""
    from plugins.cms.src.routes import _post_service as cms_factory

    return cms_factory()


def _adapter_config() -> Dict[str, Any]:
    """Read this plugin's persisted config, defaulting to the shipped baseline."""
    config_store = getattr(current_app, "config_store", None)
    if config_store:
        config = config_store.get_config(PLUGIN_NAME)
        if config:
            return config
    return {"default_status": DEFAULT_STATUS, "default_post_type": DEFAULT_POST_TYPE}


# ── create-post handler (pure-ish: services injected so it is unit-testable) ──


def create_post_from_payload(
    payload: Dict[str, Any],
    *,
    user_id: Any,
    ingest_service,
    image_service,
    post_service,
    config: Dict[str, Any],
) -> Tuple[Dict[str, Any], int]:
    """Translate a WP create-post body and create a cms post; return (body, status).

    WP-shaped envelopes: success ``200``; missing title ``422``; slug collision
    ``409`` (re-posting the same title does not 500); any other downstream
    failure ``500``. No silent ``success=False`` (Liskov) — failures raise and
    are mapped here.

    The featured image (index 0) is uploaded through the cms image service so the
    response can carry the real ``cms_image`` id, then linked to the post via
    ``PostService.update_post`` — the cms ``ContentIngestService`` only uploads
    its own ``image`` field, so the URL is set after creation. A failed upload is
    non-fatal (the WP plugin likewise returns id 0 and still creates the post).
    """
    mapper = LoopAiPayloadMapper()
    mapped = mapper.map(
        payload,
        default_status=config.get("default_status", DEFAULT_STATUS),
        default_post_type=config.get("default_post_type", DEFAULT_POST_TYPE),
    )
    if not mapped.valid:
        return {"status": "error", "message": "Missing required fields"}, 422

    ingest_payload = dict(mapped.ingest_payload)

    featured_image_id: Any = 0
    featured_image_url: Optional[str] = None
    if mapped.featured_image:
        featured_image_id, featured_image_url = _upload_featured_image(
            mapped.featured_image, image_service
        )

    # Imported here so the route module imports without the cms plugin loaded
    # (e.g. unit tests that mock the services).
    from plugins.cms.src.services.post_service import PostSlugConflictError

    try:
        result = ingest_service.ingest(ingest_payload, user_id=user_id)
        if featured_image_url:
            post_service.update_post(
                result["id"], {"featured_image_url": featured_image_url}
            )
    except PostSlugConflictError as conflict:
        return {"status": "error", "message": str(conflict)}, 409
    except Exception as failure:  # noqa: BLE001 — protocol boundary → WP 500 envelope
        logger.warning("[loopai-adapter] create-post failed: %s", failure)
        return {"status": "error", "message": str(failure)}, 500

    return (
        {
            "status": "success",
            "post_id": result.get("id"),
            "featured_image_id": featured_image_id,
            "post_content_length": len(ingest_payload.get("content_html") or ""),
        },
        200,
    )


def _upload_featured_image(
    featured_image: Dict[str, str], image_service
) -> Tuple[Any, Optional[str]]:
    """Upload the featured image via the cms image service; (id, url) or (0, None)."""
    import base64

    base64_value = featured_image.get("base64") or ""
    if base64_value.startswith("data:") and "," in base64_value:
        base64_value = base64_value.split(",", 1)[1]
    filename = featured_image.get("filename") or "upload.bin"
    try:
        raw_bytes = base64.b64decode(base64_value, validate=True)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        uploaded = image_service.upload_image(
            file_data=raw_bytes, filename=filename, mime_type=mime_type
        )
    except Exception as error:  # noqa: BLE001 - bad image is non-fatal
        logger.warning("[loopai-adapter] featured-image upload failed: %s", error)
        return 0, None
    return uploaded.get("id") or 0, uploaded.get("url_path")


# ── routes ───────────────────────────────────────────────────────────────────


@loopai_adapter_bp.route("/create-post", methods=["POST"])
@require_api_key(CREATE_POST_SCOPE)
def create_post():
    """LoopAI create-post endpoint, authored as the key's user."""
    payload = request.get_json(silent=True) or {}
    body, status_code = create_post_from_payload(
        payload,
        user_id=g.user_id,
        ingest_service=_content_ingest_service(),
        image_service=_image_service(),
        post_service=_post_service(),
        config=_adapter_config(),
    )
    return jsonify(body), status_code


@loopai_adapter_bp.route("/posts", methods=["GET"])
def list_posts():
    """Minimal WP-shaped list of published posts (for ``get_all_posts``)."""
    per_page = request.args.get("per_page", default=10, type=int)
    page = request.args.get("page", default=1, type=int)
    config = _adapter_config()
    result = _post_service().list_posts(
        post_type=config.get("default_post_type", DEFAULT_POST_TYPE),
        status="published",
        page=page,
        per_page=per_page,
        newest_first=True,
    )
    return jsonify([_to_wp_post(post) for post in result.get("items", [])]), 200


def _to_wp_post(post: Dict[str, Any]) -> Dict[str, Any]:
    """Project a cms post dict onto the subset of WP REST fields the caller reads."""
    slug = post.get("slug") or ""
    return {
        "id": post.get("id"),
        "slug": slug,
        "status": _WP_STATUS.get(post.get("status"), post.get("status")),
        "title": {"rendered": post.get("title") or ""},
        "content": {"rendered": post.get("content_html") or ""},
        "link": post.get("canonical_url") or f"/{slug}",
        "date": post.get("published_at") or post.get("created_at"),
    }
