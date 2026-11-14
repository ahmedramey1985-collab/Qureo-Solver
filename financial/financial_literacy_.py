"""
financial_literacy_.py - تسجيل دخول + حل المستويات 1-22 على منصة الثقافة المالية
(egy.app.spx-learning-square.com) باستخدام قاعدة الإجابات المرفقة.

التثبيت (مرة واحدة):
    pip install playwright
    playwright install chromium

تجهيز قاعدة الإجابات (مرة واحدة):
    python build_answers.py <ملف_الإجابات.txt> answers.json

التشغيل:
    python financial_literacy_.py

متغيرات البيئة (كلها اختيارية):
    SITE_USER / SITE_PASS   بيانات الدخول
    HEADLESS=true           تشغيل بدون واجهة
    ANSWERS_JSON            مسار answers.json (الافتراضي: جنب السكريبت)
    START_LEVEL=N           المستوى اللي هتبدأ منه (لو سيبته فاضي أول سؤال لازم يتطابق بوضوح)
    MAX_LEVEL=N             يقف بعد المستوى ده (الافتراضي 22)
    INSPECT=true            يفتح الصفحة ويحفظ dump (نص + أزرار + HTML + سكرين شوت) ويقفل - للمعايرة
    DRY_RUN=true            يطابق أول سؤال ويحدد الإجابة بدون ما يضغط Answer/التالي
    STEP_DELAY_MS=600       انتظار بعد كل خطوة
    MAX_STEPS=4000          حد أقصى للخطوات

السلوك: لو السؤال ما اتطابقش بثقة، أو ظهرت رسالة خطأ بعد الإجابة، أو الصفحة علقت،
السكريبت بيقف عند النقطة دي (سكرين شوت + dump) ومابيخمنش إجابة.
"""

import json
import os
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:  # consoles on Windows often default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent

SITE_URL = os.getenv("SITE_URL", "https://egy.app.spx-learning-square.com/login")
TARGET_URL = os.getenv("TARGET_URL", "https://egy.app.spx-learning-square.com/subjects/7")
SITE_USER = os.getenv("SITE_USER", "123456789")
SITE_PASS = os.getenv("SITE_PASS", "password")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome").strip()   # chrome = جوجل كروم المثبّت؛ فاضي = كروميوم Playwright
CHROME_PATH = os.getenv("CHROME_PATH", "").strip()                  # مسار chrome.exe لو مش في المكان المعتاد

ANSWERS_JSON = Path(os.getenv("ANSWERS_JSON", str(HERE / "answers.json")))
START_LEVEL = int(os.getenv("START_LEVEL", "0") or 0) or None
MAX_LEVEL = int(os.getenv("MAX_LEVEL", "22") or 22)
INSPECT = os.getenv("INSPECT", "false").lower() == "true"
INSPECT_LOGIN = os.getenv("INSPECT_LOGIN", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
STEP_DELAY_MS = int(os.getenv("STEP_DELAY_MS", "600"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "4000"))
UI_TIMEOUT_MS = int(os.getenv("UI_TIMEOUT_MS", "15000"))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "20000"))

# لو الكشف التلقائي ما نفعش، حدد السيليكتورات هنا (اتركها None للكشف التلقائي)
USER_SELECTOR = None
PASS_SELECTOR = None
SUBMIT_SELECTOR = None

# حدود المطابقة
NOT_A_QUESTION = 0.70     # أقل من كده الصفحة مش سؤال (محتوى/تنقل)
ACCEPT_IN_SCOPE = 0.85    # سؤال من المستوى الحالي أو اللي بعده
ACCEPT_UNKNOWN = 0.93     # أول سؤال والمستوى مش معروف
ACCEPT_OUT_OF_SCOPE = 0.97  # سؤال من مستوى تاني: لازم يكون مطابق بوضوح
AMBIGUITY_MARGIN = 0.03   # فرق أدنى بين أفضل إجابتين مختلفتين
OPTION_ACCEPT = 0.88      # أقل تشابه مقبول بين الإجابة والخيار على الشاشة
OPTION_MARGIN = 0.05      # فرق أدنى بين أفضل خيارين مختلفين


