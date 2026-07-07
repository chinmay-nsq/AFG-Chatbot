import os
import re
import json
import logging
import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from openai import OpenAI
import cartesia
from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aberdeen-chatbot")

BASE_URL = "https://www.abdn.ac.uk/qatar/"
OPENAI_MODEL = "gpt-4o"

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY")

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
cartesia_client = cartesia.Cartesia(api_key=CARTESIA_API_KEY)

CARTESIA_VOICE_ID = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
CARTESIA_MODEL_ID = "sonic-3.5"

app = Flask(__name__)
page_cache: dict = {}

# ── PostgreSQL connection ──────────────────────────────────────────────────────

DB_CONFIG = {
    "host":     os.getenv("PG_HOST", "localhost"),
    "port":     int(os.getenv("PG_PORT", 5432)),
    "dbname":   os.getenv("PG_DB",   "afg_school"),
    "user":     os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}


def get_db():
    return psycopg2.connect(**DB_CONFIG)


def run_query(sql: str) -> dict:
    """Execute a read-only SQL query and return rows as list of dicts."""
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return {"ok": True, "rows": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e), "rows": []}


def init_db():
    """Create and seed demo tables if they don't exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS students (
        roll_number   SERIAL PRIMARY KEY,
        name          TEXT NOT NULL,
        programme     TEXT NOT NULL,
        year          INT  NOT NULL,
        email         TEXT,
        phone         TEXT
    );

    CREATE TABLE IF NOT EXISTS attendance (
        id            SERIAL PRIMARY KEY,
        roll_number   INT  REFERENCES students(roll_number),
        subject       TEXT NOT NULL,
        total_classes INT  NOT NULL,
        attended      INT  NOT NULL,
        last_updated  DATE DEFAULT CURRENT_DATE
    );

    CREATE TABLE IF NOT EXISTS exam_schedule (
        id          SERIAL PRIMARY KEY,
        programme   TEXT NOT NULL,
        subject     TEXT NOT NULL,
        exam_date   DATE NOT NULL,
        start_time  TIME NOT NULL,
        end_time    TIME NOT NULL,
        venue       TEXT NOT NULL,
        exam_type   TEXT NOT NULL   -- 'Midterm' | 'Final' | 'Quiz'
    );

    CREATE TABLE IF NOT EXISTS results (
        id          SERIAL PRIMARY KEY,
        roll_number INT  REFERENCES students(roll_number),
        subject     TEXT NOT NULL,
        exam_type   TEXT NOT NULL,
        marks       NUMERIC(5,2),
        max_marks   NUMERIC(5,2),
        grade       TEXT
    );

    CREATE TABLE IF NOT EXISTS documents (
        id            SERIAL PRIMARY KEY,
        roll_number   INT  REFERENCES students(roll_number),
        doc_type      TEXT NOT NULL,
        status        TEXT NOT NULL,  -- 'Submitted' | 'Pending' | 'Verified'
        submitted_on  DATE,
        remarks       TEXT
    );
    """

    seed = """
    INSERT INTO students (name, programme, year, email, phone) VALUES
      ('Ahmed Al-Rashid',      'MA (Hons) Business Management',         2, 'ahmed@student.afg.qa',   '+974-5511-0001'),
      ('Sara Al-Kuwari',       'BSc (Hons) Computing Science',          1, 'sara@student.afg.qa',    '+974-5511-0002'),
      ('Mohammed Al-Thani',    'LLB International and Comparative Law', 3, 'mohd@student.afg.qa',    '+974-5511-0003'),
      ('Fatima Al-Dosari',     'MA (Hons) Accountancy and Finance',     2, 'fatima@student.afg.qa',  '+974-5511-0004'),
      ('Khalid Al-Mannai',     'MSc Artificial Intelligence',           1, 'khalid@student.afg.qa',  '+974-5511-0005'),
      ('Aisha Al-Naimi',       'MBA',                                   1, 'aisha@student.afg.qa',   '+974-5511-0006'),
      ('Omar Al-Sulaiti',      'BSc (Hons) Computing Science',          2, 'omar@student.afg.qa',    '+974-5511-0007'),
      ('Noora Al-Marri',       'MA (Hons) Business Management',         1, 'noora@student.afg.qa',   '+974-5511-0008'),
      ('Jassim Al-Hajri',      'MSc International Business Management', 1, 'jassim@student.afg.qa',  '+974-5511-0009'),
      ('Maryam Al-Emadi',      'LLM International Commercial Law',      1, 'maryam@student.afg.qa',  '+974-5511-0010')
    ON CONFLICT DO NOTHING;

    INSERT INTO attendance (roll_number, subject, total_classes, attended) VALUES
      (1,'Business Strategy',60,54),(1,'Marketing',60,58),(1,'Human Resource Management',60,45),
      (2,'Data Structures',60,60),(2,'Web Development',60,55),(2,'Algorithms',60,48),
      (3,'Contract Law',60,57),(3,'International Law',60,50),(3,'Legal Research',60,42),
      (4,'Financial Accounting',60,59),(4,'Taxation',60,53),(4,'Audit',60,47),
      (5,'Machine Learning',60,60),(5,'Deep Learning',60,58),(5,'NLP',60,55),
      (6,'Strategic Management',60,52),(6,'Finance for Managers',60,49),(6,'Leadership',60,60),
      (7,'Data Structures',60,40),(7,'Web Development',60,56),(7,'Algorithms',60,44),
      (8,'Business Strategy',60,58),(8,'Marketing',60,61),(8,'Economics',60,50),
      (9,'Global Business',60,54),(9,'Cross-Cultural Management',60,51),(9,'Strategy',60,48),
      (10,'Contract Law',60,60),(10,'Commercial Law',60,57),(10,'Legal Writing',60,52)
    ON CONFLICT DO NOTHING;

    INSERT INTO exam_schedule (programme, subject, exam_date, start_time, end_time, venue, exam_type) VALUES
      ('MA (Hons) Business Management','Business Strategy',    '2026-06-20','09:00','11:00','Hall A','Final'),
      ('MA (Hons) Business Management','Marketing',            '2026-06-22','09:00','11:00','Hall A','Final'),
      ('MA (Hons) Business Management','Human Resource Mgmt',  '2026-06-25','13:00','15:00','Hall B','Final'),
      ('BSc (Hons) Computing Science', 'Data Structures',      '2026-06-18','09:00','12:00','Lab 1', 'Final'),
      ('BSc (Hons) Computing Science', 'Web Development',      '2026-06-21','09:00','11:00','Lab 2', 'Final'),
      ('BSc (Hons) Computing Science', 'Algorithms',           '2026-06-24','13:00','16:00','Lab 1', 'Final'),
      ('LLB International and Comparative Law','Contract Law', '2026-06-19','09:00','12:00','Hall C','Final'),
      ('LLB International and Comparative Law','Intl Law',     '2026-06-23','09:00','12:00','Hall C','Final'),
      ('MSc Artificial Intelligence',  'Machine Learning',     '2026-06-17','09:00','12:00','Lab 3', 'Final'),
      ('MSc Artificial Intelligence',  'Deep Learning',        '2026-06-20','13:00','16:00','Lab 3', 'Final'),
      ('MBA',                           'Strategic Management', '2026-06-18','13:00','15:00','Hall B','Final'),
      ('MBA',                           'Finance for Managers', '2026-06-22','13:00','15:00','Hall B','Final'),
      ('MA (Hons) Business Management','Business Strategy',    '2026-06-10','09:00','10:00','Hall A','Midterm'),
      ('BSc (Hons) Computing Science', 'Data Structures',      '2026-06-11','09:00','10:00','Lab 1', 'Quiz'),
      ('MSc Artificial Intelligence',  'NLP',                  '2026-06-12','11:00','12:00','Lab 3', 'Quiz')
    ON CONFLICT DO NOTHING;

    INSERT INTO results (roll_number, subject, exam_type, marks, max_marks, grade) VALUES
      (1,'Business Strategy','Midterm',72,100,'B+'),(1,'Marketing','Midterm',85,100,'A'),
      (2,'Data Structures','Quiz',18,20,'A+'),(2,'Web Development','Midterm',78,100,'B+'),
      (3,'Contract Law','Midterm',68,100,'B'),(3,'International Law','Midterm',74,100,'B+'),
      (4,'Financial Accounting','Midterm',91,100,'A+'),(4,'Taxation','Midterm',80,100,'A'),
      (5,'Machine Learning','Midterm',95,100,'A+'),(5,'Deep Learning','Midterm',88,100,'A'),
      (6,'Strategic Management','Midterm',76,100,'B+'),(7,'Data Structures','Quiz',12,20,'C+'),
      (8,'Marketing','Midterm',89,100,'A'),(9,'Global Business','Midterm',71,100,'B'),
      (10,'Contract Law','Midterm',82,100,'A')
    ON CONFLICT DO NOTHING;

    INSERT INTO documents (roll_number, doc_type, status, submitted_on, remarks) VALUES
      (1,'Passport Copy','Verified','2025-09-01',NULL),
      (1,'Thanawiya Certificate','Verified','2025-09-01',NULL),
      (1,'IELTS Score Report','Verified','2025-09-01',NULL),
      (2,'Passport Copy','Verified','2025-09-03',NULL),
      (2,'IGCSE Transcripts','Verified','2025-09-03',NULL),
      (2,'IELTS Score Report','Pending',NULL,'Awaiting original'),
      (3,'Passport Copy','Verified','2025-09-02',NULL),
      (3,'Thanawiya Certificate','Submitted','2025-09-02','Under review'),
      (4,'Passport Copy','Verified','2025-09-01',NULL),
      (4,'Thanawiya Certificate','Verified','2025-09-01',NULL),
      (5,'Passport Copy','Verified','2025-09-05',NULL),
      (5,'Bachelor Degree','Verified','2025-09-05',NULL),
      (5,'IELTS Score Report','Verified','2025-09-05',NULL),
      (7,'IELTS Score Report','Pending',NULL,'Not yet submitted'),
      (9,'Bachelor Degree','Submitted','2025-09-10','Under review')
    ON CONFLICT DO NOTHING;
    """

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute(seed)
        conn.commit()
        conn.close()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"DB init failed: {e}")


