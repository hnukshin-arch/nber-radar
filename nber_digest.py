"""
NBER Literature Radar
=====================
NBER 신규 working paper를 관심분야로 걸러 웹사이트(GitHub Pages)에 매일 아침 7시(KST) 누적 게시.
(선택) Gmail 비밀값을 넣으면 같은 내용을 메일로도 보낸다.
분야 - 제목 - 저자 - abstract - related publications 순으로 정리.

저장소 구조:
    nber_digest.py                       ← 이 파일 (수집·분류·사이트 생성·메일 전부)
    .github/workflows/nber_digest.yml    ← 매일 실행 + 웹사이트 배포
    data/seen.json, data/papers.json     ← 자동 생성 (검사 기록, 누적 논문)
    site/index.html                      ← 자동 생성 (배포되는 웹페이지, 저장소엔 안 올라감)

설치 요약 (GitHub Free는 저장소가 Public이어야 Pages 사용 가능):
    1. Public 저장소 생성 → nber_digest.py 업로드 → 워크플로 파일 생성
    2. Settings → Pages → Source: GitHub Actions
    3. Actions → NBER Literature Radar → Run workflow
    4. https://<아이디>.github.io/<저장소이름>/ 접속
    (선택) Settings → Secrets → GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO

로컬 실행:
    python nber_digest.py            # 수집 → site/ 생성 → (비밀값 있으면) 메일 → data/ 갱신
    python nber_digest.py --preview  # site/ + preview.html(메일) 생성, data/는 갱신 안 함

관심분야 추가/수정: 아래 [1] CONFIG의 FIELDS만 고치면 됨. 외부 패키지 없음.
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


# ════════════════════════════════════════════════════════════════════
# [1] CONFIG — 관심분야는 여기만 고치면 됨
# ════════════════════════════════════════════════════════════════════
# 각 키워드는 정규식(대소문자 무시). 제목 매칭은 3점, abstract 매칭은 1점.
# 논문은 점수가 가장 높은 분야에 배치되고, 나머지 매칭 분야는 태그로 표시된다.

FIELDS = {
    "Household Debt": {
        "color": "#2F6FEB",
        "keywords": [
            r"household debt", r"household leverage", r"household borrowing",
            r"household balance sheets?", r"household credit", r"consumer credit",
            r"consumer debt", r"mortgages?", r"refinanc\w*", r"foreclosures?",
            r"credit cards?", r"student (loan|debt)s?", r"auto loans?",
            r"deleveraging", r"debt[- ]to[- ]income", r"buy now,? pay later",
            r"payday", r"personal bankruptcy", r"delinquen\w*",
        ],
    },
    "Housing Market": {
        "color": "#1A9E6F",
        "keywords": [
            r"housing", r"house prices?", r"home prices?", r"real estate",
            r"homeownership", r"home ?owners?", r"rents?", r"rental",
            r"renters?", r"landlords?", r"tenants?", r"residential",
            r"zoning", r"land use", r"property (tax|values?)",
        ],
    },
    "Corporate / Commercial Debt": {
        "color": "#C2581B",
        "keywords": [
            r"corporate debt", r"corporate bonds?", r"corporate credit",
            r"corporate leverage", r"firm leverage", r"firms?'? debt",
            r"bank loans?", r"leveraged loans?", r"private credit",
            r"commercial real estate", r"commercial (lending|loans?|mortgages?)",
            r"credit spreads?", r"debt overhang", r"corporate default",
            r"bankruptc\w*", r"covenants?", r"collateral constraints?",
            r"financial frictions?", r"credit supply", r"supply of credit", r"credit crunch\w*",
            r"creditors?", r"loss[- ]given[- ]default", r"debt financ\w*",
            r"(green|sustainable) (bonds?|debt|loans?)", r"bond issuances?",
            r"non[- ]recourse", r"syndicated loans?",
        ],
    },
    "External Debt": {
        "color": "#8A3FD1",
        "keywords": [
            r"external debt", r"sovereign debt", r"sovereign default",
            r"sovereign spreads?", r"capital flows?", r"sudden stops?",
            r"foreign currency debt", r"dollar debt", r"original sin",
            r"currency mismatch", r"current account", r"net foreign assets",
            r"international (borrowing|lending|liquidity)", r"debt crisis",
            r"emerging[- ]markets?", r"balance of payments", r"capital controls?",
            r"\bIMF\b", r"global financial cycle",
        ],
    },
    "Heterogeneous Agent Models": {
        "color": "#C8364F",
        "keywords": [
            r"heterogeneous[- ]agents?", r"\bHANK\b", r"incomplete markets?",
            r"Aiyagari", r"Bewley", r"Krusell[- ]Smith", r"idiosyncratic (income )?risk",
            r"heterogeneous (households|firms)", r"distributional macro\w*",
            r"sequence[- ]space", r"marginal propensit(y|ies) to consume", r"\bMPCs?\b",
            r"wealth distribution", r"hand[- ]to[- ]mouth",
            r"precautionary sav\w*", r"borrowing constraints?",
        ],
    },
}

SETTINGS = {
    "rss_url": "https://back.nber.org/rss/new.xml",
    "min_score": 1,                 # 이 점수 이상이면 메일에 포함
    "max_references": 5,            # 논문당 related literature 표시 개수
    "min_cited": 30,                # related literature 최소 피인용 수 (잡음 제거)
    "send_when_empty": False,       # 새 관심논문이 없는 날도 메일 보낼지
    "data_dir": "data",             # seen.json(검사 기록) · papers.json(누적 논문) 저장 폴더
    "site_dir": "site",             # 웹사이트 출력 폴더 (GitHub Pages가 이 폴더를 배포)
    "seen_keep_days": 120,          # 검사 기록 보관 기간
    "store_keep_days": 730,         # 웹사이트에 남겨둘 기간 (2년)
    "subject_prefix": "[NBER Digest]",
    "http_timeout": 25,
    "user_agent": "nber-digest/1.0 (personal literature alert)",
}

KST = timezone(timedelta(hours=9))


# ════════════════════════════════════════════════════════════════════
# [2] HTTP UTIL
# ════════════════════════════════════════════════════════════════════

def http_get(url, accept="*/*", retries=2):
    """GET 요청. 실패하면 짧게 재시도하고, 끝내 실패하면 None."""
    req = urllib.request.Request(
        url, headers={"User-Agent": SETTINGS["user_agent"], "Accept": accept}
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=SETTINGS["http_timeout"]) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries:
                print(f"  ! HTTP {e.code}: {url}", file=sys.stderr)
                return None
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                print(f"  ! {type(e).__name__}: {url}", file=sys.stderr)
                return None
        time.sleep(1.5 * (attempt + 1))
    return None


def http_json(url):
    text = http_get(url, accept="application/json")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ════════════════════════════════════════════════════════════════════
# [3] FETCH — NBER RSS 파싱
# ════════════════════════════════════════════════════════════════════

def clean_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_nber_feed():
    """RSS → [{id, title, authors, abstract, url, pdf, doi}]"""
    raw = http_get(SETTINGS["rss_url"], accept="application/rss+xml, application/xml")
    if raw is None:
        raise RuntimeError("NBER RSS를 가져오지 못했습니다.")
    root = ET.fromstring(raw.encode("utf-8"))
    papers = []
    for item in root.iter("item"):
        title_raw = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").split("#")[0].strip()
        m = re.search(r"/papers/(w\d+)", link)
        if not m:
            continue
        wid = m.group(1)
        # RSS 제목 형식: "Title -- by A, B, C"
        if " -- by " in title_raw:
            title, authors = title_raw.rsplit(" -- by ", 1)
        else:
            title, authors = title_raw, ""
        papers.append({
            "id": wid,
            "title": title.strip(),
            "authors": authors.strip(),
            "abstract": clean_text(item.findtext("description")),
            "url": f"https://www.nber.org/papers/{wid}",
            "pdf": f"https://www.nber.org/system/files/working_papers/{wid}/{wid}.pdf",
            "doi": f"10.3386/{wid}",
        })
    return papers


# ════════════════════════════════════════════════════════════════════
# [4] CLASSIFY — 키워드 점수로 분야 배정
# ════════════════════════════════════════════════════════════════════

_COMPILED = {
    name: [re.compile(rf"(?<![A-Za-z]){kw}(?![A-Za-z])", re.IGNORECASE) for kw in cfg["keywords"]]
    for name, cfg in FIELDS.items()
}


def classify(paper):
    """분야별 점수와 매칭된 키워드를 붙인다."""
    scores, hits = {}, {}
    for field, patterns in _COMPILED.items():
        s, h = 0, []
        for p in patterns:
            t = p.search(paper["title"])
            a = p.search(paper["abstract"])
            if t:
                s += 3
            if a:
                s += 1
            if t or a:
                h.append((t or a).group(0).lower())
        if s:
            scores[field], hits[field] = s, sorted(set(h))
    paper["scores"] = scores
    paper["hits"] = hits
    if scores:
        paper["field"] = max(scores, key=lambda f: (scores[f], -list(FIELDS).index(f)))
        paper["score"] = scores[paper["field"]]
    else:
        paper["field"], paper["score"] = None, 0
    return paper


# ════════════════════════════════════════════════════════════════════
# [5] ENRICH — related publications
# ════════════════════════════════════════════════════════════════════
# 신규 NBER 논문은 발표 당일 OpenAlex에 색인되지 않고(404), 색인된 뒤에도
# references가 거의 없다(0~3개). PDF는 기관 IP/로그인이 필요해 자동 수집 불가.
# 그래서 두 갈래로 채운다:
#   (A) Cited in abstract : abstract 속 "Author (Year)" 인용을 OpenAlex에서 찾아 연결.
#                           공저 인용은 두 저자 모두 일치, 단독 인용은 제목이 인용 문장과
#                           2단어 이상 겹칠 때만 채택. 못 찾으면 Scholar 링크로 표시.
#   (B) Related literature: 매칭 키워드 + 제목 핵심어로 상위 경제·재무 저널(+NBER WP)에서
#                           검색, 피인용 > min_cited. 결과가 적으면 키워드만으로 재검색,
#                           그래도 적으면 OpenAlex 경제·재무 분야 전체로 확장.
#   (+) 나중에 OpenAlex가 이 논문을 색인하고 references가 5개 이상이면 그걸 우선 사용.

ECON_FIELD = "primary_topic.field.id:20"   # OpenAlex: Economics, Econometrics and Finance
JOURNAL_ISSN = [
    # AER, QJE, JPE, ECMA, REStud, JF, JFE, RFS
    "0002-8282", "0033-5533", "0022-3808", "0012-9682", "0034-6527", "0022-1082", "0304-405X", "0893-9454",
    # AEJ Macro/Applied/Policy, AER Insights, JME, JIE, REStat, JEEA, RED, JEP, EJ, EER
    "1945-7707", "1945-7782", "1945-7731", "2640-205X", "0304-3932", "0022-1996", "0034-6535", "1542-4766",
    "1094-2025", "0895-3309", "0013-0133", "0014-2921",
    # JPubE, JUE, JMCB, IMF ER, JDE, JEDC, QE, BPEA, JIMF
    "0047-2727", "0094-1190", "0022-2879", "2041-4161", "0304-3878", "0165-1889", "1759-7323", "0007-2303",
    "0261-5606",
    # JBF, JFI, RoF, JHE, REE, RCFS, JFQA, MS, NBER WP
    "0378-4266", "1042-9573", "1572-3097", "1051-1377", "1080-8620", "2046-9128", "0022-1090", "0025-1909",
    "0898-2937",
]
JOURNAL_FILTER = "primary_location.source.issn:" + "|".join(JOURNAL_ISSN)
WORK_SELECT = "id,doi,title,display_name,publication_year,authorships,primary_location,cited_by_count"
STOPWORDS = set("""a an the of and or in on for to from with by at as is are be its their our
evidence effects effect impact impacts role new study analysis theory model models data using
how what why when does do can will did through between across into over under after before
than more less via toward towards lens case""".split())


def _openalex(path, params):
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    return http_json(f"https://api.openalex.org/{path}?{urllib.parse.urlencode(params)}")


def _format_work(w):
    names = [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships", [])]
    names = [n for n in names if n]
    if len(names) > 3:
        auth = f"{names[0].split()[-1]} et al."
    elif names:
        auth = ", ".join(n.split()[-1] for n in names)
    else:
        auth = "—"
    src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    doi = w.get("doi") or ""
    return {
        "key": w.get("id"),
        "authors": auth,
        "year": w.get("publication_year") or "",
        "title": w.get("title") or w.get("display_name") or "(untitled)",
        "venue": src,
        "cited": w.get("cited_by_count") or 0,
        "url": doi if doi.startswith("http") else (w.get("id") or ""),
    }


def _words(s):
    return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in STOPWORDS}


def _same_title(a, b):
    wa, wb = _words(a), _words(b)
    return bool(wa) and len(wa & wb) / len(wa | wb) > 0.7


# (A) abstract 속 인용 ─────────────────────────────────────────────
CITE_RE = re.compile(
    r"\b([A-Z][A-Za-z'\-]+(?:(?:\s+and\s+|\s*[-–&]\s*)[A-Z][A-Za-z'\-]+)?(?:\s+et\s+al\.?)?)"
    r"\s*\((\d{4})\)"
)


def cited_in_abstract(paper):
    out = []
    for m in CITE_RE.finditer(paper["abstract"]):
        names, year = m.group(1), m.group(2)
        if not (1900 <= int(year) <= datetime.now().year):
            continue
        surnames = [s.strip() for s in re.split(r"\s+and\s+|\s*[-–&]\s*|\s+et\s+al\.?", names) if s.strip()]
        start = paper["abstract"].rfind(".", 0, m.start()) + 1
        end = paper["abstract"].find(".", m.end())
        ctx = _words(paper["abstract"][start:end if end > 0 else None])
        filt = ",".join([f"publication_year:{year}"] + [f"raw_author_name.search:{s}" for s in surnames])
        data = _openalex("works", {"filter": filt, "sort": "cited_by_count:desc",
                                   "per_page": 25, "select": WORK_SELECT})
        res = (data or {}).get("results", [])
        need = 1 if len(surnames) > 1 else 2
        pick = next((w for w in res if len(_words(w.get("title") or "") & ctx) >= need), None)
        if pick is None and len(surnames) > 1 and res:
            pick = res[0]
        cite_as = f"{names} ({year})"
        if pick:
            f = _format_work(pick)
            f["cite_as"] = cite_as
        else:
            q = urllib.parse.quote(f"{' '.join(surnames)} {year}")
            f = {"key": cite_as, "cite_as": cite_as, "unresolved": True,
                 "url": f"https://scholar.google.com/scholar?q={q}"}
        out.append(f)
    return out


# (B) 관련 문헌 검색 ───────────────────────────────────────────────
def _search(query, filt, n):
    data = _openalex("works", {
        "search": query,
        "filter": f"{filt},cited_by_count:>{SETTINGS['min_cited']}",
        "per_page": n + 3, "select": WORK_SELECT,
    })
    return (data or {}).get("results", [])


def related_literature(paper, n):
    hits = paper["hits"].get(paper["field"], [])[:3]
    title_words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", paper["title"])
                   if w.lower() not in STOPWORDS][:4]
    q_full = " ".join(dict.fromkeys(s.lower() for s in hits + title_words))
    q_kw = " ".join(hits)
    picked, keys = [], set()
    for q, filt in [(q_full, JOURNAL_FILTER), (q_kw, JOURNAL_FILTER), (q_full, ECON_FIELD)]:
        if len(picked) >= n or not q:
            continue
        for w in _search(q, filt, n):
            if w.get("id") in keys or _same_title(w.get("title") or "", paper["title"]):
                continue
            keys.add(w.get("id"))
            picked.append(_format_work(w))
    paper["related_query"] = q_full
    return picked[:n]


def indexed_references(paper, n):
    """OpenAlex가 이 논문을 색인했고 references가 충분하면 피인용 상위 n개."""
    work = _openalex(f"works/doi:{paper['doi']}", {"select": "id,referenced_works"})
    ids = [i.rsplit("/", 1)[-1] for i in (work or {}).get("referenced_works") or []]
    if len(ids) < 5:
        return []
    data = _openalex("works", {
        "filter": "openalex_id:" + "|".join(ids[:50]),
        "select": WORK_SELECT, "per_page": 50,
    })
    works = sorted((data or {}).get("results", []),
                   key=lambda w: w.get("cited_by_count") or 0, reverse=True)
    return [_format_work(w) for w in works[:n]]


def enrich(paper):
    n = SETTINGS["max_references"]
    paper["cited_refs"] = cited_in_abstract(paper)
    refs = indexed_references(paper, n)
    if refs:
        paper["related"], paper["related_kind"] = refs, "references"
    else:
        paper["related"], paper["related_kind"] = related_literature(paper, n), "search"
    seen = {r["key"] for r in paper["cited_refs"]}
    paper["related"] = [r for r in paper["related"] if r["key"] not in seen]

    return paper


# ════════════════════════════════════════════════════════════════════
# [6] RENDER — HTML 메일 (인라인 CSS, Gmail/Outlook 호환)
# ════════════════════════════════════════════════════════════════════

E = html.escape
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Apple SD Gothic Neo','Malgun Gothic',Roboto,Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman','Nanum Myeongjo',serif"


def _chip(text, color, filled=False):
    if filled:
        style = f"background:{color};color:#fff;"
    else:
        style = f"background:#fff;color:{color};border:1px solid {color}55;"
    return (f'<span style="display:inline-block;{style}font-size:11px;line-height:1;'
            f'padding:4px 8px;border-radius:999px;margin:0 4px 4px 0;white-space:nowrap;">{E(text)}</span>')


def _ref_list(items, show_cite_as=False):
    rows = []
    for r in items:
        if r.get("unresolved"):
            rows.append(
                f'<li style="margin:0 0 6px 0;"><b style="color:#374151;">{E(r["cite_as"])}</b> → '
                f'<a href="{E(r["url"])}" style="color:#6b7280;">Google Scholar에서 찾기</a></li>')
            continue
        venue = f' · <i>{E(r["venue"])}</i>' if r["venue"] else ""
        cited = f' · <span style="color:#8b93a1;">cited {r["cited"]:,}</span>' if r["cited"] else ""
        lead = (f'<b style="color:#374151;">{E(r["cite_as"])}</b> → ' if show_cite_as and r.get("cite_as")
                else f'<span style="color:#4b5563;">{E(r["authors"])} ({E(str(r["year"]))})</span> ')
        rows.append(
            f'<li style="margin:0 0 6px 0;">{lead}'
            f'<a href="{E(r["url"])}" style="color:#111827;text-decoration:none;border-bottom:1px solid #d1d5db;">{E(r["title"])}</a>'
            f'{venue}{cited}</li>')
    return f'<ol style="margin:6px 0 10px 18px;padding:0;">{"".join(rows)}</ol>'


def _label(text):
    return (f'<div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;'
            f'color:#6b7280;font-weight:600;">{text}</div>')


def _render_refs(p):
    parts = []
    if p.get("cited_refs"):
        parts.append(_label("Cited in abstract") + _ref_list(p["cited_refs"], show_cite_as=True))
    if p.get("related"):
        lab = ("Key references (피인용 상위)" if p.get("related_kind") == "references"
               else "Related literature <span style='text-transform:none;letter-spacing:0;font-weight:400;'>· 자동 검색, 관련도순</span>")
        parts.append(_label(lab) + _ref_list(p["related"]))
    if not parts:
        parts.append(_label("Related literature") +
                     '<div style="margin:4px 0 10px 0;color:#8b93a1;">자동 검색 결과가 없습니다.</div>')
    t = urllib.parse.quote(p["title"])
    links = (f'<div><a href="https://scholar.google.com/scholar?q={t}" style="color:#6b7280;font-size:12px;">Google Scholar에서 찾기</a>'
             f'<span style="color:#d1d5db;"> &nbsp;|&nbsp; </span>'
             f'<a href="https://www.semanticscholar.org/search?q={t}" style="color:#6b7280;font-size:12px;">Semantic Scholar</a></div>')
    return (f'<div style="margin-top:14px;padding:12px 14px 10px 14px;background:#f7f8fa;border-radius:8px;'
            f'font-size:12.5px;line-height:1.55;">{"".join(parts)}{links}</div>')

def _render_paper(p):
    color = FIELDS[p["field"]]["color"]
    tags = "".join(_chip(k, color) for k in p["hits"][p["field"]][:6])
    others = [f for f in p["scores"] if f != p["field"]]
    tags += "".join(_chip("+ " + f, FIELDS[f]["color"]) for f in others)
    return f"""
