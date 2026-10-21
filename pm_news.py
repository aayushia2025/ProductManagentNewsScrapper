#!/usr/bin/env python3
"""PM News Radar - scan worldwide news on product management and build an interactive HTML digest.

Usage:
    python3 pm_news.py                 # last 7 days, open report in browser
    python3 pm_news.py --days 3 --max 150 --out report.html --no-open
    python3 pm_news.py --topics "AI agents product" "B2B pricing"

Optional LLM summaries: set OPENAI_API_KEY or ANTHROPIC_API_KEY (else extractive summaries are used).
No third-party dependencies required.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) PMNewsRadar/1.0"

DEFAULT_TOPICS = {
    "Product Management": '"product management" OR "product manager"',
    "AI Products": '"AI product" OR "AI agents" product launch',
    "Product Strategy": '"product strategy" OR "product roadmap"',
    "Growth & PLG": '"product-led growth" OR "growth strategy" product',
    "Pricing & Monetization": '"pricing strategy" OR "monetization strategy" OR "subscription pricing" OR "usage-based pricing"',
    "User Research & UX": '"user research" OR "customer experience" product design',
    "Product Launches": '"launches new" product feature app',
    "Careers & Org": '"product leaders" OR "chief product officer" OR "product team" layoffs hiring',
}

REGIONS = {  # Google News edition: (hl, gl, ceid)
    "US": ("en-US", "US", "US:en"),
    "UK": ("en-GB", "GB", "GB:en"),
    "India": ("en-IN", "IN", "IN:en"),
    "Australia": ("en-AU", "AU", "AU:en"),
    "Singapore": ("en-SG", "SG", "SG:en"),
    "Canada": ("en-CA", "CA", "CA:en"),
}

BLOG_FEEDS = {
    "Lenny's Newsletter": "https://www.lennysnewsletter.com/feed",
    "Product Talk": "https://www.producttalk.org/feed/",
    "SVPG": "https://www.svpg.com/feed/",
}

PM_TERMS = re.compile(r"product manag|product manager|product owner|product strateg|roadmap|product-led|\bplg\b|"
                      r"pricing|monetiz|subscription|user research|\bux\b|customer experience|product launch|"
                      r"launch|chief product|\bcpo\b|product team|product leader|feature|ai agent|agentic|"
                      r"\bgrowth\b|retention|onboarding|saas|startup|discovery|okr|prioriti|a/b test|experiment",
                      re.I)

STOPWORDS = set("""a an the and or of to in for on with at by from as is are was were be been it its this that
these those new how why what who will can about into over after more most than up out not your you our we their
they he she his her them us via vs says said say just also amid inc ltd co news report reports year years week
first one two get gets make makes using use used now all has have had do does day top best
""".split())


# ---------------------------------------------------------------- fetching
def fetch(url, timeout=15, _hops=0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:  # Python <3.11 urllib doesn't follow 308
        if e.code in (307, 308) and e.headers.get("Location") and _hops < 5:
            return fetch(urllib.parse.urljoin(url, e.headers["Location"]), timeout, _hops + 1)
        raise


def clean_text(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def parse_feed(raw):
    """Parse RSS 2.0 or Atom into a list of dicts."""
    root = ET.fromstring(raw)
    items = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        src = it.find("source")
        items.append({
            "title": clean_text(it.findtext("title")),
            "link": (it.findtext("link") or "").strip(),
            "desc": it.findtext("description") or "",
            "date": parse_date(it.findtext("pubDate")),
            "source": clean_text(src.text) if src is not None and src.text else None,
        })
    for e in root.iter(atom + "entry"):
        link_el = e.find(atom + "link")
        items.append({
            "title": clean_text(e.findtext(atom + "title")),
            "link": link_el.get("href") if link_el is not None else "",
            "desc": e.findtext(atom + "summary") or e.findtext(atom + "content") or "",
            "date": parse_date(e.findtext(atom + "updated") or e.findtext(atom + "published")),
            "source": None,
        })
    return items


def google_news_job(topic, query, region, days):
    hl, gl, ceid = REGIONS[region]
    q = urllib.parse.quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    out = []
    for it in parse_feed(fetch(url)):
        title, source = it["title"], it["source"]
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)]
        # Google's description is a link list; keep only text that isn't just the title/source
        desc = clean_text(it["desc"]).replace(title, "").replace(source or "", "").strip(" -\u00a0")
        out.append(dict(it, title=title, desc=desc, topic=topic, region=region,
                        source=source or "Google News", kind="News"))
    return out


def bing_news_job(topic, query, region, days):
    gl = REGIONS[region][1]
    interval = "7" if days <= 1 else "8" if days <= 7 else "9"  # Bing: 7=24h, 8=week, 9=month
    url = ("https://www.bing.com/news/search?format=rss&q=%s&cc=%s&qft=%s"
           % (urllib.parse.quote_plus(query), gl, urllib.parse.quote(f'interval="{interval}"')))
    raw = fetch(url)
    out = []
    root = ET.fromstring(raw)
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        src = next((c.text for c in it if c.tag.endswith("Source")), None)
        desc = clean_text(it.findtext("description"))
        desc = re.sub(r"^(This voice experience is generated by AI\. Learn more\. ?)+", "", desc)
        out.append({"title": clean_text(it.findtext("title")), "link": real, "desc": desc,
                    "date": parse_date(it.findtext("pubDate")), "source": src or "Bing News",
                    "topic": topic, "region": region, "kind": "News"})
    return out


def mtp_job(days):
    data = json.loads(fetch("https://www.mindtheproduct.com/api/marketo-posts-feed"))
    out = []
    for r in data.get("results", [])[:25]:
        d = r.get("data", {})
        if d.get("title") and d.get("url"):
            out.append({"title": clean_text(d["title"]), "link": d["url"], "desc": clean_text(d.get("excerpt") or ""),
                        "date": None, "source": "Mind the Product", "topic": "Product Management",
                        "region": "Global", "kind": "Blog"})
    return out


def blog_job(name, url, days):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(days, 14))
    out = []
    for it in parse_feed(fetch(url)):
        if it["date"] and it["date"] < cutoff:
            continue
        out.append(dict(it, desc=clean_text(it["desc"]), topic="Product Management", region="Global",
                        source=name, kind="Blog"))
    return out


def hn_job(days):
    since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).timestamp())
    out = []
    for q in ["product management", "product manager", "product strategy", "pricing"]:
        url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=20&numericFilters=created_at_i>%d&query=%s"
               % (since, urllib.parse.quote(q)))
        for h in json.loads(fetch(url)).get("hits", []):
            if not h.get("title"):
                continue
            out.append({
                "title": h["title"],
                "link": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                "desc": f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments on Hacker News.",
                "date": parse_date(h.get("created_at")), "source": "Hacker News",
                "topic": "Product Management", "region": "Global", "kind": "Community",
                "points": h.get("points", 0),
            })
    return out


def gather(topics, regions, days, log):
    jobs = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for t, q in topics.items():
            for r in regions:
                jobs.append((f"Google News [{r}] {t}", ex.submit(google_news_job, t, q, r, days)))
                jobs.append((f"Bing News [{r}] {t}", ex.submit(bing_news_job, t, q, r, days)))
        for n, u in BLOG_FEEDS.items():
            jobs.append((n, ex.submit(blog_job, n, u, days)))
        jobs.append(("Mind the Product", ex.submit(mtp_job, days)))
        jobs.append(("Hacker News", ex.submit(hn_job, days)))
        items, ok, fail = [], 0, 0
        for label, fut in jobs:
            try:
                items.extend(fut.result())
                ok += 1
            except Exception as e:  # noqa: BLE001 - one bad feed shouldn't kill the run
                fail += 1
                log(f"  ! {label}: {type(e).__name__}: {e}")
    log(f"  fetched {len(items)} raw items from {ok} feeds ({fail} failed)")
    return items


# ---------------------------------------------------------------- processing
def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:80]


def tokens(text):
    return [w for w in re.findall(r"[a-z][a-z0-9\-]{2,}", text.lower()) if w not in STOPWORDS]


def dedupe_and_rank(items, days, limit):
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days)
    groups = {}
    for it in items:
        if not it["title"] or (it["kind"] == "News" and it["date"] and it["date"] < cutoff):
            continue
        key = norm_title(it["title"])
        g = groups.get(key)
        if g is None:
            it["regions"], it["topics"], it["hits"] = {it["region"]}, {it["topic"]}, 1
            groups[key] = it
        else:
            g["regions"].add(it["region"])
            g["topics"].add(it["topic"])
            g["hits"] += 1
            if len(it.get("desc", "")) > len(g.get("desc", "")):
                g["desc"] = it["desc"]
    # global term frequency -> "trending" weight
    df = Counter()
    for g in groups.values():
        df.update(set(tokens(g["title"])))
    for g in groups.values():
        age_h = (now - g["date"]).total_seconds() / 3600 if g["date"] else 24 * days
        recency = max(0.0, 1 - age_h / (24 * max(days, 1)))
        trend = sum(min(df[w], 10) for w in set(tokens(g["title"]))) / 10
        rel = 3 * len(PM_TERMS.findall(g["title"])) + len(PM_TERMS.findall(g.get("desc", "")))
        g["score"] = round(2 * recency + 0.6 * min(g["hits"], 6) + 0.5 * len(g["regions"]) + 0.5 * trend
                           + min(rel, 8) - (3 if rel == 0 else 0) + (2 if g.get("desc") else 0)
                           + (3 if g["kind"] == "Blog" else 0) + min(g.get("points", 0), 300) / 50, 2)
    ranked = sorted(groups.values(), key=lambda g: g["score"], reverse=True)
    # guarantee curated PM blogs and top community threads a place in the report
    reserved = [g for g in ranked if g["kind"] == "Blog"][:30] + [g for g in ranked if g["kind"] == "Community"][:15]
    rest = [g for g in ranked if g not in reserved][: max(0, limit - len(reserved))]
    return sorted(reserved + rest, key=lambda g: g["score"], reverse=True), df


def extractive_summary(text, max_sentences=2, max_chars=320):
    sents = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(sents[:max_sentences]).strip()
    return (out[: max_chars - 1] + "…") if len(out) > max_chars else out


# ---------------------------------------------------------------- optional LLM
def llm(prompt, max_tokens=900):
    if os.getenv("ANTHROPIC_API_KEY"):
        body = {"model": os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"), "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                     headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                              "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())["content"][0]["text"]
    if os.getenv("OPENAI_API_KEY"):
        body = {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                                              "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    return None


def build_digest(stories, df, use_llm, log):
    """Return (mode, list_of_bullets, trending_terms)."""
    trending = [w for w, c in df.most_common(60) if c >= 3 and w not in {"product", "products"}][:15]
    if use_llm:
        headlines = "\n".join(f"- [{s['topic_main']}] {s['title']} ({s['source']}, {', '.join(sorted(s['regions']))})"
                              for s in stories[:60])
        prompt = ("You are a briefing analyst for a Product Manager. From these headlines, write 6-8 concise bullet "
                  "points (one line each, start with a bold 2-4 word theme like **AI agents:**) summarizing the most "
                  "important developments and what they mean for product managers. Output only the bullets, "
                  "each starting with '- '.\n\n" + headlines)
        try:
            text = llm(prompt)
            if text:
                bullets = [b.strip()[2:].strip() for b in text.splitlines() if b.strip().startswith(("- ", "* "))]
                if bullets:
                    return "AI-generated", bullets, trending
        except Exception as e:  # noqa: BLE001
            log(f"  ! LLM summary failed ({e}); falling back to extractive")
    bullets = []
    by_topic = {}
    for s in stories:
        by_topic.setdefault(s["topic_main"], []).append(s)
    for topic, ss in sorted(by_topic.items(), key=lambda kv: -sum(x["score"] for x in kv[1])):
        top = ss[0]
        bullets.append(f"**{topic}** ({len(ss)} stories): {top['title']} — {top['source']}")
    return "Extractive", bullets, trending


# ---------------------------------------------------------------- HTML
def md_bold(s):
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(s))


def render_html(stories, digest, days, generated):
    mode, bullets, trending = digest
    data = [{
        "title": s["title"], "link": s["link"], "summary": s["summary"], "source": s["source"],
        "date": s["date"].isoformat() if s["date"] else None, "topics": sorted(s["topics"]),
        "regions": sorted(s["regions"]), "kind": s["kind"], "score": s["score"], "hits": s["hits"],
    } for s in stories]
    topics = sorted({t for s in stories for t in s["topics"]})
    regions = sorted({r for s in stories for r in s["regions"]})
    digest_html = "".join(f"<li>{md_bold(b)}</li>" for b in bullets)
    trend_html = "".join(f'<button class="chip trend" data-q="{html.escape(t)}">{html.escape(t)}</button>'
                         for t in trending)
    return (HTML_TEMPLATE
            .replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
            .replace("__TOPICS__", json.dumps(topics))
            .replace("__REGIONS__", json.dumps(regions))
            .replace("__DIGEST__", digest_html)
            .replace("__MODE__", mode)
            .replace("__TRENDS__", trend_html)
            .replace("__META__", f"{len(stories)} stories · last {days} days · generated {generated}"))


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PM News Radar</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--fg:#1d2330;--muted:#667085;--accent:#5b5bd6;--border:#e4e7ec;--chip:#eef0ff}
.dark{--bg:#0f1117;--card:#181b24;--fg:#e6e8ee;--muted:#98a2b3;--accent:#8b8cff;--border:#2a2f3c;--chip:#23263a}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--border);padding:14px 24px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
h1{font-size:20px;margin:0 12px 0 0}.meta{color:var(--muted);font-size:13px}
input,select,button{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:7px 10px}
input[type=search]{flex:1;min-width:220px}button{cursor:pointer}
main{max-width:1200px;margin:0 auto;padding:20px 24px}
.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 20px;margin-bottom:18px}
.panel h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;cursor:pointer}
.panel ul{margin:0;padding-left:20px}.panel li{margin:4px 0}
.badge{font-size:11px;background:var(--chip);color:var(--accent);padding:2px 8px;border-radius:99px;font-weight:600}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.chip{border-radius:99px;padding:4px 12px;font-size:13px;background:var(--chip);border:1px solid transparent}
.chip.on{background:var(--accent);color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 16px;display:flex;flex-direction:column;gap:8px;transition:transform .1s,box-shadow .1s}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.08)}
.card a.t{color:var(--fg);font-weight:600;text-decoration:none;font-size:16px}.card a.t:hover{color:var(--accent)}
.card .s{color:var(--muted);font-size:14px}.card .m{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--muted)}
.tag{background:var(--chip);border-radius:6px;padding:1px 7px;cursor:pointer}
.star{margin-left:auto;border:none;background:none;font-size:18px;padding:0;color:var(--muted)}.star.on{color:#f5b301}
.read{opacity:.55}.empty{text-align:center;color:var(--muted);padding:40px}
.stat{display:flex;gap:18px;flex-wrap:wrap}.stat div{font-size:13px;color:var(--muted)}.stat b{display:block;font-size:22px;color:var(--fg)}
</style></head><body>
<header><div class="row">
 <h1>📡 PM News Radar</h1><span class="meta">__META__</span>
 <span style="flex:1"></span>
 <button id="theme" title="Toggle theme">🌓</button>
</div>
<div class="row" style="margin-top:10px">
 <input type="search" id="q" placeholder="Search headlines, summaries, sources…  ( / )">
 <select id="region"><option value="">🌍 All regions</option></select>
 <select id="kind"><option value="">All types</option><option>News</option><option>Blog</option><option>Community</option></select>
 <select id="sort"><option value="score">Sort: Relevance</option><option value="date">Sort: Newest</option><option value="hits">Sort: Coverage</option></select>
 <label class="meta"><input type="checkbox" id="saved"> ★ Saved only</label>
 <label class="meta"><input type="checkbox" id="hideRead"> Hide read</label>
</div>
<div class="chips" id="topics"></div>
</header>
<main>
 <section class="panel" id="digestPanel"><h2 onclick="this.nextElementSibling.hidden=!this.nextElementSibling.hidden">🧠 Executive digest <span class="badge">__MODE__</span></h2>
  <div><ul>__DIGEST__</ul>
  <div class="chips"><span class="meta" style="align-self:center">🔥 Trending terms:</span>__TRENDS__</div></div></section>
 <section class="panel stat" id="stats"></section>
 <div class="grid" id="grid"></div>
</main>
<script>
const DATA=__DATA__, TOPICS=__TOPICS__, REGIONS=__REGIONS__;
const $=s=>document.querySelector(s), LS=k=>JSON.parse(localStorage.getItem(k)||"[]"), SS=(k,v)=>localStorage.setItem(k,JSON.stringify(v));
let saved=new Set(LS("pmr_saved")), read=new Set(LS("pmr_read")), activeTopic="";
if(localStorage.getItem("pmr_dark")==="1"||(!localStorage.getItem("pmr_dark")&&matchMedia("(prefers-color-scheme: dark)").matches))document.body.classList.add("dark");
$("#theme").onclick=()=>{document.body.classList.toggle("dark");localStorage.setItem("pmr_dark",document.body.classList.contains("dark")?"1":"0")};
REGIONS.forEach(r=>$("#region").insertAdjacentHTML("beforeend",`<option>${r}</option>`));
function chips(){$("#topics").innerHTML=["All",...TOPICS].map(t=>{const v=t==="All"?"":t,n=v?DATA.filter(d=>d.topics.includes(v)).length:DATA.length;
 return `<button class="chip ${v===activeTopic?"on":""}" data-t="${v}">${t} <span style="opacity:.7">${n}</span></button>`}).join("");
 document.querySelectorAll("#topics .chip").forEach(b=>b.onclick=()=>{activeTopic=b.dataset.t;chips();render()})}
const esc=s=>(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function ago(iso){if(!iso)return"";const h=(Date.now()-new Date(iso))/36e5;return h<1?"just now":h<24?Math.round(h)+"h ago":Math.round(h/24)+"d ago"}
function render(){
 const q=$("#q").value.toLowerCase().trim(),rg=$("#region").value,kd=$("#kind").value,so=$("#sort").value;
 let list=DATA.filter(d=>(!activeTopic||d.topics.includes(activeTopic))&&(!rg||d.regions.includes(rg))&&(!kd||d.kind===kd)
  &&(!$("#saved").checked||saved.has(d.link))&&(!$("#hideRead").checked||!read.has(d.link))
  &&(!q||(d.title+" "+d.summary+" "+d.source).toLowerCase().includes(q)));
 list.sort((a,b)=>so==="date"?new Date(b.date||0)-new Date(a.date||0):so==="hits"?b.hits-a.hits:b.score-a.score);
 $("#grid").innerHTML=list.length?list.map(d=>`<article class="card ${read.has(d.link)?"read":""}">
  <div class="m"><span class="badge">${esc(d.kind)}</span><span>${esc(d.source)}</span><span>· ${ago(d.date)}</span>
   <button class="star ${saved.has(d.link)?"on":""}" data-l="${esc(d.link)}" title="Save">★</button></div>
  <a class="t" href="${esc(d.link)}" target="_blank" rel="noopener" data-l="${esc(d.link)}">${esc(d.title)}</a>
  ${d.summary?`<div class="s">${esc(d.summary)}</div>`:""}
  <div class="m">${d.topics.map(t=>`<span class="tag" data-t="${esc(t)}">#${esc(t)}</span>`).join("")}
   ${d.regions.map(r=>`<span class="tag" data-r="${esc(r)}">📍${esc(r)}</span>`).join("")}${d.hits>1?`<span>· covered ${d.hits}×</span>`:""}</div>
 </article>`).join(""):`<div class="empty">No stories match your filters.</div>`;
 const src=new Set(list.map(d=>d.source)).size,reg=new Set(list.flatMap(d=>d.regions)).size,day=list.filter(d=>d.date&&Date.now()-new Date(d.date)<864e5).length;
 $("#stats").innerHTML=`<div><b>${list.length}</b>stories shown</div><div><b>${src}</b>sources</div><div><b>${reg}</b>regions</div><div><b>${day}</b>in last 24h</div><div><b>${saved.size}</b>saved</div>`;
}
$("#grid").addEventListener("click",e=>{const s=e.target.closest(".star"),a=e.target.closest("a.t"),t=e.target.closest(".tag");
 if(s){const l=s.dataset.l;saved.has(l)?saved.delete(l):saved.add(l);SS("pmr_saved",[...saved]);render()}
 if(a){read.add(a.dataset.l);SS("pmr_read",[...read]);setTimeout(render,300)}
 if(t){if(t.dataset.t){activeTopic=t.dataset.t;chips()}else{$("#region").value=t.dataset.r}render()}});
document.querySelectorAll(".trend").forEach(b=>b.onclick=()=>{$("#q").value=b.dataset.q;render();scrollTo({top:0})});
["#q","#region","#kind","#sort","#saved","#hideRead"].forEach(s=>$(s).addEventListener("input",render));
addEventListener("keydown",e=>{if(e.key==="/"&&document.activeElement!==$("#q")){e.preventDefault();$("#q").focus()}});
chips();render();
</script></body></html>"""


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Scan worldwide product-management news and build an interactive HTML digest.")
    ap.add_argument("--days", type=int, default=7, help="look-back window in days (default 7)")
    ap.add_argument("--max", type=int, default=200, help="max stories in report (default 200)")
    ap.add_argument("--regions", nargs="+", default=list(REGIONS), choices=list(REGIONS), help="Google News editions")
    ap.add_argument("--topics", nargs="+", help="custom search queries (replace default PM topics)")
    ap.add_argument("--out", default=None, help="output HTML path (default reports/pm-news-<date>.html)")
    ap.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    ap.add_argument("--no-llm", action="store_true", help="skip LLM digest even if an API key is set")
    a = ap.parse_args()
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731

    topics = {t: t for t in a.topics} if a.topics else DEFAULT_TOPICS
    log(f"🔎 Scanning {len(topics)} topics × {len(a.regions)} regions (Google + Bing News) + PM blogs + Hacker News…")
    items = gather(topics, a.regions, a.days, log)
    stories, df = dedupe_and_rank(items, a.days, a.max)
    if not stories:
        log("No stories found (check your internet connection).")
        return 1
    for s in stories:
        s["topic_main"] = sorted(s["topics"])[0]
        s["summary"] = extractive_summary(s.get("desc") or "")

    use_llm = not a.no_llm and bool(os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
    log("🧠 Summarizing" + (" with LLM…" if use_llm else " (extractive; set OPENAI_API_KEY/ANTHROPIC_API_KEY for AI digest)…"))
    digest = build_digest(stories, df, use_llm, log)

    now = dt.datetime.now()
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", f"pm-news-{now:%Y-%m-%d-%H%M}.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(stories, digest, a.days, now.strftime("%b %d, %Y %H:%M")))

    print("\n=== PM News Digest ===")
    for b in digest[1]:
        print(" • " + re.sub(r"\*\*", "", b))
    print("\nTop 10 stories:")
    for i, s in enumerate(stories[:10], 1):
        print(f" {i:2}. {s['title']}  [{s['source']}]")
    print(f"\n✅ Report: {os.path.abspath(out)}")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
