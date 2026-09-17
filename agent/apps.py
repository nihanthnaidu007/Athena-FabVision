from django.apps import AppConfig


class AgentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'agent'

    def ready(self):
        """Load optional integrations; a missing one disables its feature."""
        from .registry import load_fab_tools, wire_retrieval

        try:
            load_fab_tools()
            wire_retrieval()
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                'Optional agent integrations failed to load; features stay disabled.'
            )
