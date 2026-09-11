# Importing this package activates fides_exclusion_guard, which registers
# PrivacyCare's table names into Fides' own (mutable) EXCLUDED_TABLES set so
# that Fides' own autogenerate never proposes dropping them. See that
# module's docstring for the mechanism and its residual risk.
from fides.api.privacycare import fides_exclusion_guard  # noqa: F401
