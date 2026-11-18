from flask import (
    Flask, request, session, redirect,
    url_for, jsonify, render_template, send_from_directory
)
from ddgs import DDGS
from google import genai
import os
import time
from urllib.parse import urlparse

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "ghana-search-local-secret-key")

REGIONS = {
    "worldwide": "wt-wt",
    "ghana": "gh-en",
    "usa": "us-en",
    "uk": "uk-en",
    "canada": "ca-en",
    "australia": "au-en",
    "india": "in-en",
    "germany": "de-de",
    "france": "fr-fr",
    "nigeria": "ng-en",
    "south_africa": "za-en"
}

# Search more than one independent web index for ordinary web searches.
# DDGS currently supports comma-separated backends for text metasearch.
TEXT_BACKENDS = "bing,brave,google,yahoo,duckduckgo,mojeek,startpage"
RESULTS_PER_SEARCH = 40


def _clean_url(value):
    if not value:
        return ""
    value = str(value).strip()
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        return value
    except Exception:
        return ""


def _normalize_result(item, search_type):
    if not isinstance(item, dict):
        return None

    if search_type == "images":
        url = _clean_url(item.get("url"))
        image = _clean_url(item.get("image"))
        title = item.get("title") or "Image result"
        description = item.get("source") or item.get("width") or ""
        category = "IMAGE"
    elif search_type == "news":
        url = _clean_url(item.get("url") or item.get("href"))
        image = _clean_url(item.get("image"))
        title = item.get("title") or "News result"
        description = item.get("body") or item.get("excerpt") or ""
        category = "NEWS"
    elif search_type == "videos":
        url = _clean_url(item.get("content") or item.get("url") or item.get("href"))
        images = item.get("images", {})
        image = ""
        if isinstance(images, dict):
            image = _clean_url(
                images.get("large") or images.get("medium") or images.get("small")
            )
        image = image or _clean_url(item.get("image"))
        title = item.get("title") or "Video result"
        description = item.get("description") or item.get("body") or ""
        category = "VIDEO"
    else:
        url = _clean_url(item.get("href") or item.get("url"))
        image = ""
        title = item.get("title") or "Search result"
        description = item.get("body") or item.get("description") or ""
        category = "JOB" if search_type == "jobs" else "WEB"

    if not url and not image:
        return None

    return {
        "title": str(title)[:500],
        "description": str(description)[:1800],
        "url": url,
        "image": image,
        "category": category
    }


def _deduplicate(items):
    unique = []
    seen = set()
    for item in items:
        key = (item.get("url") or item.get("image") or "").lower().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def search_web(query, search_type, region, safe):
    """Return a larger, de-duplicated result set using DDGS metasearch."""
    region_code = REGIONS.get(region, "wt-wt")
    safe_value = safe if safe in ("strict", "moderate", "off") else "moderate"
    results = []
    error = ""

    try:
        with DDGS(timeout=12) as ddgs:
            if search_type == "images":
                raw = ddgs.images(
                    query, region=region_code, safesearch=safe_value,
                    max_results=RESULTS_PER_SEARCH, page=1
                )
                results = [
                    item for item in
                    (_normalize_result(x, "images") for x in raw)
                    if item
                ]

            elif search_type == "news":
                raw = ddgs.news(
                    query, region=region_code, safesearch=safe_value,
                    max_results=RESULTS_PER_SEARCH, page=1
                )
                results = [
                    item for item in
                    (_normalize_result(x, "news") for x in raw)
                    if item
                ]

            elif search_type == "videos":
                raw = ddgs.videos(
                    query, region=region_code, safesearch=safe_value,
                    max_results=RESULTS_PER_SEARCH, page=1
                )
                results = [
                    item for item in
                    (_normalize_result(x, "videos") for x in raw)
                    if item
                ]

            else:
                search_query = query
                if search_type == "jobs":
                    search_query = f"{query} jobs vacancies careers"

                # First ask DDGS to search several indexes together.
                try:
                    raw = ddgs.text(
                        search_query,
                        region=region_code,
                        safesearch=safe_value,
                        max_results=RESULTS_PER_SEARCH,
                        page=1,
                        backend=TEXT_BACKENDS
                    )
                    results.extend(
                        item for item in
                        (_normalize_result(x, search_type) for x in raw)
                        if item
                    )
                except Exception as backend_error:
                    print("GHANA SEARCH MULTI-INDEX ERROR:", repr(backend_error))

                # If the combined request returns very little, make one
                # automatic-backend fallback request and merge unique links.
                if len(results) < 12:
                    try:
                        raw = ddgs.text(
                            search_query,
                            region=region_code,
                            safesearch=safe_value,
                            max_results=RESULTS_PER_SEARCH,
                            page=1,
                            backend="auto"
                        )
                        results.extend(
                            item for item in
                            (_normalize_result(x, search_type) for x in raw)
                            if item
                        )
                    except Exception as fallback_error:
                        print("GHANA SEARCH FALLBACK ERROR:", repr(fallback_error))

                results = _deduplicate(results)[:RESULTS_PER_SEARCH]

    except Exception as exc:
        print("GHANA SEARCH ERROR:", repr(exc))
        error = (
            "The search provider could not return results right now. "
            "Please try again shortly."
        )

    if not results and not error:
        error = "No results were returned for this search. Try different words."
    return results, error


