import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class RiskConfig:
    max_portfolio_risk_pct: float = 6.0
    max_trade_risk_pct: float = 1.0
    daily_loss_limit_pct: float = 3.0
    drawdown_warning_pct: float = 8.0
    drawdown_kill_pct: float = 12.0
    max_agent_correlation: float = 0.4
    recovery_required_pct: float = 5.0


@dataclass
class AppConfig:
    trading_mode: str = os.getenv("TRADING_MODE", "paper")   # paper | live
    starting_capital: float = float(os.getenv("STARTING_CAPITAL", 10000))
    dhan_client_id: str = os.getenv("DHAN_CLIENT_ID", "")
    dhan_access_token: str = os.getenv("DHAN_ACCESS_TOKEN", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    news_api_key: str = os.getenv("NEWS_API_KEY", "")
    risk: RiskConfig = None

    def __post_init__(self):
        self.risk = RiskConfig()


config = AppConfig()
