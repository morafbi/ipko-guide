"""Daily updater for ipko_en.json (9 DigitAlb channels -> English).

Path A (high accuracy): stargate.ipko.tv EPG gives original_title + year,
verified against TMDB. Works where stargate is reachable (bot-blocked on
some datacenter IPs, incl. GitHub runners -> HTTP 403).

Path B (fallback, runner-safe): epgshare01 AL1 XML gives Albanian title +
summary + year; English is resolved via TMDB cast+year discovery and added
only on exact single-candidate matches (conservative: never guesses).

Merge: existing pairs are kept, TMDB-verified data fills gaps, file is
never truncated. Stdlib only.
TMDB key: env TMDB_API_KEY, else .github/workflows/Secrets, else builtin.
"""
import gzip
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CHANNELS_STARGATE = [
    "FILMHITSHD", "FILMAKSION", "FILMKOMEDI", "FILMAUTOR",
    "GOLDHD", "MAXHD", "FAMILYHD", "FILMTHRILLER", "FILMDRAME",
]
CHANNELS_XML = [
    "Film.Hits.HD.al", "Film.Aksion.al", "Film.Komedi.al",
    "Film.Autor.al", "Gold.HD.al", "Max.HD.al",
    "Family.HD.al", "Film.Thriller.al", "Film.Drame.al",
]
STARGATE = "https://stargate.ipko.tv/api/titan.tv.WebEpg/GetWebEpgData"
AL1 = "https://epgshare01.online/epgshare01/epg_ripper_AL1.xml.gz"
TR = ("https://translate.googleapis.com/translate_a/single"
      "?client=gtx&sl=sq&tl=en&dt=t&q=")
TMDB = "https://api.themoviedb.org/3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BUILTIN_KEY = "1f834eb425728133b9a2c1c0c82980eb"
MANUAL = {
    "Nou fo": ("Raging Fire", 2021, "7.1/10"),
    "Su ren te gong": ("The Rookies", 2019, "5.3/10"),
}
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")


def tmdb_key():
    k = os.environ.get("TMDB_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(os.path.join(".github", "workflows", "Secrets"),
                  encoding="utf-8") as f:
            k = f.read().strip()
            if len(k) >= 32:
                return k
    except OSError:
        pass
    return BUILTIN_KEY