@app.route("/", methods=["GET", "POST"])
def home():
    query = ""
    search_type = "all"
    results = []
    error = ""

    region = session.get("region", "worldwide")
    safe = session.get("safe", "moderate")

    if request.method == "POST":
        query = request.form.get("search", "").strip()
        search_type = request.form.get("search_type", "all").lower()

        if search_type not in ["all", "news", "images", "videos", "jobs"]:
            search_type = "all"

        region = request.form.get("region", region)
        safe = request.form.get("safesearch", safe)

        if region not in REGIONS:
            region = "worldwide"
        if safe not in ["strict", "moderate", "off"]:
            safe = "moderate"

        session["region"] = region
        session["safe"] = safe

        if query:
            results, error = search_web(query, search_type, region, safe)
            history = session.get("history", [])
            history = [item for item in history if item.lower() != query.lower()]
            history.insert(0, query)
            session["history"] = history[:10]

    return render_template(
        "index.html",
        query=query,
        search_type=search_type,
        results=results,
        error=error,
        history=session.get("history", [])
    )


@app.route("/ai-answer", methods=["POST"])
def ai_answer():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({
            "error": "Gemini API key not found. Set GEMINI_API_KEY in the terminal before starting GHANA SEARCH."
        }), 500

    data = request.get_json(silent=True) or {}
    query = str(data.get("query", "")).strip()

    if not query:
        return jsonify({"error": "Please enter a question."}), 400
    if len(query) > 300:
        return jsonify({"error": "Your question is too long."}), 400

    now = time.time()
    last_request = session.get("last_ai_request", 0)
    if now - last_request < 8:
        return jsonify({
            "error": "Please wait a few seconds before asking again."
        }), 429
    session["last_ai_request"] = now

    region = session.get("region", "worldwide")
    safe = session.get("safe", "moderate")
    results, search_error = search_web(query, "all", region, safe)

    sources = []
    for item in results[:8]:
        if item.get("url"):
            sources.append({
                "title": item.get("title", "Web result"),
                "url": item["url"],
                "description": item.get("description", "")
            })

    source_text = "\n\n".join(
        f"Title: {item['title']}\n"
        f"Description: {item['description']}\n"
        f"URL: {item['url']}"
        for item in sources
    ) or "No web search results were available."

    prompt = f"""
You are GHANA SEARCH AI, an assistant inside the GHANA SEARCH search engine.

Answer the user's question clearly and helpfully.

User question:
{query}

Available web search information:
{source_text}

Instructions:
- Use the supplied search information when it is relevant.
- Do not invent facts, statistics, quotations, or sources.
- If the supplied information is insufficient, say so.
- You may provide general knowledge, but be honest about uncertainty.
- Do not pretend you visited a website.
- Keep the answer easy to read.
"""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )
        answer = response.text

        if not answer:
            return jsonify({
                "error": "The AI returned an empty answer. Please try again."
            }), 502

        return jsonify({
            "answer": answer,
            "sources": sources,
            "search_warning": search_error
        })

    except Exception as exc:
        print("GEMINI AI ERROR:", repr(exc))
        return jsonify({
            "error": (
                "GHANA SEARCH AI could not respond. Check your API key, "
                "model availability, internet connection and Gemini usage limits."
            )
        }), 500


@app.route("/clear-history", methods=["POST"])
def clear_history():
    session["history"] = []
    return redirect(url_for("home"))


@app.route("/suggestions")
def suggestions():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])

    try:
        with DDGS(timeout=8) as ddgs:
            items = list(ddgs.suggestions(query, region="wt-wt"))
        return jsonify([
            item.get("phrase", "")
            for item in items
            if item.get("phrase")
        ][:8])
    except Exception as exc:
        print("SUGGESTIONS ERROR:", repr(exc))
        return jsonify([])


@app.route("/service-worker.js")
def service_worker():
    file_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static",
        "service-worker.js"
    )

    if not os.path.exists(file_path):
        return "Service worker file not found.", 404

    response = send_from_directory(
        os.path.dirname(file_path),
        "service-worker.js",
        mimetype="application/javascript"
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


if __name__ == "__main__":
    print("-----------------------------------")
    print("GHANA SEARCH IS STARTING")
    print("Project file:", os.path.abspath(__file__))
    print("Search indexes:", TEXT_BACKENDS)
    print("Results per search:", RESULTS_PER_SEARCH)
    print("-----------------------------------")
    app.run(host="127.0.0.1", port=5000, debug=True)
 