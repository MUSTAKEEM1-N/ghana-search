import os
import re
import html
import json
import time
import secrets
import logging
from urllib.parse import urlparse, urlencode
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template, send_from_directory
)
from ddgs import DDGS
from google import genai

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ghana_search")

REGIONS = {
    "worldwide": "wt-wt", "ghana": "gh-en", "usa": "us-en",
    "uk": "uk-en", "canada": "ca-en", "australia": "au-en",
    "india": "in-en", "germany": "de-de", "france": "fr-fr",
    "nigeria": "ng-en", "south_africa": "za-en",
}
VALID_TYPES = {"all", "news", "images", "videos", "jobs"}
RESULTS_PER_PAGE = 12
MAX_PAGE = 20
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
PAYSTACK_API = "https://api.paystack.co"


def clean_url(value):
    if not value:
        return ""
    try:
        value = str(value).strip()
        parsed = urlparse(value)
        return value if parsed.scheme in ("http", "https") and parsed.netloc else ""
    except Exception:
        return ""


def clean_text(value, limit=700):
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"https?://\S+", " ", value)
    value = " ".join(value.split())
    return value[:limit].strip()


def normalize_result(item, search_type):
    if not isinstance(item, dict):
        return None

    if search_type == "images":
        url = clean_url(item.get("url") or item.get("href"))
        # Prefer provider thumbnails; direct publisher image URLs often block hotlinking.
        image = clean_url(item.get("thumbnail") or item.get("image"))
        title = clean_text(item.get("title"), 250) or "Image result"
        description = clean_text(item.get("source") or item.get("title"), 350)
        category = "IMAGE"
    elif search_type == "news":
        url = clean_url(item.get("url") or item.get("href"))
        image = clean_url(item.get("image") or item.get("thumbnail"))
        title = clean_text(item.get("title"), 250) or "News result"
        description = clean_text(item.get("body") or item.get("excerpt"), 650)
        category = "NEWS"
    elif search_type == "videos":
        url = clean_url(item.get("content") or item.get("url") or item.get("href"))
        images = item.get("images") or {}
        image = ""
        if isinstance(images, dict):
            image = clean_url(images.get("large") or images.get("medium") or images.get("small"))
        image = image or clean_url(item.get("image") or item.get("thumbnail"))
        title = clean_text(item.get("title"), 250) or "Video result"
        description = clean_text(item.get("description") or item.get("body"), 650)
        category = "VIDEO"
    else:
        url = clean_url(item.get("href") or item.get("url"))
        image = ""
        title = clean_text(item.get("title"), 250) or "Search result"
        description = clean_text(item.get("body") or item.get("description"), 650)
        category = "JOB" if search_type == "jobs" else "WEB"

    if not url and not image:
        return None
    if description.lower() == title.lower():
        description = ""

    parsed = urlparse(url) if url else None
    source = (parsed.netloc.replace("www.", "") if parsed else "")
    return {
        "title": title, "description": description, "url": url,
        "image": image, "category": category, "source": source,
        "date": clean_text(item.get("date") or item.get("published"), 80),
    }



def fetch_discover_rss(region_code="wt-wt", page=1):
    """Fallback Discover feed using Google News RSS when DDGS is unavailable."""
    params = {
        "q": "Ghana OR Africa OR world OR technology OR business OR sports",
        "hl": "en",
        "gl": "GH" if region_code == "gh-en" else "US",
        "ceid": "GH:en" if region_code == "gh-en" else "US:en",
        "start": str(max(0, (page - 1) * 18)),
    }
    rss_url = "https://news.google.com/rss/search?" + urlencode(params)
    req = Request(
        rss_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; GhanaSearch/1.0)"}
    )
    with urlopen(req, timeout=6) as response:
        payload = response.read(1_500_000)
    root = ET.fromstring(payload)
    feed = []
    for item in root.findall(".//item")[:18]:
        title = clean_text(item.findtext("title"), 250)
        link = clean_url(item.findtext("link"))
        raw_description = item.findtext("description") or ""
        description = clean_text(raw_description, 650)
        image = ""
        for image_tag in item.iter():
            if image_tag.tag.endswith(("}thumbnail", "}content", "}enclosure")):
                image = clean_url(image_tag.attrib.get("url", ""))
                if image:
                    break
        if not title or not link:
            continue
        value = (title + " " + description).lower()
        if any(word in value for word in ("ghana", "accra", "kumasi", "tamale")):
            topic = "Ghana"
        elif any(word in value for word in ("technology", "artificial intelligence", "software", "smartphone")):
            topic = "Technology"
        elif any(word in value for word in ("business", "market", "economy", "finance")):
            topic = "Business"
        elif any(word in value for word in ("football", "sports", "soccer", "league")):
            topic = "Sports"
        elif "africa" in value:
            topic = "Africa"
        else:
            topic = "World"
        feed.append({
            "title": title,
            "description": description,
            "url": link,
            "image": image,
            "category": "NEWS",
            "source": "Google News",
            "date": clean_text(item.findtext("pubDate"), 80),
            "topic": topic,
        })
    return feed


