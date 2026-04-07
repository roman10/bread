"""Configuration loading, merging, and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from bread.core.exceptions import ConfigError

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR: Path = _PROJECT_ROOT / "config"


# ---------------------------------------------------------------------------
# Pydantic settings models
# ---------------------------------------------------------------------------


class AppSettings(BaseModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    timezone: str = "America/New_York"

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except KeyError as exc:
            raise ValueError(f"Invalid timezone: {v}") from exc
        return v


class DatabaseSettings(BaseModel):
    path: str = "data/bread.db"


class DataSettings(BaseModel):
    default_timeframe: str = "1Day"
    lookback_days: int = Field(default=200, ge=30)
    request_timeout_seconds: int = Field(default=30, ge=1)
    max_retries: int = Field(default=3, ge=1)


class AlpacaSettings(BaseModel):
    paper_api_key: str | None = None
    paper_secret_key: str | None = None
    live_api_key: str | None = None
    live_secret_key: str | None = None


class IndicatorSettings(BaseModel):
    sma_periods: list[int] = Field(default=[20, 50, 200])
    ema_periods: list[int] = Field(default=[9, 21])
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    bollinger_period: int = 20
    bollinger_stddev: float = 2.0
    volume_sma_period: int = 20
    return_periods: list[int] = Field(default_factory=list)

    @property
    def longest_window(self) -> int:
        """Longest lookback window across all indicator configurations."""
        candidates = [
            max(self.sma_periods),
            max(self.ema_periods),
            self.rsi_period,
            self.macd_slow + self.macd_signal,
            self.atr_period,
            self.bollinger_period,
            self.volume_sma_period,
        ]
        if self.return_periods:
            candidates.append(max(self.return_periods))
        return max(candidates)


class UniverseProviderSpec(BaseModel):
    type: Literal["predefined", "index"]
    symbols: list[str] = Field(default_factory=list)  # predefined only
    index: str | None = None  # index type: "sp500" or "nasdaq100"
    ttl_days: int = Field(default=7, ge=1)


class StrategySettings(BaseModel):
    name: str  # canonical snake_case identifier
    config_path: str | None = None  # relative to CONFIG_DIR; defaults to strategies/{name}.yaml
    enabled: bool = True
    modes: list[Literal["paper", "live"]] = ["paper", "live"]
    weight: float = Field(default=1.0, gt=0, le=1.0)


class BacktestSettings(BaseModel):
    initial_capital: float = Field(default=10000.0, gt=0)
    commission_per_trade: float = Field(default=0.0, ge=0)
    slippage_pct: float = Field(default=0.001, ge=0)


class RiskSettings(BaseModel):
    risk_pct_per_trade: float = Field(default=0.005, gt=0, le=0.05)
    max_positions: int = Field(default=5, ge=1)
    max_position_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_asset_class_pct: float = Field(default=0.40, gt=0, le=1.0)
    max_daily_loss_pct: float = Field(default=0.015, gt=0, le=1.0)
    max_weekly_loss_pct: float = Field(default=0.03, gt=0, le=1.0)
    max_drawdown_pct: float = Field(default=0.07, gt=0, le=1.0)
    pdt_enabled: bool = True
    asset_classes: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "equity_broad": ["SPY", "QQQ", "IWM", "DIA"],
            "financials": ["XLF"],
            "technology": ["XLK"],
            "energy": ["XLE"],
            "healthcare": ["XLV"],
            "commodities": ["GLD"],
            "fixed_income": ["TLT"],
        }
    )


class PaperCostSettings(BaseModel):
    """Cost model applied to paper trading fills to simulate real-world friction.

    Alpaca paper trading fills at quoted prices with no spread or slippage.
    These adjustments make paper P&L more realistic and consistent with
    the backtest cost model (BacktestSettings).
    """

    enabled: bool = True
    slippage_pct: float = Field(default=0.001, ge=0)  # 0.1%, matches backtest default
    commission_per_trade: float = Field(default=0.0, ge=0)  # per side


class ExecutionSettings(BaseModel):
    tick_interval_minutes: int = Field(default=15, ge=1)
    take_profit_ratio: float = Field(default=2.0, gt=0)
    stale_order_timeout_minutes: int = Field(default=30, ge=5)
    paper_cost: PaperCostSettings = Field(default_factory=PaperCostSettings)


class AlertSettings(BaseModel):
    enabled: bool = False
    urls: list[str] = Field(default_factory=list)
    on_trade: bool = True
    on_daily_summary: bool = True
    on_risk_breach: bool = True
    on_error: bool = True
    on_research: bool = True
    rate_limit_seconds: int = Field(default=60, ge=0)


class ClaudeSettings(BaseModel):
    enabled: bool = False
    cli_path: str = "claude"
    default_model: str = "sonnet"
    review_model: str = "sonnet"
    research_model: str = "sonnet"
    strategy_model: str = "sonnet"
    timeout_seconds: int = Field(default=60, ge=10, le=300)
    max_turns: int = Field(default=3, ge=1, le=10)
    review_mode: Literal["advisory", "gating"] = "advisory"
    research_enabled: bool = False
    research_interval_hours: int = Field(default=4, ge=1, le=24)
    circuit_breaker_max_failures: int = Field(default=3, ge=1)
    circuit_breaker_cooldown_seconds: int = Field(default=300, ge=60)


class AppConfig(BaseModel):
    mode: Literal["paper", "live"]
    app: AppSettings = AppSettings()
    db: DatabaseSettings = DatabaseSettings()
    data: DataSettings = DataSettings()
    alpaca: AlpacaSettings = AlpacaSettings()
    indicators: IndicatorSettings = IndicatorSettings()
    strategies: list[StrategySettings] = Field(default_factory=list)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    universe_providers: dict[str, UniverseProviderSpec] = Field(default_factory=dict)
    asset_class_mapping: dict[str, str] = Field(
        default_factory=lambda: {
            "Information Technology": "technology",
            "Health Care": "healthcare",
            "Financials": "financials",
            "Consumer Discretionary": "consumer_discretionary",
            "Communication Services": "communication",
            "Industrials": "industrials",
            "Consumer Staples": "consumer_staples",
            "Energy": "energy",
            "Utilities": "utilities",
            "Real Estate": "real_estate",
            "Materials": "materials",
        }
    )

    @model_validator(mode="after")
    def _check_credentials(self) -> AppConfig:
        if self.mode == "paper":
            if not self.alpaca.paper_api_key or not self.alpaca.paper_secret_key:
                raise ValueError(
                    "paper mode requires ALPACA_PAPER_API_KEY and ALPACA_PAPER_SECRET_KEY"
                )
        else:
            if not self.alpaca.live_api_key or not self.alpaca.live_secret_key:
                raise ValueError(
                    "live mode requires ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY"
                )
        return self

    @model_validator(mode="after")
    def _unique_strategy_names(self) -> AppConfig:
        names = [s.name for s in self.strategies]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"Duplicate strategy names: {dupes}")
        return self


# ---------------------------------------------------------------------------
# Config loading helpers
# ---------------------------------------------------------------------------


def deep_merge(base: dict, override: dict) -> dict:  # type: ignore[type-arg]
    """Recursively merge *override* into *base*. Lists and scalars fully replace."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict:  # type: ignore[type-arg]
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load, merge, and validate the application config.

    Merge order:
    1. config/default.yaml
    2. BREAD_MODE env override
    3. config/{mode}.yaml overlay
    4. .env file
    5. Env-var secrets injected
    6. Pydantic validation
    """
    config_dir = config_dir or CONFIG_DIR

    # Step 1: load base
    base = _load_yaml(config_dir / "default.yaml")

    # Step 2: resolve mode
    mode = os.environ.get("BREAD_MODE", base.get("mode", "paper"))
    if mode not in ("paper", "live"):
        raise ConfigError(f"BREAD_MODE must be 'paper' or 'live', got '{mode}'")
    base["mode"] = mode

    # Step 3: overlay mode-specific config
    overlay = _load_yaml(config_dir / f"{mode}.yaml")
    merged = deep_merge(base, overlay)

    # Step 4: load .env (from the project root, one level above config dir)
    env_path = config_dir.parent / ".env"
    load_dotenv(env_path)

    # Step 5: inject secrets from environment
    alpaca = merged.setdefault("alpaca", {})
    for env_key, config_key in [
        ("ALPACA_PAPER_API_KEY", "paper_api_key"),
        ("ALPACA_PAPER_SECRET_KEY", "paper_secret_key"),
        ("ALPACA_LIVE_API_KEY", "live_api_key"),
        ("ALPACA_LIVE_SECRET_KEY", "live_secret_key"),
    ]:
        val = os.environ.get(env_key)
        if val:
            alpaca[config_key] = val

    # Step 6: validate
    try:
        return AppConfig.model_validate(merged)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
