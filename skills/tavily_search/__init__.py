"""Tavily web search — built-in skill for Servclaw.

Skill contract:
  TOOL_SCHEMA  — OpenAI function schema dict registered when this skill is enabled.
  execute(args, api_key) -> dict  — called by the agent when the LLM invokes the tool.
"""

import json
import urllib.error
import urllib.request

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tavily_web_search",
        "description": (
            "Search the web for current, real-time information using Tavily. "
            "Use this whenever the user asks about recent events, live data, news, "
            "prices, weather, or anything that may be outside your training data. "
            "Returns a direct answer (when available) and a list of source results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to run.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1–10). Default 5.",
                },
            },
            "required": ["query"],
        },
    },
}


def execute(args: dict, api_key: str) -> dict:
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    max_results = max(1, min(int(args.get("max_results", 5)), 10))

    payload = json.dumps({
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": f"Tavily API error {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": f"Request failed: {e}"}

    results = []
    for r in data.get("results", []):
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", "")[:600],
        })

    out: dict = {"results": results}
    if data.get("answer"):
        out["answer"] = data["answer"]
    return out
