"""Bread application exception hierarchy."""


class BreadError(Exception):
    """Base application error."""


class ConfigError(BreadError):
    """Configuration loading or validation error."""


class DatabaseError(BreadError):
    """Database operation error."""


class DataProviderError(BreadError):
    """Base error for data provider failures."""


class DataProviderAuthError(DataProviderError):
    """Authentication failure (401/403)."""


class DataProviderRateLimitError(DataProviderError):
    """Rate limit exceeded (429)."""


class DataProviderResponseError(DataProviderError):
    """Empty or malformed provider response."""


class CacheError(BreadError):
    """Cache read/write error."""


class IndicatorError(BreadError):
    """Indicator computation error."""


class InsufficientHistoryError(IndicatorError):
    """Not enough historical data to compute the longest configured indicator window."""
