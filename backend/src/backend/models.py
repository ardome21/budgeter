"""SQLAlchemy models.

Every table lives here (or is imported here) so that alembic/env.py picks the
whole schema up with a single import. A model that never reaches this module
is invisible to autogenerate.
"""

from .db import Base  # noqa: F401