def get_json(url, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data:
        headers.update({
            "Content-Type": "application/json",
            "Origin": "https://ipko.tv",
            "Referer": "https://ipko.tv/tv-guide",
            "X-Requested-With": "XMLHttpRequest",
        })
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def tmdb(path, key):
    with urllib.request.urlopen(
            urllib.request.Request(
                f"{TMDB}{path}{'&' if '?' in path else '?'}"
                f"api_key={key}&language=en-US",
                headers={"User-Agent": UA}), timeout=20) as r:
        return json.load(r)


def translate(text):
    try:
        with urllib.request.urlopen(
                TR + urllib.parse.quote(text), timeout=15) as r:
            j = json.load(r)
        return "".join(s[0] for s in j[0] if s and s[0]).strip()
    except Exception:
        return ""


def vote_str(v):
    return f"{round(float(v), 1)}/10" if v not in (None, "") else ""


def year_of(item, fallback=""):
    rd = item.get("release_date") or item.get("first_air_date") or ""
    if len(rd) >= 4 and rd[:4].isdigit():
        return int(rd[:4])
    try:
        return int(str(fallback)[:4])
    except (ValueError, TypeError):
        return ""


def enrich_original(al, orig, year, key):
    """Path A: ipko original_title -> TMDB verify."""
    if orig in MANUAL:
        en, y, imdb = MANUAL[orig]
        return {"al": al, "en": en, "year": y, "imdb": imdb}
    best = None
    for ep in ("search/movie", "search/tv", "search/multi"):
        try:
            res = tmdb(f"/{ep}?query={urllib.parse.quote(orig)}"
                       + (f"&year={int(str(year)[:4])}" if ep == "search/movie"
                          and str(year)[:4].isdigit() else ""), key)
            if res.get("results"):
                def score(it):
                    s = float(it.get("popularity", 0))
                    rd = (it.get("release_date") or
                          it.get("first_air_date") or "")
                    if str(year) and str(year) in rd:
                        s += 100
                    q = orig.lower()
                    if ((it.get("original_title") or
                         it.get("original_name") or "").lower() == q
                            or (it.get("title") or
                                it.get("name") or "").lower() == q):
                        s += 200
                    return s
                best = sorted(res["results"], key=score,
                              reverse=True)[0]
                break
        except Exception as e:
            print(f"WARN tmdb {orig}: {e}")
            time.sleep(1)
    if best is None:
        if orig != al:
            return enrich_original(al, al, year, key)
        return {"al": al, "en": orig, "year": year, "imdb": ""}
    title = best.get("title") or best.get("name") or orig
    return {"al": al, "en": title, "year": year_of(best, year),
            "imdb": vote_str(best.get("vote_average"))}


def cast_names(desc):
    out = []
    for m in NAME_RE.finditer(desc or ""):
        n = m.group(1)
        if n not in out and len(n.split()) <= 3:
            out.append(n)
        if len(out) >= 4:
            break
    return out[:2]


def enrich_fallback(al, desc, year, key, gloss_cache):
    """Path B: Albanian title+summary -> TMDB cast+year discovery.

    Returns a pair only on an exact single-candidate match, else None.
    """
    names = cast_names(desc)
    cands = []
    for name in names:
        try:
            pr = tmdb(f"/search/person?query={urllib.parse.quote(name)}",
                      key).get("results", [])
        except Exception:
            continue
        pid = next((p["id"] for p in pr
                    if p.get("name", "").lower() == name.lower()), None)
        if pid is None:
            continue
        for kind, ykey in (("movie", "primary_release_year"),
                           ("tv", "first_air_date_year")):
            try:
                dr = tmdb(f"/discover/{kind}?with_cast={pid}"
                          f"&{ykey}={int(str(year)[:4])}", key)
                cands += [(f"{kind}:{x['id']}", x) for x in
                          dr.get("results", [])]
            except Exception:
                continue
        time.sleep(0.2)
        if cands:
            break
    uniq = {k: v for k, v in cands}.values()
    uniq = list(uniq)
    if len(uniq) == 1:
        b = uniq[0]
        title = b.get("title") or b.get("name") or ""
        if title:
            return {"al": al, "en": title,
                    "year": year_of(b, year),
                    "imdb": vote_str(b.get("vote_average"))}
    if al not in gloss_cache:
        gloss_cache[al] = translate(al)
        time.sleep(0.2)
    gloss = gloss_cache.get(al, "")
    if gloss:
        try:
            res = tmdb(f"/search/movie?query={urllib.parse.quote(gloss)}",
                       key).get("results", [])
        except Exception:
            res = []
        same_year = [x for x in res
                     if str(year) in (x.get("release_date") or "")]
        if len(same_year) == 1:
            b = same_year[0]
            surnames = {n.split()[-1].lower() for n in names}
            try:
                kind = ("tv" if b.get("name") and not b.get("title")
                        else "movie")
                cr = tmdb(f"/{kind}/{b['id']}/credits", key)
                cast = ([c.get("name", "") for c in cr.get("cast", [])]
                        + [c.get("name", "") for c in cr.get("crew", [])])
                if any(s in c.lower() for s in surnames for c in cast):
                    return {"al": al,
                            "en": b.get("title") or b.get("name") or gloss,
                            "year": year_of(b, year),
                            "imdb": vote_str(b.get("vote_average"))}
            except Exception:
                pass
    return None


def fetch_stargate():
    now = int(time.time())
    seen = {}
    ok = 0
    for ch in CHANNELS_STARGATE:
        try:
            j = get_json(STARGATE, {"ch_ext_id": ch,
                                    "from": now - 7200,
                                    "to": now + 93600})
            shows = j.get("shows", []) or []
            ok += 1
        except Exception as e:
            print(f"WARN stargate {ch}: {e}")
            continue
        for s in shows:
            al = (s.get("title") or "").strip()
            en = (s.get("original_title") or "").strip()
            if al and en and al not in seen:
                seen[al] = (en, s.get("year", ""))
    return seen if ok >= 5 and len(seen) >= 30 else {}


def fetch_xmltv():
    req = urllib.request.Request(AL1, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = gzip.GzipFile(fileobj=io.BytesIO(r.read())).read()
    root = ET.fromstring(raw)
    now = time.time()
    out = {}
    for pg in root.iter("programme"):
        if pg.get("channel") not in CHANNELS_XML:
            continue
        try:
            start = time.mktime(time.strptime(pg.get("start")[:14],
                                              "%Y%m%d%H%M%S"))
        except (TypeError, ValueError):
            continue
        if not (now - 7200 <= start <= now + 2 * 86400):
            continue
        t = pg.findtext("title") or ""
        d = pg.findtext("desc") or ""
        y = (pg.findtext("date") or "").strip()[:4]
        t = " ".join(t.split())
        if t and t not in out:
            out[t] = (" ".join(d.split()),
                      int(y) if y.isdigit() else "")
    return out


def main():
    key = tmdb_key()
    with open("ipko_en.json", encoding="utf-8") as f:
        live = json.load(f)["pairs"]
    have = {p["al"] for p in live}
    new_pairs = {}

    sg = fetch_stargate()
    if sg:
        print(f"stargate OK: {len(sg)} titles (high-accuracy path)")
        for al, (en0, y) in sorted(sg.items()):
            if al in have or al in new_pairs:
                continue
            try:
                y = int(str(y)[:4])
            except (ValueError, TypeError):
                y = ""
            new_pairs[al] = enrich_original(al, en0, y, key)
            time.sleep(0.2)
    else:
        print("stargate blocked/empty -> XMLTV fallback path")

    if not sg:
        xml = fetch_xmltv()
        print(f"xmltv programmes (48h, 9ch): {len(xml)}")
        missing = {a: v for a, v in xml.items()
                   if a not in have and a not in new_pairs}
        print(f"missing titles to resolve: {len(missing)}")
        gloss_cache = {}
        for al, (desc, y) in sorted(missing.items()):
            r = enrich_fallback(al, desc, y, key, gloss_cache)
            if r:
                print(f"ADD {al} -> {r['en']} ({r['year']})")
                new_pairs[al] = r
            time.sleep(0.2)

    idx = {p["al"]: i for i, p in enumerate(live)}
    added = updated = 0
    for al, c in new_pairs.items():
        if al in idx:
            i = idx[al]
            if live[i]["en"] != c["en"]:
                print(f"UPDATE {al}: '{live[i]['en']}' -> '{c['en']}'")
                updated += 1
            live[i]["en"] = c["en"]
            if c["year"]:
                live[i]["year"] = c["year"]
            if c["imdb"]:
                live[i]["imdb"] = c["imdb"]
        else:
            live.append(c)
            added += 1
    dedup = {}
    for p in live:
        dedup[p["al"]] = p
    with open("ipko_en.json", "w", encoding="utf-8") as f:
        json.dump({"pairs": list(dedup.values())}, f,
                  ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"done: added={added} updated={updated} total={len(dedup)}")


if __name__ == "__main__":
    main()
