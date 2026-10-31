#!/usr/bin/env python3
"""Turn an argiro.gr or gastronomos.gr recipe page into a one-page A4 PDF.

Usage: argiro2pdf.py URL [-o output.pdf]

Needs only the standard library plus a Chromium/Chrome binary (used to print
the PDF). Uses a 10pt font; long recipes flow onto a second page (for double-sided
printing). Only if a recipe would need more than two pages is the font reduced.
"""
import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BROWSERS = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"]

CSS = """
@page{size:A4;margin:9mm 11mm}
body{font-family:"DejaVu Serif",serif;color:#000;font-size:%(fs).2fpt;line-height:1.32}
h1{font-size:24pt;font-weight:bold;margin:0 0 3px;color:#000;text-align:center}
.sub{text-align:center;color:#333;font-size:9pt;margin-bottom:6px}
.desc{text-align:center;font-style:italic;margin:0 6mm 8px}
.meta{display:flex;justify-content:center;gap:28px;border-top:2px solid #000;border-bottom:2px solid #000;padding:6px 0;margin-bottom:12px}
.meta b{display:block;font-size:8pt;color:#000;text-transform:uppercase}
.cols{display:flex;gap:22px}.cols>div{flex:1}.cols>div:first-child{flex:0 0 36%%}
h2{font-weight:bold;font-size:13pt;color:#000;border-bottom:1.5px solid #000;padding-bottom:3px;margin:0 0 6px}
h3{font-weight:bold;font-size:.98em;margin:10px 0 3px;color:#000;letter-spacing:.5px}
.m h3{margin:9px 0 4px}
ul{list-style:none;padding:0;margin:0}li{padding:1.5px 0 1.5px 17px;position:relative}
ul li:before{content:"\\2610";position:absolute;left:0;color:#000}
ol{padding-left:22px;margin:0}ol li{padding:0 0 4px 3px}
.tips{margin-top:12px;border:1.5px solid #000;border-left-width:5px;padding:2px 10px 6px}
.tips h3{margin-top:6px}.tips li{padding-left:0}.tips li:before{content:none}.tips li+li{margin-top:5px}
.tips ul{padding-left:0}
.eq{margin-top:10px}.eq b{color:#000}
.foot{margin-top:6px;text-align:center;font-size:8pt;color:#333}
"""


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def text(fragment):
    """Strip tags/scripts, unescape and collapse whitespace."""
    fragment = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).strip()


def first(pattern, src, default=""):
    m = re.search(pattern, src, re.S)
    return text(m.group(1)) if m else default


def section(page, start, ends):
    """Text of the first block beginning at `start`, up to the earliest following `ends` marker."""
    m = re.search(start, page)
    if not m:
        return ""
    rest = page[m.end():]
    cut = [e.start() for e in (re.search(p, rest) for p in ends) if e]
    return rest[:min(cut)] if cut else rest


def parse(page):
    r = {}
    r["title"] = first(r'<h1[^>]*class="single_recipe__title"[^>]*>(.*?)</h1>', page) or first(r"<h1[^>]*>(.*?)</h1>", page)
    r["lead"] = first(r'<h2[^>]*class="single_recipe__lead"[^>]*>(.*?)</h2>', page)
    r["level"] = first(r'<div class="difficulty_level">(.*?)</div>', page)
    r["total"] = first(r'id="time-display"[^>]*>(.*?)</span>', page)
    r["servings"] = first(r'class="item portions".*?<h2[^>]*>(.*?)</h2>', page)
    r["prep"] = first(r'class="item preparation_time".*?<h2[^>]*>(.*?)</h2>', page)
    r["cook"] = first(r'class="item cooking_time".*?<h2[^>]*>(.*?)</h2>', page)

    # Ingredients: the page repeats the block (desktop/mobile), so use the first one only.
    ing = section(page, r'<div class="ingredients\s*"',
                  [r'<div class="ads_element', r'<div class="equipment', r'<div class="single_recipe__method',
                   r'<div class="ingredients\s*"'])
    groups = []
    for part in re.split(r'(?=<h3 class="ingredients__title")', ing):
        name = first(r'<h3 class="ingredients__title"[^>]*>(.*?)</h3>', part)
        items = []
        for it in re.findall(r'<div class="ingredients__item">(.*?)</div>', part, re.S):
            qty = first(r'class="ingredients__quantity"[^>]*>(.*?)</span>', it)
            what = first(r"<p>(.*?)</p>", it)
            items.append(f"{qty} {what}".strip())
        # The page repeats its ingredient list (desktop/mobile); skip exact repeats.
        if items and (name, items) not in groups:
            groups.append((name, items))
    r["ingredients"] = groups

    # Method: one container per section, each with its own optional title.
    method = section(page, r'<div class="single_recipe__method tab"',
                     [r'<div class="single_recipe__tips', r'<div class="extra_info',
                      r'<div class="article__tags_container', r'<div class="equipment'])
    sections = []
    for c in re.split(r'(?=<div class="single_recipe__method_container")', method)[1:]:
        name = first(r'<h3 class="single_recipe__method_steps__title"[^>]*>(.*?)</h3>', c)
        steps = [text(s) for s in re.findall(r"<li>(.*?)</li>", c, re.S)]
        steps = [s for s in steps if s]
        if steps:
            sections.append((name, steps))
    r["method"] = sections

    tips = section(page, r'<div class="single_recipe__tips', [r'<div class="extra_info', r'<div class="article__tags_container'])
    r["tips"] = [t for t in (text(x) for x in re.findall(r"<li>(.*?)</li>", tips, re.S)) if t]
    r["equipment"] = [text(e) for e in re.findall(r'<h2 class="equipment__item__name">(.*?)</h2>', page, re.S)]
    r["author"] = "Αργυρώ Μπαρμπαρίγου"
    return r


