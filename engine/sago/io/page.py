"""Parse a rendered HTML page into the facts every auditor needs.

Stdlib only, tolerant of broken markup. Works on minified HTML, where grep does not.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from html.parser import HTMLParser

DASHES = re.compile(r"[‐-―\-]")
EXTERNAL_FONT_HOSTS = ("fonts.googleapis.com", "api.fontshare.com", "fonts.bunny.net", "use.typekit.net", "fonts.gstatic.com")


class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_head = True
        self.title = ""
        self._in_title = False
        self.metas: list[dict] = []
        self.links: list[dict] = []
        self.scripts: list[dict] = []
        self._script = None
        self.noscript_depth = 0
        self.h1: list[str] = []
        self.h2: list[dict] = []
        self._heading = None
        self._text: list[str] = []
        self.forms: list[dict] = []
        self.sections: list[dict] = []
        self.anchors: list[str] = []
        self._after_h2: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "body":
            self.in_head = False
        if tag == "noscript":
            self.noscript_depth += 1
        if tag == "title" and self.in_head:
            self._in_title = True
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "link" and not self.noscript_depth:
            a["_in_head"] = self.in_head
            self.links.append(a)
        elif tag == "script":
            self._script = {**a, "_in_head": self.in_head, "_body": []}
        elif tag in ("h1", "h2"):
            self._heading = (tag, [])
        elif tag == "form":
            self.forms.append(a)
        elif tag == "section" and not self.sections:
            self.sections.append(a)
        elif tag == "a" and a.get("href"):
            self.anchors.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "noscript" and self.noscript_depth:
            self.noscript_depth -= 1
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._script is not None:
            s = self._script
            s["_body"] = "".join(s["_body"])
            self.scripts.append(s)
            self._script = None
        elif tag in ("h1", "h2") and self._heading and self._heading[0] == tag:
            text = " ".join("".join(self._heading[1]).split())
            if tag == "h1":
                self.h1.append(text)
            else:
                entry = {"text": text, "next": ""}
                self.h2.append(entry)
                self._after_h2 = entry
            self._heading = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._script is not None:
            self._script["_body"].append(data)
            return
        if self._heading:
            self._heading[1].append(data)
        elif self._after_h2 is not None and len(self._after_h2["next"]) < 400:
            chunk = " ".join(data.split())
            if chunk:
                self._after_h2["next"] = (self._after_h2["next"] + " " + chunk).strip()
        if not self.in_head:
            self._text.append(data)


def parse_page(html: str, url: str = "", status: int = 200) -> dict:
    p = _P()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 — broken markup still yields what was parsed
        pass
    meta = {}
    robots = []
    for m in p.metas:
        name = (m.get("name") or m.get("property") or "").lower()
        if name == "robots":
            robots.append(m.get("content", ""))
        elif name and name not in meta:
            meta[name] = m.get("content", "")
    canon = [l.get("href", "") for l in p.links if "canonical" in (l.get("rel") or "")]
    stylesheets = [l for l in p.links if "stylesheet" in (l.get("rel") or "") and l.get("_in_head")]
    blocking_css = [l.get("href", "") for l in stylesheets
                    if not ("print" in (l.get("media") or "") and "onload" in l)]
    jsonld, bad_jsonld = [], 0
    for s in p.scripts:
        if (s.get("type") or "").lower() == "application/ld+json":
            try:
                jsonld.append({"source": s.get("class") or s.get("id") or "", "data": json.loads(s["_body"])})
            except (json.JSONDecodeError, TypeError):
                bad_jsonld += 1
    nodes = []
    for block in jsonld:
        d = block["data"]
        graph = d.get("@graph", [d]) if isinstance(d, dict) else d if isinstance(d, list) else []
        for n in graph:
            if isinstance(n, dict):
                nodes.append({"type": n.get("@type"), "id": n.get("@id"), "source": block["source"],
                              "logo": (n.get("logo") or {}).get("url") if isinstance(n.get("logo"), dict) else n.get("logo")})
    body_text = " ".join("".join(p._text).split())
    external_fonts = sorted({h for l in p.links for h in EXTERNAL_FONT_HOSTS if h in (l.get("href") or "")} |
                            {h for s in p.scripts for h in EXTERNAL_FONT_HOSTS if h in (s.get("src") or "") + s["_body"]})
    scripts = [{"src": s.get("src") or s.get("data-src") or "", "id": s.get("id", ""), "type": s.get("type", ""),
                "defer": "defer" in s, "async": "async" in s, "in_head": s["_in_head"],
                "inline": (s["_body"][:200] if not s.get("src") else "")} for s in p.scripts
               if (s.get("type") or "").lower() != "application/ld+json"]
    title = htmllib.unescape(" ".join(p.title.split()))
    desc = meta.get("description", "")
    return {
        "url": url, "status": status,
        "title": title, "description": desc,
        "robots": robots, "canonical": canon,
        "og_title": meta.get("og:title", ""), "og_description": meta.get("og:description", ""),
        "h1": p.h1, "h2": p.h2[:30],
        "jsonld_nodes": nodes, "jsonld_bad": bad_jsonld,
        "blocking_css": blocking_css,
        "stylesheets": [l.get("href", "") for l in stylesheets],
        "scripts": scripts,
        "external_fonts": external_fonts,
        "first_section_class": (p.sections[0].get("class", "") if p.sections else ""),
        "forms": len(p.forms),
        "words": len(body_text.split()),
        "php_error": bool(re.search(r"(Fatal error|Warning</b>:|Parse error)", html)),
        "title_has_dash": bool(DASHES.search(title)),
        "description_has_dash": bool(DASHES.search(desc)),
        "anchors": p.anchors[:500],
        "bytes": len(html),
    }
