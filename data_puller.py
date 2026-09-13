import os
from google import genai
from technicals import get_gbpjpy_technicals
from utils.schemas import TradeSignal

# 1. Initialize Gemini Client here (where the API is actually called)
client = genai.Client()


def analyze_gbpjpy_market(technical_data: str, news_sentiment: str) -> TradeSignal:
    prompt = f"""
    You are an expert algorithmic FX trader specializing in the GBP/JPY currency pair.
    Analyze the current market data and sentiment provided below to generate a crisp trading signal.
    
    CRITICAL GBP/JPY CHARACTERISTICS:
    - Highly volatile; sensitive to BoJ monetary policy adjustments and global risk sentiment.

    --- Current Technical Data ---
    {technical_data}

    --- Recent News & Economic Sentiment ---
    {news_sentiment}
    """

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': TradeSignal,
            'temperature': 0.2,
        },
    )

    # Parse the text response into your validated Pydantic object
    return TradeSignal.model_validate_json(response.text)


if __name__ == "__main__":
    # 2. Fetch live data from OANDA
    print("Fetching OANDA technicals...")
    live_technicals = get_gbpjpy_technicals()

    # 3. Present data (or mock news)
    mock_news = "BoE maintains hawkish tone due to high core services inflation. BoJ stands pat."

    print("\n--- Sending Analysis to Gemini ---")
    signal = analyze_gbpjpy_market(live_technicals, mock_news)

    # 4. View your structured signal!
    print(signal.model_dump_json(indent=2))