def deduplicate(items):
    output, seen = [], set()
    for item in items:
        key = (item.get("url") or item.get("image") or "").lower().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            output.append(item)
    return output


def result_matches_query(item, query):
    """Reject clearly unrelated vertical results without requiring every query word."""
    stop_words = {
        "the", "and", "for", "with", "from", "about", "who", "what", "where",
        "when", "why", "how", "are", "was", "were", "is", "in", "on", "at",
        "to", "of", "a", "an", "me", "show", "find", "search", "pictures",
        "picture", "images", "image", "videos", "video", "photos", "photo",
    }
    terms = [
        word for word in re.findall(r"[a-z0-9]+", query.lower())
        if len(word) > 1 and word not in stop_words
    ]
    if not terms:
        return True
    text_value = " ".join(str(item.get(key, "")) for key in
                          ("title", "description", "source", "publisher", "uploader")).lower()
    text_value = re.sub(r"[^a-z0-9]+", " ", text_value)
    matches = sum(
        1 for term in set(terms)
        if re.search(r"\b" + re.escape(term) + r"\b", text_value)
    )
    required = 1 if len(set(terms)) <= 2 else 2
    return matches >= required


def search_web(query, search_type="all", region="worldwide", safe="moderate", page=1):
    region_code = REGIONS.get(region, "wt-wt")
    safe_value = {"strict": "on", "moderate": "moderate", "off": "off"}.get(safe, "moderate")
    page = max(1, min(int(page), MAX_PAGE))
    common = {
        "region": region_code,
        "safesearch": safe_value,
        "max_results": 18 if search_type in ("images", "videos") else 12,
        "page": page,
    }
    try:
        with DDGS(timeout=5) as ddgs:
            if search_type == "images":
                try:
                    raw = ddgs.images(query, backend="bing", **common)
                except Exception as image_error:
                    logger.warning("Bing image search failed; trying DuckDuckGo: %r", image_error)
                    raw = ddgs.images(query, backend="duckduckgo", **common)
            elif search_type == "news":
                raw = ddgs.news(query, **common)
            elif search_type == "videos":
                raw = ddgs.videos(query, backend="duckduckgo", **common)
            else:
                search_query = f"{query} jobs vacancies careers" if search_type == "jobs" else query
                raw = ddgs.text(search_query, backend="auto", **common)

        results = []
        for item in raw:
            if search_type in ("images", "videos") and not result_matches_query(item, query):
                continue
            normalized = normalize_result(item, search_type)
            if normalized:
                results.append(normalized)
        return deduplicate(results)[:12], ""
    except Exception as exc:
        logger.warning("Search provider request failed: %r", exc)
        return [], "The search provider could not return results right now. Please try again shortly."


def get_ai_client():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    return genai.Client(api_key=key)


def ai_reply(question, history=None, sources=None):
    client = get_ai_client()
    if client is None:
        raise RuntimeError("The AI service is not configured yet. Please try again later.")

    history = history or []
    sources = sources or []
    source_text = "\n\n".join(
        f"Title: {s.get('title','')}\nDescription: {s.get('description','')}\nURL: {s.get('url','')}"
        for s in sources[:8]
    ) or "No live web results were available for this question."
    conversation = []
    for turn in history[-8:]:
        role = "User" if turn.get("role") == "user" else "GHANA SEARCH AI"
        conversation.append(f"{role}: {clean_text(turn.get('text'), 650)}")

    prompt = f"""You are GHANA SEARCH AI, a helpful conversational assistant inside a worldwide search engine built in Ghana.

Answer clearly, naturally and accurately. Maintain continuity with the conversation.
Use the live web results below for current or factual claims when relevant.
Never invent facts, citations, dates, figures or sources. If sources are weak or unavailable, say so.
Do not claim that you browsed a page; say that search results indicate the information.
For questions that do not need current information, answer directly using your knowledge.
Use readable formatting and concise explanations.
   
Previous conversation:
{chr(10).join(conversation) if conversation else "This is the start of the conversation."}

Current user question:
{question}

Live web search results:
{source_text}
"""
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    answer = getattr(response, "text", None)
    if not answer:
        raise RuntimeError("The AI returned an empty answer. Please try again.")
    return answer.strip()


