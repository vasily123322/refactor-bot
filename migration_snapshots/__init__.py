"""Migration-only frozen schema snapshots.

These modules are not production ORM registries. They exist so historical Alembic
revisions remain deterministic after current application models evolve.
"""