class StopSolving(Exception):
    """بنرميها لما لازم نوقف بدل ما نخمّن."""


# ───────────────────────────── تسجيل الدخول (زي ما هو) ─────────────────────────────

def login(page):
    print("[FINANCE] جاري فتح صفحة تسجيل الدخول…", flush=True)
    response = page.goto(SITE_URL, wait_until="domcontentloaded")
    # A CloudFront error page has no login form.  Waiting for selectors in
    # that page used to look like the finance runner was frozen.  Report the
    # server-side refusal immediately, without trying to type credentials.
    status = getattr(response, "status", 0) or 0
    body = (page.locator("body").inner_text(timeout=3000) or "").lower()
    if status >= 400 or "request blocked" in body or "request could not be satisfied" in body:
        raise RuntimeError(
            f"خادم الثقافة المالية رفض فتح صفحة الدخول (HTTP {status or 'غير معروف'}). "
            "جرّب مرة أخرى من شبكتك لاحقًا؛ لم تُرسل أي بيانات دخول."
        )
    if INSPECT_LOGIN:
        dump_page(page, "login_inspect")
        print("[FINANCE-INSPECT] تم حفظ تشخيص صفحة الدخول بدون إدخال أي بيانات.")
        return False

    pass_sel = PASS_SELECTOR or "input[type=password]"
    page.wait_for_selector(pass_sel, timeout=UI_TIMEOUT_MS)

    user_sel = USER_SELECTOR or (
        "input[type=email], input[name*=user i], input[id*=user i], "
        "input[name*=email i], input[id*=email i], "
        "input[type=text], input[type=tel], input[type=number]"
    )
    page.locator(user_sel).first.fill(SITE_USER)
    page.locator(pass_sel).first.fill(SITE_PASS)

    if SUBMIT_SELECTOR:
        page.click(SUBMIT_SELECTOR)
    else:
        submit = page.locator("button[type=submit], input[type=submit]")
        if submit.count():
            submit.first.click()
        else:
            page.locator(pass_sel).first.press("Enter")

    # The learning site keeps background requests open, so `networkidle` can
    # wait until Playwright times out although the next screen is ready.  Wait
    # instead for a real login transition: a new URL or the password field
    # disappearing.  This prevents a slow but valid login from being reported
    # as failed after a fixed short delay.
    deadline = time.time() + UI_TIMEOUT_MS / 1000
    while time.time() < deadline:
        still_login = page.locator(pass_sel)
        try:
            visible = still_login.count() and still_login.first.is_visible()
        except Exception:
            visible = False
        if "login" not in page.url.lower() or not visible:
            break
        page.wait_for_timeout(250)
    page.screenshot(path="after_login.png", full_page=True)

    still_login = page.locator(pass_sel)
    if "login" in page.url.lower() and still_login.count() and still_login.first.is_visible():
        raise RuntimeError("يبدو إن تسجيل الدخول فشل - راجع after_login.png واليوزر والباسورد.")

    print("تم تسجيل الدخول ✔")
    print("الصفحة الحالية:", page.url)


# ───────────────────────────── تطبيع النصوص والمطابقة ─────────────────────────────

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


_ENCLOSED = re.compile("[\u2460-\u2473\u24eb-\u24ff\u2776-\u2793\u3251-\u325f\u32b1-\u32bf]")


def _enclosed_to_num(m):
    n = unicodedata.numeric(m.group(), None)
    return f" {int(n)} " if n is not None else m.group()


