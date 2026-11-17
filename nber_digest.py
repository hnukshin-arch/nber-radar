"""
NBER Literature Radar
=====================
NBER 신규 working paper를 관심분야로 걸러 웹사이트(GitHub Pages)에 매일 아침 7시(KST) 누적 게시.
(선택) Gmail 비밀값을 넣으면 같은 내용을 메일로도 보낸다.
분야 - 제목 - 저자 - abstract - related publications 순으로 정리.

저장소 구조:
    nber_digest.py                       ← 이 파일 (수집·분류·사이트 생성·메일 전부)
    .github/workflows/nber_digest.yml    ← 매일 실행 + 웹사이트 배포
    config.json                          ← 관심분야 설정 (웹사이트 설정 탭에서 편집, 없으면 자동 생성)
    data/papers.json                     ← 자동 생성 (누적 논문)
    backfill.json                        ← 과거 논문 불러오기 요청 (설정 탭이 씀)
    data/backfill_status.json            ← 자동 생성 (불러오기 진행 상황)
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

관심분야 추가/수정: 웹사이트 [설정] 탭 (또는 config.json 직접 편집). 외부 패키지 없음.
"""

import argparse
import base64
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
# [1] CONFIG — 관심분야 기본값 (실제 설정은 config.json, 웹사이트 설정 탭에서 편집)
# ════════════════════════════════════════════════════════════════════
# config.json이 없을 때만 아래 기본값으로 새로 만든다. 그 뒤로는 config.json이 기준.
#
# 키워드 문법 (대소문자 무시):
#   mortgage            → mortgage, mortgages (끝에 s/es 자동 허용)
#   debt-to-income      → 띄어쓰기·하이픈 구분 없이 매칭 (debt to income도 OK)
#   refinanc*           → refinance, refinancing, refinanced ... (* = 뒤에 아무 글자)
#   re:\bMPCs?\b        → re: 로 시작하면 정규식 그대로
# 점수: 제목에서 매칭 3점, abstract에서 매칭 1점. 가장 높은 분야에 배치.

DEFAULT_FIELDS = [
    {"name": "Household Debt", "color": "#2F6690", "keywords": [
        "household debt", "household leverage", "household borrowing", "household balance sheet",
        "household credit", "consumer credit", "consumer debt", "mortgage", "refinanc*", "foreclosure",
        "credit card", "student loan", "student debt", "auto loan", "deleveraging", "debt-to-income",
        "buy now pay later", "payday", "personal bankruptcy", "delinquen*"]},
    {"name": "Housing Market", "color": "#4F7A3A", "keywords": [
        "housing", "house price", "home price", "real estate", "homeownership", "homeowner",
        "home owner", "rent", "rental", "renter", "landlord", "tenant", "residential", "zoning",
        "land use", "property tax", "property value"]},
    {"name": "Corporate / Commercial Debt", "color": "#B5562B", "keywords": [
        "corporate debt", "corporate bond", "corporate credit", "corporate leverage", "firm leverage",
        "firm debt", "bank loan", "leveraged loan", "private credit", "commercial real estate",
        "commercial lending", "commercial loan", "commercial mortgage", "credit spread", "debt overhang",
        "corporate default", "bankruptc*", "covenant", "collateral constraint", "financial friction",
        "credit supply", "supply of credit", "credit crunch*", "creditor", "loss-given-default",
        "debt financ*", "green bond", "sustainable bond", "sustainable debt", "sustainable loan",
        "bond issuance", "non-recourse", "syndicated loan"]},
    {"name": "External Debt", "color": "#7A4E9C", "keywords": [
        "external debt", "sovereign debt", "sovereign default", "sovereign spread", "capital flow",
        "sudden stop", "foreign currency debt", "dollar debt", "original sin", "currency mismatch",
        "current account", "net foreign asset", "international borrowing", "international lending",
        "international liquidity", "debt crisis", "debt crises", "emerging market", "balance of payments",
        "capital control", "IMF", "global financial cycle"]},
    {"name": "Heterogeneous Agent Models", "color": "#A8324A", "keywords": [
        "heterogeneous agent", "HANK", "incomplete market", "Aiyagari", "Bewley", "Krusell-Smith",
        "idiosyncratic risk", "idiosyncratic income risk", "heterogeneous household", "heterogeneous firm",
        "distributional macro*", "sequence-space", "marginal propensity to consume",
        "marginal propensities to consume", "MPC", "wealth distribution", "hand-to-mouth",
        "precautionary sav*", "borrowing constraint"]},
]

SETTINGS = {
    "rss_url": "https://back.nber.org/rss/new.xml",
    "config_file": "config.json",   # 관심분야 설정 (웹사이트 설정 탭이 이 파일을 고침)
    "max_references": 5,            # 논문당 related literature 표시 개수
    "min_cited": 30,                # related literature 최소 피인용 수 (잡음 제거)
    "send_when_empty": False,       # 새 관심논문이 없는 날도 메일 보낼지
    "data_dir": "data",             # papers.json(누적 논문) 저장 폴더
    "site_dir": "site",             # 웹사이트 출력 폴더 (GitHub Pages가 이 폴더를 배포)
    "store_keep_days": 730,         # 웹사이트에 남겨둘 기간 (수집일 기준 2년)
    "backfill_file": "backfill.json",   # 과거 논문 불러오기 요청 (웹사이트 설정 탭이 씀)
    "backfill_minutes": 40,         # 한 번 실행에서 과거 불러오기에 쓸 최대 시간 (남으면 다음 실행에 이어서)
    "backfill_earliest": "2000-01-01",
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

# 실행 중 설정 (load_config가 채움)
CONFIG = {}        # config.json 전체
FIELDS = {}        # 활성 분야: name → {"color", "keywords", "patterns"}
FIELD_COLORS = {}  # 활성 + 보관 분야 전체의 색


def keyword_pattern(kw):
    """키워드 문법 → 정규식. 위 [1] CONFIG 설명 참고."""
    kw = kw.strip()
    if kw.lower().startswith("re:"):
        body = kw[3:]
        try:
            re.compile(body)
            return body
        except re.error:
            return re.escape(body)
    wild = kw.endswith("*")
    tokens = [t for t in re.split(r"[\s\-]+", kw.rstrip("*")) if t]
    if not tokens:
        return None
    body = r"[\s\-]+".join(re.escape(t) for t in tokens)
    body += r"[A-Za-z]*" if wild else r"(?:s|es)?"
    return rf"(?<![A-Za-z]){body}(?![A-Za-z])"


def normalize_config(cfg):
    out, names = [], set()
    for f in cfg.get("fields", []):
        name = str(f.get("name", "")).strip()
        if not name or name in names:
            continue
        names.add(name)
        kws = [str(k).strip() for k in f.get("keywords", []) if str(k).strip()]
        color = f.get("color") if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(f.get("color", ""))) else "#8C7B6B"
        out.append({"name": name, "color": color, "keywords": kws, "active": bool(f.get("active", True))})
    try:
        min_score = max(1, int(cfg.get("min_score", 1)))
    except (TypeError, ValueError):
        min_score = 1
    return {"version": 1, "min_score": min_score, "fields": out}


def load_config():
    """config.json을 읽어 FIELDS/FIELD_COLORS를 채운다. 없으면 기본값으로 만든다."""
    global CONFIG, FIELDS, FIELD_COLORS
    path = Path(SETTINGS["config_file"])
    created = False
    raw = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"config.json 형식 오류: {e}")
    if raw is None:
        raw = {"min_score": 1, "fields": [dict(f, active=True) for f in DEFAULT_FIELDS]}
        created = True
    CONFIG = normalize_config(raw)
    FIELDS, FIELD_COLORS = {}, {}
    for f in CONFIG["fields"]:
        FIELD_COLORS[f["name"]] = f["color"]
        if f["active"]:
            pats = []
            for kw in f["keywords"]:
                p = keyword_pattern(kw)
                if p:
                    pats.append(re.compile(p, re.IGNORECASE))
            FIELDS[f["name"]] = {"color": f["color"], "keywords": f["keywords"], "patterns": pats}
    if created:
        path.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return CONFIG


def classify(paper):
    """분야별 점수와 매칭된 키워드를 붙인다."""
    scores, hits = {}, {}
    for field, spec in FIELDS.items():
        patterns = spec["patterns"]
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
            f'<a href="{E(r["url"])}" style="color:#2B241D;text-decoration:none;border-bottom:1px solid #d1d5db;">{E(r["title"])}</a>'
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
    return (f'<div style="margin-top:14px;padding:12px 14px 10px 14px;background:#F7EFE2;border-radius:8px;'
            f'font-size:12.5px;line-height:1.55;">{"".join(parts)}{links}</div>')