@app.route("/", methods=["GET", "POST"])
def home():
    query = ""
    search_type = "all"
    results = []
    error = ""
    region = session.get("region", "worldwide")
    safe = session.get("safe", "moderate")

    if request.method == "POST":
        query = clean_text(request.form.get("search", ""), 300)
        search_type = request.form.get("search_type", "all").lower()
        if search_type not in VALID_TYPES:
            search_type = "all"
        region = request.form.get("region", region)
        safe = request.form.get("safe", safe)
        if region not in REGIONS:
            region = "worldwide"
        if safe not in ("strict", "moderate", "off"):
            safe = "moderate"
        session["region"], session["safe"] = region, safe

        if query:
            results, error = search_web(query, search_type, region, safe, 1)
            if request.form.get("incognito") != "1":
                history = session.get("history", [])
                history = [x for x in history if x.lower() != query.lower()]
                history.insert(0, query)
                session["history"] = history[:12]

    return render_template(
        "index.html", query=query, search_type=search_type, results=results,
        error=error, history=session.get("history", []), region=region, safe=safe,
        adsense_client=os.environ.get("ADSENSE_CLIENT_ID", ""),
        adsense_slot=os.environ.get("ADSENSE_SLOT_ID", ""),
    )


@app.route("/load-more", methods=["POST"])
def load_more():
    data = request.get_json(silent=True) or {}
    query = clean_text(data.get("query", ""), 300)
    search_type = str(data.get("search_type", "all")).lower()
    region = str(data.get("region", "worldwide"))
    safe = str(data.get("safe", "moderate"))
    try:
        page = int(data.get("page", 2))
    except (TypeError, ValueError):
        page = 2
    if not query:
        return jsonify(error="Please enter a search query."), 400
    if search_type not in VALID_TYPES:
        search_type = "all"
    if region not in REGIONS:
        region = "worldwide"
    if safe not in ("strict", "moderate", "off"):
        safe = "moderate"
    if page < 2 or page > MAX_PAGE:
        return jsonify(error="No more result pages are available."), 400
    results, error = search_web(query, search_type, region, safe, page)
    return jsonify(results=results, error=error, has_more=len(results) == RESULTS_PER_PAGE, next_page=page + 1)


@app.route("/suggestions", methods=["GET", "POST"])
def suggestions():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        query = clean_text(data.get("query", ""), 100)
    else:
        query = clean_text(request.args.get("q", ""), 100)
    if len(query) < 2:
        return jsonify(suggestions=[])
    phrases = []
    try:
        with DDGS(timeout=2) as ddgs:
            items = list(ddgs.suggestions(query, region=REGIONS.get(session.get("region", "worldwide"), "wt-wt")))
        phrases = [clean_text(x.get("phrase"), 120) for x in items if x.get("phrase")]
    except Exception as exc:
        logger.warning("Suggestions provider unavailable: %r", exc)

    # Keep autocomplete useful even when the external provider is slow or unavailable.
    if not phrases:
        phrases = [
            query + " news",
            query + " meaning",
            query + " in Ghana",
            query + " today",
            query + " images",
            query + " videos",
        ]

    unique = []
    seen = set()
    for phrase in phrases:
        key = phrase.casefold()
        if phrase and key not in seen:
            seen.add(key)
            unique.append(phrase)
    return jsonify(suggestions=unique[:8])


@app.route("/related-searches", methods=["POST"])
def related_searches():
    data = request.get_json(silent=True) or {}
    query = clean_text(data.get("query", ""), 100)
    if len(query) < 2:
        return jsonify(suggestions=[])
    try:
        with DDGS(timeout=4) as ddgs:
            items = list(ddgs.suggestions(query, region="wt-wt"))
        phrases = [clean_text(x.get("phrase"), 100) for x in items if x.get("phrase")]
        return jsonify(suggestions=phrases[:6])
    except Exception:
        return jsonify(suggestions=[])