def norm(s):
    """تطبيع عربي: تشكيل/تطويل/همزات/ياء/تاء مربوطة/أرقام/ترقيم. بيتطبق على الطرفين."""
    s = _ENCLOSED.sub(_enclosed_to_num, s or "")  # ①③⑤ -> " 1 3 5 " (كل رقم لوحده)
    s = unicodedata.normalize("NFKC", s).translate(_AR_DIGITS)
    s = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", s)
    s = re.sub("[أإآٱ]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = re.sub(r"(?<=\d)[٬,](?=\d{3}(?!\d))", "", s)  # 3,000,000 -> 3000000
    s = re.sub(r"[^\w\s]|_", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def digits_of(s):
    return re.findall(r"\d+", s)


def coverage(needle, hay):
    """نسبة حروف needle (قائمة كلمات) اللي موجودة بالترتيب في hay، ببلوكات من كلمتين+ أو كلمة طويلة."""
    total = sum(len(t) for t in needle)
    if not total:
        return 0.0
    sm = SequenceMatcher(None, needle, hay, autojunk=False)
    got = 0
    for b in sm.get_matching_blocks():
        if not b.size:
            continue
        chars = sum(len(t) for t in needle[b.a:b.a + b.size])
        if b.size >= 2 or chars >= 5:
            got += chars
    return got / total


def load_db():
    if not ANSWERS_JSON.exists():
        raise SystemExit(f"مش لاقي {ANSWERS_JSON} - شغّل build_answers.py الأول.")
    raw = json.loads(ANSWERS_JSON.read_text(encoding="utf-8"))
    db = []
    for r in raw:
        if not r.get("answer"):
            continue
        q, a = norm(r["question"]), norm(r["answer"])
        db.append({
            "level": r["level"], "kind": r["kind"], "qnum": r["qnum"],
            "question": r["question"], "answer": r["answer"],
            "q": q, "a": a, "q_tok": q.split(), "a_tok": a.split(), "q_set": set(q.split()),
        })
    return db


def score_entries(db, page_text):
    """بيرجع [(score, idx)] مرتبة تنازليًا. score = 0.65*تغطية السؤال + 0.35*تغطية الإجابة."""
    hay = norm(page_text).split()
    hay_set = set(hay)
    pre = sorted(
        range(len(db)),
        key=lambda i: -(len(db[i]["q_set"] & hay_set) / max(1, len(db[i]["q_set"]))),
    )[:40]
    out = []
    for i in pre:
        e = db[i]
        if sum(len(t) for t in e["a_tok"]) < 5:  # إجابة قصيرة (③ / ①③⑤): كفاية إن أرقامها موجودة، والخيار بيتأكد بالظبط بعدين
            a_cov = 1.0 if set(e["a_tok"]) <= hay_set else 0.0
        else:
            a_cov = coverage(e["a_tok"], hay)
        out.append((0.65 * coverage(e["q_tok"], hay) + 0.35 * a_cov, i))
    out.sort(reverse=True)
    return out


def decide_question(db, scored, cur_level):
    """
    بيرجع (idx, score, note) لو السؤال متطابق بثقة، None لو الصفحة مش سؤال.
    بيرمي StopSolving لو شكله سؤال بس المطابقة مش مؤكدة.
    """
    if not scored or scored[0][0] < NOT_A_QUESTION:
        return None

    def in_scope(i):
        return cur_level is None or db[i]["level"] in (cur_level, cur_level + 1)

    best_s, best_i = scored[0]
    scoped = [(s, i) for s, i in scored if in_scope(i)]
    note = ""

    if cur_level is not None and scoped and scoped[0][0] >= ACCEPT_IN_SCOPE and scoped[0][0] >= best_s - 0.05:
        best_s, best_i = scoped[0]
        pool = scoped
        need = ACCEPT_IN_SCOPE
    elif cur_level is None:
        pool, need = scored, ACCEPT_UNKNOWN
    else:
        pool, need = scored, ACCEPT_OUT_OF_SCOPE
        note = "(من مستوى مختلف)"

    if best_s < need:
        top = "\n".join(
            f"   {s:.3f}  L{db[i]['level']} {db[i]['kind']} Q{db[i]['qnum']}: {db[i]['question'][:70]}"
            for s, i in scored[:3]
        )
        raise StopSolving(f"الصفحة شبه سؤال بس المطابقة مش مؤكدة (best={best_s:.3f}, لازم {need}).\n{top}")

    for s, i in pool[1:]:
        if s > best_s - AMBIGUITY_MARGIN and db[i]["a"] != db[best_i]["a"]:
            raise StopSolving(
                f"سؤالين متقاربين بإجابات مختلفة ({best_s:.3f} vs {s:.3f}): "
                f"L{db[best_i]['level']} {db[best_i]['kind']} Q{db[best_i]['qnum']} / "
                f"L{db[i]['level']} {db[i]['kind']} Q{db[i]['qnum']}"
            )
    return best_i, best_s, note


def option_score(ans, text):
    """تشابه بين الإجابة (مطبّعة) ونص خيار على الشاشة (مطبّع)."""
    if not text:
        return 0.0
    if text == ans:
        return 1.0
    r = SequenceMatcher(None, ans, text, autojunk=False).ratio()
    if ans in text and len(text) - len(ans) <= 8:  # ترقيم/حرف قبل الخيار (A. / 1.)
        r = max(r, 0.97)
    ad, td = digits_of(ans), digits_of(text)
    if ad and ad != td and not (text.split()[:1] and text.split()[0].isdigit() and td[1:] == ad):
        r *= 0.6  # الأرقام/الدوائر لازم تتطابق بالظبط (①③⑤ مش ①③④⑤)
    return r


# ───────────────────────────── أدوات الصفحة (JS) ─────────────────────────────

JS_CANDIDATES = """() => {
  const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
  document.querySelectorAll('[data-ai-idx]').forEach(e => e.removeAttribute('data-ai-idx'));
  const sel = 'label, button, li, a, p, span, div, td, h1, h2, h3, h4, [role=radio], [role=option], [role=checkbox], [role=button], [role=listitem]';
  const out = []; let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    if (!vis(e)) continue;
    const t = (e.innerText || '').trim();
    if (!t || t.length > 700) continue;
    let inner = false;
    for (const c of e.children) { if ((c.innerText || '').trim() === t && vis(c)) { inner = true; break; } }
    if (inner) continue;
    e.setAttribute('data-ai-idx', n);
    out.push({i: n, t});
    n++;
  }
  return out;
}"""

JS_BUTTONS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  document.querySelectorAll('[data-ai-btn]').forEach(e => e.removeAttribute('data-ai-btn'));
  const out = []; let n = 0;
  for (const e of document.querySelectorAll('button, [role=button], a, input[type=button], input[type=submit]')) {
    if (!vis(e)) continue;
    const t = ((e.innerText || e.value || e.getAttribute('aria-label') || '') + '').trim();
    if (!t || t.length > 80) continue;
    const dis = !!e.disabled || e.getAttribute('aria-disabled') === 'true' || /\\bdisabled\\b/i.test(e.className || '');
    e.setAttribute('data-ai-btn', n);
    out.push({i: n, t, dis});
    n++;
  }
  return out;
}"""

JS_SELECTED = """(i) => {
  const e = document.querySelector('[data-ai-idx="' + i + '"]'); if (!e) return null;
  const lab = e.closest('label');
  const chain = [e, e.parentElement, e.parentElement && e.parentElement.parentElement].filter(Boolean);
  for (const n of chain) {
    if (n.matches('[aria-checked=true],[aria-selected=true],[aria-pressed=true]')) return true;
    if (/(^|[\\s_-])(selected|checked|active|chosen)([\\s_-]|$)/i.test(n.className || '')) return true;
  }
  for (const n of [e, lab]) {
    if (!n) continue;
    if (n.querySelector && n.querySelector('input:checked')) return true;
    if (n.control && n.control.checked) return true;
  }
  return false;
}"""

JS_TICK_ALL = """() => {
  let n = 0;
  for (const c of document.querySelectorAll('input[type=checkbox]')) {
    if (!c.checked && !c.disabled) { c.click(); n++; }
  }
  for (const c of document.querySelectorAll('[role=checkbox][aria-checked=false]')) { c.click(); n++; }
  return n;
}"""


JS_TEXT = """() => {
  if (!document.body) return '';
  const out = [], w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const t = n.nodeValue.trim(), p = n.parentElement;
    if (!t || !p || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(p.tagName) || !p.getClientRects().length) continue;
    const cs = getComputedStyle(p);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    out.push(t);
  }
  return out.join('\\n');
}"""


def page_text(page):
    """نص الصفحة الظاهر، كل text node في سطر (عشان العناصر inline ما تلزقش في بعض)."""
    return page.evaluate(JS_TEXT) or ""


def settle(page, ms=None):
    """Briefly yield to the SPA without waiting for perpetual background traffic."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=1000)
    except PWTimeout:
        pass
    page.wait_for_timeout(ms if ms is not None else STEP_DELAY_MS)