<tr><td style="padding:0 0 18px 0;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;border-left:4px solid {color};">
  <tr><td style="padding:18px 20px;">
    <div style="font-size:11px;color:#8b93a1;letter-spacing:.04em;margin-bottom:6px;">
      NBER {E(p['id'].upper())} &nbsp;·&nbsp;
      <a href="{E(p['url'])}" style="color:#8b93a1;">page</a> &nbsp;·&nbsp;
      <a href="{E(p['pdf'])}" style="color:#8b93a1;">PDF</a>
    </div>
    <a href="{E(p['url'])}" style="font-family:{SERIF};font-size:19px;line-height:1.35;color:#111827;text-decoration:none;font-weight:700;">{E(p['title'])}</a>
    <div style="margin:8px 0 10px 0;font-size:13.5px;color:#374151;">{E(p['authors'])}</div>
    <div>{tags}</div>
    <div style="margin-top:10px;font-size:14px;line-height:1.7;color:#1f2937;">{E(p['abstract'])}</div>
    {_render_refs(p)}
  </td></tr></table>
</td></tr>"""


def render_email(papers, today):
    groups = {f: [] for f in FIELDS}
    for p in papers:
        groups[p["field"]].append(p)
    for g in groups.values():
        g.sort(key=lambda p: p["score"], reverse=True)

    summary = "".join(
        _chip(f"{f}  {len(g)}", FIELDS[f]["color"], filled=True) for f, g in groups.items() if g
    )
    sections = []
    for f, g in groups.items():
        if not g:
            continue
        anchor = re.sub(r"[^a-z]", "", f.lower())
        sections.append(f"""