# ── Web scraper ────────────────────────────────────────────────────────────────

def fetch_page_text(url: str) -> str:
    if url in page_cache:
        return page_cache[url]
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        from html.parser import HTMLParser

        class _TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
                self._skip = False
            def handle_starttag(self, tag, *_):
                if tag in ("script", "style", "nav", "footer"):
                    self._skip = True
            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "footer"):
                    self._skip = False
            def handle_data(self, data):
                if not self._skip:
                    text = data.strip()
                    if text:
                        self.parts.append(text)

        extractor = _TextExtractor()
        extractor.feed(resp.text)
        text = "\n".join(extractor.parts)
        page_cache[url] = text
        return text
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return ""


def get_site_links() -> list[str]:
    if "_links" in page_cache:
        return page_cache["_links"]
    from html.parser import HTMLParser
    from urllib.parse import urljoin, urlparse

    class _LinkExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                for k, v in attrs:
                    if k == "href" and v:
                        self.links.append(v)

    try:
        resp = requests.get(BASE_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        extractor = _LinkExtractor()
        extractor.feed(resp.text)
        base = urlparse(BASE_URL)
        links = []
        seen = set()
        for href in extractor.links:
            full = urljoin(BASE_URL, href)
            p = urlparse(full)
            if p.netloc == base.netloc and p.path.startswith(base.path) and p.scheme in ("http", "https"):
                clean = p._replace(fragment="", query="").geturl()
                if clean not in seen:
                    seen.add(clean)
                    links.append(clean)
        page_cache["_links"] = links
        return links
    except Exception as e:
        logger.warning(f"Could not fetch site links: {e}")
        return []


# ── LLM tools ─────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Fetch live content from the AFG Aberdeen Qatar website. "
                "Use for questions about programmes, fees, admissions, policies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_site_pages",
            "description": "List all known pages on the AFG Aberdeen Qatar site. Call first if unsure which URL to fetch.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "Run a read-only SQL SELECT query against the school database. "
                "Use for: student info, attendance, exam schedules, results, documents. "
                "Tables: students(roll_number,name,programme,year,email,phone), "
                "attendance(roll_number,subject,total_classes,attended,last_updated), "
                "exam_schedule(programme,subject,exam_date,start_time,end_time,venue,exam_type), "
                "results(roll_number,subject,exam_type,marks,max_marks,grade), "
                "documents(roll_number,doc_type,status,submitted_on,remarks). "
                "Always compute attendance_pct as ROUND(attended*100.0/total_classes,1). "
                "Only SELECT statements allowed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A safe SELECT SQL query"}
                },
                "required": ["sql"],
            },
        },
    },
]