def _render_paper(p):
    color = FIELDS[p["field"]]["color"]
    tags = "".join(_chip(k, color) for k in p["hits"][p["field"]][:6])
    others = [f for f in p["scores"] if f != p["field"]]
    tags += "".join(_chip("+ " + f, FIELDS[f]["color"]) for f in others)
    return f"""
<tr><td style="padding:0 0 18px 0;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FFFAF2;border:1px solid #E5D7C1;border-radius:10px;border-left:4px solid {color};">
  <tr><td style="padding:18px 20px;">
    <div style="font-size:11px;color:#8b93a1;letter-spacing:.04em;margin-bottom:6px;">
      NBER {E(p['id'].upper())} &nbsp;·&nbsp;
      <a href="{E(p['url'])}" style="color:#8b93a1;">page</a> &nbsp;·&nbsp;
      <a href="{E(p['pdf'])}" style="color:#8b93a1;">PDF</a>
    </div>
    <a href="{E(p['url'])}" style="font-family:{SERIF};font-size:19px;line-height:1.35;color:#2B241D;text-decoration:none;font-weight:700;">{E(p['title'])}</a>
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
<body style="margin:0;padding:0;background:#F4ECDF;font-family:{FONT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4ECDF;">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:700px;">

<tr><td style="padding:0 0 18px 0;">
  <div style="font-size:12px;color:#6b7280;letter-spacing:.08em;">{E(today)}</div>
  <div style="font-family:{SERIF};font-size:28px;font-weight:700;color:#2B241D;margin-top:4px;">NBER Working Papers</div>
  <div style="font-size:14px;color:#4b5563;margin-top:4px;">관심분야 신규 논문 <b>{len(papers)}</b>편{f' · <a href="{E(site_url())}" style="color:#C8742E;">웹에서 전체 보기 →</a>' if site_url() else ''}</div>
  <div style="margin-top:12px;">{summary}</div>
</td></tr>

{''.join(sections)}

<tr><td style="padding:18px 0 0 0;border-top:1px solid #E5D7C1;font-size:11.5px;color:#9ca3af;line-height:1.6;">
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
# [8] SITE — GitHub Pages용 웹사이트
# ════════════════════════════════════════════════════════════════════
# site/index.html 한 파일 + 아이콘 파일. 누적 논문과 설정(config.json)을 페이지에 박아 넣고,
# 브라우저에서 분야 필터·검색·설정 편집이 동작한다.
# 설정 저장은 사용자가 이 브라우저에 연결한 GitHub 토큰으로 config.json을 직접 커밋한다
# (커밋되면 워크플로가 자동 실행돼 새 분야로 이번 주 NBER 논문을 다시 훑는다).

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="#4E3927"/><stop offset="1" stop-color="#2A1E15"/></linearGradient>'
            '<radialGradient id="s" cx="32" cy="33" r="23" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#F5B95E"/>'
            '<stop offset="1" stop-color="#F5B95E" stop-opacity=".15"/></radialGradient></defs>'
            '<rect width="64" height="64" rx="14" fill="url(#g)"/>'
            '<circle cx="32" cy="33" r="21.5" fill="none" stroke="#F3E3C7" stroke-opacity=".6" stroke-width="3"/>'
            '<circle cx="32" cy="33" r="11" fill="none" stroke="#F3E3C7" stroke-opacity=".6" stroke-width="3"/>'
            '<path d="M32 33 L32 11.5 A21.5 21.5 0 0 1 50.6 22.2 Z" fill="url(#s)" fill-opacity=".85"/>'
            '<line x1="32" y1="33" x2="50.6" y2="22.2" stroke="#FFC766" stroke-width="3.4" stroke-linecap="round"/>'
            '<circle cx="32" cy="33" r="4" fill="#FFC766"/><circle cx="43" cy="18.5" r="4.4" fill="#FFE6B0"/></svg>')
ICON_PNG32 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAMAAABEpIrGAAAAwFBMVEU2JxtGMyOpmIJALiCZiHNqWUcAAABXRzd3ZlQ+LSCrlXiKemb8xWX+5a9wVjaKc1Wuhk27klLOp2s4KRw1JhuGZThGMyMxIxjms13wu2LjyZk0JRY+LSBBMB9MOCdKNyV+YDaFblKii2zXpFQtHxUwIBg7Kx4+LSA+MCRBLx9AMCBLNyRLNyafkHupf0TcsnDTvZbt1KIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADa8Y1wAAAAQHRSTlP+/v/+//8A///9////////////S7X/EIP///8RhP9IsP////9JIHC5//8gcIH//////wAAAAAAAAAAAAAAAAAA4ZuxVAAAAZ1JREFUeNp9U9di2zAMBCFwiJS1bNmNk7QZbdJmdPz/z/VIyhadusaDROgOB4g8kjFm+/ZFfYiqovr5ZguQjPms1L94VRNiHQnncShkBm3/Xx/jml7P4DTXI56omM+6IN5LcIqOsaEFd8ws9533nl21UI7lwsFCv23b+/1q334kaGad+oPgV6vVb3tKsPwwzz9N0x6EP96eEIRtxK0OQ99FhYdfY0nQ7PKYXsZx7Hs/XDWNLggiePwQdlT3P4dx/NQ0nUhBiAIVZJD3fQ94UKRZHQmaW8yHklpNj49Nc+UBWLYFAbhiNB0awJNm7HTKZ4IDIVdAvVFYRvWCoNlW9UFhILSnU4WWNc4vjn17d3dLlH5ALzOgvUtfXP4xl2od1zMB5y8+Ng0c8FJ4IaklzPsQ/aO5i0scuAizq5PAfBjZfyGnSnedTvtjeRag7D/lpbARcnn/flhn/1n2esG1Z7vgOaxPI6bywEc3wJUH/+Izw7AwLnOnFtPeFI0dMLBKV+/ohS7GNzLrS/g6Xt4LjK/pdpvtbnMO3eyujTF/AZu0ET5RRmLAAAAAAElFTkSuQmCC"
ICON_PNG180 = "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAMAAAAKE/YAAAAAwFBMVEU3JxtFMyOpmIJALiD+xmU+LSD+5a90VjKJZjjPqXArHhWyhkiYh3Kqlnp4Z1VXRjVoWEaJeWW5oHumfUNnTS+Mc1PElk/otFzvu2Tx2KSijXO7kVHOt46CbVXYp1aTcD21pInDnGPXs3U+MCNeT0BfUEDlypdBMB5+cF3izKCAXzXCq4hfUD9+YDacgF2fkHrfxZQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABHAMC0AAAAQHRSTlP/////////////////////////////////////////////////////////////////AAAAAAAAAAAAAAAAAAAAMQDqUQAACq9JREFUeNrtnNt227gOhkXFjuyIJyWWkzinJk2729kze8/7P91IPkg8gCQo0VK81iA3vWjcr79BEABJZGQmu1J/jrZsfrL2Z3n8cRmZ03rejjvEOyu0qXKEzrMqDeh8Ujr7ekpD/pzhdZ5RaVPng8pLhM4zKz3En2dUerg/zwE9Kj7PqvTQ+Dy70leDdZ5F6XH+PDX06Pg8m9Jj4vOsSo/x5xmUHu/PU0Inic+zKD02Ps+m9NVonSdVOo0/TwWdLD5PrnSK+DyL0in8eUKl0/nzFNBJ4/OkSqeKz5MrfZVM50mUTuvPZ4EWkvOXutpbzTmXIl18Pge05NUuB4xWXJIU8Tk1tKxp7jVaN+DLFDqngRY8ANyBczLan9NAu4hZ8wNzj9R5NLSonLLeHozdMqbTV2KcziOhNz63uFXMIK/EOP8Ys/b8nnxrmopNN7MoLZzIlO4jdF3/j+52zNC7/1tkemgOR4dmKzntg8XmYEWz1yjoPTafGHqzA4CFnm8UR9sUe/J61+nd/YqYEvrFIpZ2vlH0dpS8586/NTZc7CHMhjczDubPhWZ7vRvu1lGeH6/39vt5oGfHIxd61GXSkdcVtrXc9Qm5tcdnJqaA5n7kPn8uIOqi+Ptas99DXGSca3BP/gxBF/XjtWGP3ypyZmiNmQpfPQhC/7y27He8Y8dtKCzgzGqdAjGvrwGLX46DmWmoHgSYX65BawQQ54IWQW/W6kGs0K3UkdTDmFkR7m/8sSraH9UeYejH9hPJOaBV39gJRN29OtkfXei4dti3yAQKDc287gz0N1aKHRRnLuj/RqZ9A2IdxfU3Vrr5oJ8jk1Uks1JVVcj+88oyN/Tha6zSQvNona+WNnTpdI9jcSBTQotYf277Gza0cyF2FSRJCM2idQaVXrlC3u2JGunWcQ7NIvrPAPQzDN0Qn6rHKhW07J1DoHUmoq6RTv3YxJWD1E2FIBNB984hEf7ctiFpGyDXjTFWlir7Byj0ZvPSVuofi8YeNkmga0+wM3TWunrrk72Xdx01kJo+twUNZevFwR5IAmjhXoSGPxstsrVq7Ki3XQT83Fdhq4fFyZ4SQNOAQ590JmZXb63b0wG7/mnq3BZh24Vi46Glx6EVlYHmzdq08iC2mp8+sjYnKR9U5oUYDU39EfqgM5FAV3e9dmDXH0cfeVy3Gcnb/UI3OhZaupxD9WcCtvUA6PX70bXLJsKVdavy6tVAXnzkYiQ09UaOo85wK3INWq3lfaunhWUsp+OgNx2C258Fs9uQRfO93JWNPXmpDWfe69x+mhgF7RX6oHNhHU8c++V3Byuf3nXqLmhbztwE6VvUZo6M0cLlz0I/mSD9ecpdZ+UWot7ayOvuex0BzT1CH/2ZaYcp6vlgWSrY76aHAM58958K2brGZR3C4c/aGpT6+WC5t7sjeqlSv5W2Z2wbdbuvjQ6HFu4YbccNJozzwfJkNrW9/u6L/e90nycGQ78Am6GWb1RqRWOew5YG9Z0b+eHtSCNx/oHyDldfFFiBma10x/0OIy/Kbt2R7nsbCi2gZajmG71zVMB5dwlS28ivagZdofwDEzsknNdJxTeA8+7SpgY2k/uV9kuiX9XDoCvTO/T8ufceRqB7BaVJXW6hMKfjLLvvbiA0HDu6OqVPRgV4f8OEfnoAw5xhFOPUCJfmYD1IlENM8F6BwXzvCnO6ccymiMhKBVgP8t454HsQGvKr7RlwWYVyakRBC/c3utAhHfc3ttun7REZcOYPBu96S0ykDmd4FOxv9LslqPPmr7fX123D3SI7sjnYAShiJbqhT8ffL2B/gyur0NRZdKHtvuG2kRdrjwNwRPoRDh4c7CN134OlMym11eZCdmnZLaUh0P2KgPp13X/pxdRZZ14AYc4fID4R4cN9BG4ED71fJ/tFbvizn7kNc396AwRBbOThiAf2n/vQYvjzyod8yOaIN0AQRMwLZx5gX7TqY4f+cfce5lM25w8QKaAZ2H+mnVj6OvQI3Wdz3AvNEkDvwP4zdXzDr05nXmGjGg3vLsGyhYL3n13r8MHBrGVz0psUjYHuvBbs8zscjzjCnIgIxUmgofcprrhUwNDCkRSBofj0D9fjlLZvmbuU3qCgf6Cgfw2ArhWl7fv8ce5Bsonco1vi4H1+5vhkMEzfR+VEI6Cv1Dhtv5ugDscDN/FyKmhlc4Hep1SO/UEgXDqrzrW59CkRgd6n1K5Q+4Soq7xaLsds432JCL1P4a5czE4+7omrDOQps7x9XO4+uoDep7ibbpuQc7j/w4ggjq1cJPQ+hbibshtN6/tNZGujk2M5QOmugcTB91aVWxC1dimJ+/uvUteIDXWX9YLvrbxNWfG2fX14eN2+CV87Bl5p1YBqvM+eT7+9A98BEaWRF3d7kfmdloZ38cx9DsvVmGe/H6QDr/Zz/xEFGdJh6quUvnYF31v1jd4oqUnup5KYBrX7XkH38TX43mpJUV1Zl8u6FhpHBI/Mc3/jRLWD37UVOaoB7hDSpSTFnG957slwf/85oznuLAraOVxQBLVSPPeR+qgG90VFHhtBlKNSEfCOmDMXNeNXdj34fVgV+abpu3KylPm9w+vSme9+HTU7o27hMNTKaZjrqxG4xe27X9f7xzLkooir5uoVCxEK4jKLUlrJQ4l+QuENBsEYgvmrXRXH0FPc7P5GFQSq9HsT7n9G+4vB/1gVPtB3zt+QQZfVrwJxx1/T75K5QzDyPD/zzt/oV5pERDGX9y/162/uNStxJ+OG0lZ/g4fVMa5dsYoLBXwpeMWQzPgczD+vgCCWGbEfNDO6f69aUfu6XrUML1UWejQcmL/BMZsez9Hm0XD5f3SyG5i/QVAvYiUSmf3AJNrse4ZV2jF/o0ZlRaTCMHvf8JGIB8Oh+Rt9dPBfhhLBcQiBnZ6iPdpW2pq/IbEK+N++00/shonIzoPzka7wabP4xRy+zMXYRNtW2jtPpv+8XTiVE7XFzepwjdDXbqiKEzFPpsqjqsFm/EvVPmVg7XgBzj+jKkdcbY+Yj0RSTALAtRUwq7CFRsxHkvl5qWUeWW2i5iPxfEjljbUf0Zqg5iMp6yQXqZlFHhU5VGj/fCTCzkYt8viRH8j5derd7qTUYogc2HlfEdXgwDUY8bHoeV88P0MM4cOkwM9Ve8HmawO6kZFCRMyvq/I8yZgcsFCrsyho/LyvShvfkHIJxrWLs6j5dVWezrG1Gq2O/OWouWp6/2JE7NNrBh7PHDO/TuLrJ3x1Fu1psfPrpDGyZlBw1nLuAaN2oufXGcUglZGTzZZGWTYoDkXPCTQrb8Zjhlxwhm7eeP7jA+btWc2ZCjmTzZquNzRwDpkTaPcLwqVrJjiL7CoElI5e/kAnzGg9av+I1YYcGekHzmN0vFFtp4F+tpeXmw9ctrewRftgP77fFGYemFWyfITRUWnA8LmX3yWdB3nk3Mth2HR0sjVyLmo8Nj1DQR+f+/AIbsqTVJjL6bgTEaez0ITh5gU8yb6itbOn7bOhZtOR4nv2ta3ZUMQPubfP5o9fHXcau/mXeiLim4vU+eYCiS/SO25uLlDnm0vU+aKUPul8We5x1PmCoHudL0npTueLgVZ1vhylFZ0vBFrX+VKU1nS+CGhT58tQ2tD5AqBtnS9BaUvnLw8N6fz1lQZ0vrn5B3dm6b8Jsjl9AAAAAElFTkSuQmCC"
ICON_PNG16 = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAMAAAAoLQ9TAAAAwFBMVEU2JxtHNCRpWEd4Z1VXRjdBLyCLeWY0JRk0Jhqth08/LiCGclrsuWIAAAA7Kx5LNyZKNiWTgm/XqFkqFRVAKhVALx9eT0GDYjaKbUqhfEKpjmi7lF68nWi/qIPGm1vWuoftt13jypr/5rAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD8HvggAAAAQHRSTlP+/v//////h9b//f//AXVt1f//DAz//////////////////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAa2mAJAAAAJFJREFUeNpVjgcOwzAMA6nhldmke4//P7Ky3TYoIUDmUYaEflxRlXNNHHqMtHgAA2quzLpFUbFe+NBe+QvU2/w+vZ70AZ6I23O6X7oKmB0LpducmAsQdR7Hx3wCSQVW6DqUbsCJos27BJq/OGeNJpGJwJpBA7FA7b2rh5iY7QLin7dcvJc8j4iAPwVsQlxsDOs3qcEERAJqURkAAAAASUVORK5CYII="
ICON_PNG48 = "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAMAAABg3Am1AAAAwFBMVEXt0p03JxtFMyOpmIJALiAAAAD+xmVVRDSXhnE+LSBoWEb+5a+JeGVwVTWolXp3Z1SvhkuIZjiTdk7Np283Jxw1JhrotFykfUOii2u7klI1JhpIMyTuvGcwJBRiTTY4KRxMNiZLNiUzJRk9LSA+LSBDKhhBMB9KNiWFb1S6oXrGlk7So1TRu5PZwZXs1KItHxUsHxYvIBY+MCRALiBALSBALiBMOSZKNiZKNSVeT0CAXzW/qIDHn2TBr43ZsnHpzZlU377LAAAAQHRSTlP//v7//wD//////////////////06T/////8UQ/xf/LkqjtGzMD//M/////////0n/aP84mKA2cZD/////////yZBr3gAAArVJREFUeNqVVteW4jAMdQHsGMcp9GGAnbKz03a29/b/f7WS7Jg4lAN6gBPlXuuqRAnrk339dvVG7LWe6IHdfrnZeCTDn+WVOGgI7/GMcz5ZNoRXR+Ai4sHGnnAE7wNkAU8M1l+eej7aayCcpL+xSZ8tzzgfQ7B9GShjisIYlegPWbDvXfS6djKYu1cdPJ+wTr9yTVCt5849OCe1SgkXLMUj3BrUn4Pdz389PzuRMBKCmsPZymeMhLwcgr2oQwQF4k1THyK8IGH4Rx0gAH4d619V1c+c8MO3bVUs0a+29a/QAkHqbA+hCHoogDJlDYR/RJg//F3sIUhZhPOVhVqNRiNXlwzwo3IwGKx3CFbqMBCWGjFCW9RgU8Bf1jsEL6gnBJTWKsFnZfkbKPUTwKe1lKJDMOCiAHC+wRTLspyVo0uAD2Z33EnTIVhpSX8e8EgoF4hf4Nlwu0NwqAjKo6Umv3osH0n8nC5NcG8J0AOqv5M5eNeI9eIdweD+DoECCJ/dk8fP7sI1FzHrQFDgwP7m/kTKdYAj5KQfpAMEDv898JKiqQcSIesSBBF4JjxgRoIw96Ad/Nlu0vj8almgu1osKvwvmqKlSUN9NHQmo/rJ1vQDzoSyJn0A9UUY4Uy3Z7npCjSu1Wmqv2qyUjI++Eo34UQ6S7TggniU7feAwamNPh2ntdlveTxE6WYvNaFEKzEW9yfoDYys8PgiJJPpbQBPoPqAeBvdAp7ueGFlawuw7f40bUbL6qTSrLWfQYkWXXhmY+qB0NrnyDAp/odO8ZzdtvezwdKYGEUYLJhJl/Hn5MCwMupCKWOpvFZ01v1NRzMtpWi2s+35B/ZppyrQY62d09qajzs3N6x/wc+wa3iLbs4hrPDFPj4Z/n7sPx1OZryLHyfXp8Anq/g10++vxpOjyV9MxiuP/A8iOiryPa50hQAAAABJRU5ErkJggg=="
ICON_VERSION = "2"   # 아이콘을 바꾸면 숫자를 올려야 브라우저가 새로 받음


def make_ico(pngs):
    """PNG 여러 장을 하나의 .ico로 묶는다 (PNG-in-ICO, 모든 현행 브라우저 지원)."""
    import struct
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in pngs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return head + entries + blobs


SITE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NBER Literature Radar</title>
<link rel="icon" href="favicon.ico?v=__IV__" sizes="any">
<link rel="icon" href="favicon.svg?v=__IV__" type="image/svg+xml">
<link rel="icon" href="favicon-32.png?v=__IV__" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png?v=__IV__">
<link rel="manifest" href="site.webmanifest?v=__IV__">
<meta name="theme-color" content="#F4ECDF" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1B1713" media="(prefers-color-scheme: dark)">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
:root{--bg:#F4ECDF;--card:#FFFAF2;--ink:#2B241D;--sub:#65584A;--mute:#9A8B79;--line:#E5D7C1;--soft:#F7EFE2;--accent:#C8742E;--ok:#4F7A3A;--err:#A8324A}
@media (prefers-color-scheme:dark){:root{--bg:#1B1713;--card:#24201B;--ink:#EFE6D8;--sub:#C2B4A2;--mute:#8D7F6E;--line:#3A332B;--soft:#2C2721;--accent:#E3A05A;--ok:#8DB872;--err:#E07A8C}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Pretendard,-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}
button,input,textarea{font-family:inherit;font-size:inherit;color:inherit}
.wrap{max-width:820px;margin:0 auto;padding:36px 20px 80px}
header{display:flex;align-items:center;gap:16px}
header .logo{width:56px;height:56px;flex:none;filter:drop-shadow(0 4px 10px rgba(74,54,38,.18))}
header .eyebrow{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);font-weight:600}
header h1{font-family:Georgia,'Times New Roman',serif;font-size:31px;margin:2px 0 0;letter-spacing:-.01em}
#sub{margin:14px 0 0;color:var(--sub);font-size:13.5px}
nav{display:flex;gap:4px;margin-top:20px;border-bottom:1px solid var(--line)}
nav a{padding:10px 14px;font-size:14px;color:var(--mute);text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px;font-weight:600}
nav a.on{color:var(--ink);border-color:var(--accent)}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:16px 0 12px;border-bottom:1px solid var(--line)}
.search{width:100%;padding:11px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);font-size:14px;outline:none}
.search:focus,.inp:focus,textarea:focus{border-color:var(--accent);outline:none}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--sub);font-size:12.5px;padding:6px 11px;border-radius:999px;cursor:pointer;user-select:none}
.chip .n{opacity:.6;margin-left:4px}
.chip.on{color:#fff;border-color:transparent}
.chip.arch{border-style:dashed;background:transparent}
.chip.arch .tagx{font-size:10.5px;opacity:.7;margin-left:5px}
.day{margin:32px 0 12px;display:flex;align-items:baseline;gap:10px}
.day h2{font-size:15px;margin:0;font-weight:700}
.day span{font-size:12.5px;color:var(--mute)}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:12px;padding:20px 22px;margin-bottom:14px;box-shadow:0 1px 0 rgba(120,90,50,.04)}
.meta{font-size:11.5px;color:var(--mute);letter-spacing:.03em;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.meta a{color:var(--mute)}
.field{color:var(--c);font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.title{display:block;font-family:Georgia,'Times New Roman',serif;font-size:20px;line-height:1.35;font-weight:700;text-decoration:none;margin:8px 0 6px}
.title:hover{text-decoration:underline;text-decoration-thickness:1px}
.authors{font-size:13.5px;color:var(--sub);margin-bottom:10px}
.tag{display:inline-block;font-size:11px;padding:3px 8px;border-radius:999px;border:1px solid color-mix(in srgb,var(--tc) 40%,transparent);color:var(--tc);margin:0 4px 4px 0}
.abs{font-size:14.5px;line-height:1.75;margin-top:8px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.abs.open{display:block}
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
/* settings */
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-top:22px}
.panel h3{margin:0 0 4px;font-size:16px}
.panel .desc{margin:0 0 14px;color:var(--sub);font-size:13px;line-height:1.6}
.fe{border:1px solid var(--line);border-left:4px solid var(--c);border-radius:10px;padding:12px 14px;margin-bottom:10px;background:var(--bg)}
.fe-top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.fe-top input[type=color]{width:34px;height:34px;border:1px solid var(--line);border-radius:8px;padding:2px;background:var(--card);cursor:pointer}
.inp{flex:1;min-width:180px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);font-size:14px;font-weight:600}
.inp:disabled{opacity:.75}
.cnt{font-size:12px;color:var(--mute);white-space:nowrap}
textarea{width:100%;margin-top:8px;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);font-size:13px;line-height:1.55;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.hint{font-size:12px;color:var(--mute);margin-top:6px;line-height:1.55}
.btn{border:1px solid var(--line);background:var(--card);border-radius:8px;padding:8px 13px;font-size:13px;cursor:pointer;color:var(--sub)}
.btn:hover{border-color:var(--mute)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.btn.primary:disabled{opacity:.45;cursor:not-allowed}
.btn.small{padding:6px 10px;font-size:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.arch-item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-top:1px dashed var(--line);font-size:14px}
.arch-item:first-child{border-top:0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:1px}
code{background:var(--soft);padding:1px 6px;border-radius:5px;font-size:12.5px}
.syntax{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px;color:var(--sub)}
.msg{margin-top:12px;font-size:13px;line-height:1.6}
.msg.ok{color:var(--ok)} .msg.err{color:var(--err)}
.dirty{font-size:12.5px;color:var(--accent);font-weight:600}
details.howto{margin-top:10px;font-size:13px;color:var(--sub);line-height:1.7}
details.howto summary{cursor:pointer;font-weight:600}
details.howto ol{margin-top:6px}
footer{margin-top:50px;font-size:12px;color:var(--mute);line-height:1.7;border-top:1px solid var(--line);padding-top:16px}
</style></head>
<body><div class="wrap">
<header>
  <div class="logo">__ICON__</div>
  <div><div class="eyebrow">NBER Working Papers</div><h1>NBER Literature Radar</h1></div>
</header>
<p id="sub"></p>
<nav><a href="#papers" id="tab-papers">논문</a><a href="#settings" id="tab-settings">설정</a></nav>

<section id="view-papers">
  <div class="controls">
    <input class="search" id="q" type="search" placeholder="제목·저자·abstract·키워드 검색">
    <div class="chips" id="chips"></div>
  </div>
  <main id="list"></main>
</section>

<section id="view-settings" hidden></section>

<footer>
  Source: NBER new working papers RSS · Related literature: OpenAlex (경제·재무 분야, 상위 저널 우선, 피인용 __MINCITED__회 이상)<br>
  분야는 제목·abstract 키워드로 자동 분류됩니다. Related literature는 검색 기반이라 실제 참고문헌과 다를 수 있습니다.<br>
  PDF는 NBER 구독 기관 네트워크(교내망)나 NBER 로그인이 필요할 수 있습니다.
</footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const PAPERS = D.papers;
const TOKEN_KEY = 'nber_radar_token';
const PALETTE = ['#2F6690','#4F7A3A','#B5562B','#7A4E9C','#A8324A','#1F7A7A','#8A6A1F','#5C6B8A','#9C4F6B','#3E7C59'];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LS = {get:k=>{try{return localStorage.getItem(k)}catch(e){return null}}, set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}, del:k=>{try{localStorage.removeItem(k)}catch(e){}}};
const COUNTS = {}; PAPERS.forEach(p => COUNTS[p.field] = (COUNTS[p.field]||0) + 1);
let saved = D.config;                                   // GitHub에 저장된 설정
let draft = JSON.parse(JSON.stringify(D.config));       // 설정 탭에서 편집 중인 설정
let active = 'ALL', query = '';

const colorOf = n => (saved.fields.find(f => f.name === n) || {}).color || '#8C7B6B';
const latestN = PAPERS.filter(p => p.added === D.latest && !p.backfill).length;
const backN = PAPERS.filter(p => p.backfill).length;
document.getElementById('sub').textContent =
  `마지막 업데이트 ${D.updated} · 누적 ${PAPERS.length}편` + (backN ? ` (과거 불러오기 ${backN}편 포함)` : '')
  + (D.latest ? ` · 최근 발행분(${D.latest}) ${latestN}편` : '');

/* ── 논문 탭 ─────────────────────────────────────────── */
function fieldGroups(){
  const act = saved.fields.filter(f => f.active).map(f => f.name);
  const arch = [...new Set([...saved.fields.filter(f => !f.active).map(f => f.name), ...Object.keys(COUNTS)])]
    .filter(n => !act.includes(n) && COUNTS[n]);
  return {act, arch};
}
function chips(){
  const {act, arch} = fieldGroups();
  const mk = (k, label, n, c, isArch) => {
    const on = active === k;
    const st = on ? `background:${c};color:${k==='ALL'?'var(--bg)':'#fff'}` : (isArch ? `color:${c};border-color:${c}66` : '');
    return `<button class="chip ${on?'on':''} ${isArch?'arch':''}" data-k="${esc(k)}" style="${st}" title="${isArch?'관심분야에서 뺀 분야 — 과거에 받은 논문만 남아 있음':''}">${esc(label)}<span class="n">${n}</span>${isArch?'<span class="tagx">보관</span>':''}</button>`;
  };
  document.getElementById('chips').innerHTML =
    mk('ALL', '전체', PAPERS.length, 'var(--ink)', false) +
    act.map(n => mk(n, n, COUNTS[n]||0, colorOf(n), false)).join('') +
    arch.map(n => mk(n, n, COUNTS[n], colorOf(n), true)).join('');
  document.querySelectorAll('.chip').forEach(b => b.onclick = () => { active = b.dataset.k; renderPapers(); });
}
function refList(items, citeAs){
  return '<ol>' + items.map(r => r.unresolved
    ? `<li><b>${esc(r.cite_as)}</b> → <a href="${esc(r.url)}" target="_blank" rel="noopener">Google Scholar에서 찾기</a></li>`
    : `<li>${citeAs && r.cite_as ? `<b>${esc(r.cite_as)}</b> → ` : `<span class="src">${esc(r.authors)} (${esc(r.year)})</span> `}`
      + `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>`
      + `${r.venue ? ` · <i>${esc(r.venue)}</i>` : ''}${r.cited ? ` <span class="src">· cited ${Number(r.cited).toLocaleString()}</span>` : ''}</li>`).join('') + '</ol>';
}
function card(p, openRefs){
  const c = colorOf(p.field);
  const tags = (p.hits[p.field]||[]).slice(0,6).map(t => `<span class="tag" style="--tc:${c}">${esc(t)}</span>`).join('')
    + Object.keys(p.scores||{}).filter(f => f!==p.field).map(f => `<span class="tag" style="--tc:${colorOf(f)}">+ ${esc(f)}</span>`).join('');
  let refs = '';
  if (p.cited_refs?.length) refs += `<div class="lab">Cited in abstract</div>` + refList(p.cited_refs, true);
  if (p.related?.length) refs += `<div class="lab">${p.related_kind==='references' ? 'Key references <i>· 피인용 상위</i>' : 'Related literature <i>· 자동 검색, 관련도순</i>'}</div>` + refList(p.related, false);
  if (!refs) refs = '<div class="lab">자동 검색 결과 없음</div>';
  const n = (p.cited_refs?.length||0) + (p.related?.length||0);
  return `<article class="card" style="--c:${c}">
    <div class="meta"><span class="field">${esc(p.field)}</span><span>NBER ${esc(p.id.toUpperCase())}</span>${p.backfill && p.date ? `<span>${esc(p.date)}</span>` : ''}
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
// 매일 수집분은 수집일로, 과거 불러오기분은 발행 월로 묶는다
const fmtMonth = k => `${k.slice(0,4)}년 ${+k.slice(5,7)}월`;
const gkey = p => p.backfill ? ((p.date || '').slice(0,7) || '0000-00') : p.added;
const glabel = k => /^\d{4}-\d{2}$/.test(k)
  ? (k === '0000-00' ? '과거 불러오기 · 날짜 미상' : `${fmtMonth(k)} 발행 · 과거 불러오기`)
  : k + (k === D.latest ? ' · 최신' : '');
function renderPapers(){
  chips();
  const q = query.trim().toLowerCase();
  const list = PAPERS.filter(p => (active==='ALL' || p.field===active) &&
    (!q || [p.title,p.authors,p.abstract,...Object.values(p.hits||{}).flat()].join(' ').toLowerCase().includes(q)));
  const days = {};
  list.forEach(p => (days[gkey(p)] ||= []).push(p));
  const keys = Object.keys(days).sort().reverse();
  const isArch = fieldGroups().arch.includes(active);
  document.getElementById('list').innerHTML = (keys.length ? keys.map(d =>
    `<div class="day"><h2>${glabel(d)}</h2><span>${days[d].length}편</span></div>` +
    days[d].map(p => card(p, d===D.latest && !isArch)).join('')).join('')
    : `<div class="empty">${active!=='ALL' && !COUNTS[active] ? '아직 이 분야로 받은 논문이 없습니다. 다음 NBER 발행분부터 모입니다.' : '조건에 맞는 논문이 없습니다.'}</div>`);
  document.querySelectorAll('.abs').forEach(el => el.onclick = () => el.classList.toggle('open'));
}
document.getElementById('q').addEventListener('input', e => { query = e.target.value; renderPapers(); });

/* ── 설정 탭 ─────────────────────────────────────────── */
const cleanCfg = c => ({version: 1, min_score: Math.max(1, parseInt(c.min_score) || 1),
  fields: c.fields.map(f => ({name: f.name.trim(), color: f.color, active: !!f.active,
    keywords: [...new Set(f.keywords.map(k => k.trim()).filter(Boolean))]}))});
const isDirty = () => JSON.stringify(cleanCfg(draft)) !== JSON.stringify(cleanCfg(saved));
const lockedName = f => !!COUNTS[f._orig || f.name] && saved.fields.some(s => s.name === (f._orig || f.name));

function validate(){
  const names = draft.fields.map(f => f.name.trim());
  if (names.some(n => !n)) return '이름이 빈 분야가 있습니다.';
  const dup = names.find((n, i) => names.indexOf(n) !== i);
  if (dup) return `분야 이름 "${dup}"이(가) 중복됩니다.`;
  const empty = draft.fields.find(f => f.active && !f.keywords.some(k => k.trim()));
  if (empty) return `"${empty.name}" 분야에 키워드가 하나도 없습니다.`;
  return null;
}
function fieldEditor(f, i){
  const n = COUNTS[f.name] || 0, locked = lockedName(f);
  const action = n ? `<button class="btn small" data-act="archive" data-i="${i}" title="관심분야에서 빼고, 받은 논문은 첫 페이지에 '보관'으로 남김">관심분야에서 빼기</button>`
                   : `<button class="btn small" data-act="delete" data-i="${i}">삭제</button>`;
  return `<div class="fe" style="--c:${esc(f.color)}">
    <div class="fe-top">
      <input type="color" value="${esc(f.color)}" data-k="color" data-i="${i}" title="색">
      <input class="inp" value="${esc(f.name)}" data-k="name" data-i="${i}" ${locked?'disabled title="논문이 쌓인 분야는 이름을 바꿀 수 없어요"':''} placeholder="분야 이름">
      <span class="cnt">논문 ${n}편</span>${action}
    </div>
    <textarea rows="${Math.min(8, Math.max(3, Math.ceil(f.keywords.length/3)))}" data-k="keywords" data-i="${i}" placeholder="키워드를 한 줄에 하나씩 (예: mortgage)">${esc(f.keywords.join('\n'))}</textarea>
    ${locked ? '<div class="hint">이미 논문이 쌓인 분야라 이름은 고정입니다. 이름을 바꾸고 싶으면 새 분야를 만들고 이 분야는 빼세요.</div>' : ''}
  </div>`;
}
function renderSettings(){
  const act = draft.fields.map((f, i) => [f, i]).filter(([f]) => f.active);
  const arch = draft.fields.map((f, i) => [f, i]).filter(([f]) => !f.active);
  const orphan = Object.keys(COUNTS).filter(n => !draft.fields.some(f => f.name === n));
  const token = LS.get(TOKEN_KEY);
  const repoOk = !!D.repo;
  document.getElementById('view-settings').innerHTML = `
  <div class="panel">
    <h3>관심분야</h3>
    <p class="desc">제목이나 abstract에 키워드가 나오면 그 분야로 모읍니다 (제목 3점, abstract 1점, 가장 높은 분야에 배치).<br>
    저장하면 바로 수집이 다시 돌아서, 새로 넣은 분야로 <b>이번 주 NBER 발행분</b>도 훑습니다.</p>
    ${act.map(([f, i]) => fieldEditor(f, i)).join('') || '<p class="desc">활성 분야가 없습니다.</p>'}
    <button class="btn" id="add">+ 새 분야 추가</button>
  </div>

  <div class="panel">
    <h3>보관된 분야</h3>
    <p class="desc">관심분야에서 뺀 분야입니다. 더 이상 새 논문을 모으지 않지만, 받았던 논문은 첫 페이지에서 <b>보관</b> 표시로 계속 볼 수 있습니다.</p>
    ${arch.length || orphan.length ? arch.map(([f, i]) => `<div class="arch-item"><span><span class="dot" style="background:${esc(f.color)}"></span>${esc(f.name)} <span class="cnt">· 논문 ${COUNTS[f.name]||0}편</span></span>
        <span class="row"><button class="btn small" data-act="restore" data-i="${i}">다시 관심분야로</button>${COUNTS[f.name] ? '' : `<button class="btn small" data-act="delete" data-i="${i}">완전 삭제</button>`}</span></div>`).join('')
      + orphan.map(n => `<div class="arch-item"><span><span class="dot" style="background:#8C7B6B"></span>${esc(n)} <span class="cnt">· 논문 ${COUNTS[n]}편 (설정 파일에 없음)</span></span></div>`).join('')
      : '<p class="desc" style="margin:0">아직 없습니다.</p>'}
  </div>

  <div class="panel" id="bfpanel">
    <h3>과거 논문 불러오기</h3>
    <p class="desc">고른 날짜부터 지금까지 나온 NBER working paper 중 <b>아직 없는 것</b>을 지금 저장된 관심분야로 걸러 추가합니다.
    첫 페이지에서는 <b>발행 월</b>별로 묶여 보입니다. 메일은 보내지 않습니다.</p>
    <div class="row">
      <input class="inp" type="date" id="bfdate" min="2000-01-01" max="${TODAY}" value="${esc(bfDefault())}" style="flex:none;width:170px;font-weight:400">
      <button class="btn small" data-bf="3">3개월</button><button class="btn small" data-bf="6">6개월</button>
      <button class="btn small" data-bf="12">1년</button><button class="btn small" data-bf="24">2년</button>
      <button class="btn primary" id="bfgo" ${repoOk && token ? '' : 'disabled'}>불러오기 요청</button>
    </div>
    <div class="hint" id="bfest"></div>
    ${repoOk && !token ? '<div class="hint">요청하려면 아래 <b>저장</b> 칸에서 GitHub에 먼저 연결하세요.</div>' : ''}
    <div class="msg" id="bfmsg">${bfStatusHtml()}</div>
  </div>

  <div class="panel">
    <h3>키워드 쓰는 법</h3>
    <div class="syntax">
      <code>mortgage</code><span>mortgage, mortgages 모두 (끝의 s/es 자동 허용, 대소문자 무시)</span>
      <code>debt-to-income</code><span>띄어쓰기·하이픈 구분 없이 (debt to income도 매칭)</span>
      <code>refinanc*</code><span>refinance, refinancing, refinanced … (<b>*</b> = 뒤에 아무 글자)</span>
      <code>re:\\bMPCs?\\b</code><span><b>re:</b>로 시작하면 정규식 그대로 (고급)</span>
    </div>
    <div class="row" style="margin-top:14px"><span style="font-size:13px;color:var(--sub)">최소 점수</span>
      <input class="inp" style="flex:none;width:70px;min-width:0" type="number" min="1" value="${esc(draft.min_score)}" id="minscore">
      <span class="hint" style="margin:0">1 = abstract에 한 번만 나와도 수집 · 2 이상 = 잡음 감소, 놓치는 논문 증가</span></div>
  </div>

  <div class="panel">
    <h3>저장</h3>
    ${!repoOk ? '<p class="desc">로컬 미리보기라 저장소 정보가 없습니다. GitHub Pages에서 열면 저장할 수 있어요.</p>' : ''}
    <div class="row" style="justify-content:space-between">
      <span class="dirty" id="dirty"></span>
      <span class="row">
        <button class="btn" id="copy">설정 JSON 복사</button>
        <button class="btn primary" id="save" ${repoOk && token ? '' : 'disabled'}>GitHub에 저장</button>
      </span>
    </div>
    <div class="msg" id="msg"></div>
    <div style="margin-top:14px;padding-top:14px;border-top:1px dashed var(--line)">
      ${token
        ? `<div class="row" style="justify-content:space-between"><span style="font-size:13px;color:var(--ok)">● 이 브라우저는 GitHub에 연결됨</span><button class="btn small" id="unlink">연결 해제</button></div>`
        : `<div class="row"><input class="inp" type="password" id="token" placeholder="GitHub 토큰 (github_pat_...)" autocomplete="off" style="font-weight:400"><button class="btn" id="link">연결</button></div>
           <details class="howto"><summary>토큰 만드는 법 (처음 한 번, 3분)</summary><ol>
             <li><a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub → Fine-grained token 만들기</a> 열기</li>
             <li>Token name: <code>nber-radar</code> · Expiration: 1년(366 days) 정도</li>
             <li>Repository access: <b>Only select repositories</b> → <code>${esc((D.repo||'').split('/')[1] || 'nber-radar')}</code></li>
             <li>Permissions → Repository permissions → <b>Contents: Read and write</b> (나머지는 그대로)</li>
             <li>Generate token → 나온 값을 위 칸에 붙여넣고 <b>연결</b></li></ol>
             토큰은 이 브라우저에만 저장되고, 이 저장소의 파일 쓰기 외에는 아무 권한이 없습니다. 다른 기기에서는 다시 연결해야 합니다.</details>`}
      ${repoOk ? `<div class="hint">토큰 없이 하려면: <b>설정 JSON 복사</b> → <a href="https://github.com/${esc(D.repo)}/edit/HEAD/config.json" target="_blank" rel="noopener">GitHub에서 config.json 편집</a> → 전체 붙여넣기 → Commit</div>` : ''}
    </div>
  </div>`;
  bindSettings();
  updateDirty();
}
function updateDirty(){
  const el = document.getElementById('dirty');
  if (el) el.textContent = isDirty() ? '● 저장하지 않은 변경 있음' : '';
}
function msg(text, kind){ const el = document.getElementById('msg'); el.className = 'msg ' + (kind||''); el.innerHTML = text; }
function bindSettings(){
  const V = document.getElementById('view-settings');
  V.querySelectorAll('[data-k]').forEach(el => el.addEventListener('input', () => {
    const f = draft.fields[+el.dataset.i], k = el.dataset.k;
    if (k === 'keywords') f.keywords = el.value.split('\n');
    else f[k] = el.value;
    if (k === 'color') el.closest('.fe').style.setProperty('--c', el.value);
    updateDirty();
  }));
  V.querySelectorAll('[data-act]').forEach(b => b.onclick = () => {
    const i = +b.dataset.i, f = draft.fields[i];
    if (b.dataset.act === 'archive') f.active = false;
    if (b.dataset.act === 'restore') f.active = true;
    if (b.dataset.act === 'delete') draft.fields.splice(i, 1);
    renderSettings();
  });
  document.getElementById('add').onclick = () => {
    const used = draft.fields.map(f => f.color.toLowerCase());
    draft.fields.push({name: '', color: PALETTE.find(c => !used.includes(c.toLowerCase())) || PALETTE[0], keywords: [], active: true});
    renderSettings();
    const ins = document.querySelectorAll('#view-settings .inp[data-k=name]');
    ins[ins.length - 1]?.focus();
  };
  document.getElementById('minscore').oninput = e => { draft.min_score = e.target.value; updateDirty(); };
  document.getElementById('copy').onclick = async () => {
    const err = validate(); if (err) return msg(err, 'err');
    const text = JSON.stringify(cleanCfg(draft), null, 2);
    try { await navigator.clipboard.writeText(text); msg('복사했습니다. GitHub의 config.json 내용을 전부 지우고 붙여넣은 뒤 Commit 하세요.', 'ok'); }
    catch (e) { msg('<textarea rows="8" readonly>' + esc(text) + '</textarea>자동 복사가 막혀 있어요. 위 내용을 직접 복사하세요.'); }
  };
  const link = document.getElementById('link');
  if (link) link.onclick = () => {
    const v = document.getElementById('token').value.trim();
    if (!/^(github_pat_|ghp_)/.test(v)) return msg('토큰 형식이 아닙니다 (github_pat_ 로 시작).', 'err');
    LS.set(TOKEN_KEY, v); renderSettings(); msg('연결했습니다. 이제 GitHub에 저장할 수 있어요.', 'ok');
  };
  const unlink = document.getElementById('unlink');
  if (unlink) unlink.onclick = () => { LS.del(TOKEN_KEY); renderSettings(); };
  document.getElementById('save').onclick = save;
  const bd = document.getElementById('bfdate'), be = document.getElementById('bfest');
  const est = () => be.textContent = bfEstimate(bd.value);
  bd.oninput = est; est();
  V.querySelectorAll('[data-bf]').forEach(b => b.onclick = () => { bd.value = monthsAgo(+b.dataset.bf); est(); });
  document.getElementById('bfgo').onclick = requestBackfill;
}
const TODAY = new Date(Date.now() + 9*3600e3).toISOString().slice(0,10);   // 한국 날짜
const BF = D.backfill || {request: null, status: {}};
function monthsAgo(n){ const d = new Date(TODAY); d.setMonth(d.getMonth() - n); return d.toISOString().slice(0,10); }
function bfDefault(){ return (BF.request && BF.request.from) || monthsAgo(6); }
function bfEstimate(v){
  if (!v) return '';
  const months = Math.max(0.25, (new Date(TODAY) - new Date(v)) / (30.4 * 864e5));
  const total = Math.round(months * 150), hit = Math.round(total * 0.2);
  const mins = Math.round(2 + hit * 1.8 / 60);
  return `NBER 약 ${total.toLocaleString()}편 확인 → 관심분야 ${hit.toLocaleString()}편 안팎 예상 · 약 ${mins}분`
    + (mins > 40 ? ' (40분이 넘으면 다음 날 아침 실행에서 이어서 불러옵니다)' : '');
}
function bfStatusHtml(){
  const r = BF.request, s = BF.status || {};
  if (!r) return '<span style="color:var(--mute)">아직 요청한 적이 없습니다.</span>';
  if (s.request !== r.requested_at)
    return `⏳ <b>${esc(r.from)}</b>부터 불러오기 요청됨 — 처리 대기 중입니다. 몇 분 뒤 새로고침하세요.`;
  if (s.state === 'running')
    return `⏳ <b>${esc(s.from)}</b>부터 진행 중 — 관심분야 ${s.matched ?? '?'}편 중 ${s.added}편 추가, ${s.remaining ?? '?'}편 남음.<br><span style="color:var(--mute)">${esc(s.message || '')}</span>`;
  if (s.state === 'error')
    return `<span style="color:var(--err)">⚠ ${esc(s.message || '불러오기 실패')}</span>`;
  return `<span style="color:var(--ok)">✓ <b>${esc(s.from)}</b>부터 불러오기 완료 (${esc(s.finished || '')})</span> — NBER ${(s.listed||0).toLocaleString()}편 확인, 관심분야 <b>${s.added}편</b> 추가.`
    + (s.short_abstracts ? `<br><span style="color:var(--mute)">${s.short_abstracts}편은 전체 abstract를 구하지 못해 앞부분으로만 분류했습니다.</span>` : '')
    + `<br><span style="color:var(--mute)">당시 기준 분야: ${esc((s.fields || []).join(', '))}</span>`;
}
async function putFile(path, text, message){
  const token = LS.get(TOKEN_KEY);
  const api = `https://api.github.com/repos/${D.repo}/contents/${path}`;
  const h = {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'};
  let sha;
  const g = await fetch(api, {headers: h, cache: 'no-store'});
  if (g.ok) sha = (await g.json()).sha; else if (g.status !== 404) throw g.status;
  const r = await fetch(api, {method: 'PUT', headers: h, body: JSON.stringify({message, content: b64(text), sha})});
  if (!r.ok) throw r.status;
}
async function requestBackfill(){
  const out = document.getElementById('bfmsg');
  const say = (t, k) => { out.className = 'msg ' + (k || ''); out.innerHTML = t; };
  const v = document.getElementById('bfdate').value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) return say('날짜를 고르세요.', 'err');
  if (v < '2000-01-01' || v > TODAY) return say('2000-01-01부터 오늘 사이의 날짜를 고르세요.', 'err');
  if (isDirty()) return say('관심분야에 저장하지 않은 변경이 있습니다. 불러오기는 <b>저장된</b> 분야 기준이라, 먼저 저장하세요.', 'err');
  const btn = document.getElementById('bfgo'); btn.disabled = true; say('요청 중…');
  try {
    const req = {from: v, requested_at: new Date().toISOString()};
    await putFile('backfill.json', JSON.stringify(req, null, 2) + '\n', `backfill: from ${v}`);
    BF.request = req;
    say(`요청했습니다. 수집이 자동으로 시작돼요 — ${esc(bfEstimate(v).split('· ')[1] || '')} 뒤 새로고침하세요.
      (<a href="https://github.com/${esc(D.repo)}/actions" target="_blank" rel="noopener">진행 상황 보기</a>)`, 'ok');
  } catch (s) {
    say(typeof s === 'number' ? apiErr(s) : '네트워크 오류로 요청하지 못했습니다.', 'err');
  }
  btn.disabled = false;
}
function b64(str){ const bytes = new TextEncoder().encode(str); let s = ''; bytes.forEach(b => s += String.fromCharCode(b)); return btoa(s); }
function apiErr(status){
  return ({401: '토큰이 잘못됐거나 만료됐습니다. 연결 해제 후 새 토큰을 넣으세요.',
           403: '토큰 권한이 부족합니다. 이 저장소에 Contents: Read and write 권한을 줬는지 확인하세요.',
           404: '저장소를 찾을 수 없습니다. 토큰의 Repository access에 이 저장소가 포함됐는지 확인하세요.',
           409: '그사이 config.json이 바뀌었습니다. 새로고침 후 다시 저장하세요.',
           422: 'GitHub가 요청을 거절했습니다. 새로고침 후 다시 시도하세요.'})[status] || `GitHub 오류 (${status})`;
}
async function save(){
  const err = validate(); if (err) return msg(err, 'err');
  if (!isDirty()) return msg('바뀐 내용이 없습니다.');
  const btn = document.getElementById('save'); btn.disabled = true; msg('저장 중…');
  try {
    const next = cleanCfg(draft);
    await putFile('config.json', JSON.stringify(next, null, 2) + '\n', 'settings: update interest fields');
    saved = next; draft = JSON.parse(JSON.stringify(next));
    renderSettings(); renderPapers();
    msg(`저장했습니다. 수집이 자동으로 시작돼요 — <b>2~3분 뒤 새로고침</b>하면 반영됩니다.
         (<a href="https://github.com/${esc(D.repo)}/actions" target="_blank" rel="noopener">진행 상황 보기</a>)`, 'ok');
  } catch (s) {
    msg(typeof s === 'number' ? apiErr(s) : '네트워크 오류로 저장하지 못했습니다.', 'err');
    btn.disabled = false;
  }
}

/* ── 탭 전환 ─────────────────────────────────────────── */
function route(){
  const s = location.hash === '#settings';
  document.getElementById('view-papers').hidden = s;
  document.getElementById('view-settings').hidden = !s;
  document.getElementById('tab-papers').classList.toggle('on', !s);
  document.getElementById('tab-settings').classList.toggle('on', s);
  if (s) renderSettings(); else renderPapers();
}
window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', e => { if (isDirty()) { e.preventDefault(); e.returnValue = ''; } });
route();
</script></body></html>"""