<tr><td style="padding:22px 0 10px 0;">
  <a name="{anchor}" id="{anchor}"></a>
  <div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:{FIELDS[f]['color']};">{E(f)}</div>
  <div style="height:2px;background:{FIELDS[f]['color']};width:36px;margin-top:6px;"></div>
</td></tr>
{''.join(_render_paper(p) for p in g)}""")

    if not papers:
        sections.append('<tr><td style="padding:30px 0;color:#6b7280;font-size:14px;text-align:center;">'
                        '오늘은 관심분야에 맞는 새 working paper가 없습니다.</td></tr>')

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NBER Digest {today}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:{FONT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:700px;">

<tr><td style="padding:0 0 18px 0;">
  <div style="font-size:12px;color:#6b7280;letter-spacing:.08em;">{E(today)}</div>
  <div style="font-family:{SERIF};font-size:28px;font-weight:700;color:#111827;margin-top:4px;">NBER Working Papers</div>
  <div style="font-size:14px;color:#4b5563;margin-top:4px;">관심분야 신규 논문 <b>{len(papers)}</b>편{f' · <a href="{E(site_url())}" style="color:#2F6FEB;">웹에서 전체 보기 →</a>' if site_url() else ''}</div>
  <div style="margin-top:12px;">{summary}</div>
</td></tr>

{''.join(sections)}

<tr><td style="padding:18px 0 0 0;border-top:1px solid #e5e7eb;font-size:11.5px;color:#9ca3af;line-height:1.6;">
  Source: NBER new working papers RSS · Related literature: OpenAlex (경제·재무 분야, 피인용 {SETTINGS['min_cited']}회 이상)<br>
  PDF는 NBER 구독 기관 IP(교내망)나 NBER 로그인이 있어야 열립니다.<br>
  분야는 제목·abstract 키워드로 자동 분류됩니다. 키워드는 <code>nber_digest.py</code>의 FIELDS에서 수정하세요.
</td></tr>

</table></td></tr></table></body></html>"""