def iso_duration(d):
    """'PT1H15M' -> '1 ώρα και 15 λεπτά'."""
    m = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?", d or "")
    if not m or not any(m.groups()):
        return ""
    h, mi = (int(x or 0) for x in m.groups())
    parts = ([f"{h} ώρα" if h == 1 else f"{h} ώρες"] if h else []) + ([f"{mi} λεπτά"] if mi else [])
    return " και ".join(parts)


def parse_jsonld(page):
    """Parse sites (e.g. gastronomos.gr) that publish a schema.org Recipe as JSON-LD."""
    for blob in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else data.get("@graph", [data])):
            if isinstance(d, dict) and d.get("@type") == "Recipe":
                break
        else:
            continue
        author = d.get("author") or [{}]
        author = author[0] if isinstance(author, list) else author
        steps = [text(s.get("text", "")) for s in d.get("recipeInstructions", [])]
        ings = [text(i) for i in d.get("recipeIngredient", [])]
        return {
            "title": text(d.get("name", "")), "lead": text(d.get("description", "")),
            "author": text(author.get("name", "")) if isinstance(author, dict) else "",
            "level": "", "total": iso_duration(d.get("totalTime")),
            "servings": text(str(d.get("recipeYield", ""))),
            "prep": iso_duration(d.get("prepTime")), "cook": iso_duration(d.get("cookTime")),
            "ingredients": [("", [i for i in ings if i])] if ings else [],
            "method": [("", [s for s in steps if s])] if steps else [],
            "tips": [], "equipment": [],
        }
    return {}


def esc(s):
    return html.escape(s)


def render(r, url, fs):
    meta = [("Επίπεδο", r["level"]), ("Συνολικός χρόνος", r["total"]), ("Μερίδες", r["servings"]),
            ("Προετοιμασία", r["prep"]), ("Μαγείρεμα", r["cook"])]
    meta_html = "".join(f"<div><b>{esc(k)}</b>{esc(v)}</div>" for k, v in meta if v)

    ing_html = ""
    for name, items in r["ingredients"]:
        if name:
            ing_html += f"<h3>{esc(name)}</h3>"
        ing_html += "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"
    if not ing_html:
        ing_html = ""
    eq_html = (f'<div class="eq"><b>Εξοπλισμός:</b> {esc(", ".join(r["equipment"]))}</div>'
               if r["equipment"] else "")

    method_html, n = "", 1
    for name, steps in r["method"]:
        if name:
            method_html += f"<h3>{esc(name)}</h3>"
        method_html += f'<ol start="{n}">' + "".join(f"<li>{esc(s)}</li>" for s in steps) + "</ol>"
        n += len(steps)

    tips_html = ""
    if r["tips"]:
        tips_html = ('<div class="tips"><h3>ΣΥΜΒΟΥΛΕΣ</h3><ul>'
                     + "".join(f"<li>{esc(t)}</li>" for t in r["tips"]) + "</ul></div>")

    return f"""<!doctype html><html lang="el"><head><meta charset="utf-8">
<title>{esc(r['title'])}</title><style>{CSS % {'fs': fs}}</style></head><body>
<h1>{esc(r['title'])}</h1>
<div class="sub">{esc(r.get('author', ''))}</div>
<div class="desc">{esc(r['lead'])}</div>
<div class="meta">{meta_html}</div>
<div class="cols"><div><h2>Υλικά</h2>{ing_html}{eq_html}</div>
<div class="m"><h2>Εκτέλεση</h2>{method_html}{tips_html}</div></div>
<div class="foot">Πηγή: {esc(re.sub(r'^https?://(www\\.)?', '', url).rstrip('/'))}</div>
</body></html>"""