# أنماط الأزرار (على النص المطبّع، مطابقة كاملة)
BTN = {
    "answer": r"answer|check answer|check|submit|submit answer|verify|تحقق|تحقق من الاجابه|تاكيد|تاكيد الاجابه|ارسال|اجابه|اجب|الاجابه",
    "reviewed": r"راجعت.*|i have reviewed.*|i reviewed.*|reviewed.*",
    "complete": r"complete|completed|complete lesson|finish|finish lesson|mark as complete|اكمال|اكمل|اكمال الدرس|انهاء|انهاء الدرس|اتمام",
    "next": r"next|next question|next lesson|continue|continue learning|resume|resume learning|keep learning|"
            r"التالي|السؤال التالي|الدرس التالي|المستوي التالي|متابعه|متابعه التعلم|استمرار|استمر|كمل|كمل التعلم|كمل الدرس|واصل|مواصله",
    "start": r"start|start learning|begin|get started|ابدا|ابدا التعلم|ابدا الان|بدء|ابدا الدرس",
    "ok": r"ok|okay|got it|close|حسنا|تمام|موافق|فهمت|اغلاق|اغلق",
}
BTN_RE = {k: re.compile(rf"^(?:{v})$") for k, v in BTN.items()}


def click_button(page, cats, wait_s=6.0):
    """بيضغط أول زر ظاهر ومفعّل يطابق الفئات بالترتيب. بيستنى لو الزر موجود بس disabled. بيرجع (فئة, نص) أو None."""
    deadline = time.time() + wait_s
    while True:
        btns = page.evaluate(JS_BUTTONS)
        seen_disabled = False
        for cat in cats:
            for b in btns:
                if BTN_RE[cat].match(norm(b["t"])):
                    if b["dis"]:
                        seen_disabled = True
                        continue
                    page.locator(f'[data-ai-btn="{b["i"]}"]').first.click(timeout=5000)
                    return cat, b["t"]
        if not seen_disabled or time.time() >= deadline:
            return None
        page.wait_for_timeout(400)