SYSTEM_PROMPT = f"""You are a smart student advisor AI agent for AFG College with the University of Aberdeen (Qatar campus).
Website: {BASE_URL}

You help parents, students, and staff with:
- Programmes, fees, admission, schedules (use fetch_page tool)
- Student attendance, exam schedules, results, documents (use query_database tool)

TOOL USAGE:
- For website info: use fetch_page. Use list_site_pages if unsure which URL.
- For student/academic data: ALWAYS use query_database. Never guess data.
- You may call multiple tools in sequence if needed.

FORMATTING RULES:
- Use markdown tables for any tabular data (attendance, results, schedule, fees).
- Use **bold** for key terms, names, important values.
- Use bullet lists for multiple items.
- For attendance, always show percentage. Flag if below 75% with ⚠️.
- Source links: after web-fetched info add: > 🔗 [Page Title](url)
- If data not found: "I don't have that detail — please contact **info@afg-aberdeen.edu.qa** or call **(+974) 44201000**."
- Be concise and warm. Do NOT write long paragraphs when a table works better.
- Reply in the same language as the user (English or Arabic).

SPEECH RULE — very important:
At the very END of your full response, add a line:
SPEAK: followed by 1-2 short sentences (max 30 words) summarising the key point for text-to-speech.
This spoken summary should feel natural and conversational, NOT read out tables or lists.
Example: SPEAK: Ahmed has 90% attendance in Business Strategy. He is doing well overall.

SUGGESTIONS RULE:
After SPEAK, add: SUGGESTIONS: three short follow-up questions separated by |
Example: SUGGESTIONS: Show exam schedule|Check documents status|View all results
"""

SUGGEST_RE = re.compile(r"SUGGESTIONS:\s*(.+)$", re.DOTALL | re.MULTILINE)
SPEAK_RE   = re.compile(r"SPEAK:\s*(.+?)(?=\nSUGGESTIONS:|SUGGESTIONS:|\Z)", re.DOTALL)


def extract_meta(text: str):
    """Strip SPEAK and SUGGESTIONS lines, return (clean_text, speak_text, chips)."""
    speak = ""
    chips = []

    sgm = SUGGEST_RE.search(text)
    if sgm:
        raw_chips = sgm.group(1)
        # Strip any trailing content after the pipe list
        raw_chips = raw_chips.split("\n")[0]
        chips = [s.strip() for s in raw_chips.split("|") if s.strip()][:3]

    sm = SPEAK_RE.search(text)
    if sm:
        speak = sm.group(1).strip()
        # Hard-strip SUGGESTIONS if it leaked in
        speak = re.split(r"SUGGESTIONS:", speak, maxsplit=1)[0].strip()
        # Strip pipe-separated content (suggestions without header)
        speak = speak.split("|")[0].strip()
        # Strip trailing newlines/whitespace
        speak = speak.strip()

    # Cut display text before either tag
    cut = len(text)
    if sm:
        cut = min(cut, sm.start())
    if sgm:
        cut = min(cut, sgm.start())
    # Also cut at bare "SPEAK:" if it appears mid-text
    bare = text.find("\nSPEAK:")
    if bare >= 0:
        cut = min(cut, bare)
    clean = text[:cut].strip()
    return clean, speak, chips


# ── Streaming chat ─────────────────────────────────────────────────────────────

def chat_stream(messages: list):
    llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    while True:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=llm_messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
            stream=True,
        )

        collected_tool_calls = {}
        collected_content = ""
        finish_reason = None

        for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            finish_reason = chunk.choices[0].finish_reason if chunk.choices else None

            if delta and delta.content:
                collected_content += delta.content
                yield f"data: {json.dumps({'type': 'delta', 'text': delta.content})}\n\n"

            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        collected_tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            collected_tool_calls[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            collected_tool_calls[idx]["arguments"] += tc.function.arguments

        if finish_reason == "tool_calls" and collected_tool_calls:
            tool_call_list = []
            for idx in sorted(collected_tool_calls.keys()):
                tc = collected_tool_calls[idx]
                tool_call_list.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                })

            llm_messages.append({
                "role": "assistant",
                "content": collected_content or None,
                "tool_calls": tool_call_list,
            })

            for tc in tool_call_list:
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])

                if name == "fetch_page":
                    url = args["url"]
                    label = f"Searching website: {url.replace('https://www.abdn.ac.uk/qatar/', '').strip('/') or 'homepage'}…"
                    yield f"data: {json.dumps({'type': 'tool_status', 'label': label})}\n\n"
                    result = fetch_page_text(url)
                    result = result[:15_000] if result else "No content found."

                elif name == "list_site_pages":
                    yield f"data: {json.dumps({'type': 'tool_status', 'label': 'Listing site pages…'})}\n\n"
                    links = get_site_links()
                    result = "\n".join(links) if links else "Could not retrieve page list."

                elif name == "query_database":
                    sql = args.get("sql", "").strip()
                    # Safety: only allow SELECT
                    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
                        result = "Error: only SELECT queries are allowed."
                    else:
                        label = "Querying student database…"
                        yield f"data: {json.dumps({'type': 'tool_status', 'label': label})}\n\n"
                        qr = run_query(sql)
                        if qr["ok"]:
                            result = json.dumps(qr["rows"], default=str) if qr["rows"] else "No records found."
                        else:
                            result = f"DB error: {qr['error']}"
                else:
                    result = "Unknown tool."

                llm_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
            continue

        # Final answer — extract speak + suggestions
        if collected_content:
            _, speak, chips = extract_meta(collected_content)
            if speak:
                yield f"data: {json.dumps({'type': 'speak', 'text': speak})}\n\n"
            if chips:
                yield f"data: {json.dumps({'type': 'suggestions', 'chips': chips})}\n\n"
        break

    yield "data: [DONE]\n\n"


