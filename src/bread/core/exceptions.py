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


class StrategyError(BreadError):
    """Strategy evaluation error."""


class BacktestError(BreadError):
    """Backtest engine error."""


class ExecutionError(BreadError):
    """Execution engine error."""


class RiskError(BreadError):
    """Risk management error."""


class OrderError(ExecutionError):
    """Order submission or tracking error."""


class ClaudeError(BreadError):
    """Base for Claude AI integration errors."""


class ClaudeTimeoutError(ClaudeError):
    """CLI call exceeded timeout."""


class ClaudeParseError(ClaudeError):
    """Failed to parse CLI response."""


class ClaudeUnavailableError(ClaudeError):
    """Circuit breaker is open — Claude temporarily disabled."""


class ClaudeCliNotFoundError(ClaudeError):
    """Claude CLI binary not found on PATH."""
