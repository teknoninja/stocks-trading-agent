"""Stock Analysis Agents."""

# LLM agents need optional heavy deps (openai-agents, ollama). Guarded so the
# technical engine / scanner work in minimal environments (e.g. GitHub Actions).
try:
    from .simple_agent import SimpleAgent
    from .conversation_agent import ConversationAgent
    from .structured_agent import StructuredAgent
    from .free_agent import FreeAgent
except ImportError:  # pragma: no cover
    SimpleAgent = ConversationAgent = StructuredAgent = FreeAgent = None  # type: ignore

# Also expose for direct use (tools need minsearch/tqdm — also optional)
try:
    from .tools import (
    get_company_info_basic,
    get_company_info,
    get_eps_trend,
    get_earnings_dates,
    get_earnings_analysis,
    get_historical_prices,
    get_ticker_news,
    search_news_by_ticker,
    search_news_by_query,
    search_companies,
    get_top_value_companies,
    get_top_growth_companies,
    # NEW: Enhanced social sentiment tools
    get_twitter_posts_by_engagement,
    get_reddit_discussions_by_impact,
    get_social_sentiment,
    # NEW: SEC filing analysis
    get_sec_filing,
    # NEW: multi-strategy technical BUY/SELL/HOLD flag
    get_technical_flag
    )
except ImportError:  # pragma: no cover
    pass

__all__ = [
    'SimpleAgent',
    'ConversationAgent',
    'StructuredAgent',
    'FreeAgent',
    # Tools
    'get_company_info_basic',
    'get_company_info',
    'get_eps_trend',
    'get_earnings_dates',
    'get_earnings_analysis',
    'get_historical_prices',
    'get_ticker_news',
    'search_news_by_ticker',
    'search_news_by_query',
    'search_companies',
    'get_top_value_companies',
    'get_top_growth_companies',
    'get_twitter_posts_by_engagement',
    'get_reddit_discussions_by_impact',
    'get_social_sentiment',
    'get_sec_filing',
    'get_technical_flag'
]
