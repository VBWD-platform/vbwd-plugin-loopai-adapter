"""LoopAiPayloadMapper — the pure WordPress→CMS translation layer (S95.1).

This is the single home for every behaviour cloned from the WordPress
``wp-loopai-adapter`` plugin, kept free of HTTP/DB so it is fully unit-testable:

- **Field guessing** (port of ``Service/DataExtractor``): a required field absent
  at the top level is recovered by cleaning every key (lowercase + strip
  non-alphanumeric) and recursively matching anywhere in the payload tree —
  e.g. ``article__body`` / ``ARTICLE_BODY`` / ``articleBody`` → ``article_body``.
- **Category fallback** (port of ``LoopAIRequest::getCategory``):
  ``main_category`` then ``sub_category`` then ``category``.
- **Recursive image extraction** (port of ``DataExtractor::extractImageData``):
  every node carrying BOTH ``image_base64`` and ``image_file`` is collected in
  document order; index 0 is the featured image, indices ``1..n`` are embedded
  in the body HTML.
- **HTML build** (port of ``LoopAIWordPressPlugin::createPostContent``): a
  byte-stable template the fixture test pins.

The output is a ready-to-ingest payload for the cms ``ContentIngestService`` —
the mapper never persists anything (SRP/DRY): post/image/term creation has one
home, the cms plugin.
"""
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Required content fields, mirroring the WordPress plugin's ``$requiredFields``.
REQUIRED_FIELDS = (
    "title",
    "lead_paragraph",
    "summary",
    "category",
    "article_body",
    "tags",
)

# A node is an image only when it carries BOTH of these (the WP ``$imageFields``).
IMAGE_BASE64_KEY = "image_base64"
IMAGE_FILE_KEY = "image_file"

# WP falls back to this when no category is resolvable; we omit empties instead
# of creating a junk term, but keep the constant for the (single) named default.
DEFAULT_CATEGORY = "Uncategorized"

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


@dataclass
class MappedPayload:
    """Result of mapping a WordPress create-post body to a cms ingest payload.

    ``valid`` is False only when the title is missing — the handler turns that
    into the WP 422 envelope. ``featured_image`` (when present) is uploaded by
    the handler so the response can carry the real ``cms_image`` id; embedded
    images ``1..n`` are already referenced by filename inside
    ``ingest_payload["content_html"]`` (the WP plugin does not upload them).
    """

    valid: bool
    ingest_payload: Dict[str, Any]
    featured_image: Optional[Dict[str, str]]


class LoopAiPayloadMapper:
    """Translate a WordPress create-post body into a cms ingest payload."""

    def map(
        self,
        raw_payload: Dict[str, Any],
        *,
        default_status: str,
        default_post_type: str,
    ) -> MappedPayload:
        """Map a free-form WordPress payload to a cms ``ContentIngestService`` dict."""
        title = self._as_text(self._resolve_field(raw_payload, "title"))
        if not title.strip():
            return MappedPayload(valid=False, ingest_payload={}, featured_image=None)

        summary = self._as_text(self._resolve_field(raw_payload, "summary"))
        lead_paragraph = self._as_text(self._resolve_field(raw_payload, "lead_paragraph"))
        article_body = self._as_text(self._resolve_field(raw_payload, "article_body"))
        tags_value = self._as_text(self._resolve_field(raw_payload, "tags"))

        images = self._extract_images(raw_payload)
        content_html = self._build_html(
            summary=summary,
            lead_paragraph=lead_paragraph,
            article_body=article_body,
            title=title,
            images=images,
        )

        ingest_payload: Dict[str, Any] = {
            "type": default_post_type,
            "title": title,
            "content_html": content_html,
            "status": default_status,
            "categories": self._resolve_categories(raw_payload),
            "tags": self._split_csv(tags_value),
        }

        featured_image = images[0] if images else None
        return MappedPayload(
            valid=True, ingest_payload=ingest_payload, featured_image=featured_image
        )

    # ── field guessing (port of DataExtractor) ──────────────────────────────

    def _resolve_field(self, raw_payload: Any, key: str) -> Any:
        """Return the field's top-level value, else a recursively guessed one."""
        if isinstance(raw_payload, dict):
            value = raw_payload.get(key)
            if value is not None:
                return value
        return self._recursive_search(raw_payload, self._clean_key(key))

    def _resolve_categories(self, raw_payload: Any) -> List[str]:
        """Category fallback (main → sub → category), comma-split, empties dropped."""
        category_value = None
        if isinstance(raw_payload, dict):
            for candidate_key in ("main_category", "sub_category"):
                candidate = raw_payload.get(candidate_key)
                if candidate is not None:
                    category_value = candidate
                    break
        if category_value is None:
            category_value = self._resolve_field(raw_payload, "category")
        return self._split_csv(self._as_text(category_value))

    def _recursive_search(self, data: Any, clean_pattern: str) -> Any:
        """Depth-first, pre-order search for a key whose cleaned form matches."""
        if isinstance(data, dict):
            for key, value in data.items():
                if self._clean_key(str(key)) == clean_pattern:
                    return value
                if isinstance(value, (dict, list)):
                    found = self._recursive_search(value, clean_pattern)
                    if found is not None:
                        return found
        elif isinstance(data, list):
            for value in data:
                if isinstance(value, (dict, list)):
                    found = self._recursive_search(value, clean_pattern)
                    if found is not None:
                        return found
        return None

    @staticmethod
    def _clean_key(key: str) -> str:
        return _NON_ALPHANUMERIC.sub("", key.lower())

    # ── recursive image extraction (port of DataExtractor) ──────────────────

    def _extract_images(self, raw_payload: Any) -> List[Dict[str, str]]:
        collected: List[Dict[str, str]] = []
        self._collect_images(raw_payload, collected)
        return collected

    def _collect_images(self, data: Any, collected: List[Dict[str, str]]) -> None:
        if isinstance(data, dict):
            base64_value = data.get(IMAGE_BASE64_KEY)
            if isinstance(base64_value, str) and data.get(IMAGE_FILE_KEY) is not None:
                collected.append(
                    {
                        "base64": base64_value,
                        "filename": str(data.get(IMAGE_FILE_KEY) or ""),
                    }
                )
            for value in data.values():
                self._collect_images(value, collected)
        elif isinstance(data, list):
            for value in data:
                self._collect_images(value, collected)

    # ── HTML build (port of createPostContent) ──────────────────────────────

    def _build_html(
        self,
        *,
        summary: str,
        lead_paragraph: str,
        article_body: str,
        title: str,
        images: List[Dict[str, str]],
    ) -> str:
        html = f"<strong>{summary}</strong>"
        html += f"<p>{lead_paragraph}</p>"
        html += f"<div>{article_body}</div>"
        for index, image in enumerate(images):
            if index == 0:
                continue
            html += '<img src="%s" alt="%s" style="max-width:100%%; height:auto;" />' % (
                self._escape_url(image["filename"]),
                self._escape_attribute(title),
            )
        return html

    @staticmethod
    def _escape_attribute(value: str) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#039;")
        )

    @staticmethod
    def _escape_url(value: str) -> str:
        return (
            str(value)
            .replace("&", "&#038;")
            .replace('"', "%22")
            .replace("'", "&#039;")
            .replace(" ", "%20")
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _as_text(value: Any) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _split_csv(value: str) -> List[str]:
        return [part.strip() for part in value.split(",") if part.strip()]