# ───────────────────────────── حل السؤال ─────────────────────────────

def locate_option(page, entry):
    """بيدوّر على عنصر الخيار اللي نصه أقرب إجابة في القاعدة. بيرجع (idx, score) أو يرمي StopSolving."""
    cands = page.evaluate(JS_CANDIDATES)
    scored = [(option_score(entry["a"], norm(c["t"])), norm(c["t"]), c["i"]) for c in cands]
    scored = [x for x in scored if x[1]]
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < OPTION_ACCEPT:
        raise StopSolving(
            "ما لقيتش خيار مطابق للإجابة في الصفحة (أقرب تشابه = "
            f"{scored[0][0]:.2f} '{scored[0][1][:60]}').\nالإجابة: {entry['answer']}" if scored
            else f"مفيش عناصر نصية ظاهرة.\nالإجابة: {entry['answer']}"
        )
    best = scored[0]
    for s, t, _i in scored[1:]:
        if t != best[1] and s > best[0] - OPTION_MARGIN:
            raise StopSolving(
                f"خيارين متقاربين على الشاشة ({best[0]:.2f}: '{best[1][:50]}' / {s:.2f}: '{t[:50]}') - مش هخمّن.\n"
                f"الإجابة: {entry['answer']}"
            )
    return best[2], best[0]


WRONG_RE = re.compile(
    r"(اجابه (خاطيه|خاطئه|غير صحيحه|خطا)|غير صحيح|خطا|incorrect|wrong|try again|حاول مره)"
)


