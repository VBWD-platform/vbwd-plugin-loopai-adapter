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

# Every ingested post lands under this top-level category; the request's
# sub_category / category becomes a child subcategory below it. Overridable via
# the plugin's ``default_parent_category`` config.
DEFAULT_PARENT_CATEGORY = "blog"

# Synthesized-field limits: the excerpt is a short teaser; the SEO description
# tracks the ~160-char meta-description convention; SEO titles ride the model's
# 255-char column. Truncation snaps back to the last word boundary.
EXCERPT_MAX_LENGTH = 300
SEO_DESCRIPTION_MAX_LENGTH = 160
SEO_TITLE_MAX_LENGTH = 255

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


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
        default_parent_category: str = DEFAULT_PARENT_CATEGORY,
    ) -> MappedPayload:
        """Map a free-form WordPress payload to a cms ``ContentIngestService`` dict."""
        title = self._as_text(self._resolve_field(raw_payload, "title"))
        if not title.strip():
            return MappedPayload(valid=False, ingest_payload={}, featured_image=None)

        summary = self._as_text(self._resolve_field(raw_payload, "summary"))
        lead_paragraph = self._as_text(
            self._resolve_field(raw_payload, "lead_paragraph")
        )
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
            "categories": self._resolve_categories(
                raw_payload, default_parent_category
            ),
            "tags": self._split_csv(tags_value),
        }

        excerpt = self._build_excerpt(summary, lead_paragraph)
        if excerpt:
            ingest_payload["excerpt"] = excerpt

        seo = self._build_seo(title, summary, lead_paragraph)
        if seo:
            ingest_payload["seo"] = seo

        featured_image = images[0] if images else None
        return MappedPayload(
            valid=True, ingest_payload=ingest_payload, featured_image=featured_image
        )

    # ── synthesized excerpt + SEO (loopai sends none — we derive them) ──────

    def _build_excerpt(self, summary: str, lead_paragraph: str) -> Optional[str]:
        """Excerpt from ``summary`` (fallback ``lead_paragraph``), plain + trimmed."""
        text = self._plain_text(summary) or self._plain_text(lead_paragraph)
        if not text:
            return None
        return self._truncate_on_word_boundary(text, EXCERPT_MAX_LENGTH)

    def _build_seo(
        self, title: str, summary: str, lead_paragraph: str
    ) -> Dict[str, str]:
        """Synthesize meta/OpenGraph title + description; omit empty sources.

        Canonical URL and robots are deliberately left unset so the cms post keeps
        its model defaults (og:image is injected by the route once uploaded).
        """
        seo: Dict[str, str] = {}
        trimmed_title = title.strip()[:SEO_TITLE_MAX_LENGTH]
        if trimmed_title:
            seo["meta_title"] = trimmed_title
            seo["og_title"] = trimmed_title

        description_source = self._plain_text(summary) or self._plain_text(
            lead_paragraph
        )
        description = self._truncate_on_word_boundary(
            description_source, SEO_DESCRIPTION_MAX_LENGTH
        )
        if description:
            seo["meta_description"] = description
            seo["og_description"] = description
        return seo

    # ── field guessing (port of DataExtractor) ──────────────────────────────

    def _resolve_field(self, raw_payload: Any, key: str) -> Any:
        """Return the field's top-level value, else a recursively guessed one."""
        if isinstance(raw_payload, dict):
            value = raw_payload.get(key)
            if value is not None:
                return value
        return self._recursive_search(raw_payload, self._clean_key(key))

    def _resolve_categories(
        self, raw_payload: Any, default_parent_category: str
    ) -> List[Dict[str, str]]:
        """Hierarchy: parent = ``default_parent_category`` + a subcategory child.

        The subcategory is ``sub_category`` when present/non-empty, else the first
        comma-split value of ``category``. When neither yields a value the post
        lands under the parent category only.
        """
        subcategory = self._resolve_subcategory(raw_payload)
        if subcategory:
            return [
                {"name": default_parent_category},
                {"name": subcategory, "parent": default_parent_category},
            ]
        return [{"name": default_parent_category}]

    def _resolve_subcategory(self, raw_payload: Any) -> Optional[str]:
        """First non-empty of ``sub_category`` then ``category`` (comma → first)."""
        for field_name in ("sub_category", "category"):
            parts = self._split_csv(
                self._as_text(self._resolve_field(raw_payload, field_name))
            )
            if parts:
                return parts[0]
        return None

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
            html += (
                '<img src="%s" alt="%s" style="max-width:100%%; height:auto;" />'
                % (
                    self._escape_url(image["filename"]),
                    self._escape_attribute(title),
                )
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

    @classmethod
    def _plain_text(cls, value: Any) -> str:
        """Strip HTML tags and collapse whitespace to a single-spaced string."""
        without_tags = _HTML_TAG.sub(" ", cls._as_text(value))
        return _WHITESPACE.sub(" ", without_tags).strip()

    @staticmethod
    def _truncate_on_word_boundary(text: str, max_length: int) -> str:
        """Truncate to at most ``max_length`` chars, snapping to a word boundary."""
        if len(text) <= max_length:
            return text
        truncated = text[:max_length].rstrip()
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated.rstrip()
