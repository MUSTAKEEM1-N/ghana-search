import os
import re
import html
import json
import time
import secrets
import logging
from urllib.parse import urlparse
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
RESULTS_PER_PAGE = 20
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
        image = clean_url(item.get("image") or item.get("thumbnail"))
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


def deduplicate(items):
    output, seen = [], set()
    for item in items:
        key = (item.get("url") or item.get("image") or "").lower().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            output.append(item)
    return output


def search_web(query, search_type="all", region="worldwide", safe="moderate", page=1):
    region_code = REGIONS.get(region, "wt-wt")
    safe_value = {"strict": "on", "moderate": "moderate", "off": "off"}.get(safe, "moderate")
    page = max(1, min(int(page), MAX_PAGE))
    try:
        with DDGS(timeout=15) as ddgs:
            common = {
                "region": region_code,
                "safesearch": safe_value,
                "max_results": RESULTS_PER_PAGE,
                "page": page,
            }
            if search_type == "images":
                raw = ddgs.images(query, **common)
            elif search_type == "news":
                raw = ddgs.news(query, **common)
            elif search_type == "videos":
                raw = ddgs.videos(query, **common)
            else:
                search_query = f"{query} jobs vacancies careers" if search_type == "jobs" else query
                try:
                    raw = ddgs.text(search_query, backend="all", **common)
                except Exception as first_error:
                    logger.warning("All-backend search failed; trying auto: %r", first_error)
                    raw = ddgs.text(search_query, backend="auto", **common)

        results = [
            normalized for normalized in
            (normalize_result(item, search_type) for item in raw)
            if normalized
        ]
        return deduplicate(results)[:RESULTS_PER_PAGE], ""
    except Exception as exc:
        logger.exception("Search provider error")
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
    try:
        with DDGS(timeout=8) as ddgs:
            items = list(ddgs.suggestions(query, region=REGIONS.get(session.get("region", "worldwide"), "wt-wt")))
        phrases = [clean_text(x.get("phrase"), 120) for x in items if x.get("phrase")]
        return jsonify(suggestions=phrases[:8])
    except Exception as exc:
        logger.warning("Suggestions failed: %r", exc)
        return jsonify(suggestions=[])


@app.route("/related-searches", methods=["POST"])
def related_searches():
    data = request.get_json(silent=True) or {}
    query = clean_text(data.get("query", ""), 100)
    if len(query) < 2:
        return jsonify(suggestions=[])
    try:
        with DDGS(timeout=8) as ddgs:
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

    region = session.get("region", "worldwide")
    safe = session.get("safe", "moderate")
    sources, warning = search_web(question, "all", region, safe, 1)
    source_links = [
        {"title": x["title"], "url": x["url"], "description": x["description"]}
        for x in sources[:6] if x.get("url")
    ]
    history = session.get("ai_history", [])
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
    topics = [
        ("Ghana", "Ghana latest news"),
        ("World", "world latest news"),
        ("Technology", "latest technology news"),
        ("Business", "global business news"),
        ("Sports", "latest sports news"),
        ("Africa", "Africa latest news"),
    ]
    
    feed = []
    try:
        with DDGS(timeout=15) as ddgs:
            for label, term in topics:
                try:
                    raw = ddgs.news(term, region=region_code, safesearch="moderate", max_results=5, page=1)
                    for item in raw:
                        row = normalize_result(item, "news")
                        if row:
                            row["topic"] = label
                            feed.append(row)
                except Exception as exc:
                    logger.warning("Discover topic %s failed: %r", label, exc)
        feed = deduplicate(feed)
        return jsonify(items=feed[:25], message="" if feed else "The news feed is temporarily unavailable.")
    except Exception:
        logger.exception("Discover feed failed")
        return jsonify(items=[], message="The news feed is temporarily unavailable."), 200


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