# ════════════════════════════════════════════════════════════════════
# [7] SEND — Gmail SMTP
# ════════════════════════════════════════════════════════════════════

def send_email(subject, html_body):
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    to = [a.strip() for a in os.environ.get("MAIL_TO", user).split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.attach(MIMEText("HTML 메일을 지원하는 클라이언트에서 열어주세요.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())


# ════════════════════════════════════════════════════════════════════
# [8] SITE — GitHub Pages용 웹사이트 (site/index.html 한 파일)
# ════════════════════════════════════════════════════════════════════
# 누적된 논문 전체를 데이터로 박아 넣은 정적 페이지. 서버 없이 브라우저에서
# 분야 필터·검색·날짜별 묶음이 동작한다.

SITE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NBER Literature Radar</title>
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#14161a;--sub:#4b5260;--mute:#8a919e;--line:#e3e6eb;--soft:#f6f7f9}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#eceef1;--sub:#b3b9c4;--mute:#7d8592;--line:#2b2f36;--soft:#22262c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Pretendard,-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}
.wrap{max-width:820px;margin:0 auto;padding:40px 20px 80px}
header .eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);font-weight:600}
header h1{font-family:Georgia,'Times New Roman',serif;font-size:34px;margin:6px 0 6px;letter-spacing:-.01em}
header p{margin:0;color:var(--sub);font-size:14px}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:18px 0 12px;margin-top:18px;border-bottom:1px solid var(--line)}
.search{width:100%;padding:11px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font-size:14px;outline:none}
.search:focus{border-color:#7aa2f7}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--sub);font-size:12.5px;padding:6px 11px;border-radius:999px;cursor:pointer;user-select:none;font-family:inherit}
.chip .n{opacity:.6;margin-left:4px}
.chip.on{color:#fff;border-color:transparent}
.day{margin:34px 0 12px;display:flex;align-items:baseline;gap:10px}
.day h2{font-size:15px;margin:0;font-weight:700}
.day span{font-size:12.5px;color:var(--mute)}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:12px;padding:20px 22px;margin-bottom:14px}
.meta{font-size:11.5px;color:var(--mute);letter-spacing:.03em;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.meta a{color:var(--mute)}
.field{color:var(--c);font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.title{display:block;font-family:Georgia,'Times New Roman',serif;font-size:20px;line-height:1.35;font-weight:700;text-decoration:none;margin:8px 0 6px}
.title:hover{text-decoration:underline;text-decoration-thickness:1px}
.authors{font-size:13.5px;color:var(--sub);margin-bottom:10px}
.tag{display:inline-block;font-size:11px;padding:3px 8px;border-radius:999px;border:1px solid color-mix(in srgb,var(--tc) 40%,transparent);color:var(--tc);margin:0 4px 4px 0}
.abs{font-size:14.5px;line-height:1.75;color:var(--ink);margin-top:8px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.abs.open{display:block}
.more{font-size:12px;color:var(--mute);cursor:pointer;margin-top:4px;background:none;border:0;padding:0;font-family:inherit}
details.refs{margin-top:14px;background:var(--soft);border-radius:9px;padding:10px 14px;font-size:13px;line-height:1.55}
details.refs summary{cursor:pointer;font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--sub);font-weight:700}
.lab{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--mute);font-weight:700;margin-top:10px}
.lab i{text-transform:none;letter-spacing:0;font-weight:400;font-style:normal}
ol{margin:6px 0 4px 18px;padding:0}
li{margin-bottom:6px}
li a{text-decoration:none;border-bottom:1px solid var(--line)}
li a:hover{border-color:var(--mute)}
.src{color:var(--mute)}
.ext{margin-top:8px;font-size:12px}
.ext a{color:var(--mute)}
.empty{text-align:center;color:var(--mute);padding:60px 0}
footer{margin-top:50px;font-size:12px;color:var(--mute);line-height:1.7;border-top:1px solid var(--line);padding-top:16px}
</style></head>
<body><div class="wrap">
<header>
  <div class="eyebrow">NBER Working Papers · Literature Radar</div>
  <h1>NBER Literature Radar</h1>
  <p id="sub"></p>
</header>
<div class="controls">
  <input class="search" id="q" type="search" placeholder="제목·저자·abstract·키워드 검색">
  <div class="chips" id="chips"></div>
</div>
<main id="list"></main>
<footer>
  Source: NBER new working papers RSS · Related literature: OpenAlex (경제·재무 분야, 상위 저널 우선, 피인용 __MINCITED__회 이상)<br>
  분야는 제목·abstract 키워드로 자동 분류됩니다. Related literature는 검색 기반이라 실제 참고문헌과 다를 수 있습니다.<br>
  PDF는 NBER 구독 기관 네트워크(교내망)나 NBER 로그인이 필요할 수 있습니다.
</footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const FIELDS = D.fields, PAPERS = D.papers;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let active = 'ALL', query = '';

document.getElementById('sub').textContent =
  `마지막 업데이트 ${D.updated} · 누적 ${PAPERS.length}편 · 최근 발행분 ${PAPERS.filter(p=>p.added===D.latest).length}편`;

function chips(){
  const cnt = {}; PAPERS.forEach(p => cnt[p.field] = (cnt[p.field]||0) + 1);
  const all = [['ALL','전체',PAPERS.length,'var(--ink)'], ...Object.keys(FIELDS).map(f => [f,f,cnt[f]||0,FIELDS[f]])];
  document.getElementById('chips').innerHTML = all.map(([k,l,n,c]) =>
    `<button class="chip ${active===k?'on':''}" data-k="${esc(k)}" style="${active===k?`background:${c};color:${k==='ALL'?'var(--bg)':'#fff'}`:''}">${esc(l)}<span class="n">${n}</span></button>`).join('');
  document.querySelectorAll('.chip').forEach(b => b.onclick = () => { active = b.dataset.k; render(); });
}

function refList(items, citeAs){
  return '<ol>' + items.map(r => r.unresolved
    ? `<li><b>${esc(r.cite_as)}</b> → <a href="${esc(r.url)}" target="_blank" rel="noopener">Google Scholar에서 찾기</a></li>`
    : `<li>${citeAs && r.cite_as ? `<b>${esc(r.cite_as)}</b> → ` : `<span class="src">${esc(r.authors)} (${esc(r.year)})</span> `}`
      + `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>`
      + `${r.venue ? ` · <i>${esc(r.venue)}</i>` : ''}${r.cited ? ` <span class="src">· cited ${Number(r.cited).toLocaleString()}</span>` : ''}</li>`).join('') + '</ol>';
}

function card(p, openRefs){
  const c = FIELDS[p.field];
  const tags = (p.hits[p.field]||[]).slice(0,6).map(t => `<span class="tag" style="--tc:${c}">${esc(t)}</span>`).join('')
    + Object.keys(p.scores).filter(f => f!==p.field).map(f => `<span class="tag" style="--tc:${FIELDS[f]}">+ ${esc(f)}</span>`).join('');
  let refs = '';
  if (p.cited_refs?.length) refs += `<div class="lab">Cited in abstract</div>` + refList(p.cited_refs, true);
  if (p.related?.length) refs += `<div class="lab">${p.related_kind==='references' ? 'Key references <i>· 피인용 상위</i>' : 'Related literature <i>· 자동 검색, 관련도순</i>'}</div>` + refList(p.related, false);
  if (!refs) refs = '<div class="lab">자동 검색 결과 없음</div>';
  const n = (p.cited_refs?.length||0) + (p.related?.length||0);
  return `<article class="card" style="--c:${c}">
    <div class="meta"><span class="field">${esc(p.field)}</span><span>NBER ${esc(p.id.toUpperCase())}</span>
      <a href="${esc(p.url)}" target="_blank" rel="noopener">page</a><a href="${esc(p.pdf)}" target="_blank" rel="noopener">PDF</a></div>
    <a class="title" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>
    <div class="authors">${esc(p.authors)}</div>
    <div>${tags}</div>
    <div class="abs" title="클릭해서 펼치기/접기">${esc(p.abstract)}</div>
    <details class="refs" ${openRefs?'open':''}><summary>Related publications (${n})</summary>${refs}
      <div class="ext"><a href="https://scholar.google.com/scholar?q=${encodeURIComponent(p.title)}" target="_blank" rel="noopener">Google Scholar</a> ·
      <a href="https://www.semanticscholar.org/search?q=${encodeURIComponent(p.title)}" target="_blank" rel="noopener">Semantic Scholar</a></div></details>
  </article>`;
}

function render(){
  chips();
  const q = query.trim().toLowerCase();
  const list = PAPERS.filter(p => (active==='ALL' || p.field===active) &&
    (!q || [p.title,p.authors,p.abstract,...Object.values(p.hits).flat()].join(' ').toLowerCase().includes(q)));
  const days = {};
  list.forEach(p => (days[p.added] ||= []).push(p));
  const keys = Object.keys(days).sort().reverse();
  document.getElementById('list').innerHTML = keys.length ? keys.map(d =>
    `<div class="day"><h2>${d}${d===D.latest?' · 최신':''}</h2><span>${days[d].length}편</span></div>` +
    days[d].map(p => card(p, d===D.latest)).join('')).join('')
    : '<div class="empty">조건에 맞는 논문이 없습니다.</div>';
  document.querySelectorAll('.abs').forEach(el => el.onclick = () => el.classList.toggle('open'));
}
document.getElementById('q').addEventListener('input', e => { query = e.target.value; render(); });
render();
</script></body></html>"""


def build_site(store):
    order = list(FIELDS)
    papers = sorted(store.values(),
                    key=lambda p: (p["added"], -order.index(p["field"]) if p["field"] in order else 0, p["score"]),
                    reverse=True)
    papers = [p for p in papers if p.get("field") in FIELDS]   # 설정에서 지운 분야는 숨김
    data = {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "latest": max((p["added"] for p in papers), default=""),
        "fields": {f: c["color"] for f, c in FIELDS.items()},
        "papers": papers,
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = SITE_TEMPLATE.replace("__DATA__", blob).replace("__MINCITED__", str(SETTINGS["min_cited"]))
    out = Path(SETTINGS["site_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / ".nojekyll").write_text("")
    return out / "index.html"


def site_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{name}/"


# ════════════════════════════════════════════════════════════════════
# [9] STATE — data/seen.json (검사한 논문), data/papers.json (누적 논문)
# ════════════════════════════════════════════════════════════════════

def _load_json(path):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def _cutoff(days):
    return (datetime.now(KST) - timedelta(days=days)).date().isoformat()


KEEP_KEYS = ["id", "title", "authors", "abstract", "url", "pdf", "field", "score", "scores", "hits",
             "cited_refs", "related", "related_kind"]


# ════════════════════════════════════════════════════════════════════
# [10] MAIN
# ════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true",
                    help="메일은 preview.html로 저장하고 data/는 갱신하지 않음 (site/는 생성)")
    args = ap.parse_args()

    now = datetime.now(KST)
    stamp = now.date().isoformat()
    seen_path = Path(SETTINGS["data_dir"]) / "seen.json"
    store_path = Path(SETTINGS["data_dir"]) / "papers.json"
    seen, store = _load_json(seen_path), _load_json(store_path)

    feed = fetch_nber_feed()
    print(f"RSS: {len(feed)} papers")
    new = [p for p in feed if p["id"] not in seen]
    matched = [classify(p) for p in new]
    matched = [p for p in matched if p["score"] >= SETTINGS["min_score"]]
    print(f"new: {len(new)} / matched: {len(matched)}")

    for p in matched:
        print(f"  enrich {p['id']} [{p['field']}] {p['title'][:60]}")
        enrich(p)
        time.sleep(0.3)
        store[p["id"]] = {**{k: p.get(k) for k in KEEP_KEYS}, "added": stamp}

    keep = _cutoff(SETTINGS["store_keep_days"])
    store = {k: v for k, v in store.items() if v.get("added", stamp) >= keep}
    print("site:", build_site(store))

    body = render_email(matched, now.strftime("%Y-%m-%d (%a)"))
    if args.preview:
        Path("preview.html").write_text(body, encoding="utf-8")
        print("preview.html saved (data/ not updated)")
        return

    mail_ready = os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD")
    if mail_ready and (matched or SETTINGS["send_when_empty"]):
        subject = f"{SETTINGS['subject_prefix']} {now:%m/%d} · {len(matched)}편"
        if matched:
            top = max(matched, key=lambda p: p["score"])
            subject += f" — {top['title'][:50]}"
        send_email(subject, body)
        print("email sent")
    elif not mail_ready:
        print("email skipped (GMAIL secrets not set — 웹사이트만 갱신)")
    else:
        print("no matched papers — email skipped")

    for p in new:
        seen[p["id"]] = stamp
    seen = {k: v for k, v in seen.items() if v >= _cutoff(SETTINGS["seen_keep_days"])}
    _save_json(seen_path, seen)
    _save_json(store_path, store)


if __name__ == "__main__":
    main()