@app.route("/ai-chat", methods=["POST"])
def ai_chat():
    data = request.get_json(silent=True) or {}
    question = clean_text(data.get("query", ""), 1200)
    if not question:
        return jsonify(error="Type a question first."), 400
    now = time.time()
    last = session.get("last_ai_request", 0)
    if now - last < 3:
        return jsonify(error="Please wait a moment before sending another message."), 429
    session["last_ai_request"] = now

    # Greetings and short social messages should go straight to AI.
    # This avoids irrelevant web results and saves search-provider requests.
    greeting_pattern = re.compile(
        r"^(?:hi|hello|hey|hiya|howdy|good morning|good afternoon|good evening)"
        r"(?: there)?[!.?, ]*$",
        re.IGNORECASE,
    )
    is_greeting = bool(greeting_pattern.fullmatch(question.strip()))

    history = session.get("ai_history", [])

    if is_greeting:
        source_links = []
        warning = ""
    else:
        region = session.get("region", "worldwide")
        safe = session.get("safe", "moderate")

        # For short follow-ups, include the previous user question in the
        # web search so words like "when?", "why?" and "what about it?"
        # are searched with the right context.
        previous_user_question = next(
            (clean_text(turn.get("text"), 180)
             for turn in reversed(history)
             if turn.get("role") == "user" and turn.get("text")),
            "",
        )
        followup_pattern = re.compile(
            r"^(?:and\b|also\b|what about\b|how about\b|why\b|when\b|where\b|who\b|which\b|how many\b|how much\b|tell me more\b|explain more\b|continue\b|what happened\b|is it\b|does it\b|can it\b|they\b|he\b|she\b|it\b)",
            re.IGNORECASE,
        )
        is_followup = bool(previous_user_question) and (
            len(question.split()) <= 9 or followup_pattern.search(question.strip())
        )
        search_query = (
            f"{previous_user_question} {question}"
            if is_followup else question
        )
        sources, warning = search_web(search_query, "all", region, safe, 1)
        source_links = [
            {"title": x["title"], "url": x["url"], "description": x["description"]}
            for x in sources[:6] if x.get("url")
        ]
    try:
        answer = ai_reply(question, history, source_links)
        history.extend([
            {"role": "user", "text": question},
            {"role": "assistant", "text": answer},
        ])
        session["ai_history"] = history[-8:]
        return jsonify(answer=answer, sources=source_links, search_warning=warning)
    except Exception as exc:
        logger.exception("Gemini chat error")
        return jsonify(error=str(exc) if "not configured" in str(exc).lower() else
                       "GHANA SEARCH AI could not respond right now. Please try again."), 502


@app.route("/ai-answer", methods=["POST"])
def ai_answer():
    # Backwards-compatible endpoint used by older versions of the page.
    return ai_chat()


@app.route("/ai-chat/clear", methods=["POST"])
def clear_ai_chat():
    session.pop("ai_history", None)
    return jsonify(ok=True)


@app.route("/discover")
def discover():
    return render_template(
        "index.html", query="", search_type="all", results=[],
        error="", history=session.get("history", []),
        region=session.get("region", "worldwide"),
        safe=session.get("safe", "moderate"),
        discover_page=True,
        adsense_client=os.environ.get("ADSENSE_CLIENT_ID", ""),
        adsense_slot=os.environ.get("ADSENSE_SLOT_ID", ""),
    )


@app.route("/api/discover")
def api_discover():
    region = request.args.get("region", session.get("region", "worldwide"))
    region_code = REGIONS.get(region, "wt-wt")
    try:
        page = max(1, min(int(request.args.get("page", "1")), 10))
    except (TypeError, ValueError):
        page = 1
    try:
        with DDGS(timeout=5) as ddgs:
            raw = ddgs.news(
                "trending breaking latest world news",
                region=region_code, safesearch="moderate",
                max_results=18, page=page,
            )
            feed = []
            for item in raw:
                row = normalize_result(item, "news")
                if not row:
                    continue
                value = (row.get("title", "") + " " + row.get("description", "")).lower()
                if any(w in value for w in ("ghana", "accra", "kumasi", "tamale")):
                    row["topic"] = "Ghana"
                elif any(w in value for w in ("technology", "artificial intelligence", "software")):
                    row["topic"] = "Technology"
                elif any(w in value for w in ("business", "market", "economy", "finance")):
                    row["topic"] = "Business"
                elif any(w in value for w in ("football", "sports", "soccer", "league")):
                    row["topic"] = "Sports"
                elif "africa" in value:
                    row["topic"] = "Africa"
                else:
                    row["topic"] = "World"
                feed.append(row)
        feed = deduplicate(feed)
        if feed:
            return jsonify(items=feed[:18], message="")
        logger.warning("Discover primary provider returned no stories; trying RSS fallback.")
    except Exception as exc:
        logger.warning("Discover primary provider failed: %r; trying RSS fallback.", exc)
    try:
        feed = deduplicate(fetch_discover_rss(region_code, page=page))
        if feed:
            return jsonify(items=feed[:18], message="")
    except Exception as exc:
        logger.warning("Discover RSS fallback failed: %r", exc)
    return jsonify(items=[], message="Worldwide trending stories are temporarily unavailable. Please try again shortly."), 200