def feedback_failed(before, after):
    old = {norm(l) for l in before.splitlines() if l.strip()}
    new = [norm(l) for l in after.splitlines() if l.strip() and norm(l) not in old]
    for l in new:
        if WRONG_RE.search(l):
            return l
    return None


def answer_question(page, entry):
    """بيجاوب سؤال متطابق: اختيار من متعدد (أو حقل نصي)، بعدين Answer، بعدين يتحقق من النتيجة."""
    before = page_text(page)
    try:
        idx, sc = locate_option(page, entry)
        page.locator(f'[data-ai-idx="{idx}"]').first.click(timeout=5000)
        sel = page.evaluate(JS_SELECTED, idx)
        if sel is False:
            print("   ⚠ ما قدرتش أتأكد إن الخيار اتحدد (ممكن واجهة مختلفة) - هكمل.")
    except StopSolving:
        field = page.locator("textarea, input[type=text]:visible, input:not([type]):visible")
        if field.count() and field.first.is_visible():
            field.first.fill(entry["answer"])  # الإجابة حرفيًا من القاعدة
        else:
            raise

    page.wait_for_timeout(250)
    if DRY_RUN:
        page.screenshot(path="dry_run.png", full_page=True)
        raise StopSolving("DRY_RUN: اتحدد الخيار ولسه ما ضغطتش Answer. راجع dry_run.png.")

    clicked = click_button(page, ["answer"], wait_s=3.0)
    settle(page)
    bad = feedback_failed(before, page_text(page))
    if bad:
        raise StopSolving(f"ظهرت رسالة بعد الإجابة شكلها رفض: «{bad[:100]}»")
    return clicked


# ───────────────────────────── حلقة التشغيل ─────────────────────────────

class Progress:
    def __init__(self, level):
        self.level = level
        self.last_idx = None
        self.times = {}
        self.sig_hist = []
        self.done = {}


def signature(page, text):
    return page.url + "|" + norm(text)[:1500]


def navigate(page, st):
    """صفحة مش سؤال (أو سؤال اتجاوب): علّم النقاط المهمة لو فيه، وبعدين اضغط أنسب زر."""
    ticked = page.evaluate(JS_TICK_ALL)
    if ticked:
        print(f"   ✓ علّمت {ticked} نقطة")
        page.wait_for_timeout(300)
    res = click_button(page, ["reviewed", "complete", "next", "start", "ok"])
    if not res:
        raise StopSolving("مش لاقي زر أضغط عليه (Next/Complete/راجعت/ابدأ...). شغّل INSPECT=true وابعتلي الـdump.")
    print(f"   → {res[1]}")
    return res


def looks_like_question_page(page):
    """radio موجود أو زر Answer ظاهر (checkboxes لوحدها ممكن تكون صفحة النقاط المهمة)."""
    if page.locator("input[type=radio]:visible").count():
        return True
    return any(BTN_RE["answer"].match(norm(b["t"])) for b in page.evaluate(JS_BUTTONS))


def solve(page, db, st):
    for _ in range(MAX_STEPS):
        settle(page)
        text = page_text(page)
        found = decide_question(db, score_entries(db, text), st.level)

        if found is None and st.last_idx is None and looks_like_question_page(page):
            best = score_entries(db, text)[0]
            raise StopSolving(
                f"شكلها صفحة سؤال بس مفيش مطابقة في القاعدة (أقرب سؤال {best[0]:.2f}: "
                f"L{db[best[1]]['level']} Q{db[best[1]]['qnum']}). جاوبه يدويًا وكمّل."
            )

        if found is None or (st.last_idx is not None and found[0] == st.last_idx):
            sig = signature(page, text)
            st.sig_hist.append(sig)
            if len(st.sig_hist) >= 4 and len(set(st.sig_hist[-4:])) == 1:
                raise StopSolving("الصفحة ما بتتغيرش بعد الضغط 4 مرات - لازم تتراجع يدويًا.")
            navigate(page, st)
            st.last_idx = None
            continue

        idx, sc, note = found
        e = db[idx]
        if e["level"] > MAX_LEVEL:
            print(f"وصلت للمستوى {e['level']} (> MAX_LEVEL={MAX_LEVEL}) - وقفت هنا.")
            return
        st.times[idx] = st.times.get(idx, 0) + 1
        if st.times[idx] > 2:
            raise StopSolving(f"السؤال اتجاوب {st.times[idx]-1} مرات والصفحة ما اتحركتش: L{e['level']} Q{e['qnum']}")
        st.level = e["level"]
        print(f"[L{e['level']} {e['kind']} Q{e['qnum']}] {sc:.2f} {note} → {e['answer'][:70]}")
        answer_question(page, e)
        st.last_idx = idx
        st.sig_hist.clear()
        st.done[e["level"]] = st.done.get(e["level"], 0) + 1
    raise StopSolving(f"وصلت لحد MAX_STEPS={MAX_STEPS}.")


