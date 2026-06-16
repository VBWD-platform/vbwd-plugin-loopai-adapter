"""S95 integration — end-to-end create-post against a real DB with cms enabled.

A real core ApiKey scoped ``loopai:posts:create`` drives the WordPress-shaped
endpoint; the post is created through the cms ContentIngestService and resolves
via the public cms API. Re-posting the same title does not 500. The WP read
endpoint returns the post with WP-shaped keys.
"""
import base64

import pytest

from vbwd.models.user import User
from vbwd.repositories.api_key_repository import ApiKeyRepository
from vbwd.services.api_key_service import ApiKeyService
from plugins.cms.src.repositories.post_repository import PostRepository
from plugins.cms.src.services import post_type_registry, term_type_registry
from plugins.cms.src.services.post_type_registry import PostType
from plugins.cms.src.services.term_type_registry import TermType

CREATE_POST_URL = "/api/v1/loopai-adapter/create-post"
WP_POSTS_URL = "/api/v1/loopai-adapter/posts"

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake").decode()


@pytest.fixture(autouse=True)
def _registry():
    post_type_registry.clear_post_types()
    post_type_registry.register_post_type(
        PostType(key="page", label="Page", routable=True, hierarchical=True)
    )
    post_type_registry.register_post_type(
        PostType(key="post", label="Post", routable=True, hierarchical=False)
    )
    term_type_registry.clear_term_types()
    term_type_registry.register_term_type(
        TermType(key="category", label="Category", hierarchical=True)
    )
    yield
    post_type_registry.clear_post_types()
    term_type_registry.clear_term_types()


def _make_key(db, scopes):
    user = db.session.query(User).filter_by(email="test@example.com").first()
    assert user is not None, "seeded test user missing"
    service = ApiKeyService(ApiKeyRepository(db.session))
    _, plaintext = service.generate(user_id=user.id, label="loopai test", scopes=scopes)
    return user, plaintext


def _broadcast_body(title="Ingested headline"):
    return {
        "title": title,
        "summary": "The summary",
        "lead_paragraph": "The lead",
        "article_body": "<p>Body</p>",
        "category": "News",
        "tags": "saas, ai",
        "image_base64": _PNG,
        "image_file": "hero.png",
    }


def test_no_key_returns_401(client, db):
    response = client.post(CREATE_POST_URL, json=_broadcast_body())
    assert response.status_code == 401


def test_create_post_end_to_end(client, db):
    user, plaintext = _make_key(db, scopes=["loopai:posts:create"])

    response = client.post(
        CREATE_POST_URL, json=_broadcast_body(), headers={"X-API-Key": plaintext}
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["status"] == "success"
    assert body["post_id"]
    assert "featured_image_id" in body
    assert body["post_content_length"] > 0

    post = PostRepository(db.session).find_by_id(body["post_id"])
    assert post is not None
    assert post.type == "post"
    assert post.status == "published"
    assert str(post.author_id) == str(user.id)
    assert post.featured_image_url  # featured image uploaded + linked

    # Resolves via the public cms API.
    public = client.get(f"/api/v1/cms/posts/{post.slug}?type=post")
    assert public.status_code == 200, public.get_data(as_text=True)
    assert public.get_json()["slug"] == post.slug


def test_reposting_same_title_does_not_500(client, db):
    _, plaintext = _make_key(db, scopes=["loopai:posts:create"])
    headers = {"X-API-Key": plaintext}

    first = client.post(CREATE_POST_URL, json=_broadcast_body(), headers=headers)
    assert first.status_code == 200, first.get_data(as_text=True)

    second = client.post(CREATE_POST_URL, json=_broadcast_body(), headers=headers)
    assert second.status_code != 500
    assert second.status_code in (200, 409)


def test_wp_posts_list_returns_created_post(client, db):
    _, plaintext = _make_key(db, scopes=["loopai:posts:create"])
    created = client.post(
        CREATE_POST_URL,
        json=_broadcast_body("Listed headline"),
        headers={"X-API-Key": plaintext},
    )
    assert created.status_code == 200, created.get_data(as_text=True)

    listed = client.get(WP_POSTS_URL)
    assert listed.status_code == 200
    items = listed.get_json()
    assert isinstance(items, list)
    match = next(
        (item for item in items if item["title"]["rendered"] == "Listed headline"), None
    )
    assert match is not None
    assert match["status"] == "publish"
    assert "content" in match and "rendered" in match["content"]
    assert "slug" in match
