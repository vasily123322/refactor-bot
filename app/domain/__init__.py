"""Domain package registry.

Import ORM modules here so SQLAlchemy metadata is complete before startup
`Base.metadata.create_all()` and Alembic migrations run. Domain services should
still import concrete models from their own modules rather than from this registry.
"""

from app.domain import ai_auto_task as _ai_auto_task_models  # noqa: F401
from app.domain import models as _legacy_models  # noqa: F401
from app.domain import scheduler as _scheduler_models  # noqa: F401
from app.domain.content import models as _content_models  # noqa: F401
from app.domain.publishing import models as _publishing_models  # noqa: F401
from app.domain.sources import enrichment as _source_enrichment_models  # noqa: F401
from app.domain.sources import ingestion as _source_ingestion_models  # noqa: F401
from app.domain.sources import models as _sources_models  # noqa: F401
from app.domain.sources import rewrite as _source_rewrite_models  # noqa: F401

__all__: list[str] = []
