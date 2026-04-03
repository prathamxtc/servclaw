# Tavily Web Search — Skill Guide

Use `tavily_web_search` whenever the user asks about anything that could be recent, live, or outside your training data.

## When to use it
- Current events, news, or dates ("what happened with X today", "latest release of Y")
- Real-time data: prices, weather, scores, stock values
- Verifying facts you're not confident about
- Research tasks where sourced information matters

## Query tips
- Be specific. "Python 3.13 release date" beats "Python release".
- For simple lookups use `max_results=3`. For research tasks use `max_results=8-10`.
- If the first search doesn't have what you need, rephrase and try once more.

## Using the results
- If `answer` is present in the response, lead with it — it's a direct answer synthesised by Tavily.
- Cite sources naturally. Don't dump all URLs; pick the 1-2 most relevant.
- If results look off-topic, tell the user and offer to search differently.

## Don't use it for
- General knowledge well within training data (history, definitions, how-to explanations)
- Tasks the user wants done locally (coding, file ops, math)
