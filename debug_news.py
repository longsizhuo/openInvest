import traceback
from ddgs import DDGS
import trafilatura
import logging

# 设置 trafilatura 的日志级别为 DEBUG 以便看到底层错误
logging.basicConfig(level=logging.DEBUG)

def test_ddgs_and_fetch():
    query = "Nasdaq outlook Australia (earnings OR CPI OR GDP OR Fed OR RBA OR guidance OR filings OR statement)"
    simple_query = "Nasdaq outlook Australia"

    print(f"--- 1. Testing Complex Query: {query} ---")
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, region="wt-wt", safesearch="off", max_results=5))
            print(f"Complex query returned {len(results)} results.")
            for i, r in enumerate(results):
                print(f"  [{i}] {r.get('title')} - {r.get('url')}")
    except Exception as e:
        print(f"Complex query FAILED: {e}")

    print(f"\n--- 2. Testing Simple Query (Fallback): {simple_query} ---")
    results = []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(simple_query, region="wt-wt", safesearch="off", max_results=5))
            print(f"Simple query returned {len(results)} results.")
            for i, r in enumerate(results):
                print(f"  [{i}] {r.get('title')} - {r.get('url')}")
    except Exception as e:
        print(f"Simple query FAILED: {e}")
        return

    if not results:
        print("No results to test fetch.")
        return

    # Pick the first URL to test extraction
    test_url = results[0].get('url')
    print(f"\n--- 3. Testing Extraction on: {test_url} ---")

    try:
        print("Downloading & Extracting with news._extract_main_text (using Session/Retry/Readability)...")
        # Import the internal function for testing
        from news import _extract_main_text
        
        text = _extract_main_text(test_url)
        
        if text:
            print(f"Extraction SUCCESS. Text length: {len(text)}")
            print(f"Full Extracted Text:\n{text}")
        else:
            print("ERROR: _extract_main_text returned Empty. The content might be protected or empty.")
            
    except Exception as e:
        print(f"Extraction Exception: {e}")

if __name__ == "__main__":
    test_ddgs_and_fetch()
