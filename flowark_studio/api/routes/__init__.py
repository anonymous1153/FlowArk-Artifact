from flowark_studio.api.routes.system import create_system_router
from flowark_studio.api.routes.tags import create_tag_router
from flowark_studio.api.routes.tasks import create_task_router

__all__ = [
    "create_system_router",
    "create_tag_router",
    "create_task_router",
]
