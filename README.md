# loopai-adapter

A WordPress drop-in for the **LoopAI** content pipeline. It serves the same
`/wp-json/...` endpoints the `wp-loopai-adapter` WordPress plugin exposes — but
writes to the vbwd **CMS** and authenticates with vbwd's built-in **User API
Token** system instead of WP Basic-Auth.

A LoopAI pipeline configured with `WP_SITE=<vbwd instance>` and a vbwd API token
creates published CMS posts with no code changes other than sending the token via
the `X-API-Key` header.

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/wp-json/loopai-adapter/v1/create-post` | `X-API-Key` scope `loopai:posts:create` | Create a published post |
| `GET`  | `/wp-json/wp/v2/posts` | none (published only) | Minimal WP-shaped post list |

### `create-post`

Accepts the free-form `BroadcastData.__json__()` body the pipeline assembles
(`title`, `lead_paragraph`, `summary`, `category`, `article_body`, `tags`, plus
nested `{image_base64, image_file}` objects). It reproduces the WordPress
plugin's behaviour:

- **Field guessing** — a missing required field is recovered by matching cleaned
  keys (`article__body` / `ARTICLE_BODY` → `article_body`).
- **Category fallback** — `main_category` → `sub_category` → `category`.
- **Recursive images** — the first `{image_base64, image_file}` found becomes the
  featured image; the rest are embedded in the body HTML by filename.
- **Immediate publish** — posts are created with status `published` (override to
  `draft` in Settings).

Responses match WordPress:

- `200 {status, post_id, featured_image_id, post_content_length}`
- `422 {status: "error", message: "Missing required fields"}` (no title)
- `409 {status: "error", message}` (slug collision — re-posting the same title)
- `500 {status: "error", message}` (downstream failure)

## Design

The plugin is a protocol shim. The WP→CMS translation lives in the pure
`LoopAiPayloadMapper`; all post/image/term/SEO creation is reused from the `cms`
plugin (`ContentIngestService` / `CmsImageService` / `PostService`). It edits
neither core nor cms.

```
LoopAI pipeline ──POST /wp-json/loopai-adapter/v1/create-post──▶ loopai-adapter
                       LoopAiPayloadMapper (WP fields → cms ingest)
                                     │
                       cms ContentIngestService.ingest() → PostService / CmsImageService
```

## Configuration

`config.json` / `admin-config.json`: `debug_mode`, `default_status`
(`published`/`draft`), `default_post_type`.

## Tests

```bash
bin/pre-commit-check.sh --plugin loopai-adapter --full
```