def build_site(store):
    papers = sorted(store.values(), key=lambda p: (p["added"], p.get("score", 0)), reverse=True)
    data = {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "latest": max((p["added"] for p in papers if not p.get("backfill")), default=""),
        "backfill": {"request": BACKFILL.get("request"), "status": BACKFILL.get("status") or {}},
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "config": CONFIG,
        "papers": papers,
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    icon_inline = ICON_SVG.replace('id="g"', 'id="hg"').replace("url(#g)", "url(#hg)") \
                          .replace('id="s"', 'id="hs"').replace("url(#s)", "url(#hs)")
    page = (SITE_TEMPLATE.replace("__DATA__", blob)
                         .replace("__ICON__", icon_inline)
                         .replace("__MINCITED__", str(SETTINGS["min_cited"]))
                         .replace("__IV__", ICON_VERSION))
    out = Path(SETTINGS["site_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "favicon.svg").write_text(ICON_SVG, encoding="utf-8")
    (out / "favicon-32.png").write_bytes(base64.b64decode(ICON_PNG32))
    (out / "apple-touch-icon.png").write_bytes(base64.b64decode(ICON_PNG180))
    (out / "favicon.ico").write_bytes(make_ico([(16, base64.b64decode(ICON_PNG16)),
                                                (32, base64.b64decode(ICON_PNG32)),
                                                (48, base64.b64decode(ICON_PNG48))]))
    (out / "site.webmanifest").write_text(json.dumps({
        "name": "NBER Literature Radar", "short_name": "NBER Radar", "start_url": "./", "display": "browser",
        "background_color": "#F4ECDF", "theme_color": "#F4ECDF",
        "icons": [{"src": f"apple-touch-icon.png?v={ICON_VERSION}", "sizes": "180x180", "type": "image/png"},
                  {"src": f"favicon.svg?v={ICON_VERSION}", "sizes": "any", "type": "image/svg+xml"}]},
        ensure_ascii=False), encoding="utf-8")
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
# [9] BACKFILL — 과거 특정 날짜부터 지금까지의 working paper 불러오기
# ════════════════════════════════════════════════════════════════════
# 요청: backfill.json  {"from": "2025-06-01", "requested_at": "..."}   ← 웹사이트 설정 탭이 씀
# 상태: data/backfill_status.json                                       ← 이 스크립트가 씀
# 흐름: 목록(NBER API, 실패 시 OpenAlex) → 이미 있는 논문 제외 → 전체 abstract 보강
#       → 현재 활성 분야로 분류 → 시작 월의 논문만 정확한 발행일 확인 → related 검색 → 저장
# 시간 제한(backfill_minutes)에 걸리면 저장하고 멈추며, 다음 실행(매일 아침 또는 수동)에서 이어간다.

NBER_LIST_API = "https://www.nber.org/api/v1/working_page_listing/contentType/working_paper/_/_/search"
OPENALEX_NBER_SOURCE = "S2809516038"
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


def _month_of(display):
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{4})", display or "")
    if m and m.group(1).capitalize() in MONTHS:
        return f"{m.group(2)}-{MONTHS[m.group(1).capitalize()]:02d}"
    return ""


def _paper(pid, title, authors, abstract, month, full):
    return {"id": pid, "title": title, "authors": authors, "abstract": abstract, "month": month,
            "abstract_full": full, "url": f"https://www.nber.org/papers/{pid}",
            "pdf": f"https://www.nber.org/system/files/working_papers/{pid}/{pid}.pdf",
            "doi": f"10.3386/{pid}"}


def list_nber_since(start):
    """NBER 목록 API (최신순, 100편씩). abstract는 앞부분만 온다. 실패하면 None."""
    start_month, out = start[:7], []
    for page in range(1, 500):
        q = urllib.parse.urlencode({"page": page, "perPage": 100, "sortBy": "public_date"})
        data = http_json(f"{NBER_LIST_API}?{q}")
        results = (data or {}).get("results") or []
        if not results:
            return out if page > 1 else None
        older = 0
        for r in results:
            m = re.search(r"/papers/(w\d+)", r.get("url") or "")
            month = _month_of(r.get("displaydate"))
            if not m:
                continue
            if month and month < start_month:
                older += 1
                continue
            authors = ", ".join(clean_text(a) for a in (r.get("authors") or []))
            out.append(_paper(m.group(1), clean_text(r.get("title")), authors,
                              clean_text(r.get("abstract")), month, False))
        if older == len(results):          # 페이지 전체가 시작 월보다 이전 → 끝
            break
        time.sleep(0.3)
    return out


def _deinvert(inv):
    pos = sorted((i, w) for w, idxs in (inv or {}).items() for i in idxs)
    return " ".join(w for _, w in pos)


def list_openalex_since(start):
    """예비 경로: OpenAlex의 NBER working paper 목록 (abstract 포함, 최근 2~3주는 빠질 수 있음)."""
    out, cursor = [], "*"
    while cursor:
        data = _openalex("works", {
            "filter": f"primary_location.source.id:{OPENALEX_NBER_SOURCE},from_publication_date:{start[:7]}-01",
            "per_page": 200, "cursor": cursor,
            "select": "doi,title,authorships,publication_date,abstract_inverted_index"})
        if not data:
            return out or None
        for w in data.get("results", []):
            m = re.search(r"10\.3386/(w\d+)$", w.get("doi") or "")
            if not m:
                continue
            authors = ", ".join((a.get("author") or {}).get("display_name", "") for a in w.get("authorships", []))
            abstract = clean_text(_deinvert(w.get("abstract_inverted_index")))
            out.append(_paper(m.group(1), clean_text(w.get("title")), authors, abstract,
                              (w.get("publication_date") or "")[:7], bool(abstract)))
        cursor = (data.get("meta") or {}).get("next_cursor") if data.get("results") else None
        time.sleep(0.2)
    return out


def fill_abstracts(papers):
    """앞부분만 있는 abstract를 OpenAlex 전체 abstract로 교체 (50편씩)."""
    need = [p for p in papers if not p["abstract_full"]]
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        data = _openalex("works", {"filter": "doi:" + "|".join(p["doi"] for p in chunk),
                                   "per_page": 50, "select": "doi,abstract_inverted_index"})
        found = {}
        for w in (data or {}).get("results", []):
            m = re.search(r"(w\d+)$", w.get("doi") or "")
            text = clean_text(_deinvert(w.get("abstract_inverted_index")))
            if m and text:
                found[m.group(1)] = text
        for p in chunk:
            if len(found.get(p["id"], "")) >= max(40, len(p["abstract"]) * 0.8):
                p["abstract"], p["abstract_full"] = found[p["id"]], True
        time.sleep(0.2)


def fetch_nber_page(pid):
    """NBER 논문 페이지 → (발행일 YYYY-MM-DD, 전체 abstract). 실패 시 (None, None)."""
    page = http_get(f"https://www.nber.org/papers/{pid}", accept="text/html")
    if not page:
        return None, None
    d = re.search(r'citation_publication_date"\s+content="(\d{4})/(\d{2})/(\d{2})', page)
    a = re.search(r'<div class="page-header__intro-inner">(.*?)</div>', page, re.S)
    return (f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else None,
            clean_text(a.group(1)) if a else None)


def backfill_request():
    req = _load_json(SETTINGS["backfill_file"])
    start = str(req.get("from", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
        return None
    today = datetime.now(KST).date().isoformat()
    start = min(max(start, SETTINGS["backfill_earliest"]), today)
    return {"from": start, "requested_at": str(req.get("requested_at", start))}


def run_backfill(store, req, status, stamp, deadline):
    """store를 직접 갱신하고 새 status를 돌려준다."""
    first = status.get("request") != req["requested_at"]
    status = {"request": req["requested_at"], "from": req["from"], "state": "running",
              "added": 0 if first else status.get("added", 0), "fields": list(FIELDS),
              "started": status.get("started") if not first else stamp}
    print(f"backfill from {req['from']} ({'new request' if first else 'resume'})")

    listing, source = list_nber_since(req["from"]), "nber"
    if listing is None:
        listing, source = list_openalex_since(req["from"]), "openalex"
    if listing is None:
        status.update(state="error", message="NBER·OpenAlex 목록을 모두 가져오지 못했습니다. 다음 실행에서 다시 시도합니다.")
        return status
    status.update(source=source, listed=len(listing))
    cand = [p for p in listing if p["id"] not in store]
    print(f"  listed {len(listing)} via {source}, not yet collected {len(cand)}")

    fill_abstracts(cand)                         # 실측: 6개월치 913편 중 89%를 OpenAlex가 채움
    for p in [p for p in cand if not p["abstract_full"]][:400]:
        d, a = fetch_nber_page(p["id"])          # 나머지는 NBER 논문 페이지에서
        if a and len(a) > len(p["abstract"]):
            p["abstract"], p["abstract_full"] = a, True
        if d:
            p["date"] = d
        time.sleep(0.2)

    matched = []
    for p in cand:
        classify(p)
        if p["score"] < CONFIG["min_score"]:
            continue
        if p["month"] == req["from"][:7] and not p.get("date"):
            p["date"] = fetch_nber_page(p["id"])[0]   # 시작 월만 정확한 날짜 확인
            time.sleep(0.2)
        p.setdefault("date", None)
        p["date"] = p["date"] or p["month"]
        if p["date"] and p["date"] < req["from"][:len(p["date"])]:
            continue
        matched.append(p)
    status.update(matched=len(matched), short_abstracts=sum(not p["abstract_full"] for p in matched))
    print(f"  matched {len(matched)}")

    done = 0
    for p in matched:
        if time.time() > deadline:
            break
        if len(p["date"] or "") < 10:           # 월만 알면 페이지에서 정확한 발행일 확인
            p["date"] = fetch_nber_page(p["id"])[0] or p["date"]
        enrich(p)
        p["backfill"] = True
        store[p["id"]] = {**{k: p.get(k) for k in KEEP_KEYS}, "added": stamp}
        done += 1
        time.sleep(0.2)
    status["added"] += done
    status["remaining"] = len(matched) - done
    if status["remaining"]:
        status["message"] = "시간 제한으로 멈췄습니다. 다음 실행(매일 아침 또는 수동 실행)에서 이어서 불러옵니다."
    else:
        status.update(state="done", finished=stamp, message="")
    print(f"  added {done}, remaining {status['remaining']}")
    return status


# ════════════════════════════════════════════════════════════════════
# [10] STATE — data/papers.json (누적 논문), data/backfill_status.json
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


BACKFILL = {}   # 사이트에 보여줄 과거 불러오기 요청/상태

KEEP_KEYS = ["id", "title", "authors", "abstract", "url", "pdf", "field", "score", "scores", "hits",
             "cited_refs", "related", "related_kind", "date", "backfill"]


# ════════════════════════════════════════════════════════════════════
# [11] MAIN
# ════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true",
                    help="메일은 preview.html로 저장하고 data/는 갱신하지 않음 (site/는 생성)")
    args = ap.parse_args()

    load_config()
    print("fields:", ", ".join(FIELDS) or "(none)", "| min_score:", CONFIG["min_score"])
    now = datetime.now(KST)
    stamp = now.date().isoformat()
    store_path = Path(SETTINGS["data_dir"]) / "papers.json"
    store = _load_json(store_path)

    # 이미 모은 논문만 건너뛰고 RSS 전체를 매번 다시 분류한다.
    # → 설정에서 분야를 새로 넣으면 이번 주 발행분 중 해당 논문이 바로 들어온다.
    feed = fetch_nber_feed()
    matched = []
    for p in feed:
        if p["id"] in store:
            continue
        classify(p)
        if p["score"] >= CONFIG["min_score"]:
            matched.append(p)
    print(f"RSS: {len(feed)} papers / new matches: {len(matched)}")

    for p in matched:
        print(f"  enrich {p['id']} [{p['field']}] {p['title'][:60]}")
        enrich(p)
        time.sleep(0.3)
        p["date"] = stamp
        store[p["id"]] = {**{k: p.get(k) for k in KEEP_KEYS}, "added": stamp}

    # 과거 불러오기: 새 요청이 있거나 지난번에 다 못 끝냈으면 실행
    status_path = Path(SETTINGS["data_dir"]) / "backfill_status.json"
    bstatus = _load_json(status_path)
    req = backfill_request()
    if req and (bstatus.get("request") != req["requested_at"] or bstatus.get("state") == "running"
                or bstatus.get("state") == "error"):
        deadline = time.time() + SETTINGS["backfill_minutes"] * 60
        bstatus = run_backfill(store, req, bstatus, stamp, deadline)
        bstatus["updated"] = now.strftime("%Y-%m-%d %H:%M KST")
    BACKFILL.update(request=req, status=bstatus)

    keep = (now - timedelta(days=SETTINGS["store_keep_days"])).date().isoformat()
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
        print("no new matches — email skipped")

    _save_json(store_path, store)
    if bstatus:
        _save_json(status_path, bstatus)


if __name__ == "__main__":
    main()
