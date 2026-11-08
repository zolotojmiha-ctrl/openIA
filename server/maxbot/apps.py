from django.apps import AppConfig


class MaxbotConfig(AppConfig):
    name = 'maxbot'

    def ready(self):
        from . import notices  # noqa: F401