@app.route("/support")
def support_page():
    return render_template("index.html", query="", search_type="all", results=[],
                           error="", history=session.get("history", []),
                           region=session.get("region", "worldwide"),
                           safe=session.get("safe", "moderate"), support_page=True,
                           adsense_client=os.environ.get("ADSENSE_CLIENT_ID", ""),
                           adsense_slot=os.environ.get("ADSENSE_SLOT_ID", ""))



def paystack_request(path, payload=None, method="GET"):
    secret = os.environ.get("PAYSTACK_SECRET_KEY")

    if not secret:
        raise RuntimeError(
            "Paystack is not configured. Add PAYSTACK_SECRET_KEY in Render environment settings."
        )

    body = json.dumps(payload).encode("utf-8") if payload is not None else None

    req = Request(
        PAYSTACK_API + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print("PAYSTACK HTTP ERROR:", error.code, details)
        raise RuntimeError(f"Paystack error ({error.code}): {details}")

    except URLError as error:
        print("PAYSTACK CONNECTION ERROR:", str(error))
        raise RuntimeError("Could not connect to Paystack. Please try again.")


@app.route("/support/initialize", methods=["POST"])
def support_initialize():
    data = request.get_json(silent=True) or request.form
    email = clean_text(data.get("email", ""), 180)
    try:
        amount = round(float(data.get("amount", "0")), 2)
    except (TypeError, ValueError):
        amount = 0
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify(error="Enter a valid email address."), 400
    if amount < 0.10 or amount > 10000:
        return jsonify(error="Enter an amount between GH₵0.10 and GH₵10,000."), 400

    reference = "GS-" + secrets.token_hex(12)
    expected_amount = int(round(amount * 100))
    session["support_expected_amount"] = expected_amount
    payload = {
        "email": email,
        "amount": expected_amount,
        "currency": "GHS",
        "reference": reference,
        "callback_url": url_for("support_callback", _external=True),
        "metadata": {
            "custom_fields": [
                {"display_name": "Purpose", "variable_name": "purpose",
                 "value": "Support GHANA SEARCH"}
            ]
        },
    }
    try:
        result = paystack_request("/transaction/initialize", payload, "POST")
        if not result.get("status") or not result.get("data", {}).get("authorization_url"):
            return jsonify(error="Paystack could not start the payment. Please try again."), 502
        session["support_reference"] = reference
        return jsonify(url=result["data"]["authorization_url"])
    except HTTPError as exc:
        logger.error("Paystack initialize HTTP error: %s", exc.code)
        return jsonify(error="Paystack could not start the payment. Check your Paystack setup and try again."), 502
    except Exception:
        logger.exception("Paystack initialization failed")
        return jsonify(error="Payments are not configured yet. Please try again later."), 503


@app.route("/support/callback")
def support_callback():
    reference = clean_text(request.args.get("reference", ""), 100)
    if not reference:
        return redirect(url_for("support_page") + "?payment=missing")
    try:
        result = paystack_request("/transaction/verify/" + reference)
        details = result.get("data") or {}
        valid = (
            result.get("status") is True
            and details.get("status") == "success"
            and details.get("currency") == "GHS"
            and int(details.get("amount", 0)) == int(session.get("support_expected_amount", -1))
            and reference == session.get("support_reference")
        )
        if valid:
            session["support_payment_success"] = True
            session["support_reference"] = reference
            return redirect(url_for("support_page") + "?payment=success")
        return redirect(url_for("support_page") + "?payment=notconfirmed")
    except Exception:
        logger.exception("Paystack verification failed")
        return redirect(url_for("support_page") + "?payment=verifyerror")


@app.route("/health")
def health():
    return jsonify(status="online", service="GHANA SEARCH",
                   ai_configured=bool(os.environ.get("GEMINI_API_KEY")),
                   payments_configured=bool(os.environ.get("PAYSTACK_SECRET_KEY")),
                   ads_configured=bool(os.environ.get("ADSENSE_CLIENT_ID") and os.environ.get("ADSENSE_SLOT_ID")))


@app.route("/clear-history", methods=["POST"])
def clear_history():
    session["history"] = []
    return redirect(url_for("home"))


@app.route("/service-worker.js")
def service_worker():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "service-worker.js")
    if not os.path.exists(path):
        return "Service worker file not found.", 404
    response = send_from_directory(os.path.dirname(path), "service-worker.js",
                                   mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


if __name__ == "__main__":
    print("GHANA SEARCH starting | DDGS page size:", RESULTS_PER_PAGE)
    app.run(host="127.0.0.1", port=5000, debug=True)