# ───────────────────────────── معايرة / تشخيص ─────────────────────────────

def dump_page(page, tag):
    ts = time.strftime("%H%M%S")
    base = f"{tag}_{ts}"
    page.screenshot(path=f"{base}.png", full_page=True)
    Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
    btns = page.evaluate(JS_BUTTONS)
    cands = page.evaluate(JS_CANDIDATES)
    inputs = page.evaluate(
        "() => [...document.querySelectorAll('input,textarea,select')].map(e => "
        "({tag: e.tagName, type: e.type, name: e.name, id: e.id, checked: e.checked, vis: !!(e.offsetWidth||e.offsetHeight)}))"
    )
    lines = [f"URL: {page.url}", f"TITLE: {page.title()}", "", "--- BUTTONS ---"]
    lines += [f"[{b['i']}] dis={b['dis']} {b['t']!r}" for b in btns]
    lines += ["", "--- INPUTS ---"] + [json.dumps(x, ensure_ascii=False) for x in inputs]
    lines += ["", "--- LEAF TEXT CANDIDATES ---"] + [f"[{c['i']}] {c['t'][:120]!r}" for c in cands[:80]]
    lines += ["", "--- BODY TEXT ---", page_text(page)[:4000]]
    Path(f"{base}.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"dump: {base}.png / .html / .txt")


def run_solver(page):
    db = load_db()
    print(f"قاعدة الإجابات: {len(db)} سؤال.")
    st = Progress(START_LEVEL)
    while True:
        try:
            solve(page, db, st)
            break
        except StopSolving as e:
            print("\n⛔ وقفت عند:", e)
            dump_page(page, "stop")
            if HEADLESS:
                sys.exit(2)
            ans = input("اتصرف يدويًا في المتصفح ثم Enter للمتابعة، أو q للخروج: ").strip().lower()
            if ans == "q":
                break
            st.last_idx = None
            st.sig_hist.clear()
            st.times.clear()
    print("ملخص الأسئلة المجاوبة لكل مستوى:", dict(sorted(st.done.items())))


def do_next_steps(page):
    """بعد اللوجين يفتح صفحة المادة ويبدأ الحل (أو dump لو INSPECT)."""
    print("[FINANCE] جاري فتح مادة الثقافة المالية…", flush=True)
    page.goto(TARGET_URL, wait_until="domcontentloaded")
    settle(page, 1200)
    page.screenshot(path="subject_page.png", full_page=True)
    print("فتحت الصفحة:", page.url)
    if INSPECT:
        dump_page(page, "inspect")
        return
    run_solver(page)


def main():
    with sync_playwright() as p:
        launch = {"headless": HEADLESS}
        if CHROME_PATH:
            launch["executable_path"] = CHROME_PATH
        elif BROWSER_CHANNEL:
            launch["channel"] = BROWSER_CHANNEL
        browser = p.chromium.launch(**launch)
        page = browser.new_context().new_page()
        page.set_default_timeout(UI_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        try:
            logged_in = login(page)
            if logged_in is not False:
                do_next_steps(page)
            if not HEADLESS:
                input("اضغط Enter لإغلاق المتصفح...")
        except PWTimeout:
            page.screenshot(path="timeout.png")
            print("انتهى الوقت وأنا بدوّر على عنصر في الصفحة - شوف timeout.png.")
        except Exception as e:
            print("خطأ:", e)
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
