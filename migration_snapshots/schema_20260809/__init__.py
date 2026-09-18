"""Frozen ORM registry for Alembic revision 20260809_0001.

Source definitions are copied from repository commit
f5bbfdc25a7ce323db3ace39ba66ce0fb6dc2a97, the commit that froze the
0001 baseline membership. Do not import production app.domain models here.
"""

from .base import Base
from . import models as _models  # noqa: F401
from . import ai_auto_task as _ai_auto_task  # noqa: F401
from . import content_models as _content_models  # noqa: F401
from . import publishing_models as _publishing_models  # noqa: F401
from . import sources_models as _sources_models  # noqa: F401
from . import sources_enrichment as _sources_enrichment  # noqa: F401
from . import sources_ingestion as _sources_ingestion  # noqa: F401
from . import sources_rewrite as _sources_rewrite  # noqa: F401

__all__ = ["Base"]
