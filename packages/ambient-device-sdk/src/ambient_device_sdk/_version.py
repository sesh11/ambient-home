"""Package and protocol versions."""

__version__ = "0.1.0"

PROTOCOL_VERSION = 1
"""Device protocol major version.

Bumped only on a breaking change to the contract. Additive changes (new optional
fields, new enum members documented as ignorable) keep the same number.
"""
