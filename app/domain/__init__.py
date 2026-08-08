"""Domain package registry.

Import ORM modules here so SQLAlchemy metadata is complete before startup
`Base.metadata.create_all()` runs. Domain services should still import concrete
models from their own modules rather than from this registry.
"""

from app.domain.content import models as _content_models  # noqa: F401

__all__: list[str] = []
