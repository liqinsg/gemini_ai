from utils import get_latest_news_sentiment, get_news_risk_bias

print("=== TEST GEMINI ===")
print(get_latest_news_sentiment())
print(get_news_risk_bias("GBP_JPY"))