# ── UI ─────────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AFG Aberdeen Qatar — Student Advisor</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<style>
  :root {
    --navy:          #00374e;
    --navy-mid:      #1b4c61;
    --navy-dark:     #082b40;
    --seafoam:       #75e0aa;
    --seafoam-light: #e0f8ec;
    --seafoam-mid:   #3f7767;
    --gold:          #fbcb3b;
    --white:         #ffffff;
    --bg:            #eff3f4;
    --border:        #cfd9de;
    --text:          #082b40;
    --muted:         #486f80;
    --danger:        #dc2626;
    --warn:          #f59e0b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', 'Segoe UI', sans-serif; background: linear-gradient(160deg, var(--navy-dark) 0%, var(--navy) 60%, var(--navy-mid) 100%); display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 12px; }
  .chat-container { width: 780px; max-width: 100%; height: calc(100vh - 24px); max-height: 820px; min-height: 560px; background: var(--white); border-radius: 20px; box-shadow: 0 24px 80px rgba(0,0,0,0.35); display: flex; flex-direction: column; overflow: hidden; }

  /* ── Header ── */
  .header { background: var(--navy); color: var(--white); display: flex; flex-direction: column; flex-shrink: 0; }
  .header-top { padding: 14px 18px; display: flex; align-items: center; gap: 12px; }
  .header-bar { height: 4px; background: linear-gradient(90deg, var(--seafoam) 0%, var(--gold) 100%); }
  .avatar { width: 46px; height: 46px; border-radius: 50%; background: rgba(117,224,170,0.15); border: 2px solid var(--seafoam); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .avatar-face { font-size: 22px; }
  .wave { display: none; align-items: flex-end; gap: 2px; height: 20px; }
  .wave.active { display: flex; }
  .wave span { display: block; width: 3px; background: var(--seafoam); border-radius: 2px; animation: wave-anim 0.55s ease-in-out infinite alternate; }
  .wave span:nth-child(1){height:5px;animation-delay:0s}
  .wave span:nth-child(2){height:11px;animation-delay:.1s}
  .wave span:nth-child(3){height:20px;animation-delay:.2s}
  .wave span:nth-child(4){height:11px;animation-delay:.3s}
  .wave span:nth-child(5){height:5px;animation-delay:.4s}
  @keyframes wave-anim { from{transform:scaleY(0.3)} to{transform:scaleY(1)} }
  .header-text h2 { font-size: 15px; font-weight: 700; }
  .header-text .uni { font-size: 10px; opacity: 0.6; text-transform: uppercase; letter-spacing: .06em; margin-top: 1px; }
  .status { display: flex; align-items: center; gap: 5px; margin-top: 4px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--seafoam); flex-shrink: 0; }
  .dot.busy { background: var(--gold); animation: blink .8s infinite; }
  #status-text { font-size: 11px; opacity: .75; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.25} }

  /* ── Messages ── */
  .messages { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 12px; background: var(--bg); }
  .msg-row { display: flex; align-items: flex-end; gap: 8px; }
  .msg-row.user { flex-direction: row-reverse; }
  .msg-av { width: 30px; height: 30px; border-radius: 50%; background: var(--navy); color: var(--white); display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; border: 2px solid var(--border); }
  .msg-av.u { background: var(--navy-mid); border-color: var(--navy-mid); }
  .msg { max-width: 92%; padding: 11px 14px; border-radius: 16px; font-size: 13.5px; line-height: 1.65; word-break: break-word; }
  .msg.user { max-width: 75%; }
  .msg.bot { background: var(--white); color: var(--text); border-bottom-left-radius: 4px; border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(0,55,78,.07); }
  .msg.user { background: var(--navy); color: var(--white); border-bottom-right-radius: 4px; }

  /* Markdown inside bot */
  .msg.bot p { margin: 0 0 6px; }
  .msg.bot p:last-child { margin-bottom: 0; }
  .msg.bot ul,.msg.bot ol { padding-left: 20px; margin: 6px 0; }
  .msg.bot li { margin: 3px 0; }
  .msg.bot strong { font-weight: 600; color: var(--navy-dark); }
  .msg.bot th strong, .msg.bot th em, .msg.bot th * { color: #ffffff !important; }
  .msg.bot em { color: var(--muted); }
  .msg.bot h2,.msg.bot h3 { font-size: 13.5px; font-weight: 700; color: var(--navy); margin: 10px 0 5px; padding-bottom: 4px; border-bottom: 2px solid var(--seafoam-light); }
  .msg.bot h2:first-child,.msg.bot h3:first-child { margin-top: 0; }
  /* Tables — horizontal scroll so they never overflow the bubble */
  .msg.bot .tbl-wrap { overflow-x: auto; margin: 8px 0; border-radius: 8px; border: 1px solid var(--border); }
  .msg.bot .tbl-wrap table { border-collapse: collapse; width: 100%; font-size: 12px; min-width: 360px; }
  .msg.bot .tbl-wrap thead { background: var(--navy); color: #ffffff !important;}
  .msg.bot .tbl-wrap thead tr th,
  .msg.bot .tbl-wrap table thead th,
  .msg.bot .tbl-wrap th { background: var(--navy) !important; color: #ffffff !important; padding: 8px 12px; text-align: left; font-weight: 600; font-size: 11.5px; white-space: nowrap; border: none; color: #ffffff !important; }
  .msg.bot .tbl-wrap td { border-top: 1px solid var(--border); padding: 7px 12px; white-space: nowrap; color: var(--text); }
  .msg.bot .tbl-wrap tbody tr:nth-child(even) td { background: var(--seafoam-light); }
  .msg.bot .tbl-wrap tbody tr:hover td { background: #d7f0e8; }
  /* Source cards */
  .msg.bot blockquote { margin: 10px 0 4px; padding: 7px 12px; border-left: 3px solid var(--seafoam); background: var(--seafoam-light); border-radius: 0 8px 8px 0; font-size: 12.5px; }
  .msg.bot blockquote p { margin: 0; }
  .msg.bot blockquote a { color: var(--navy); font-weight: 600; text-decoration: none; }
  .msg.bot blockquote a:hover { color: var(--seafoam-mid); text-decoration: underline; }
  .msg.bot a { color: var(--navy-mid); font-weight: 500; text-decoration: underline; text-underline-offset: 2px; }
  .msg.bot code { background: #f0f4f8; padding: 1px 5px; border-radius: 4px; font-size: 12px; font-family: monospace; }

  /* Typing dots */
  .typing-dots { display: flex; gap: 5px; align-items: center; padding: 4px 0; }
  .typing-dots span { width: 7px; height: 7px; border-radius: 50%; background: var(--navy-mid); opacity: .35; animation: dot-bounce 1.2s infinite; }
  .typing-dots span:nth-child(2){animation-delay:.2s}
  .typing-dots span:nth-child(3){animation-delay:.4s}
  @keyframes dot-bounce { 0%,80%,100%{transform:translateY(0);opacity:.35} 40%{transform:translateY(-6px);opacity:1} }

  /* Tool status pill */
  .tool-status { font-size: 11.5px; color: var(--muted); background: var(--white); border: 1px solid var(--border); border-radius: 20px; padding: 5px 12px; display: flex; align-items: center; gap: 7px; align-self: flex-start; margin-left: 38px; }
  .spinner { width: 11px; height: 11px; border: 2px solid var(--border); border-top-color: var(--seafoam-mid); border-radius: 50%; animation: spin .75s linear infinite; flex-shrink: 0; }
  @keyframes spin { to{transform:rotate(360deg)} }

  /* Chips */
  .chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 6px 14px 10px; background: var(--bg); border-top: 1px solid var(--border); }
  .chip { background: var(--white); border: 1.5px solid var(--seafoam-mid); color: var(--navy); font-size: 12px; font-weight: 500; padding: 5px 13px; border-radius: 20px; cursor: pointer; transition: all .15s; white-space: nowrap; }
  .chip:hover { background: var(--navy); color: var(--white); border-color: var(--navy); }

  /* Input row */
  .input-row { padding: 10px 14px; border-top: 1px solid var(--border); display: flex; gap: 7px; align-items: center; background: var(--white); }
  .input-row input { flex: 1; border: 1.5px solid var(--border); border-radius: 24px; padding: 9px 16px; font-size: 13.5px; outline: none; font-family: inherit; background: var(--bg); color: var(--text); transition: border-color .2s, background .2s; }
  .input-row input:focus { border-color: var(--seafoam-mid); background: var(--white); }
  .input-row input::placeholder { color: var(--muted); }
  .input-row button { background: var(--navy); color: var(--white); border: none; border-radius: 50%; width: 38px; height: 38px; cursor: pointer; font-size: 15px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background .2s, transform .1s; }
  .input-row button:hover:not(:disabled) { filter: brightness(1.15); transform: scale(1.06); }
  .input-row button:disabled { opacity: .4; cursor: default; transform: none; }
  #send { background: var(--seafoam-mid); }
  #mic.recording { background: var(--danger); animation: pulse 1s infinite; }
  #stop-btn { background: var(--danger); display: none; }
  #stop-btn.visible { display: flex; }
  @keyframes pulse { 0%,100%{box-shadow:0 0 0 0 rgba(220,38,38,.45)} 50%{box-shadow:0 0 0 8px rgba(220,38,38,0)} }
  .messages::-webkit-scrollbar { width: 4px; }
  .messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
</style>
</head>
<body>
<div class="chat-container">
  <div class="header">
    <div class="header-top">
      <div class="avatar" id="avatar">
        <span class="avatar-face" id="avatar-face">🎓</span>
        <div class="wave" id="avatar-wave"><span></span><span></span><span></span><span></span><span></span></div>
      </div>
      <div class="header-text">
        <h2>AFG Aberdeen Qatar</h2>
        <div class="uni">University of Aberdeen · Qatar Campus</div>
        <div class="status">
          <span class="dot" id="status-dot"></span>
          <span id="status-text">Student Advisor — Online</span>
        </div>
      </div>
    </div>
    <div class="header-bar"></div>
  </div>

  <div class="messages" id="messages">
    <div class="msg-row bot">
      <div class="msg-av">🎓</div>
      <div class="msg bot">Hello! I'm your AI student advisor for <strong>AFG College with the University of Aberdeen</strong>, Qatar Campus.<br><br>
      I can help with:<br>
      • Programmes, fees &amp; admissions<br>
      • Student attendance &amp; records<br>
      • Exam schedules &amp; results<br>
      • Document status &amp; portal access<br><br>
      <em>مرحباً! كيف يمكنني مساعدتك؟</em></div>
    </div>
  </div>

  <div class="chips" id="chips">
    <span class="chip">What programmes are offered?</span>
    <span class="chip">Roll number 1 attendance</span>
    <span class="chip">Exam schedule for MBA</span>
    <span class="chip">How do I apply?</span>
  </div>

  <div class="input-row">
    <button id="mic" title="Click to speak">🎤</button>
    <input id="input" type="text" placeholder="Ask anything… e.g. 'show attendance for roll 3'" autocomplete="off"/>
    <button id="stop-btn" title="Stop speaking">⏹</button>
    <button id="send">&#9658;</button>
  </div>
</div>

<script>
  // ── Init ─────────────────────────────────────────────────────────────────────
  function initMarked() {
    if (typeof marked === "undefined") return;
    const _r = new marked.Renderer();
    // Links open in new tab
    _r.link = function(href, title, text) {
      if (typeof href === "object" && href !== null) { title = href.title; text = href.text; href = href.href; }
      const t = title ? ` title="${title}"` : "";
      return `<a href="${href}"${t} target="_blank" rel="noopener">${text}</a>`;
    };
    // Wrap tables in scroll container — handle both marked v4 and v9+ signatures
    const _origTable = _r.table.bind(_r);
    _r.table = function(headerOrToken, body) {
      // v9+: single object token; v4: (header_html, body_html) strings
      let inner;
      if (typeof headerOrToken === "object" && headerOrToken !== null) {
        inner = _origTable(headerOrToken);
      } else {
        inner = `<table><thead>${headerOrToken}</thead><tbody>${body || ""}</tbody></table>`;
      }
      // Wrap in scroll div regardless
      if (!inner.startsWith('<div class="tbl-wrap">')) {
        inner = `<div class="tbl-wrap">${inner}</div>`;
      }
      // Force white text on every th — beats any CSS specificity issue
      inner = inner.replace(/<th(\b[^>]*)>/g, '<th$1 style="color:#ffffff !important ; background:#00374e!important;">');
      return inner;
    };
    marked.setOptions({ renderer: _r, breaks: true, gfm: true });
  }
  initMarked();

  function safeParse(text) {
    return marked.parse(text);
  }

  const messages = [];
  let mediaRecorder = null, dgSocket = null, micStream = null;
  let isRecording = false, isFlushing = false, finalTranscript = "";
  let vadCtx = null, vadAnalyser = null, vadTimer = null, speechDetected = false;
  const VAD_SILENCE_MS = 1500, VAD_SPEECH_THRESH = 10;

  // ── Status helpers ───────────────────────────────────────────────────────────
  function setStatus(state, label) {
    const dot     = document.getElementById("status-dot");
    const txt     = document.getElementById("status-text");
    const face    = document.getElementById("avatar-face");
    const wave    = document.getElementById("avatar-wave");
    const stopBtn = document.getElementById("stop-btn");
    const mic     = document.getElementById("mic");
    if (state === "speaking") {
      face.style.display = "none"; wave.classList.add("active");
      dot.className = "dot busy"; txt.textContent = "Speaking…";
      stopBtn.classList.add("visible");
      mic.disabled = true; mic.style.opacity = "0.4";
    } else if (state === "thinking") {
      face.style.display = ""; wave.classList.remove("active");
      dot.className = "dot busy"; txt.textContent = label || "Thinking…";
      stopBtn.classList.remove("visible");
      mic.disabled = true; mic.style.opacity = "0.4";
    } else {
      face.style.display = ""; wave.classList.remove("active");
      dot.className = "dot"; txt.textContent = "Student Advisor — Online";
      stopBtn.classList.remove("visible");
      mic.disabled = false; mic.style.opacity = "";
    }
  }

  // ── Chips ────────────────────────────────────────────────────────────────────
  function chipClick(el) {
    stopSpeaking();
    document.getElementById("input").value = el.textContent;
    sendMessage();
  }
  function showChips(list) {
    const c = document.getElementById("chips");
    c.innerHTML = "";
    list.forEach(s => {
      const el = document.createElement("span");
      el.className = "chip";
      el.textContent = s;
      el.addEventListener("click", () => chipClick(el));
      c.appendChild(el);
    });
  }

  // ── Wire up all DOM events after page loads ──────────────────────────────────
  document.addEventListener("DOMContentLoaded", function() {
    initMarked();
    document.getElementById("input").addEventListener("keydown", e => {
      if (e.key === "Enter") sendMessage();
    });
    document.getElementById("send").addEventListener("click", sendMessage);
    document.getElementById("mic").addEventListener("click", toggleMic);
    document.getElementById("stop-btn").addEventListener("click", stopSpeaking);
    // Wire initial chips
    document.querySelectorAll("#chips .chip").forEach(el => {
      el.addEventListener("click", () => chipClick(el));
    });
  });

  // ── VAD ──────────────────────────────────────────────────────────────────────
  function startVAD(stream) {
    vadCtx = new AudioContext();
    const source = vadCtx.createMediaStreamSource(stream);
    vadAnalyser = vadCtx.createAnalyser();
    vadAnalyser.fftSize = 512;
    source.connect(vadAnalyser);
    speechDetected = false;
    const buf = new Uint8Array(vadAnalyser.frequencyBinCount);
    function tick() {
      if (!isRecording) return;
      vadAnalyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) { const v = buf[i]-128; sum += v*v; }
      const rms = Math.sqrt(sum / buf.length);
      if (rms > VAD_SPEECH_THRESH) {
        speechDetected = true; clearTimeout(vadTimer); vadTimer = null;
        document.getElementById("mic").title = "🔴 Listening…";
      } else if (speechDetected && !vadTimer) {
        document.getElementById("mic").title = "⏳ Done?";
        vadTimer = setTimeout(() => { if (isRecording) flushAndSend(); }, VAD_SILENCE_MS);
      }
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }
  function stopVAD() {
    clearTimeout(vadTimer); vadTimer = null;
    if (vadCtx) { try { vadCtx.close(); } catch {} vadCtx = null; }
    vadAnalyser = null; speechDetected = false;
  }

  // ── Mic flush ────────────────────────────────────────────────────────────────
  function flushAndSend() {
    if (!isRecording || isFlushing) return;
    isFlushing = true; isRecording = false;
    stopVAD();
    document.getElementById("mic").title = "Click to speak";
    document.getElementById("mic").classList.remove("recording");
    if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
    if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
    if (dgSocket && dgSocket.readyState === WebSocket.OPEN) dgSocket.send(JSON.stringify({ type: "Finalize" }));
    const fallback = setTimeout(() => commitSend(), 2000);
    if (dgSocket) {
      dgSocket.onmessage = e => {
        const msg = JSON.parse(e.data);
        const alt = msg?.channel?.alternatives?.[0];
        if (alt && msg.is_final) {
          const text = (alt.transcript||"").trim();
          if (text) finalTranscript += (finalTranscript?" ":"") + text;
          document.getElementById("input").value = finalTranscript;
        }
        if (msg.type==="Finalized"||(msg.is_final&&msg.speech_final)) { clearTimeout(fallback); commitSend(); }
      };
    }
  }
  function commitSend() {
    isFlushing = false;
    if (dgSocket) { try { dgSocket.send(JSON.stringify({type:"CloseStream"})); dgSocket.close(); } catch {} dgSocket = null; }
    mediaRecorder = null;
    const text = document.getElementById("input").value.trim();
    if (text) sendMessage();
  }

  // ── Mic toggle ───────────────────────────────────────────────────────────────
  async function toggleMic() {
    if (isRecording || isFlushing) { commitSend(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { alert("Microphone access denied."); return; }
    micStream = stream;
    const { key } = await (await fetch("/deepgram-key")).json();
    const dgUrl = "wss://api.deepgram.com/v1/listen?model=nova-2&punctuate=true&interim_results=true&language=en-US&endpointing=500";
    dgSocket = new WebSocket(dgUrl, ["token", key]);
    dgSocket.onopen = () => {
      finalTranscript = ""; isFlushing = false;
      document.getElementById("input").value = "";
      isRecording = true;
      document.getElementById("mic").classList.add("recording");
      document.getElementById("mic").title = "Speak now…";
      mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
      mediaRecorder.ondataavailable = e => { if (dgSocket&&dgSocket.readyState===WebSocket.OPEN&&e.data.size>0) dgSocket.send(e.data); };
      mediaRecorder.start(100);
      startVAD(stream);
    };
    dgSocket.onmessage = e => {
      if (isFlushing) return;
      const msg = JSON.parse(e.data);
      const alt = msg?.channel?.alternatives?.[0];
      if (!alt) return;
      const text = (alt.transcript||"").trim();
      if (!text) return;
      if (msg.is_final) {
        finalTranscript += (finalTranscript?" ":"") + text;
        document.getElementById("input").value = finalTranscript;
      } else {
        document.getElementById("input").value = finalTranscript + (finalTranscript?" ":"") + text;
      }
    };
    dgSocket.onerror = err => console.error("DG WS error:", err);
    dgSocket.onclose = () => { if (isRecording) flushAndSend(); };
  }

  // ── Stop speaking ────────────────────────────────────────────────────────────
  function stopSpeaking() {
    if (ttsAbortController) ttsAbortController.abort();
    if (audioCtx) { try { audioCtx.close(); } catch {} audioCtx = null; }
    setStatus("idle");
  }

  // ── Send & stream ────────────────────────────────────────────────────────────
  async function sendMessage() {
    const input = document.getElementById("input");
    const text = input.value.trim();
    if (!text) return;

    // Stop any ongoing speech immediately when user sends a new message
    stopSpeaking();

    input.value = "";
    document.getElementById("send").disabled = true;
    document.getElementById("chips").innerHTML = "";

    appendUserMsg(text);
    messages.push({ role: "user", content: text });

    const typingRow = appendTyping();
    setStatus("thinking", "Thinking…");

    let fullReply = "";
    let botDiv = null;
    let toolStatusEl = null;
    let speakText = "";

    try {
      const res = await fetch("/chat-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages })
      });
      if (!res.ok) throw new Error("stream error");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\\n");
        buf = lines.pop();

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6).trim();
          if (!raw || raw === "[DONE]") continue;
          let ev;
          try { ev = JSON.parse(raw); } catch { continue; }

          if (ev.type === "tool_status") {
            setStatus("thinking", ev.label);
            if (!toolStatusEl) {
              toolStatusEl = document.createElement("div");
              toolStatusEl.className = "tool-status";
              document.getElementById("messages").appendChild(toolStatusEl);
            }
            toolStatusEl.innerHTML = `<div class="spinner"></div>${ev.label}`;
            scrollBottom(); continue;
          }

          if (ev.type === "delta") {
            if (toolStatusEl) { toolStatusEl.remove(); toolStatusEl = null; }
            if (!botDiv) {
              typingRow.remove();
              setStatus("thinking", "Responding…");
              botDiv = appendBotMsg("");
            }
            fullReply += ev.text;
            // Strip SPEAK/SUGGESTIONS from live display
            const displayText = fullReply
              .replace(/\\nSPEAK:[\s\S]*$/, "")
              .replace(/\\nSUGGESTIONS:[\s\S]*$/, "");
            botDiv.innerHTML = safeParse(displayText);
            scrollBottom(); continue;
          }

          if (ev.type === "speak") { speakText = ev.text; continue; }
          if (ev.type === "suggestions") { showChips(ev.chips); }
        }
      }
    } catch (err) {
      typingRow.remove();
      if (toolStatusEl) { toolStatusEl.remove(); toolStatusEl = null; }
      appendBotMsgText("Sorry, something went wrong. Please try again.");
    }

    if (fullReply) {
      // Strip meta tags from stored message
      const clean = fullReply.replace(/\\nSPEAK:[\s\S]*$/, "").replace(/\\nSUGGESTIONS:[\s\S]*$/, "").trim();
      messages.push({ role: "assistant", content: clean });
      // Speak only the concise summary, not the full response
      if (speakText) await speakReply(speakText);
      else setStatus("idle");
    } else {
      setStatus("idle");
    }

    document.getElementById("send").disabled = false;
    input.focus();
  }

  // ── TTS ──────────────────────────────────────────────────────────────────────
  let audioCtx = null, nextStartTime = 0, ttsAbortController = null;

  function getAudioCtx() {
    if (!audioCtx || audioCtx.state === "closed") {
      audioCtx = new AudioContext({ sampleRate: 22050 });
      nextStartTime = 0;
    }
    return audioCtx;
  }

  // speakReply returns a Promise that resolves ONLY when playback fully ends
  function speakReply(text) {
    return new Promise(async (resolve) => {
      if (audioCtx) { try { audioCtx.close(); } catch {} audioCtx = null; }
      ttsAbortController = new AbortController();
      const ctx = getAudioCtx();
      nextStartTime = ctx.currentTime;
      setStatus("speaking");

      const done_ = () => { setStatus("idle"); resolve(); };

      try {
        const res = await fetch("/tts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
          signal: ttsAbortController.signal
        });
        if (!res.ok) { done_(); return; }

        const reader = res.body.getReader();
        const BYTES_PER_SAMPLE = 4;
        let leftover = new Uint8Array(0);
        let lastSrc = null;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const combined = new Uint8Array(leftover.length + value.length);
          combined.set(leftover); combined.set(value, leftover.length);
          const usable = combined.length - (combined.length % BYTES_PER_SAMPLE);
          leftover = combined.slice(usable);
          if (usable === 0) continue;
          const numSamples = usable / BYTES_PER_SAMPLE;
          const floats = new Float32Array(combined.buffer, 0, numSamples);
          const audioBuf = ctx.createBuffer(1, numSamples, 22050);
          audioBuf.copyToChannel(floats, 0);
          const src = ctx.createBufferSource();
          src.buffer = audioBuf; src.connect(ctx.destination);
          const when = Math.max(nextStartTime, ctx.currentTime);
          src.start(when); nextStartTime = when + audioBuf.duration;
          lastSrc = src;
        }
        // Resolve when the last scheduled chunk finishes playing
        if (lastSrc) { lastSrc.onended = done_; }
        else { done_(); }
      } catch (err) {
        if (err.name !== "AbortError") console.warn("TTS failed:", err);
        done_();
      }
    });
  }

  // ── DOM helpers ──────────────────────────────────────────────────────────────
  function appendUserMsg(text) {
    const row = document.createElement("div");
    row.className = "msg-row user";
    row.innerHTML = `<div class="msg-av u">👤</div><div class="msg user"></div>`;
    row.querySelector(".msg").textContent = text;
    document.getElementById("messages").appendChild(row);
    scrollBottom();
  }
  function appendBotMsg(html) {
    const row = document.createElement("div");
    row.className = "msg-row bot";
    row.innerHTML = `<div class="msg-av">🎓</div><div class="msg bot"></div>`;
    const d = row.querySelector(".msg");
    if (html) d.innerHTML = html;
    document.getElementById("messages").appendChild(row);
    scrollBottom();
    return d;
  }
  function appendBotMsgText(text) {
    const d = appendBotMsg(""); d.textContent = text; return d;
  }
  function appendTyping() {
    const row = document.createElement("div");
    row.className = "msg-row bot";
    row.innerHTML = `<div class="msg-av">🎓</div><div class="msg bot"><div class="typing-dots"><span></span><span></span><span></span></div></div>`;
    document.getElementById("messages").appendChild(row);
    scrollBottom();
    return row;
  }
  function scrollBottom() {
    const c = document.getElementById("messages");
    c.scrollTop = c.scrollHeight;
  }
</script>
</body>
</html>"""


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/chat-stream", methods=["POST"])
def chat_stream_endpoint():
    data = request.get_json()
    user_messages = data.get("messages", [])
    if not user_messages:
        return jsonify({"error": "No messages provided"}), 400
    return Response(
        stream_with_context(chat_stream(user_messages)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/tts", methods=["POST"])
def tts_endpoint():
    data = request.get_json()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    def generate():
        stream = cartesia_client.tts.sse(
            model_id=CARTESIA_MODEL_ID,
            transcript=text,
            voice={"mode": "id", "id": CARTESIA_VOICE_ID},
            output_format={"container": "raw", "encoding": "pcm_f32le", "sample_rate": 22050},
        )
        for event in stream:
            if hasattr(event, "audio") and event.audio:
                yield event.audio

    return Response(generate(), mimetype="audio/pcm", headers={
        "X-Sample-Rate": "22050", "X-Channels": "1", "Cache-Control": "no-cache",
    })


@app.route("/stt", methods=["POST"])
def stt_endpoint():
    audio_data = request.data
    if not audio_data:
        return jsonify({"error": "No audio data"}), 400
    resp = requests.post(
        "https://api.deepgram.com/v1/listen?model=nova-3&punctuate=true&language=en",
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": request.content_type or "audio/webm",
        },
        data=audio_data, timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    transcript = (
        result.get("results", {}).get("channels", [{}])[0]
        .get("alternatives", [{}])[0].get("transcript", "").strip()
    )
    return jsonify({"transcript": transcript})


@app.route("/deepgram-key", methods=["GET"])
def deepgram_key():
    return jsonify({"key": DEEPGRAM_API_KEY})


# ── Run ────────────────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)