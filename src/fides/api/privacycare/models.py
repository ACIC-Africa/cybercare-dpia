# PrivacyCare's own SQLAlchemy metadata.
#
# Deliberately NOT Fides' Base.metadata: keeping a separate MetaData means our
# Alembic chain can never autogenerate a drop for a Fides table, and our models
# are not registered in an Ethyca-authored module.
from sqlalchemy import MetaData

PRIVACYCARE_METADATA = MetaData()
