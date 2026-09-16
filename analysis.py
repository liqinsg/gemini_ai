from google import genai
from pydantic import BaseModel
# Import the schema we defined above
# from utils.schemas import TradeSignal 
from utils import TradeSignal

client = genai.Client()


def analyze_gbpjpy_market(technical_data: str, news_sentiment: str) -> TradeSignal:
    prompt = f"""
    You are an expert algorithmic FX trader specializing in the GBP/JPY currency pair.
    Analyze the current market data and sentiment provided below to generate a crisp trading signal.
    
    CRITICAL GBP/JPY CHARACTERISTICS:
    - High volatility, sensitive to BoJ monetary policy adjustments (Yen strength/weakness).
    - Driven heavily by global risk sentiment (Yen acts as a safe-haven asset).

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
            'temperature': 0.2, # Keep temperature low for structured, analytical tasks
        },
    )
    
    # Return the validated object directly
    return TradeSignal.model_validate_json(response.text)

# Example Execution
mock_technicals = "Price: 202.85. 4H RSI: 68 (Approaching overbought). 50 EMA is above 200 EMA (Bullish structure)."
mock_news = "Bank of Japan hints at maintaining ultra-low interest rates; BoE speaker hints at sticky inflation keeping UK rates higher for longer."

signal = analyze_gbpjpy_market(mock_technicals, mock_news)
print(signal.model_dump_json(indent=2))