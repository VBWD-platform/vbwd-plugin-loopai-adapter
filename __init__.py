"""loopai-adapter plugin — a WordPress drop-in for the LoopAI pipeline.

Serves the WordPress ``wp-loopai-adapter`` endpoints (``/wp-json/...``) from a
vbwd instance so a LoopAI pipeline pointed at vbwd creates CMS posts unchanged.
It is a pure translation layer: the WP→cms mapping lives in
``LoopAiPayloadMapper`` and all post/image/term creation is reused from the cms
plugin — this plugin edits neither core nor cms.

The plugin class MUST be defined here (not re-exported): the plugin manager's
discovery skips classes whose ``__module__`` differs from the package module.
"""
from importlib import import_module
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from vbwd.plugins.base import BasePlugin, PluginMetadata

if TYPE_CHECKING:
    from flask import Blueprint


DEFAULT_CONFIG: Dict[str, Any] = {
    "debug_mode": False,
    # Status applied to created posts. WordPress publishes immediately; an
    # operator who prefers review can set this to "draft" via Settings.
    "default_status": "published",
    # cms post type created by the adapter (the WP plugin only made posts).
    "default_post_type": "post",
}


class LoopaiAdapterPlugin(BasePlugin):
    """WordPress-drop-in: serve the LoopAI create-post API from vbwd CMS."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="loopai-adapter",
            version="26.6",
            author="VBWD Team",
            description=(
                "WordPress-compatible LoopAI ingest API backed by the vbwd CMS"
            ),
            # Hard dependency: every post/image/term write is reused from cms.
            dependencies=["cms"],
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        merged: Dict[str, Any] = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    def get_blueprint(self) -> Optional["Blueprint"]:
        # The plugin directory is hyphenated, so the inner package is reached via
        # import_module (a normal ``from`` statement cannot name a hyphen dir).
        routes_module = import_module("plugins.loopai-adapter.loopai_adapter.routes")
        return routes_module.loopai_adapter_bp

    def get_url_prefix(self) -> Optional[str]:
        # Classic vbwd API namespace — routes are defined relative to this.
        return "/api/v1/loopai-adapter"

    @property
    def api_scopes(self) -> List[Dict[str, Any]]:
        """API-key scope the create-post endpoint requires (read by core S52).

        Its own scope (not cms:posts:create) gives the pipeline an independently
        revocable, labelled token. ``user_grantable`` lets a user self-mint one
        at ``/dashboard/api-keys``.
        """
        return [
            {
                "key": "loopai:posts:create",
                "label": "Create posts via the LoopAI adapter",
                "description": (
                    "Create a published CMS post through the WordPress-compatible "
                    "LoopAI ingest endpoint."
                ),
                "user_grantable": True,
            }
        ]

    def on_enable(self) -> None:
        pass

    def on_disable(self) -> None:
        pass
