from django.apps import AppConfig
from django.conf import settings

class ApiConfig(AppConfig):
    name = 'api'

    def ready(self):
        from .queue_manager import init_queue
        from .auto_downloads import start_auto_download_scheduler
        init_queue()
        start_auto_download_scheduler()
