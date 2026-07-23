# models_ensemble.py
"""
AI model decision logic: Gemini, Qwen, DeepSeek
"""
import json
from google import genai
from google.genai import types

# Only import from config — NO imports from utils/trading_core here!
from config import (
    GEMINI_API_KEY,
    GEMINI_NEWS_MODEL,
    USE_GEMINI_AI
)

# Initialize clients locally
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if USE_GEMINI_AI else None

# Model enable flags
use_qwen = False
use_deepseek = False


def get_gemini_decision(prompt: str):
    """Get structured decision from Gemini"""
    if not USE_GEMINI_AI or not gemini_client:
        raise RuntimeError("Gemini is disabled or not configured")

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_NEWS_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json"
            )
        )
        # Parse your TradeSignal-compatible response here
        return json.loads(response.text.strip("`json \n"))
    except Exception as e:
        raise RuntimeError(f"Gemini call failed: {e}") from e


def get_qwen_decision(prompt: str):
    if not use_qwen:
        raise RuntimeError("Qwen is disabled")
    # Add your existing Qwen logic here
    raise NotImplementedError("Qwen integration not implemented")


def get_deepseek_decision(prompt: str):
    if not use_deepseek:
        raise RuntimeError("DeepSeek is disabled")
    # Add your existing DeepSeek logic here
    raise NotImplementedError("DeepSeek integration not implemented")