def find_browser():
    for b in BROWSERS:
        p = shutil.which(b)
        if p:
            return p
    sys.exit("No Chromium/Chrome found in PATH (tried: %s)" % ", ".join(BROWSERS))


def pdf_pages(path):
    return len(re.findall(rb"/Type\s*/Page[^s]", Path(path).read_bytes()))


def build_pdf(r, url, out, fs, max_pages):
    browser = find_browser()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "recipe.html"
        while True:
            src.write_text(render(r, url, fs), encoding="utf-8")
            subprocess.run([browser, "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={out}", str(src)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if pdf_pages(out) <= max_pages or fs <= 7:
                break
            fs -= 0.5
    return fs


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_printer():
    """Default printer if set, else the first enabled printer that has a working driver (PPD)."""
    if not shutil.which("lp"):
        sys.exit("CUPS not found (no `lp` command); can't print.")
    d = run(["lpstat", "-d"])
    if d and ":" in d.stdout:
        return d.stdout.split(":", 1)[1].strip()
    p = run(["lpstat", "-p"])
    for line in (p.stdout.splitlines() if p else []):
        m = re.match(r"printer (\S+) (is idle|now printing)", line)
        if m and (o := run(["lpoptions", "-p", m.group(1), "-l"])) and o.returncode == 0 and o.stdout.strip():
            return m.group(1)
    sys.exit("No available printer found (check `lpstat -p`).")


def print_pdf(path, printer, pages, long_edge=False):
    opts = run(["lpoptions", "-p", printer, "-l"])
    avail = opts.stdout if opts else ""
    cmd = ["lp", "-d", printer, "-o", "media=A4"]
    if "TonerSaveMode" in avail:
        cmd += ["-o", "TonerSaveMode=OFF"]
    if "1200dpi" in avail:
        cmd += ["-o", "Resolution=1200dpi"]
    if pages > 1:
        # Use the PPD's own Duplex option when there is one: Brother's filter only reads
        # Duplex=..., not the IPP `sides` option, so `sides=` alone is not applied reliably.
        if "Duplex/" in avail:
            cmd += ["-o", "Duplex=DuplexNoTumble" if long_edge else "Duplex=DuplexTumble"]
        else:
            cmd += ["-o", "sides=two-sided-long-edge" if long_edge else "sides=two-sided-short-edge"]
    cmd.append(str(path))
    res = run(cmd)
    if not res or res.returncode != 0:
        sys.exit("Printing failed: " + (res.stderr.strip() if res else "lp could not be run"))
    print(f"Sent to {printer}: {res.stdout.strip()}" + (f" (two-sided, {'long' if long_edge else 'short'} edge)" if pages > 1 else ""))


def main():
    ap = argparse.ArgumentParser(description="Make a one-page A4 PDF from an argiro.gr or gastronomos.gr recipe URL.")
    ap.add_argument("url")
    ap.add_argument("-o", "--output", help="output PDF path (default: <recipe-slug>.pdf)")
    ap.add_argument("-s", "--font-size", type=float, default=10.0, help="body font size in pt (default: 10)")
    ap.add_argument("-p", "--max-pages", type=int, default=2, help="shrink the font only if more pages are needed (default: 2)")
    ap.add_argument("--print", action="store_true", dest="do_print",
                    help="also print the PDF (default printer, else first available; two-sided if >1 page)")
    ap.add_argument("--long-edge", action="store_true",
                    help="with --print, flip two-sided pages over the long edge (default: short edge)")
    ap.add_argument("--printer", help="printer name to use with --print (see `lpstat -p`)")
    a = ap.parse_args()
    if "argiro.gr/recipe/" in a.url:
        r = parse(fetch(a.url))
    elif "gastronomos.gr/syntagh/" in a.url:
        r = parse_jsonld(fetch(a.url))
    else:
        sys.exit("URL should look like https://www.argiro.gr/recipe/<name>/ or https://www.gastronomos.gr/syntagh/<name>/<id>/")

    if not r or not r["title"] or not r["method"] or not r["ingredients"]:
        sys.exit("Couldn't find the title, ingredients or method on that page (did the site layout change?).")

    slug = next(p for p in reversed(a.url.rstrip("/").split("/")) if not p.isdigit())
    out = Path(a.output or f"{slug}.pdf").resolve()
    fs = build_pdf(r, a.url, out, a.font_size, a.max_pages)
    nsteps = sum(len(s) for _, s in r["method"])
    nings = sum(len(i) for _, i in r["ingredients"])
    print(f"Wrote {out} ({nings} ingredients, {nsteps} steps, {len(r["tips"])} tips, {pdf_pages(out)} page(s), font {fs:.1f}pt)")
    if a.do_print:
        print_pdf(out, a.printer or find_printer(), pdf_pages(out), a.long_edge)


if __name__ == "__main__":
    main()
