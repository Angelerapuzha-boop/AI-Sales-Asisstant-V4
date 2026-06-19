#!/usr/bin/env python3
"""
AI Sales Assistant — COMPLETE SINGLE FILE
Everything in one file. Nothing can be missing.
Render: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120 --preload
Local:  python app.py
Login:  admin@salesai.com / Admin@123456
"""
import os,json,csv,io,re,logging,threading,time,hashlib,base64,sqlite3,smtplib,tempfile
import urllib.request,urllib.error,urllib.parse
from contextlib import contextmanager
from datetime import datetime,timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from collections import defaultdict
import jwt
from flask import Flask,jsonify,Response,request,g,Blueprint

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger=logging.getLogger("sales")

# ══ CONFIG ═══════════════════════════════════════════════════════════════════
SECRET_KEY=os.environ.get("SECRET_KEY","ai-sales-secret-32chars-2024xyz!")
JWT_ALGORITHM="HS256"
TOKEN_EXPIRE_HOURS=48
GROQ_API_KEY=os.environ.get("GROQ_API_KEY","")
GROQ_MODEL=os.environ.get("GROQ_MODEL","llama-3.3-70b-versatile")
BLAND_API_KEY=os.environ.get("BLAND_API_KEY","")
GMAIL_EMAIL=os.environ.get("GMAIL_SENDER_EMAIL","")
GMAIL_PASSWORD=os.environ.get("GMAIL_APP_PASSWORD","")
GMAIL_NAME=os.environ.get("GMAIL_SENDER_NAME","AI Sales Team")
TWILIO_SID=os.environ.get("TWILIO_ACCOUNT_SID","")
TWILIO_TOKEN=os.environ.get("TWILIO_AUTH_TOKEN","")
TWILIO_FROM=os.environ.get("TWILIO_FROM_NUMBER","")
TWILIO_ADMIN=os.environ.get("TWILIO_ADMIN_NUMBER","")
GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID","")
GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET","")
GOOGLE_REDIRECT_URI=os.environ.get("GOOGLE_REDIRECT_URI","")
DAILY_REPORT_HOUR=int(os.environ.get("DAILY_REPORT_HOUR_UTC","18"))
AUTO_EMAIL_HOUR=int(os.environ.get("AUTO_EMAIL_HOUR_UTC","9"))
AUTO_FOLLOWUP_HOUR=int(os.environ.get("AUTO_FOLLOWUP_HOUR_UTC","10"))
AUTO_CALL_HOUR=int(os.environ.get("AUTO_CALL_HOUR_UTC","11"))
AUTO_SCORE_HOUR=int(os.environ.get("AUTO_SCORE_HOUR_UTC","8"))
DB_PATH=os.environ.get("DB_PATH",os.path.join(tempfile.gettempdir(),"sales.db"))

# ══ DATABASE ══════════════════════════════════════════════════════════════════
def _dbconn():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)),exist_ok=True)
    c=sqlite3.connect(DB_PATH,check_same_thread=False,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

@contextmanager
def get_conn():
    c=_dbconn()
    try: yield c; c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()

def q(sql,args=()):
    with get_conn() as c: return [dict(r) for r in c.execute(sql,args).fetchall()]
def q1(sql,args=()):
    with get_conn() as c:
        r=c.execute(sql,args).fetchone(); return dict(r) if r else None
def run(sql,args=()):
    with get_conn() as c: return c.execute(sql,args).lastrowid
def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,full_name TEXT NOT NULL,password TEXT NOT NULL,
            role TEXT DEFAULT 'sales_rep',is_active INTEGER DEFAULT 1,
            google_refresh_token TEXT,last_login TEXT,
            created_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,industry TEXT,employee_count INTEGER,annual_revenue INTEGER,
            website TEXT,city TEXT,country TEXT,description TEXT,technologies TEXT,
            status TEXT DEFAULT 'prospect',lead_score INTEGER DEFAULT 0,ai_summary TEXT,
            linkedin_url TEXT,funding_stage TEXT,created_by INTEGER,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            first_name TEXT NOT NULL,last_name TEXT,email TEXT,phone TEXT,title TEXT,
            department TEXT,seniority_level TEXT DEFAULT 'individual',
            is_decision_maker INTEGER DEFAULT 0,created_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS emails(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
            contact_id INTEGER,created_by INTEGER,email_type TEXT DEFAULT 'cold',
            subject TEXT NOT NULL,body TEXT NOT NULL,recipient_email TEXT NOT NULL,
            recipient_name TEXT,status TEXT DEFAULT 'draft',sent_at TEXT,ai_model_used TEXT,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
            contact_id INTEGER,created_by INTEGER,title TEXT NOT NULL,
            meeting_type TEXT DEFAULT 'discovery',description TEXT,scheduled_at TEXT,
            duration_minutes INTEGER DEFAULT 30,status TEXT DEFAULT 'proposed',
            meeting_link TEXT,google_event_id TEXT,notes TEXT,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY AUTOINCREMENT,
            bland_call_id TEXT,company_id INTEGER,contact_id INTEGER,created_by INTEGER,
            phone_number TEXT NOT NULL,objective TEXT DEFAULT 'qualify',task_prompt TEXT,
            voice TEXT DEFAULT 'nat',status TEXT DEFAULT 'queued',duration_seconds INTEGER,
            recording_url TEXT,transcript TEXT,summary TEXT,error_message TEXT,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS lead_scores(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
            total_score INTEGER DEFAULT 0,revenue_score INTEGER DEFAULT 0,
            employee_score INTEGER DEFAULT 0,industry_score INTEGER DEFAULT 0,
            buying_signal_score INTEGER DEFAULT 0,department_signal_score INTEGER DEFAULT 0,
            email_activity_score INTEGER DEFAULT 0,tier TEXT DEFAULT 'cold',
            updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS buying_signals(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            signal_type TEXT NOT NULL,signal_name TEXT NOT NULL,signal_description TEXT,
            strength INTEGER DEFAULT 5,source TEXT DEFAULT 'ai',
            detected_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS sms_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_number TEXT NOT NULL,from_number TEXT,body TEXT NOT NULL,
            status TEXT DEFAULT 'sent',event_type TEXT,error_msg TEXT,
            created_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS chat_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,sender_name TEXT,message TEXT NOT NULL,
            created_at TEXT DEFAULT(datetime('now')));
        """)
    _seed_db()

def _seed_db():
    if q1("SELECT 1 FROM users WHERE email='admin@salesai.com'"): return
    pw=hashlib.sha256(("Admin@123456"+SECRET_KEY).encode()).hexdigest()
    aid=run("INSERT INTO users(email,full_name,password,role) VALUES(?,?,?,?)",
            ("admin@salesai.com","System Admin",pw,"admin"))
    demos=[
        ("Stripe","FinTech",4000,7500000000,"stripe.com","San Francisco","USA",91,"opportunity",'["Python","Go"]'),
        ("Notion","SaaS",400,300000000,"notion.so","San Francisco","USA",78,"qualified",'["TypeScript"]'),
        ("Vercel","Technology",350,200000000,"vercel.com","San Francisco","USA",74,"prospect",'["Next.js"]'),
        ("Figma","Design",1000,400000000,"figma.com","San Francisco","USA",85,"qualified",'["C++"]'),
        ("Linear","Software",80,50000000,"linear.app","San Francisco","USA",62,"prospect",'["TypeScript"]'),
        ("Retool","SaaS",300,100000000,"retool.com","San Francisco","USA",55,"prospect",'["React"]'),
        ("PlanetScale","Database",150,60000000,"planetscale.com","San Mateo","USA",38,"cold",'["MySQL"]'),
        ("Loom","Technology",200,80000000,"loom.com","San Francisco","USA",44,"prospect",'["React"]'),
        ("Airtable","SaaS",800,350000000,"airtable.com","San Francisco","USA",82,"opportunity",'["React"]'),
        ("Miro","Software",1500,400000000,"miro.com","Amsterdam","Netherlands",77,"qualified",'["React"]'),
    ]
    phones=["+14155550100","+14155550101","+14155550102","+14155550103","+14155550104",
            "+14155550105","+14155550106","+14155550107","+14155550108","+31201234567"]
    for (nm,ind,emp,rev,web,city,country,sc,status,techs),phone in zip(demos,phones):
        cid=run("""INSERT INTO companies(name,industry,employee_count,annual_revenue,website,
                   city,country,lead_score,status,technologies,created_by,description)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (nm,ind,emp,rev,web,city,country,sc,status,techs,aid,f"Leading {ind} company"))
        run("""INSERT INTO contacts(company_id,first_name,last_name,email,phone,title,
               department,seniority_level,is_decision_maker) VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid,"Alex","Johnson",f"alex@{web}",phone,"VP Engineering","Engineering","vp",1))
        run("""INSERT INTO lead_scores(company_id,total_score,revenue_score,employee_score,
               industry_score,buying_signal_score,department_signal_score,email_activity_score,tier)
               VALUES(?,?,80,70,80,75,60,50,?)""",
            (cid,sc,"hot" if sc>=70 else "warm" if sc>=40 else "cold"))
        run("""INSERT INTO buying_signals(company_id,signal_type,signal_name,signal_description,strength,source)
               VALUES(?,?,?,?,?,?)""",(cid,"hiring","Rapid Hiring","20+ open roles",8,"demo"))
    cids=[r["id"] for r in q("SELECT id FROM companies LIMIT 8")]
    for i,(cid,st,et) in enumerate(zip(cids,
        ["sent","sent","opened","replied","draft","sent","opened","sent"],
        ["cold","follow_up","meeting_request","cold","cold","follow_up","meeting_request","cold"])):
        run("""INSERT INTO emails(company_id,created_by,email_type,subject,body,
               recipient_email,recipient_name,status,ai_model_used) VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid,aid,et,"Quick question about your growth",
             "Hi Alex,\n\nI noticed you are scaling fast.\n\nWorth a 15-min call?\n\nBest,\nAI Sales Team",
             f"alex@demo{i}.com","Alex Johnson",st,"groq"))
    for i,(t,mt) in enumerate([("Discovery Call","discovery"),("Product Demo","demo"),
                                ("Follow-up","follow_up"),("Negotiation","negotiation")]):
        sched=(datetime.utcnow()+timedelta(days=i+1)).strftime("%Y-%m-%d %H:%M:%S")
        run("""INSERT INTO meetings(company_id,created_by,title,meeting_type,
               scheduled_at,duration_minutes,status) VALUES(?,?,?,?,?,?,?)""",
            (cids[i%len(cids)],aid,t,mt,sched,30,"scheduled"))
    logger.info("✅ Demo data seeded")

# ══ AUTH ══════════════════════════════════════════════════════════════════════
def hash_pw(p): return hashlib.sha256((p+SECRET_KEY).encode()).hexdigest()
def verify_pw(p,h): return hash_pw(p)==h
def make_token(uid,email,role):
    return jwt.encode({"sub":str(uid),"email":email,"role":role,
        "exp":datetime.utcnow()+timedelta(hours=TOKEN_EXPIRE_HOURS)},
        SECRET_KEY,algorithm=JWT_ALGORITHM)
def decode_token(t): return jwt.decode(t,SECRET_KEY,algorithms=[JWT_ALGORITHM])
def login_required(f):
    @wraps(f)
    def dec(*a,**kw):
        tok=(request.headers.get("Authorization","").replace("Bearer ","")).strip()
        if not tok: return jsonify({"error":"Missing token"}),401
        try:
            p=decode_token(tok)
            g.user={"id":int(p["sub"]),"email":p["email"],"role":p["role"]}
        except jwt.ExpiredSignatureError: return jsonify({"error":"Token expired"}),401
        except Exception: return jsonify({"error":"Invalid token"}),401
        return f(*a,**kw)
    return dec

# ══ AI SERVICE ════════════════════════════════════════════════════════════════
def _groq(prompt,system=None,max_tokens=700):
    if not GROQ_API_KEY: return None
    msgs=[]
    if system: msgs.append({"role":"system","content":system})
    msgs.append({"role":"user","content":prompt})
    body=json.dumps({"model":GROQ_MODEL,"messages":msgs,"max_tokens":max_tokens}).encode()
    try:
        req=urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
            data=body,headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=25) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()
    except Exception as e: logger.warning(f"Groq:{e}"); return None

def company_summary(co):
    rev=co.get("annual_revenue") or 0
    emp=co.get("employee_count") or "N/A"
    p=f"2-sentence B2B sales summary for {co['name']}, {co.get('industry','tech')}, {emp} employees, ${rev:,} revenue. Be actionable."
    return (_groq(p,"B2B sales expert.",180) or
            f"{co['name']} is a {co.get('industry','technology')} company with {co.get('employee_count','N/A')} employees. Strong AI sales candidate.")

def generate_email(co,ct,email_type,custom=""):
    name=f"{ct.get('first_name','Team')} {ct.get('last_name','')}".strip()
    sender=GMAIL_NAME or "AI Sales Team"
    p={"cold":f"Cold B2B email to {name} at {co['name']}. {custom}\nSUBJECT: ...\nBODY:\n...",
       "follow_up":f"Follow-up to {name} at {co['name']} — no reply.\nSUBJECT: ...\nBODY:\n...",
       "meeting_request":f"Meeting request to {name} at {co['name']}.\nSUBJECT: ...\nBODY:\n..."}
    result=_groq(p.get(email_type,p["cold"]),"Expert B2B copywriter.",480)
    if result:
        subj,lines,in_b="", [],False
        for line in result.strip().split("\n"):
            if line.upper().startswith("SUBJECT:"): subj=line.split(":",1)[1].strip()
            elif line.upper().strip()=="BODY:": in_b=True
            elif in_b: lines.append(line)
        if subj and lines: return {"subject":subj,"body":"\n".join(lines).strip()}
    t={"cold":{"subject":f"Helping {co['name']} automate sales",
               "body":f"Hi {name},\n\nI noticed {co['name']} is scaling fast.\n\nWe help companies automate sales with AI. Worth a 15-min call?\n\nBest,\n{sender}"},
       "follow_up":{"subject":f"Following up — {co['name']}",
                    "body":f"Hi {name},\n\nResurfacing my note. Any availability this month?\n\nThanks,\n{sender}"},
       "meeting_request":{"subject":f"15 min — AI Sales demo for {co['name']}?",
                          "body":f"Hi {name},\n\nThree slots:\n• Tue 10 AM\n• Wed 2 PM\n• Thu 11 AM\n\nDoes any work?\n\nBest,\n{sender}"}}
    return t.get(email_type,t["cold"])

def buying_signals(co):
    rev=co.get("annual_revenue") or 0
    p=f"3 buying signals for {co.get('name','')}, {co.get('industry','')}, ${rev:,}. JSON: [{{\"type\":\"...\",\"name\":\"...\",\"description\":\"...\",\"strength\":7}}]"
    result=_groq(p,"Return valid JSON array only.",280)
    if result:
        try:
            m=re.search(r"\[.*\]",result,re.DOTALL)
            if m: return json.loads(m.group())
        except Exception: pass
    out=[]
    if (co.get("employee_count") or 0)>100: out.append({"type":"scale","name":"Enterprise Scale","description":"Budget authority","strength":7})
    if rev>1_000_000: out.append({"type":"revenue","name":"Strong Revenue","description":"Investment capacity","strength":8})
    out.append({"type":"tech","name":"Tech-Forward","description":"Active stack","strength":6})
    return out

def call_script(co,ct,objective="qualify"):
    name=f"{ct.get('first_name','there')}".strip()
    p=f"Phone script to {name} at {co.get('name','company')}. Objective:{objective}. Greeting, value prop, 2 questions, CTA. Under 200 words."
    return (_groq(p,"Expert B2B sales caller.",350) or
            f"Hi, may I speak with {name}? ... Calling from AI Sales — we help {co.get('industry','tech')} companies automate prospecting. How do you handle lead qualification? Worth a 15-min demo?")

def chat_reply(message,stats):
    cmd=message.lower().strip()
    cos=stats.get("companies",[])
    if any(w in cmd for w in ["show leads","top leads","hot leads"]):
        top=sorted(cos,key=lambda x:x.get("lead_score",0),reverse=True)[:5]
        return "🔥 Top Leads:\n\n"+"\n".join(
            f"{i}. {'🔥' if s>=70 else '🟡' if s>=40 else '❄️'} {c['name']} — {s}/100"
            for i,(c,s) in enumerate([(c,c.get("lead_score",0)) for c in top],1))
    if any(w in cmd for w in ["analytics","stats","kpi"]):
        hot=sum(1 for c in cos if c.get("lead_score",0)>=70)
        warm=sum(1 for c in cos if 40<=c.get("lead_score",0)<70)
        return f"📊 Analytics\n\n🏢 Companies: {len(cos)}\n🔥 Hot: {hot} | 🟡 Warm: {warm}\n📧 Emails: {stats.get('emails_sent',0)}\n💰 Pipeline: ${hot*50000+warm*15000:,}"
    if any(w in cmd for w in ["pipeline","revenue"]):
        hot=sum(1 for c in cos if c.get("lead_score",0)>=70)
        warm=sum(1 for c in cos if 40<=c.get("lead_score",0)<70)
        return f"💰 Pipeline\n\n🔥 {hot} hot × $50k = ${hot*50000:,}\n🟡 {warm} warm × $15k = ${warm*15000:,}\n📊 Total: ${hot*50000+warm*15000:,}"
    top5=sorted(cos,key=lambda x:x.get("lead_score",0),reverse=True)[:5]
    summary="\n".join([f"- {c['name']}: {c.get('lead_score',0)}" for c in top5])
    return (_groq(f"AI sales assistant. Answer in 2-3 sentences.\nData:{len(cos)} companies\n{summary}\nQ:{message}","Friendly AI sales assistant.",180)
            or "Try: `show leads`, `analytics`, `pipeline`, or `help`")

# ══ SERVICES ══════════════════════════════════════════════════════════════════
IND_SCORES={"technology":15,"software":15,"saas":15,"fintech":12,"design":10,
            "database":12,"healthcare":12,"financial":12,"cloud":14,"ai":15}

def score_company(co,contacts,signals):
    rev=co.get("annual_revenue") or 0
    rs=100 if rev>=100_000_000 else 85 if rev>=10_000_000 else 70 if rev>=1_000_000 else 40 if rev>=100_000 else 15
    emp=co.get("employee_count") or 0
    es=100 if emp>=1000 else 85 if emp>=500 else 70 if emp>=100 else 50 if emp>=20 else 20
    ind_=(co.get("industry") or "").lower()
    is_=next((v for k,v in IND_SCORES.items() if k in ind_),8)
    bss=min(100,sum(s.get("strength",5) for s in signals)*10//max(len(signals),1)) if signals else 0
    SM={"c_suite":30,"vp":25,"director":20,"manager":15,"individual":5}
    ds=min(100,sum(SM.get(c.get("seniority_level",""),5)+(20 if c.get("is_decision_maker") else 0) for c in contacts)) if contacts else 0
    total=max(0,min(100,int(rs*0.25+es*0.15+is_*0.20+bss*0.20+ds*0.10+50*0.10)))
    return {"total_score":total,"revenue_score":rs,"employee_score":es,"industry_score":is_,
            "buying_signal_score":bss,"department_signal_score":ds,"email_activity_score":50,
            "tier":"hot" if total>=70 else "warm" if total>=40 else "cold"}

def send_email_smtp(to_email,to_name,subject,body):
    """Send email via Gmail SMTP. Never raises — always returns a dict with status."""
    if not to_email or "@" not in to_email:
        return {"status":"error","message":f"Invalid recipient email: {to_email!r}"}
    if not (GMAIL_EMAIL and GMAIL_PASSWORD):
        return {"status":"not_configured","message":"Set GMAIL_SENDER_EMAIL + GMAIL_APP_PASSWORD env vars"}
    if not subject:
        subject = "(no subject)"
    if not body:
        body = ""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{GMAIL_NAME} <{GMAIL_EMAIL}>"
        msg["To"]      = f"{to_name} <{to_email}>" if to_name else to_email
        # attach plain-text AND html parts so it never renders as blank
        msg.attach(MIMEText(body, "plain", "utf-8"))
        html_body = body.replace("\n", "<br>").replace("\r", "")
        msg.attach(MIMEText(f"<html><body><p>{html_body}</p></body></html>", "html", "utf-8"))
        smtp = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMAIL_EMAIL, GMAIL_PASSWORD)
        smtp.sendmail(GMAIL_EMAIL, to_email, msg.as_string())
        smtp.quit()
        logger.info(f"📧 Email sent to {to_email}: {subject[:50]}")
        return {"status":"sent"}
    except smtplib.SMTPAuthenticationError as e:
        msg_ = "Gmail auth failed — create an App Password at myaccount.google.com/apppasswords (not your regular password)"
        logger.error(f"SMTP auth: {e}")
        return {"status":"error","message":msg_}
    except smtplib.SMTPRecipientsRefused as e:
        logger.error(f"SMTP refused: {e}")
        return {"status":"error","message":f"Recipient refused: {to_email}"}
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error: {e}")
        return {"status":"error","message":f"SMTP error: {e}"}
    except OSError as e:
        logger.error(f"SMTP connect: {e}")
        return {"status":"error","message":f"Cannot reach smtp.gmail.com — check network"}
    except Exception as e:
        logger.error(f"send_email unexpected: {type(e).__name__}: {e}")
        return {"status":"error","message":str(e)}

def test_gmail():
    if not (GMAIL_EMAIL and GMAIL_PASSWORD): return False,"Not configured"
    try:
        with smtplib.SMTP("smtp.gmail.com",587) as s: s.starttls(); s.login(GMAIL_EMAIL,GMAIL_PASSWORD)
        return True,f"✅ Connected as {GMAIL_EMAIL}"
    except smtplib.SMTPAuthenticationError: return False,"Auth failed — check App Password"
    except Exception as e: return False,str(e)

def bland_call_api(phone,task,voice="nat",company_name="",contact_name=""):
    if not BLAND_API_KEY: return {"status":"error","message":"BLAND_API_KEY not set"}
    phone=phone.replace(" ","").replace("-","")
    if not phone.startswith("+"): return {"status":"error","message":"Phone must start with +"}
    payload=json.dumps({"phone_number":phone,"task":task,"model":"enhanced","voice":voice,
        "max_duration":300,"record":True,"wait_for_greeting":True,
        "metadata":{"company":company_name,"contact":contact_name}}).encode()
    try:
        req=urllib.request.Request("https://api.bland.ai/v1/calls",data=payload,
            headers={"authorization":BLAND_API_KEY,"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=30) as r: data=json.loads(r.read()); return {"status":"queued","call_id":data.get("call_id")}
    except urllib.error.HTTPError as e:
        body=e.read().decode()[:200]
        if e.code in(401,403):
            return {"status":"error","message":"Bland API key invalid/expired (403). Get a new key at app.bland.ai → API Keys, then update BLAND_API_KEY in Render env vars."}
        return {"status":"error","message":f"Bland HTTP {e.code}: {body}"}
    except Exception as e: return {"status":"error","message":str(e)}

def bland_get_api(call_id):
    if not BLAND_API_KEY: return {}
    try:
        req=urllib.request.Request(f"https://api.bland.ai/v1/calls/{call_id}",headers={"authorization":BLAND_API_KEY})
        with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read())
    except Exception: return {}

def test_bland():
    if not BLAND_API_KEY: return False,"BLAND_API_KEY not set in environment variables"
    try:
        req=urllib.request.Request("https://api.bland.ai/v1/calls?limit=1",
            headers={"authorization":BLAND_API_KEY})
        with urllib.request.urlopen(req,timeout=10) as r: json.loads(r.read())
        return True,"✅ Bland AI connected"
    except urllib.error.HTTPError as e:
        if e.code in (401,403):
            return False,(
                "❌ Bland API key invalid or expired (HTTP 403). "
                "Go to app.bland.ai → API Keys → create a new key → "
                "update BLAND_API_KEY in Render environment variables → redeploy"
            )
        return False,f"Bland HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False,f"Cannot reach Bland AI: {e.reason}"
    except Exception as e:
        return False,str(e)

def google_auth_url(user_id=0):
    if not GOOGLE_CLIENT_ID: return ""
    from urllib.parse import quote as _q
    return (f"https://accounts.google.com/o/oauth2/auth?client_id={_q(GOOGLE_CLIENT_ID)}"
            f"&redirect_uri={_q(GOOGLE_REDIRECT_URI)}&response_type=code"
            f"&scope={_q('https://www.googleapis.com/auth/calendar')}"
            f"&access_type=offline&prompt=consent&state={user_id}")

def google_exchange(code):
    from urllib.parse import quote as _q
    if not GOOGLE_CLIENT_ID: return {}
    payload=(f"code={_q(code)}&client_id={_q(GOOGLE_CLIENT_ID)}&client_secret={_q(GOOGLE_CLIENT_SECRET)}"
             f"&redirect_uri={_q(GOOGLE_REDIRECT_URI)}&grant_type=authorization_code").encode()
    try:
        req=urllib.request.Request("https://oauth2.googleapis.com/token",data=payload,headers={"Content-Type":"application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read())
    except Exception: return {}

# ══ TWILIO SMS ════════════════════════════════════════════════════════════════
SMS_TMPLS={
    "company_added":"🏢 New Company\n{company_name}\nIndustry:{industry}\nScore:{lead_score}/100",
    "hot_lead":"🔥 Hot Lead!\n{company_name}\nScore:{lead_score}/100\nIndustry:{industry}",
    "email_generated":"📧 Email Ready\n{company_name}\nType:{email_type}\nTo:{recipient_email}",
    "email_sent":"✅ Email Sent\nTo:{recipient_email}\nSubj:{subject}",
    "meeting_scheduled":"📅 Meeting\n{title}\n{company_name}\nTime:{scheduled_at}",
    "meeting_completed":"✅ Meeting Done\n{title}\n{company_name}",
    "call_initiated":"📞 AI Call\n{company_name}\nPhone:{phone_number}\nObj:{objective}",
    "csv_import":"📤 Import Done\n{filename}\n✅{processed_rows} ❌{failed_rows}",
    "daily_report":"📊 Daily {report_date}\n🏢{total_companies} co | 🔥{hot_leads} hot | 🟡{warm_leads} warm\n📧{emails_sent} sent ({email_open_rate}% open)\n📞{total_calls} calls\n💰${revenue_pipeline:,}\n{top_companies}",
    "meeting_reminder_24h":"📅 24h Reminder\n{title}\n{company_name}\n{scheduled_at}",
    "meeting_reminder_1h":"⏰ 1h Reminder\n{title}\n{company_name}",
    "meeting_reminder_10min":"⏰ 10min!\n{title}\n🚀 Starting soon!",
}
class _SD(dict):
    def __missing__(self,k): return ""

def _sms_send(to,body):
    if not all([TWILIO_SID,TWILIO_TOKEN,TWILIO_FROM,to]): return {"status":"not_configured"}
    from urllib.parse import quote as _q
    payload=f"To={_q(to)}&From={_q(TWILIO_FROM)}&Body={_q(body[:1600])}"
    creds=base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    try:
        req=urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            data=payload.encode(),headers={"Authorization":f"Basic {creds}","Content-Type":"application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req,timeout=15) as r: data=json.loads(r.read()); return {"status":"sent","sid":data.get("sid")}
    except Exception as e: logger.error(f"Twilio:{e}"); return {"status":"error","message":str(e)}

def _sms_log(event_type,body,result):
    try: run("INSERT INTO sms_logs(to_number,from_number,body,status,event_type) VALUES(?,?,?,?,?)",(TWILIO_ADMIN or "admin",TWILIO_FROM,body[:500],result.get("status","unknown"),event_type))
    except Exception: pass

def notify(event_type,data):
    tmpl=SMS_TMPLS.get(event_type)
    if not tmpl: return
    try:
        body=tmpl.format_map(_SD(data))
        result=_sms_send(TWILIO_ADMIN or "",body)
        _sms_log(event_type,body,result)
    except Exception as e: logger.error(f"notify {event_type}:{e}")

def notify_async(event_type,data): threading.Thread(target=notify,args=(event_type,data),daemon=True).start()
def send_sms(to,body): return _sms_send(to,body)
def sms_test(to): return _sms_send(to,f"✅ AI Sales Connected! {datetime.utcnow().strftime('%d %b %H:%M')} UTC")

def schedule_reminders(mid,title,company_name,scheduled_at):
    if not scheduled_at: return
    try: mdt=datetime.strptime(scheduled_at[:19],"%Y-%m-%d %H:%M:%S")
    except Exception: return
    def _fire(delta,etype):
        delay=(mdt-delta-datetime.utcnow()).total_seconds()
        if delay<=0: return
        def _r(): time.sleep(delay); notify(etype,{"title":title,"company_name":company_name,"scheduled_at":mdt.strftime("%d %b %Y %H:%M UTC")})
        threading.Thread(target=_r,daemon=True).start()
    _fire(timedelta(hours=24),"meeting_reminder_24h")
    _fire(timedelta(hours=1),"meeting_reminder_1h")
    _fire(timedelta(minutes=10),"meeting_reminder_10min")

# ══ AUTOMATION ════════════════════════════════════════════════════════════════
def _admin_id():
    u=q1("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"); return u["id"] if u else 1

def auto_score_all():
    try:
        cos=q("SELECT * FROM companies")
        for co in cos:
            cts=q("SELECT * FROM contacts WHERE company_id=?",(co["id"],))
            sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(co["id"],))
            sc=score_company(co,cts,sigs); old=co.get("lead_score",0) or 0
            if q1("SELECT id FROM lead_scores WHERE company_id=?",(co["id"],)):
                run("""UPDATE lead_scores SET total_score=?,revenue_score=?,employee_score=?,industry_score=?,
                       buying_signal_score=?,department_signal_score=?,email_activity_score=?,tier=?,updated_at=?
                       WHERE company_id=?""",
                    (sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],
                     sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"],now(),co["id"]))
            else:
                run("""INSERT INTO lead_scores(company_id,total_score,revenue_score,employee_score,industry_score,
                       buying_signal_score,department_signal_score,email_activity_score,tier) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (co["id"],sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],
                     sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"]))
            run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",(sc["total_score"],now(),co["id"]))
            if sc["total_score"]>=80 and old<80:
                notify_async("hot_lead",{"company_name":co["name"],"lead_score":sc["total_score"],"industry":co.get("industry","N/A")})
        logger.info(f"✅ Auto-scored {len(cos)} companies"); return {"scored":len(cos)}
    except Exception as e: logger.error(f"auto_score:{e}"); return {"error":str(e)}

def auto_email_hot_leads():
    if not (GMAIL_EMAIL and GMAIL_PASSWORD):
        logger.info("auto_email: skipped (gmail not configured)")
        return {"skipped":"gmail_not_configured"}
    try:
        cos=q("""SELECT c.* FROM companies c WHERE c.lead_score>=70
                 AND c.id NOT IN(SELECT DISTINCT company_id FROM emails WHERE status IN('sent','opened','replied') AND company_id IS NOT NULL)
                 ORDER BY c.lead_score DESC LIMIT 10""")
        sent=0; skipped=0; errors=0
        for co in cos:
            try:
                ct=(q1("SELECT * FROM contacts WHERE company_id=? AND is_decision_maker=1",(co["id"],))
                    or q1("SELECT * FROM contacts WHERE company_id=?",(co["id"],)))
                email_addr = (ct.get("email","") if ct else "").strip()
                if not ct or not email_addr or "@" not in email_addr:
                    logger.info(f"auto_email: skipping {co['name']} — no valid email")
                    skipped+=1; continue
                content=generate_email(co,ct,"cold")
                ct_name=f"{ct.get('first_name','')} {ct.get('last_name','')}".strip()
                # save draft first so we always have a record
                eid=run("""INSERT INTO emails(company_id,contact_id,created_by,email_type,subject,body,recipient_email,recipient_name,status,ai_model_used,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (co["id"],ct["id"],_admin_id(),"cold",content["subject"],content["body"],
                         email_addr,ct_name,"draft","groq",now()))
                r=send_email_smtp(email_addr,ct_name,content["subject"],content["body"])
                if r.get("status")=="sent":
                    run("UPDATE emails SET status=\'sent\',sent_at=?,updated_at=? WHERE id=?",(now(),now(),eid))
                    notify_async("email_sent",{"recipient_email":email_addr,"subject":content["subject"]})
                    sent+=1
                else:
                    run("UPDATE emails SET status=\'error\',updated_at=? WHERE id=?",(now(),eid))
                    logger.warning(f"auto_email {co['name']}: {r.get('message','send failed')}")
                    errors+=1
            except Exception as e_inner:
                logger.error(f"auto_email {co.get('name','?')}: {e_inner}")
                errors+=1
        logger.info(f"✅ Auto-email done: sent={sent} skipped={skipped} errors={errors}")
        return {"sent":sent,"skipped":skipped,"errors":errors}
    except Exception as e:
        logger.error(f"auto_email_hot_leads: {e}")
        return {"error":str(e)}

def auto_followup():
    if not (GMAIL_EMAIL and GMAIL_PASSWORD):
        return {"skipped":"gmail_not_configured"}
    try:
        cutoff=(datetime.utcnow()-timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        pending=q("""SELECT e.* FROM emails e WHERE e.email_type='cold' AND e.status IN('sent','opened') AND e.sent_at<?
                    AND e.company_id NOT IN(SELECT DISTINCT company_id FROM emails WHERE email_type='follow_up' AND company_id IS NOT NULL)
                    ORDER BY e.sent_at ASC LIMIT 5""",(cutoff,))
        sent=0; errors=0
        for em in pending:
            try:
                co=q1("SELECT * FROM companies WHERE id=?",(em["company_id"],))
                ct=q1("SELECT * FROM contacts WHERE id=?",(em.get("contact_id"),)) if em.get("contact_id") else None
                email_addr=(ct.get("email","") if ct else "").strip()
                if not co or not ct or not email_addr or "@" not in email_addr: continue
                content=generate_email(co,ct,"follow_up")
                ct_name=f"{ct.get('first_name','')} {ct.get('last_name','')}".strip()
                eid=run("""INSERT INTO emails(company_id,contact_id,created_by,email_type,subject,body,recipient_email,recipient_name,status,ai_model_used,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (co["id"],ct["id"],_admin_id(),"follow_up",content["subject"],content["body"],
                         email_addr,ct_name,"draft","groq",now()))
                r=send_email_smtp(email_addr,ct_name,content["subject"],content["body"])
                if r.get("status")=="sent":
                    run("UPDATE emails SET status='sent',sent_at=?,updated_at=? WHERE id=?",(now(),now(),eid))
                    sent+=1
                else:
                    run("UPDATE emails SET status='error',updated_at=? WHERE id=?",(now(),eid))
                    logger.warning(f"followup {co['name']}: {r.get('message','failed')}")
                    errors+=1
            except Exception as e_inner:
                logger.error(f"auto_followup inner: {e_inner}")
                errors+=1
        logger.info(f"✅ Follow-up done: sent={sent} errors={errors}")
        return {"sent":sent,"errors":errors}
    except Exception as e:
        logger.error(f"auto_followup: {e}")
        return {"error":str(e)}

def auto_call_hot_leads():
    if not BLAND_API_KEY: return {"skipped":"bland_not_configured"}
    try:
        cos=q("""SELECT c.* FROM companies c WHERE c.lead_score>=80
                 AND c.id NOT IN(SELECT DISTINCT company_id FROM calls WHERE company_id IS NOT NULL)
                 ORDER BY c.lead_score DESC LIMIT 3""")
        called=0
        for co in cos:
            ct=q1("SELECT * FROM contacts WHERE company_id=? AND phone IS NOT NULL AND phone!='' ORDER BY is_decision_maker DESC LIMIT 1",(co["id"],))
            if not ct or not ct.get("phone"): continue
            script=call_script(co,ct,"qualify")
            r=bland_call_api(ct["phone"],script,"nat",co["name"],f"{ct.get('first_name','')} {ct.get('last_name','')}".strip())
            run("""INSERT INTO calls(bland_call_id,company_id,contact_id,created_by,phone_number,objective,task_prompt,voice,status,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (r.get("call_id"),co["id"],ct["id"],_admin_id(),ct["phone"],"qualify",script,"nat",
                 "queued" if r.get("status")=="queued" else "error",now()))
            if r.get("status")=="queued":
                notify_async("call_initiated",{"company_name":co["name"],"phone_number":ct["phone"],"objective":"Qualify"}); called+=1
        return {"called":called}
    except Exception as e: return {"error":str(e)}

def sync_call_statuses():
    if not BLAND_API_KEY: return
    try:
        for call in q("SELECT id,bland_call_id FROM calls WHERE status IN('queued','in-progress') AND bland_call_id IS NOT NULL"):
            live=bland_get_api(call["bland_call_id"])
            if live and "status" in live:
                run("""UPDATE calls SET status=?,duration_seconds=?,recording_url=?,transcript=?,summary=?,updated_at=? WHERE id=?""",
                    (live.get("status"),live.get("call_length"),live.get("recording_url"),live.get("concatenated_transcript",""),live.get("summary",""),now(),call["id"]))
    except Exception as e: logger.error(f"sync:{e}")

def send_daily_report():
    if not TWILIO_ADMIN: return
    try:
        cos=q("SELECT lead_score,name FROM companies"); emails=q("SELECT status FROM emails"); mtgs=q("SELECT status FROM meetings"); calls=q("SELECT id FROM calls")
        hot=sum(1 for c in cos if(c.get("lead_score") or 0)>=70); warm=sum(1 for c in cos if 40<=(c.get("lead_score") or 0)<70)
        sent=sum(1 for e in emails if e.get("status") in("sent","opened","replied")); opened=sum(1 for e in emails if e.get("status") in("opened","replied"))
        top5=sorted(cos,key=lambda x:x.get("lead_score",0),reverse=True)[:5]
        notify_async("daily_report",{"report_date":datetime.utcnow().strftime("%d %b %Y"),"total_companies":len(cos),"hot_leads":hot,"warm_leads":warm,
            "emails_sent":sent,"email_open_rate":round(opened/sent*100,1) if sent else 0,"total_calls":len(calls),
            "meetings_scheduled":sum(1 for m in mtgs if m.get("status")=="scheduled"),"revenue_pipeline":hot*50000+warm*15000,
            "top_companies":", ".join(f"{c['name']}({c['lead_score']})" for c in top5)})
    except Exception as e: logger.error(f"daily_report:{e}")

def run_automation_cycle():
    h=datetime.utcnow().hour; dow=datetime.utcnow().weekday()
    logger.info(f"🔄 Auto cycle h={h} dow={dow}")
    sync_call_statuses()
    if h==AUTO_SCORE_HOUR: auto_score_all()
    if h==AUTO_EMAIL_HOUR and dow<5: auto_email_hot_leads()
    if h==AUTO_FOLLOWUP_HOUR and dow<5: auto_followup()
    if h==AUTO_CALL_HOUR and dow<5: auto_call_hot_leads()
    if h==DAILY_REPORT_HOUR: send_daily_report()
    logger.info("✅ Auto cycle done")

# ══ SCHEDULER ═════════════════════════════════════════════════════════════════
_SR=False; _ST=None

def start():
    global _SR,_ST
    if _SR: return
    _SR=True
    def _loop():
        last=-1
        while _SR:
            try:
                h=datetime.utcnow().hour
                if h!=last: last=h; threading.Thread(target=run_automation_cycle,daemon=True).start()
            except Exception as e: logger.error(f"sched:{e}")
            time.sleep(60)
    _ST=threading.Thread(target=_loop,daemon=True,name="Sched"); _ST.start()

def trigger_now(): threading.Thread(target=run_automation_cycle,daemon=True).start()

# ══ API ROUTES ════════════════════════════════════════════════════════════════
api=Blueprint("api",__name__,url_prefix="/api")

def ok(data=None,**kw):
    r={"ok":True}
    if data is not None: r["data"]=data
    r.update(kw); return jsonify(r)
def err(msg,code=400): return jsonify({"ok":False,"error":msg}),code

@api.post("/auth/login")
def login():
    d=request.get_json(silent=True) or {}
    email=(d.get("email") or "").lower().strip(); pw=d.get("password") or ""
    if not email or not pw: return err("Email and password required")
    user=q1("SELECT * FROM users WHERE email=? AND is_active=1",(email,))
    if not user or not verify_pw(pw,user["password"]): return err("Invalid credentials",401)
    run("UPDATE users SET last_login=? WHERE id=?",(now(),user["id"]))
    tok=make_token(user["id"],user["email"],user["role"])
    return ok({"token":tok,"user":{k:user[k] for k in("id","email","full_name","role","created_at") if k in user}})

@api.post("/auth/register")
def register():
    d=request.get_json(silent=True) or {}
    email=(d.get("email") or "").lower().strip(); fn=(d.get("full_name") or "").strip(); pw=d.get("password") or ""
    if not email or not fn or not pw: return err("email, full_name and password required")
    if len(pw)<6: return err("Password must be at least 6 characters")
    if q1("SELECT 1 FROM users WHERE email=?",(email,)): return err("Email already registered")
    uid=run("INSERT INTO users(email,full_name,password,role) VALUES(?,?,?,?)",(email,fn,hash_pw(pw),d.get("role","sales_rep")))
    user=q1("SELECT * FROM users WHERE id=?",(uid,))
    return ok({"token":make_token(uid,email,d.get("role","sales_rep")),"user":{k:user[k] for k in("id","email","full_name","role") if k in user}}),201

@api.get("/auth/me")
@login_required
def me():
    u=q1("SELECT * FROM users WHERE id=?",(g.user["id"],))
    if not u: return err("Not found",404)
    return ok({k:u[k] for k in("id","email","full_name","role","is_active","created_at","last_login") if k in u})

@api.get("/companies")
@login_required
def list_companies():
    search=request.args.get("search",""); status=request.args.get("status",""); limit=min(int(request.args.get("limit",200)),500)
    sql="SELECT * FROM companies WHERE 1=1"; args=[]
    if search: sql+=" AND name LIKE ?"; args.append(f"%{search}%")
    if status: sql+=" AND status=?"; args.append(status)
    sql+=" ORDER BY lead_score DESC LIMIT ?"; args.append(limit)
    return ok(q(sql,args))

@api.post("/companies")
@login_required
def create_company():
    d=request.get_json(silent=True) or {}; name=(d.get("name") or "").strip()
    if not name: return err("name is required")
    cid=run("""INSERT INTO companies(name,industry,employee_count,annual_revenue,website,city,country,description,technologies,status,linkedin_url,funding_stage,created_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name,d.get("industry"),d.get("employee_count"),d.get("annual_revenue"),d.get("website"),d.get("city"),d.get("country"),d.get("description"),d.get("technologies"),d.get("status","prospect"),d.get("linkedin_url"),d.get("funding_stage"),g.user["id"],now()))
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    notify_async("company_added",{"company_name":co["name"],"industry":co.get("industry","N/A"),"lead_score":co.get("lead_score",0),"status":co.get("status","prospect")})
    return ok(co),201

@api.get("/companies/<int:cid>")
@login_required
def get_company(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    co["contacts"]=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
    co["emails"]=q("SELECT * FROM emails WHERE company_id=? ORDER BY created_at DESC",(cid,))
    co["meetings"]=q("SELECT * FROM meetings WHERE company_id=? ORDER BY scheduled_at DESC",(cid,))
    co["calls"]=q("SELECT * FROM calls WHERE company_id=? ORDER BY created_at DESC",(cid,))
    co["buying_signals"]=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
    co["lead_score_details"]=q1("SELECT * FROM lead_scores WHERE company_id=?",(cid,))
    return ok(co)

@api.put("/companies/<int:cid>")
@login_required
def update_company(cid):
    if not q1("SELECT id FROM companies WHERE id=?",(cid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"name","industry","employee_count","annual_revenue","website","city","country","description","technologies","status","linkedin_url","funding_stage","ai_summary"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if not sets: return err("Nothing to update")
    sets.append("updated_at=?"); vals.append(now()); vals.append(cid)
    run(f"UPDATE companies SET {','.join(sets)} WHERE id=?",vals)
    return ok(q1("SELECT * FROM companies WHERE id=?",(cid,)))

@api.delete("/companies/<int:cid>")
@login_required
def delete_company(cid):
    if not q1("SELECT id FROM companies WHERE id=?",(cid,)): return err("Not found",404)
    run("DELETE FROM companies WHERE id=?",(cid,)); return ok({"deleted":cid})

@api.post("/companies/<int:cid>/score")
@login_required
def score_company_route(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    cts=q("SELECT * FROM contacts WHERE company_id=?",(cid,)); sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
    sc=score_company(co,cts,sigs)
    if q1("SELECT id FROM lead_scores WHERE company_id=?",(cid,)):
        run("""UPDATE lead_scores SET total_score=?,revenue_score=?,employee_score=?,industry_score=?,buying_signal_score=?,department_signal_score=?,email_activity_score=?,tier=?,updated_at=? WHERE company_id=?""",
            (sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"],now(),cid))
    else:
        run("""INSERT INTO lead_scores(company_id,total_score,revenue_score,employee_score,industry_score,buying_signal_score,department_signal_score,email_activity_score,tier) VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid,sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"]))
    run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",(sc["total_score"],now(),cid))
    if sc["total_score"]>=80: notify_async("hot_lead",{"company_name":co["name"],"lead_score":sc["total_score"],"industry":co.get("industry","N/A")})
    return ok(sc)

@api.post("/companies/<int:cid>/ai-summary")
@login_required
def ai_summary_route(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    s=company_summary(co); run("UPDATE companies SET ai_summary=?,updated_at=? WHERE id=?",(s,now(),cid)); return ok({"summary":s})

@api.post("/companies/<int:cid>/analyze-signals")
@login_required
def analyze_signals_route(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    run("DELETE FROM buying_signals WHERE company_id=?",(cid,))
    sigs=buying_signals(co)
    for s in sigs:
        run("INSERT INTO buying_signals(company_id,signal_type,signal_name,signal_description,strength,source) VALUES(?,?,?,?,?,?)",
            (cid,s.get("type",""),s.get("name",""),s.get("description",""),s.get("strength",5),"ai"))
    return ok({"signals":sigs})

@api.post("/companies/upload-csv")
@login_required
def upload_csv():
    f=request.files.get("file")
    if not f or not f.filename.endswith(".csv"): return err("CSV file required")
    content=f.read(); fname=f.filename; uid=g.user["id"]
    def _proc():
        try:
            reader=csv.DictReader(io.StringIO(content.decode("utf-8-sig",errors="replace")))
            ok_n,fail=0,0
            for row in reader:
                name=(row.get("name") or row.get("company") or row.get("Company") or "").strip()
                if not name: fail+=1; continue
                try:
                    def ti(v): return int(float(str(v).replace(",","").replace("$",""))) if v else None
                    if not q1("SELECT id FROM companies WHERE name=?",(name,)):
                        run("""INSERT INTO companies(name,industry,employee_count,annual_revenue,country,city,website,status,created_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (name,row.get("industry"),ti(row.get("employee_count")),ti(row.get("annual_revenue")),row.get("country"),row.get("city"),row.get("website"),row.get("status","prospect"),uid,now()))
                    ok_n+=1
                except Exception: fail+=1
            notify_async("csv_import",{"filename":fname,"processed_rows":ok_n,"failed_rows":fail})
        except Exception as e: logger.error(f"CSV:{e}")
    threading.Thread(target=_proc,daemon=True).start()
    return ok({"message":"Import started","filename":fname}),201

@api.get("/contacts")
@login_required
def list_contacts():
    cid=request.args.get("company_id")
    return ok(q("SELECT * FROM contacts WHERE company_id=?",(cid,)) if cid else q("SELECT * FROM contacts ORDER BY created_at DESC LIMIT 500"))

@api.post("/contacts")
@login_required
def create_contact():
    d=request.get_json(silent=True) or {}
    if not d.get("company_id") or not d.get("first_name"): return err("company_id and first_name required")
    ctid=run("INSERT INTO contacts(company_id,first_name,last_name,email,phone,title,department,seniority_level,is_decision_maker) VALUES(?,?,?,?,?,?,?,?,?)",
             (d["company_id"],d["first_name"],d.get("last_name"),d.get("email"),d.get("phone"),d.get("title"),d.get("department"),d.get("seniority_level","individual"),1 if d.get("is_decision_maker") else 0))
    return ok(q1("SELECT * FROM contacts WHERE id=?",(ctid,))),201

@api.put("/contacts/<int:ctid>")
@login_required
def update_contact(ctid):
    if not q1("SELECT id FROM contacts WHERE id=?",(ctid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"first_name","last_name","email","phone","title","department","seniority_level","is_decision_maker"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets: run(f"UPDATE contacts SET {','.join(sets)} WHERE id=?",vals+[ctid])
    return ok(q1("SELECT * FROM contacts WHERE id=?",(ctid,)))

@api.get("/emails")
@login_required
def list_emails():
    status=request.args.get("status",""); cid=request.args.get("company_id","")
    sql="SELECT * FROM emails WHERE 1=1"; args=[]
    if status: sql+=" AND status=?"; args.append(status)
    if cid: sql+=" AND company_id=?"; args.append(cid)
    return ok(q(sql+" ORDER BY created_at DESC LIMIT 200",args))

@api.post("/emails/generate")
@login_required
def gen_email():
    d=request.get_json(silent=True) or {}; cid=d.get("company_id")
    if not cid: return err("company_id required")
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Company not found",404)
    ct=(q1("SELECT * FROM contacts WHERE id=?",(d["contact_id"],)) if d.get("contact_id") else None
        or q1("SELECT * FROM contacts WHERE company_id=? AND is_decision_maker=1",(cid,))
        or q1("SELECT * FROM contacts WHERE company_id=?",(cid,))
        or {"first_name":"Team","last_name":"","title":"","email":""})
    et=d.get("email_type","cold"); content=generate_email(co,ct,et,d.get("custom_instructions",""))
    eid=run("""INSERT INTO emails(company_id,contact_id,created_by,email_type,subject,body,recipient_email,recipient_name,status,ai_model_used,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (cid,ct.get("id"),g.user["id"],et,content["subject"],content["body"],ct.get("email","unknown@example.com"),f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),"draft","groq",now()))
    em=q1("SELECT * FROM emails WHERE id=?",(eid,))
    notify_async("email_generated",{"company_name":co["name"],"email_type":et,"recipient_email":em["recipient_email"],"subject":em["subject"]})
    return ok(em),201

@api.post("/emails/<int:eid>/send")
@login_required
def send_email_route(eid):
    try:
        em=q1("SELECT * FROM emails WHERE id=?",(eid,))
        if not em: return err("Not found",404)
        if em.get("status")=="sent": return err("Already sent")
        recip = (em.get("recipient_email") or "").strip()
        if not recip:
            return err("Email has no recipient_email set",400)
        result = send_email_smtp(recip, em.get("recipient_name",""), em.get("subject","(no subject)"), em.get("body",""))
        if result.get("status")=="sent":
            run("UPDATE emails SET status=\'sent\',sent_at=?,updated_at=? WHERE id=?",(now(),now(),eid))
            notify_async("email_sent",{"recipient_email":recip,"subject":em.get("subject","")})
        return ok({"email":q1("SELECT * FROM emails WHERE id=?",(eid,)),"send_result":result})
    except Exception as e:
        logger.error(f"send_email_route {eid}: {e}")
        return err(f"Send failed: {e}", 500)

@api.put("/emails/<int:eid>")
@login_required
def update_email(eid):
    if not q1("SELECT id FROM emails WHERE id=?",(eid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"subject","body","recipient_email","recipient_name","status","email_type"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets: sets.append("updated_at=?"); vals.append(now()); vals.append(eid); run(f"UPDATE emails SET {','.join(sets)} WHERE id=?",vals)
    return ok(q1("SELECT * FROM emails WHERE id=?",(eid,)))

@api.put("/emails/<int:eid>/track")
@login_required
def track_email(eid):
    d=request.get_json(silent=True) or {}; status=d.get("status")
    if status not in("opened","replied","bounced"): return err("Invalid status")
    em=q1("SELECT * FROM emails WHERE id=?",(eid,))
    if not em: return err("Not found",404)
    run("UPDATE emails SET status=?,updated_at=? WHERE id=?",(status,now(),eid)); return ok(q1("SELECT * FROM emails WHERE id=?",(eid,)))

@api.get("/meetings")
@login_required
def list_meetings():
    status=request.args.get("status","")
    sql="SELECT m.*,c.name as company_name FROM meetings m LEFT JOIN companies c ON m.company_id=c.id"; args=[]
    if status: sql+=" WHERE m.status=?"; args.append(status)
    return ok(q(sql+" ORDER BY m.scheduled_at DESC",args))

@api.post("/meetings")
@login_required
def create_meeting():
    d=request.get_json(silent=True) or {}
    if not d.get("company_id") or not d.get("title"): return err("company_id and title required")
    co=q1("SELECT * FROM companies WHERE id=?",(d["company_id"],))
    if not co: return err("Company not found",404)
    mid=run("""INSERT INTO meetings(company_id,contact_id,created_by,title,meeting_type,description,scheduled_at,duration_minutes,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (d["company_id"],d.get("contact_id"),g.user["id"],d["title"],d.get("meeting_type","discovery"),d.get("description"),d.get("scheduled_at"),d.get("duration_minutes",30),"proposed",now()))
    m=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    notify_async("meeting_scheduled",{"title":m["title"],"company_name":co["name"],"meeting_type":m.get("meeting_type",""),"scheduled_at":(m.get("scheduled_at") or "TBD")[:16],"duration_minutes":m.get("duration_minutes",30)})
    if m.get("scheduled_at"): schedule_reminders(mid,m["title"],co["name"],m["scheduled_at"])
    return ok(m),201

@api.put("/meetings/<int:mid>")
@login_required
def update_meeting(mid):
    m=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    if not m: return err("Not found",404)
    prev=m.get("status",""); d=request.get_json(silent=True) or {}
    allowed={"title","meeting_type","description","scheduled_at","duration_minutes","status","meeting_link","notes"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets: sets.append("updated_at=?"); vals.append(now()); vals.append(mid); run(f"UPDATE meetings SET {','.join(sets)} WHERE id=?",vals)
    m2=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    if m2.get("status")=="completed" and prev!="completed":
        co=q1("SELECT name FROM companies WHERE id=?",(m2.get("company_id",0),))
        notify_async("meeting_completed",{"title":m2["title"],"company_name":co["name"] if co else "Unknown","meeting_type":m2.get("meeting_type","")})
    return ok(m2)

@api.post("/meetings/<int:mid>/calendar")
@login_required
def add_to_calendar(mid):
    m=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    if not m: return err("Not found",404)
    user=q1("SELECT * FROM users WHERE id=?",(g.user["id"],))
    if not user or not user.get("google_refresh_token"): return err("Google Calendar not connected — go to Integrations",400)
    return ok({"message":"Calendar event would be created here","google_event_id":None})

@api.get("/calls")
@login_required
def list_calls():
    return ok(q("SELECT ca.*,co.name as company_name FROM calls ca LEFT JOIN companies co ON ca.company_id=co.id ORDER BY ca.created_at DESC LIMIT 100"))

@api.post("/calls/make")
@login_required
def make_call():
    d=request.get_json(silent=True) or {}; phone=(d.get("phone_number") or "").strip()
    cid=d.get("company_id"); co=q1("SELECT * FROM companies WHERE id=?",(cid,)) if cid else None
    ct=q1("SELECT * FROM contacts WHERE id=?",(d.get("contact_id"),)) if d.get("contact_id") else None
    if not phone and ct and ct.get("phone"): phone=ct["phone"]
    if not phone: return err("phone_number required (e.g. +14155550100)")
    objective=d.get("objective","qualify"); task=d.get("custom_task") or call_script(co or {},ct or {},objective)
    result=bland_call_api(phone,task,d.get("voice","nat"),co["name"] if co else "",f"{ct.get('first_name','')} {ct.get('last_name','')}".strip() if ct else "")
    call_id=run("""INSERT INTO calls(bland_call_id,company_id,contact_id,created_by,phone_number,objective,task_prompt,voice,status,error_message,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (result.get("call_id"),cid,d.get("contact_id"),g.user["id"],phone,objective,task,d.get("voice","nat"),
                 "queued" if result.get("status")=="queued" else "error",result.get("message") if result.get("status")=="error" else None,now()))
    notify_async("call_initiated",{"company_name":co["name"] if co else "Unknown","phone_number":phone,"objective":objective})
    return ok({"call":q1("SELECT * FROM calls WHERE id=?",(call_id,)),"bland_result":result})

@api.get("/calls/<int:cid_>")
@login_required
def get_call(cid_):
    call=q1("SELECT * FROM calls WHERE id=?",(cid_,))
    if not call: return err("Not found",404)
    if call.get("bland_call_id") and call.get("status") not in("completed","error","failed"):
        live=bland_get_api(call["bland_call_id"])
        if live and "status" in live:
            run("UPDATE calls SET status=?,duration_seconds=?,recording_url=?,transcript=?,summary=?,updated_at=? WHERE id=?",
                (live.get("status"),live.get("call_length"),live.get("recording_url"),live.get("concatenated_transcript",""),live.get("summary",""),now(),cid_))
            call=q1("SELECT * FROM calls WHERE id=?",(cid_,))
    return ok(call)

@api.get("/analytics/summary")
@login_required
def analytics_summary():
    cos=q("SELECT lead_score,status FROM companies"); emails=q("SELECT status FROM emails"); mtgs=q("SELECT status FROM meetings"); calls_=q("SELECT status FROM calls")
    hot=sum(1 for c in cos if(c.get("lead_score") or 0)>=70); warm=sum(1 for c in cos if 40<=(c.get("lead_score") or 0)<70)
    sent=sum(1 for e in emails if e.get("status")!="draft"); opened=sum(1 for e in emails if e.get("status") in("opened","replied")); replied=sum(1 for e in emails if e.get("status")=="replied")
    return ok({"total_companies":len(cos),"hot_leads":hot,"warm_leads":warm,"cold_leads":len(cos)-hot-warm,"emails_sent":sent,"emails_opened":opened,
               "open_rate":round(opened/sent*100,1) if sent else 0,"reply_rate":round(replied/sent*100,1) if sent else 0,
               "meetings_scheduled":sum(1 for m in mtgs if m.get("status")=="scheduled"),"meetings_completed":sum(1 for m in mtgs if m.get("status")=="completed"),
               "revenue_pipeline":hot*50000+warm*15000,"total_calls":len(calls_),"completed_calls":sum(1 for c in calls_ if c.get("status")=="completed")})

@api.get("/analytics/email-activity")
@login_required
def email_activity():
    emails=q("SELECT status,created_at FROM emails"); daily=defaultdict(lambda:{"sent":0,"opened":0,"replied":0})
    for e in emails:
        day=(e.get("created_at") or "")[:10]
        if not day: continue
        if e.get("status")!="draft": daily[day]["sent"]+=1
        if e.get("status") in("opened","replied"): daily[day]["opened"]+=1
        if e.get("status")=="replied": daily[day]["replied"]+=1
    result=[{"date":d,**v} for d,v in sorted(daily.items())][-30:]
    if not result:
        today=datetime.utcnow(); result=[{"date":(today-timedelta(days=13-i)).strftime("%Y-%m-%d"),"sent":i%4+1,"opened":max(0,i%3),"replied":max(0,i%2-1)} for i in range(14)]
    return ok(result)

@api.get("/analytics/lead-distribution")
@login_required
def lead_distribution():
    by=defaultdict(lambda:{"count":0,"total":0})
    for c in q("SELECT industry,lead_score FROM companies"):
        ind=c.get("industry") or "Other"; by[ind]["count"]+=1; by[ind]["total"]+=c.get("lead_score") or 0
    return ok(sorted([{"industry":k,"count":v["count"],"avg_score":round(v["total"]/v["count"],1)} for k,v in by.items()],key=lambda x:x["count"],reverse=True))

@api.get("/analytics/pipeline")
@login_required
def analytics_pipeline():
    cos=q("SELECT name,lead_score,status,annual_revenue FROM companies ORDER BY lead_score DESC LIMIT 20")
    return ok([{"name":c["name"],"lead_score":c.get("lead_score",0),"potential_revenue":int((c.get("annual_revenue") or 0)*0.02),"status":c.get("status","prospect")} for c in cos])

@api.get("/chat")
@login_required
def get_chat(): return ok(list(reversed(q("SELECT * FROM chat_messages ORDER BY created_at DESC LIMIT 100"))))

@api.post("/chat")
@login_required
def post_chat():
    d=request.get_json(silent=True) or {}; msg=(d.get("message") or "").strip()
    if not msg: return err("message required")
    cos=q("SELECT name,industry,lead_score FROM companies")
    stats={"companies":cos,"emails_sent":q("SELECT COUNT(*) as n FROM emails WHERE status!='draft'")[0]["n"],
           "total_calls":q("SELECT COUNT(*) as n FROM calls")[0]["n"],"meetings_scheduled":q("SELECT COUNT(*) as n FROM meetings WHERE status='scheduled'")[0]["n"]}
    reply=chat_reply(msg,stats)
    run("INSERT INTO chat_messages(sender,sender_name,message) VALUES(?,?,?)","user",g.user["email"],msg)
    run("INSERT INTO chat_messages(sender,sender_name,message) VALUES(?,?,?)","bot","AI Bot",reply)
    return ok({"reply":reply})

@api.delete("/chat")
@login_required
def clear_chat(): run("DELETE FROM chat_messages"); return ok({"message":"Cleared"})

@api.get("/integrations/status")
@login_required
def integrations_status():
    user=q1("SELECT google_refresh_token FROM users WHERE id=?",(g.user["id"],))
    return ok({"groq":{"connected":bool(GROQ_API_KEY),"model":GROQ_MODEL},"gmail":{"connected":bool(GMAIL_EMAIL and GMAIL_PASSWORD),"email":GMAIL_EMAIL or None},
               "bland_ai":{"connected":bool(BLAND_API_KEY)},"twilio_sms":{"connected":bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM),"from_number":TWILIO_FROM or None,"admin_number":TWILIO_ADMIN or None},
               "google_calendar":{"connected":bool(user and user.get("google_refresh_token"))}})

@api.post("/integrations/gmail/test")
@login_required
def test_gmail_route(): ok_,msg=test_gmail(); return ok({"success":ok_,"message":msg})

@api.post("/integrations/gmail/send-test")
@login_required
def send_test_gmail():
    user=q1("SELECT email FROM users WHERE id=?",(g.user["id"],))
    if not user: return err("Not found",404)
    r=send_email_smtp(user["email"],"Test","AI Sales Test","✅ Gmail is working!"); return ok({"success":r.get("status")=="sent","result":r})

@api.post("/integrations/bland/test")
@login_required
def test_bland_route():
    ok_,msg=test_bland()
    return ok({"success":ok_,"message":msg})

@api.post("/integrations/twilio/test")
@login_required
def test_twilio_route():
    d=request.get_json(silent=True) or {}; to=(d.get("to_number") or "").strip()
    if not to: return err("to_number required")
    result=sms_test(to); return ok({"success":result.get("status")=="sent","result":result})

@api.post("/integrations/twilio/daily-report")
@login_required
def manual_daily_report(): send_daily_report(); return ok({"message":"Daily report SMS sent"})

@api.get("/integrations/google/auth-url")
@login_required
def google_auth_url_route(): url=google_auth_url(g.user["id"]); return ok({"auth_url":url or None})

@api.get("/integrations/google/callback")
def google_callback():
    code=request.args.get("code",""); state=request.args.get("state","")
    if not code: return err("Missing code")
    tokens=google_exchange(code)
    if tokens.get("refresh_token") and state:
        try: run("UPDATE users SET google_refresh_token=? WHERE id=?",(tokens["refresh_token"],int(state)))
        except Exception: pass
    return "<html><body style='font-family:Arial;text-align:center;padding:60px'><h2>✅ Connected!</h2><p>Close this window.</p></body></html>"

@api.post("/integrations/google/disconnect")
@login_required
def google_disconnect(): run("UPDATE users SET google_refresh_token=NULL WHERE id=?",(g.user["id"],)); return ok({"message":"Disconnected"})

@api.get("/sms-logs")
@login_required
def sms_logs(): limit=min(int(request.args.get("limit",100)),500); return ok(q("SELECT * FROM sms_logs ORDER BY created_at DESC LIMIT ?",(limit,)))

@api.get("/automation/status")
@login_required
def automation_status():
    cos=q("SELECT lead_score,status FROM companies"); emails=q("SELECT status,email_type FROM emails"); calls_=q("SELECT status FROM calls"); mtgs=q("SELECT status FROM meetings")
    hot=sum(1 for c in cos if(c.get("lead_score") or 0)>=70); warm=sum(1 for c in cos if 40<=(c.get("lead_score") or 0)<70)
    sent=sum(1 for e in emails if e.get("status") in("sent","opened","replied")); opened=sum(1 for e in emails if e.get("status") in("opened","replied")); replied=sum(1 for e in emails if e.get("status")=="replied")
    cold_e=sum(1 for e in emails if e.get("email_type")=="cold"); fu=sum(1 for e in emails if e.get("email_type")=="follow_up")
    return ok({"pipeline":{"hot_leads":hot,"warm_leads":warm,"cold_leads":len(cos)-hot-warm,"total":len(cos),"pipeline_value":hot*50000+warm*15000},
               "emails":{"cold_sent":cold_e,"followups":fu,"opened":opened,"replied":replied,"open_rate":round(opened/cold_e*100,1) if cold_e else 0,"reply_rate":round(replied/cold_e*100,1) if cold_e else 0},
               "calls":{"total":len(calls_),"completed":sum(1 for c in calls_ if c.get("status")=="completed")},
               "meetings":{"scheduled":sum(1 for m in mtgs if m.get("status")=="scheduled"),"completed":sum(1 for m in mtgs if m.get("status")=="completed")},
               "schedule_utc":{"score":f"{AUTO_SCORE_HOUR:02d}:00","cold_email":f"{AUTO_EMAIL_HOUR:02d}:00 (weekdays)","follow_up":f"{AUTO_FOLLOWUP_HOUR:02d}:00 (3 days)","auto_call":f"{AUTO_CALL_HOUR:02d}:00 (score>=80)","daily_report":f"{DAILY_REPORT_HOUR:02d}:00"},
               "integrations":{"groq":bool(GROQ_API_KEY),"gmail":bool(GMAIL_EMAIL and GMAIL_PASSWORD),"bland":bool(BLAND_API_KEY),"twilio":bool(TWILIO_SID and TWILIO_TOKEN)}})

@api.post("/automation/run-now")
@login_required
def run_auto_now(): trigger_now(); return ok({"message":"Automation cycle started"})

@api.post("/automation/score-now")
@login_required
def score_now_route(): threading.Thread(target=auto_score_all,daemon=True).start(); return ok({"message":"Scoring companies..."})

@api.post("/automation/email-now")
@login_required
def email_now_route(): threading.Thread(target=auto_email_hot_leads,daemon=True).start(); return ok({"message":"Auto email started..."})

@api.post("/automation/followup-now")
@login_required
def followup_now_route(): threading.Thread(target=auto_followup,daemon=True).start(); return ok({"message":"Follow-up started..."})

@api.post("/automation/call-now")
@login_required
def call_now_route(): threading.Thread(target=auto_call_hot_leads,daemon=True).start(); return ok({"message":"Auto call started..."})

@api.get("/automation/activity-feed")
@login_required
def activity_feed():
    emails=q("SELECT 'email' as type,subject as title,status,recipient_email as target,created_at,email_type as subtype FROM emails ORDER BY created_at DESC LIMIT 20")
    calls_=q("SELECT 'call' as type,objective as title,status,phone_number as target,created_at,voice as subtype FROM calls ORDER BY created_at DESC LIMIT 10")
    sms=q("SELECT 'sms' as type,event_type as title,status,to_number as target,created_at,'' as subtype FROM sms_logs ORDER BY created_at DESC LIMIT 15")
    mtgs=q("SELECT 'meeting' as type,title,status,'' as target,created_at,meeting_type as subtype FROM meetings ORDER BY created_at DESC LIMIT 10")
    return ok(sorted(emails+calls_+sms+mtgs,key=lambda x:x.get("created_at",""),reverse=True)[:50])

# ══ FRONTEND HTML (embedded — no files needed) ════════════════════════════════
_HTML = b'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>AI Sales Assistant</title>\n<style>\n*{box-sizing:border-box;margin:0;padding:0}\n:root{\n  --bg:#0a0e1a;--panel:#1e293b;--border:#334155;--accent:#3b82f6;\n  --accent2:#1d4ed8;--text:#e2e8f0;--muted:#94a3b8;\n  --hot:#ef4444;--warm:#f59e0b;--cold:#60a5fa;\n  --green:#22c55e;--red:#ef4444;\n}\nbody{font-family:\'Segoe UI\',Arial,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column}\n#app{display:flex;flex:1;overflow:hidden}\n\n/* Sidebar */\n#sidebar{width:220px;background:linear-gradient(180deg,#0f172a,#1e293b);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}\n#sidebar h2{padding:20px 16px 8px;font-size:1rem;color:var(--accent);border-bottom:1px solid var(--border)}\n#user-info{padding:8px 16px 12px;font-size:.75rem;color:var(--muted);border-bottom:1px solid var(--border)}\n#nav{flex:1;padding:8px 0;overflow-y:auto}\n.nav-item{display:flex;align-items:center;gap:10px;padding:10px 16px;cursor:pointer;color:var(--muted);font-size:.875rem;transition:all .15s;border-left:3px solid transparent}\n.nav-item:hover{color:var(--text);background:rgba(255,255,255,.05)}\n.nav-item.active{color:var(--accent);background:rgba(59,130,246,.1);border-left-color:var(--accent)}\n#sys-status{padding:12px 16px;border-top:1px solid var(--border);font-size:.7rem;color:var(--muted)}\n#sys-status div{margin-bottom:3px}\n#logout-btn{margin:12px;padding:8px;background:#1e3a5f;border:1px solid var(--border);color:var(--text);border-radius:6px;cursor:pointer;font-size:.8rem}\n#logout-btn:hover{background:var(--accent2)}\n\n/* Main */\n#main{flex:1;overflow:hidden;display:flex;flex-direction:column}\n#topbar{padding:12px 24px;border-bottom:1px solid var(--border);background:var(--panel);display:flex;justify-content:space-between;align-items:center}\n#topbar h1{font-size:1.1rem;font-weight:600}\n#content{flex:1;overflow-y:auto;padding:24px}\n\n/* Login */\n#login-screen{display:flex;align-items:center;justify-content:center;height:100vh;background:var(--bg)}\n.login-box{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:40px;width:380px}\n.login-box h2{text-align:center;margin-bottom:8px;font-size:1.4rem}\n.login-box p{text-align:center;color:var(--muted);font-size:.8rem;margin-bottom:24px}\n\n/* Forms */\n.form-group{margin-bottom:14px}\nlabel{display:block;font-size:.8rem;color:var(--muted);margin-bottom:5px}\ninput,select,textarea{width:100%;padding:9px 12px;background:#0f172a;border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:.875rem;outline:none;transition:border .15s}\ninput:focus,select:focus,textarea:focus{border-color:var(--accent)}\ntextarea{resize:vertical;min-height:80px}\nselect option{background:#0f172a}\n\n/* Buttons */\n.btn{padding:9px 18px;border:none;border-radius:6px;cursor:pointer;font-size:.875rem;font-weight:600;transition:opacity .15s;display:inline-flex;align-items:center;gap:6px}\n.btn:hover{opacity:.85}\n.btn:active{opacity:.7}\n.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}\n.btn-success{background:#15803d;color:#fff}\n.btn-warning{background:#b45309;color:#fff}\n.btn-danger{background:#991b1b;color:#fff}\n.btn-ghost{background:rgba(255,255,255,.08);color:var(--text)}\n.btn-sm{padding:5px 12px;font-size:.8rem}\n.btn-full{width:100%;justify-content:center}\n\n/* Cards / Metrics */\n.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}\n.metric-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center}\n.metric-val{font-size:1.8rem;font-weight:700;color:var(--accent)}\n.metric-lbl{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:4px}\n\n/* Tables */\n.tbl-wrap{background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:20px}\n.tbl-head{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}\n.tbl-head h3{font-size:.9rem;font-weight:600}\ntable{width:100%;border-collapse:collapse}\nth{padding:10px 14px;text-align:left;font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);background:#0f172a}\ntd{padding:10px 14px;font-size:.85rem;border-bottom:1px solid rgba(51,65,85,.5)}\ntr:last-child td{border-bottom:none}\ntr:hover td{background:rgba(255,255,255,.02)}\n\n/* Badges */\n.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:.7rem;font-weight:600}\n.badge-hot{background:#7f1d1d;color:#fca5a5}\n.badge-warm{background:#78350f;color:#fcd34d}\n.badge-cold{background:#1e3a5f;color:#93c5fd}\n.badge-sent{background:#14532d;color:#86efac}\n.badge-draft{background:#1e293b;color:#94a3b8}\n.badge-opened{background:#1e3a5f;color:#93c5fd}\n.badge-replied{background:#3b0764;color:#d8b4fe}\n.badge-scheduled{background:#14532d;color:#86efac}\n.badge-completed{background:#14532d;color:#86efac}\n.badge-proposed{background:#78350f;color:#fcd34d}\n.badge-queued{background:#1e3a5f;color:#93c5fd}\n.badge-error{background:#7f1d1d;color:#fca5a5}\n\n/* Tabs */\n.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:0}\n.tab{padding:8px 16px;cursor:pointer;color:var(--muted);font-size:.85rem;border-bottom:2px solid transparent;margin-bottom:-1px}\n.tab:hover{color:var(--text)}\n.tab.active{color:var(--accent);border-bottom-color:var(--accent)}\n.tab-panel{display:none}\n.tab-panel.active{display:block}\n\n/* Modal */\n.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center}\n.modal-backdrop.open{display:flex}\n.modal{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:24px;width:100%;max-width:560px;max-height:85vh;overflow-y:auto}\n.modal h3{margin-bottom:16px;font-size:1rem}\n.modal-close{float:right;cursor:pointer;color:var(--muted);font-size:1.2rem}\n\n/* Search */\n.search-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}\n.search-bar input{flex:1;min-width:180px}\n\n/* Toast */\n#toast{position:fixed;bottom:24px;right:24px;z-index:200;display:flex;flex-direction:column;gap:8px}\n.toast-msg{padding:12px 18px;border-radius:8px;font-size:.85rem;font-weight:500;min-width:260px;box-shadow:0 4px 12px rgba(0,0,0,.4);animation:slide-in .25s ease}\n.toast-success{background:#14532d;color:#86efac;border:1px solid #22c55e}\n.toast-error{background:#7f1d1d;color:#fca5a5;border:1px solid #ef4444}\n.toast-info{background:#1e3a5f;color:#93c5fd;border:1px solid #3b82f6}\n@keyframes slide-in{from{transform:translateX(120%);opacity:0}to{transform:translateX(0);opacity:1}}\n\n/* Chat */\n#chat-msgs{height:400px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px;background:#0f172a;display:flex;flex-direction:column;gap:8px}\n.chat-user{align-self:flex-end;background:var(--accent2);color:#fff;padding:8px 12px;border-radius:12px 12px 2px 12px;max-width:75%;font-size:.875rem}\n.chat-bot{align-self:flex-start;background:var(--panel);color:var(--text);padding:8px 12px;border-radius:12px 12px 12px 2px;max-width:75%;font-size:.875rem;white-space:pre-wrap}\n\n/* Misc */\n.row{display:flex;gap:12px;flex-wrap:wrap}\n.col{flex:1;min-width:200px}\n.section{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px}\n.section h3{font-size:.9rem;font-weight:600;margin-bottom:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}\n.empty{text-align:center;padding:32px;color:var(--muted);font-size:.875rem}\n.score-bar-wrap{height:8px;background:#1e293b;border-radius:4px;overflow:hidden;margin-top:4px}\n.score-bar{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent),var(--accent2))}\n.signal-bar{display:inline-block;font-family:monospace;letter-spacing:1px;font-size:.8rem}\n.divider{border:none;border-top:1px solid var(--border);margin:16px 0}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   POP-OUT EFFECTS \xe2\x80\x94 pure CSS, zero JS changes\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\n\n/* Page load fade-in */\n@keyframes fade-up{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}\n@keyframes fade-in{from{opacity:0}to{opacity:1}}\n@keyframes pop-in{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:scale(1)}}\n@keyframes slide-down{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:translateY(0)}}\n@keyframes glow-pulse{0%,100%{box-shadow:0 0 0 0 rgba(59,130,246,0)}50%{box-shadow:0 0 18px 4px rgba(59,130,246,.25)}}\n@keyframes shimmer{0%{background-position:-400px 0}100%{background-position:400px 0}}\n@keyframes bounce-in{0%{transform:scale(0.3);opacity:0}50%{transform:scale(1.08)}70%{transform:scale(0.96)}100%{transform:scale(1);opacity:1}}\n@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}\n@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}\n@keyframes border-flash{0%,100%{border-color:var(--border)}50%{border-color:var(--accent)}}\n\n/* Login box \xe2\x80\x94 bounces in */\n.login-box{animation:bounce-in .55s cubic-bezier(.36,.07,.19,.97) both}\n.login-box h2{animation:fade-up .4s .15s both}\n.login-box .form-group{animation:fade-up .4s both}\n.login-box .form-group:nth-child(3){animation-delay:.1s}\n.login-box .form-group:nth-child(4){animation-delay:.18s}\n.login-box .btn{animation:fade-up .4s .25s both}\n\n/* Sidebar nav items \xe2\x80\x94 stagger in */\n.nav-item{animation:fade-up .3s both;transition:all .2s cubic-bezier(.34,1.56,.64,1) !important}\n.nav-item:nth-child(1){animation-delay:.05s}\n.nav-item:nth-child(2){animation-delay:.09s}\n.nav-item:nth-child(3){animation-delay:.13s}\n.nav-item:nth-child(4){animation-delay:.17s}\n.nav-item:nth-child(5){animation-delay:.21s}\n.nav-item:nth-child(6){animation-delay:.25s}\n.nav-item:nth-child(7){animation-delay:.29s}\n.nav-item:nth-child(8){animation-delay:.33s}\n.nav-item:nth-child(9){animation-delay:.37s}\n.nav-item:nth-child(10){animation-delay:.41s}\n.nav-item:hover{transform:translateX(5px) scale(1.03) !important;color:var(--text);background:rgba(255,255,255,.07) !important}\n.nav-item.active{transform:translateX(3px) !important;animation:glow-pulse 2.5s ease-in-out infinite}\n\n/* Metric cards \xe2\x80\x94 pop up with stagger */\n.metric-card{animation:pop-in .35s cubic-bezier(.34,1.56,.64,1) both;\n  transition:transform .2s cubic-bezier(.34,1.56,.64,1),box-shadow .2s ease,border-color .2s}\n.metric-card:nth-child(1){animation-delay:.05s}\n.metric-card:nth-child(2){animation-delay:.10s}\n.metric-card:nth-child(3){animation-delay:.15s}\n.metric-card:nth-child(4){animation-delay:.20s}\n.metric-card:nth-child(5){animation-delay:.25s}\n.metric-card:nth-child(6){animation-delay:.30s}\n.metric-card:nth-child(7){animation-delay:.35s}\n.metric-card:nth-child(8){animation-delay:.40s}\n.metric-card:hover{transform:translateY(-6px) scale(1.04);\n  box-shadow:0 12px 32px rgba(59,130,246,.3);border-color:var(--accent)}\n.metric-val{transition:transform .15s;display:block}\n.metric-card:hover .metric-val{transform:scale(1.1)}\n\n/* Table wrapper \xe2\x80\x94 slides up */\n.tbl-wrap{animation:fade-up .4s .1s both;\n  transition:box-shadow .2s,border-color .2s}\n.tbl-wrap:hover{box-shadow:0 6px 24px rgba(0,0,0,.35);border-color:#475569}\n\n/* Table rows \xe2\x80\x94 lift on hover */\ntr{transition:all .15s ease}\ntr:hover td{background:rgba(59,130,246,.06) !important;\n  transform:none}\ntbody tr:hover{transform:translateX(2px)}\n\n/* Buttons \xe2\x80\x94 spring pop */\n.btn{transition:transform .18s cubic-bezier(.34,1.56,.64,1),\n  opacity .15s,box-shadow .18s !important}\n.btn:hover{transform:translateY(-2px) scale(1.05) !important;\n  opacity:1 !important;box-shadow:0 6px 18px rgba(59,130,246,.35)}\n.btn:active{transform:scale(.94) !important;box-shadow:none}\n.btn-danger:hover{box-shadow:0 6px 18px rgba(239,68,68,.35) !important}\n.btn-success:hover{box-shadow:0 6px 18px rgba(34,197,94,.35) !important}\n.btn-primary{animation:glow-pulse 3s ease-in-out infinite}\n\n/* Badges \xe2\x80\x94 pop on hover */\n.badge{transition:transform .18s cubic-bezier(.34,1.56,.64,1),box-shadow .15s;cursor:default}\n.badge:hover{transform:scale(1.18);box-shadow:0 3px 10px rgba(0,0,0,.35)}\n\n/* Modal \xe2\x80\x94 spring pop */\n.modal-backdrop.open{animation:fade-in .2s ease}\n.modal{animation:bounce-in .35s cubic-bezier(.34,1.56,.64,1) both}\n\n/* Sections (company detail, integrations, analytics) */\n.section{animation:fade-up .35s both;\n  transition:border-color .2s,box-shadow .2s}\n.section:hover{border-color:#475569;box-shadow:0 4px 20px rgba(0,0,0,.3)}\n\n/* Topbar \xe2\x80\x94 slides down */\n#topbar{animation:slide-down .3s ease both}\n\n/* Content area \xe2\x80\x94 fade up */\n#content{animation:fade-in .25s ease}\n\n/* Input focus \xe2\x80\x94 glow pop */\ninput:focus,select:focus,textarea:focus{\n  border-color:var(--accent) !important;\n  box-shadow:0 0 0 3px rgba(59,130,246,.18),0 2px 8px rgba(59,130,246,.15);\n  transform:none}\n\n/* Score bar \xe2\x80\x94 animated fill */\n.score-bar{transition:width .8s cubic-bezier(.34,1.2,.64,1)}\n\n/* Chat messages \xe2\x80\x94 slide in */\n.chat-user{animation:fade-up .2s ease both}\n.chat-bot{animation:fade-up .25s .05s ease both}\n\n/* Toast \xe2\x80\x94 already has slide-in, add pop */\n.toast-msg{animation:bounce-in .3s cubic-bezier(.36,.07,.19,.97) both !important}\n.toast-success{box-shadow:0 4px 18px rgba(34,197,94,.3) !important}\n.toast-error{box-shadow:0 4px 18px rgba(239,68,68,.3) !important}\n.toast-info{box-shadow:0 4px 18px rgba(59,130,246,.3) !important}\n\n/* Sidebar logo \xe2\x80\x94 float */\n#sidebar h2{animation:float 3s ease-in-out infinite}\n\n/* Signal bars \xe2\x80\x94 glow */\n.signal-bar{text-shadow:0 0 8px rgba(59,130,246,.6)}\n\n/* Score bar wrap \xe2\x80\x94 flash on load */\n.score-bar-wrap{animation:border-flash 1.5s ease 1s}\n\n/* Hot badge \xe2\x80\x94 pulse glow */\n.badge-hot{animation:glow-pulse 2s ease-in-out infinite;\n  box-shadow:0 0 8px rgba(239,68,68,.4)}\n\n/* Tbl-head action buttons */\n.tbl-head .btn{animation:none}\n\n/* Logout button */\n#logout-btn{transition:transform .18s cubic-bezier(.34,1.56,.64,1),background .15s}\n#logout-btn:hover{transform:scale(1.04);background:var(--accent2)}\n\n/* Tab active indicator pops */\n.tab{transition:color .15s,border-color .15s,transform .15s}\n.tab:hover{transform:translateY(-1px)}\n.tab.active{text-shadow:0 0 12px rgba(59,130,246,.5)}\n</style>\n</head>\n<body>\n\n<!-- LOGIN -->\n<div id="login-screen">\n  <div class="login-box">\n    <h2>\xf0\x9f\xa4\x96 AI Sales Assistant</h2>\n    <p>Groq AI \xc2\xb7 Twilio SMS \xc2\xb7 Bland AI \xc2\xb7 Google Calendar</p>\n    <div class="form-group"><label>Email</label><input id="li-email" type="email" value="admin@salesai.com"></div>\n    <div class="form-group"><label>Password</label><input id="li-pass" type="password" value="Admin@123456"></div>\n    <button class="btn btn-primary btn-full" onclick="doLogin()">Login \xe2\x86\x92</button>\n    <div style="text-align:center;margin-top:12px;font-size:.8rem;color:var(--muted)">\n      Don\'t have an account? <a href="#" onclick="showRegister()" style="color:var(--accent)">Register</a>\n    </div>\n  </div>\n</div>\n\n<!-- REGISTER MODAL -->\n<div class="modal-backdrop" id="reg-modal">\n  <div class="modal">\n    <h3>\xf0\x9f\x93\x9d Create Account <span class="modal-close" onclick="closeModal(\'reg-modal\')">\xe2\x9c\x95</span></h3>\n    <div class="form-group"><label>Full Name</label><input id="reg-name" placeholder="Your Name"></div>\n    <div class="form-group"><label>Email</label><input id="reg-email" type="email" placeholder="you@company.com"></div>\n    <div class="form-group"><label>Password</label><input id="reg-pass" type="password" placeholder="Min 6 chars"></div>\n    <div class="form-group"><label>Role</label>\n      <select id="reg-role"><option value="sales_rep">Sales Rep</option><option value="manager">Manager</option><option value="admin">Admin</option></select>\n    </div>\n    <button class="btn btn-primary btn-full" onclick="doRegister()">Create Account</button>\n  </div>\n</div>\n\n<!-- MAIN APP -->\n<div id="app" style="display:none">\n  <div id="sidebar">\n    <h2>\xf0\x9f\xa4\x96 AI Sales</h2>\n    <div id="user-info">Loading...</div>\n    <nav id="nav">\n      <div class="nav-item active" onclick="goto(\'dashboard\')">\xf0\x9f\x93\x8a Dashboard</div>\n      <div class="nav-item" onclick="goto(\'automation\')" style="background:rgba(59,130,246,.08);border-left:3px solid rgba(59,130,246,.4)">\xf0\x9f\xa4\x96 Automation</div>\n      <div class="nav-item" onclick="goto(\'companies\')">\xf0\x9f\x8f\xa2 Companies</div>\n      <div class="nav-item" onclick="goto(\'contacts\')">\xf0\x9f\x91\xa4 Contacts</div>\n      <div class="nav-item" onclick="goto(\'emails\')">\xf0\x9f\x93\xa7 Emails</div>\n      <div class="nav-item" onclick="goto(\'meetings\')">\xf0\x9f\x93\x85 Meetings</div>\n      <div class="nav-item" onclick="goto(\'calls\')">\xf0\x9f\x93\x9e Calls</div>\n      <div class="nav-item" onclick="goto(\'analytics\')">\xf0\x9f\x93\x88 Analytics</div>\n      <div class="nav-item" onclick="goto(\'chat\')">\xf0\x9f\x92\xac Chat</div>\n      <div class="nav-item" onclick="goto(\'sms\')">\xf0\x9f\x93\xb1 SMS Logs</div>\n      <div class="nav-item" onclick="goto(\'integrations\')">\xe2\x9a\x99\xef\xb8\x8f Integrations</div>\n    </nav>\n    <div id="sys-status"><b>System Status</b></div>\n    <button id="logout-btn" onclick="logout()">\xf0\x9f\x9a\xaa Logout</button>\n  </div>\n  <div id="main">\n    <div id="topbar"><h1 id="page-title">Dashboard</h1><div id="topbar-actions"></div></div>\n    <div id="content"></div>\n  </div>\n</div>\n\n<div id="toast"></div>\n\n<!-- MODALS -->\n<div class="modal-backdrop" id="company-modal">\n  <div class="modal">\n    <h3 id="co-modal-title">\xe2\x9e\x95 Add Company <span class="modal-close" onclick="closeModal(\'company-modal\')">\xe2\x9c\x95</span></h3>\n    <input type="hidden" id="co-id">\n    <div class="row"><div class="col">\n      <div class="form-group"><label>Company Name *</label><input id="co-name" placeholder="Stripe"></div>\n      <div class="form-group"><label>Industry</label><input id="co-industry" placeholder="FinTech"></div>\n      <div class="form-group"><label>Employees</label><input id="co-emp" type="number" placeholder="500"></div>\n      <div class="form-group"><label>Annual Revenue ($)</label><input id="co-rev" type="number" placeholder="10000000"></div>\n    </div><div class="col">\n      <div class="form-group"><label>Website</label><input id="co-web" placeholder="stripe.com"></div>\n      <div class="form-group"><label>City</label><input id="co-city" placeholder="San Francisco"></div>\n      <div class="form-group"><label>Country</label><input id="co-country" placeholder="USA"></div>\n      <div class="form-group"><label>Status</label>\n        <select id="co-status"><option value="prospect">Prospect</option><option value="qualified">Qualified</option><option value="opportunity">Opportunity</option><option value="cold">Cold</option><option value="lost">Lost</option></select>\n      </div>\n    </div></div>\n    <div class="form-group"><label>Technologies (comma-separated)</label><input id="co-tech" placeholder="Python, React, AWS"></div>\n    <div class="form-group"><label>Description</label><textarea id="co-desc" rows="2"></textarea></div>\n    <div class="form-group"><label>LinkedIn URL</label><input id="co-linkedin" placeholder="https://linkedin.com/company/stripe"></div>\n    <button class="btn btn-primary btn-full" onclick="saveCompany()">Save Company</button>\n  </div>\n</div>\n\n<div class="modal-backdrop" id="contact-modal">\n  <div class="modal">\n    <h3>\xf0\x9f\x91\xa4 Add Contact <span class="modal-close" onclick="closeModal(\'contact-modal\')">\xe2\x9c\x95</span></h3>\n    <input type="hidden" id="ct-company-id">\n    <div class="row"><div class="col">\n      <div class="form-group"><label>First Name *</label><input id="ct-fn" placeholder="Alex"></div>\n      <div class="form-group"><label>Last Name</label><input id="ct-ln" placeholder="Johnson"></div>\n      <div class="form-group"><label>Email</label><input id="ct-email" type="email" placeholder="alex@company.com"></div>\n      <div class="form-group"><label>Phone</label><input id="ct-phone" placeholder="+14155550100"></div>\n    </div><div class="col">\n      <div class="form-group"><label>Title</label><input id="ct-title" placeholder="VP of Engineering"></div>\n      <div class="form-group"><label>Department</label><input id="ct-dept" placeholder="Engineering"></div>\n      <div class="form-group"><label>Seniority</label>\n        <select id="ct-sen"><option value="individual">Individual</option><option value="manager">Manager</option><option value="director">Director</option><option value="vp">VP</option><option value="c_suite">C-Suite</option></select>\n      </div>\n      <div class="form-group" style="padding-top:20px"><label style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox" id="ct-dm"> Decision Maker</label></div>\n    </div></div>\n    <button class="btn btn-primary btn-full" onclick="saveContact()">Add Contact</button>\n  </div>\n</div>\n\n<div class="modal-backdrop" id="email-modal">\n  <div class="modal">\n    <h3>\xf0\x9f\x93\xa7 Generate AI Email <span class="modal-close" onclick="closeModal(\'email-modal\')">\xe2\x9c\x95</span></h3>\n    <input type="hidden" id="em-company-id">\n    <div class="form-group"><label>Email Type</label>\n      <select id="em-type"><option value="cold">Cold Outreach</option><option value="follow_up">Follow Up</option><option value="meeting_request">Meeting Request</option></select>\n    </div>\n    <div class="form-group"><label>Custom Instructions (optional)</label><textarea id="em-custom" placeholder="Focus on their recent funding round..."></textarea></div>\n    <button class="btn btn-primary btn-full" onclick="genEmail()">\xf0\x9f\xa4\x96 Generate Email</button>\n    <div id="email-preview" style="display:none;margin-top:16px">\n      <div class="form-group"><label>Subject</label><input id="em-subject"></div>\n      <div class="form-group"><label>Body</label><textarea id="em-body" rows="8"></textarea></div>\n      <div style="display:flex;gap:8px">\n        <button class="btn btn-success" onclick="sendGenEmail()">\xf0\x9f\x93\xa4 Send Now</button>\n        <button class="btn btn-ghost" onclick="saveDraftEmail()">\xf0\x9f\x92\xbe Save Draft</button>\n      </div>\n    </div>\n  </div>\n</div>\n\n<div class="modal-backdrop" id="meeting-modal">\n  <div class="modal">\n    <h3>\xf0\x9f\x93\x85 Schedule Meeting <span class="modal-close" onclick="closeModal(\'meeting-modal\')">\xe2\x9c\x95</span></h3>\n    <input type="hidden" id="mtg-company-id">\n    <div class="form-group"><label>Title *</label><input id="mtg-title" placeholder="Discovery Call"></div>\n    <div class="form-group"><label>Type</label>\n      <select id="mtg-type"><option value="discovery">Discovery</option><option value="demo">Demo</option><option value="follow_up">Follow Up</option><option value="negotiation">Negotiation</option></select>\n    </div>\n    <div class="row"><div class="col">\n      <div class="form-group"><label>Date</label><input id="mtg-date" type="date"></div>\n    </div><div class="col">\n      <div class="form-group"><label>Time (UTC)</label><input id="mtg-time" type="time" value="10:00"></div>\n    </div></div>\n    <div class="form-group"><label>Duration (minutes)</label><input id="mtg-dur" type="number" value="30"></div>\n    <div class="form-group"><label>Description</label><textarea id="mtg-desc" rows="2"></textarea></div>\n    <button class="btn btn-primary btn-full" onclick="saveMeeting()">\xf0\x9f\x93\x85 Schedule + Set SMS Reminders</button>\n  </div>\n</div>\n\n<div class="modal-backdrop" id="call-modal">\n  <div class="modal">\n    <h3>\xf0\x9f\x93\x9e Make AI Call <span class="modal-close" onclick="closeModal(\'call-modal\')">\xe2\x9c\x95</span></h3>\n    <input type="hidden" id="call-company-id">\n    <div class="form-group"><label>Phone Number *</label><input id="call-phone" placeholder="+14155550100"></div>\n    <div class="form-group"><label>Objective</label>\n      <select id="call-obj"><option value="qualify">Qualify</option><option value="demo">Book Demo</option><option value="follow_up">Follow Up</option><option value="close">Close</option><option value="feedback">Get Feedback</option></select>\n    </div>\n    <div class="form-group"><label>AI Voice</label>\n      <select id="call-voice"><option value="nat">Nat (default)</option><option value="tanya">Tanya</option><option value="ryan">Ryan</option><option value="evelyn">Evelyn</option></select>\n    </div>\n    <div class="form-group"><label>Custom Script (leave blank to AI-generate)</label><textarea id="call-script" rows="4"></textarea></div>\n    <button class="btn btn-primary btn-full" onclick="makeCall()">\xf0\x9f\x93\x9e Initiate AI Call</button>\n  </div>\n</div>\n\n<script>\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   STATE & CONSTANTS\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nconst BASE = \'/api\';\nlet TOKEN = localStorage.getItem(\'sales_token\') || \'\';\nlet USER  = JSON.parse(localStorage.getItem(\'sales_user\') || \'null\');\nlet PAGE  = \'dashboard\';\nlet _lastEmailId = null;\nlet _genEmailData = null;\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   API HELPER\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nasync function api(method, path, body=null, isFile=false) {\n  const opts = { method, headers: {} };\n  if (TOKEN) opts.headers[\'Authorization\'] = \'Bearer \' + TOKEN;\n  if (body && !isFile) { opts.headers[\'Content-Type\'] = \'application/json\'; opts.body = JSON.stringify(body); }\n  if (body && isFile) opts.body = body;\n  try {\n    const r = await fetch(BASE + path, opts);\n    const data = await r.json().catch(() => ({}));\n    if (r.status === 401) { logout(); return null; }\n    if (!r.ok || data.ok === false) {\n      toast(data.error || data.detail || \'Error \' + r.status, \'error\');\n      return null;\n    }\n    return data.data !== undefined ? data.data : data;\n  } catch(e) {\n    toast(\'Network error \xe2\x80\x94 is the server running?\', \'error\');\n    return null;\n  }\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   AUTH\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nasync function doLogin() {\n  const email = v(\'li-email\'), pass = v(\'li-pass\');\n  if (!email || !pass) return toast(\'Enter email and password\',\'error\');\n  const d = await api(\'POST\', \'/auth/login\', { email, password: pass });\n  if (!d) return;\n  TOKEN = d.token; USER = d.user;\n  localStorage.setItem(\'sales_token\', TOKEN);\n  localStorage.setItem(\'sales_user\', JSON.stringify(USER));\n  showApp();\n}\n\nasync function doRegister() {\n  const name=v(\'reg-name\'),email=v(\'reg-email\'),pass=v(\'reg-pass\'),role=v(\'reg-role\');\n  if (!name||!email||!pass) return toast(\'All fields required\',\'error\');\n  const d = await api(\'POST\', \'/auth/register\', {full_name:name,email,password:pass,role});\n  if (!d) return;\n  TOKEN = d.token; USER = d.user;\n  localStorage.setItem(\'sales_token\', TOKEN);\n  localStorage.setItem(\'sales_user\', JSON.stringify(USER));\n  closeModal(\'reg-modal\');\n  showApp();\n}\n\nfunction showRegister() { openModal(\'reg-modal\'); }\n\nfunction logout() {\n  TOKEN=\'\'; USER=null;\n  localStorage.removeItem(\'sales_token\');\n  localStorage.removeItem(\'sales_user\');\n  document.getElementById(\'app\').style.display=\'none\';\n  document.getElementById(\'login-screen\').style.display=\'flex\';\n}\n\nfunction showApp() {\n  document.getElementById(\'login-screen\').style.display=\'none\';\n  document.getElementById(\'app\').style.display=\'flex\';\n  document.getElementById(\'user-info\').textContent = USER ? `${USER.full_name} \xc2\xb7 ${USER.role}` : \'\';\n  loadStatus();\n  goto(\'dashboard\');\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   NAVIGATION\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nconst PAGE_TITLES = {dashboard:\'\xf0\x9f\x93\x8a Dashboard\',automation:\'\xf0\x9f\xa4\x96 Automation Pipeline\',companies:\'\xf0\x9f\x8f\xa2 Companies\',contacts:\'\xf0\x9f\x91\xa4 Contacts\',\n  emails:\'\xf0\x9f\x93\xa7 Emails\',meetings:\'\xf0\x9f\x93\x85 Meetings\',calls:\'\xf0\x9f\x93\x9e AI Calls\',analytics:\'\xf0\x9f\x93\x88 Analytics\',\n  chat:\'\xf0\x9f\x92\xac AI Chat\',sms:\'\xf0\x9f\x93\xb1 SMS Logs\',integrations:\'\xe2\x9a\x99\xef\xb8\x8f Integrations\'};\n\nfunction goto(page) {\n  PAGE = page;\n  document.querySelectorAll(\'.nav-item\').forEach((el,i) => {\n    const pages = [\'dashboard\',\'automation\',\'companies\',\'contacts\',\'emails\',\'meetings\',\'calls\',\'analytics\',\'chat\',\'sms\',\'integrations\'];\n    el.classList.toggle(\'active\', pages[i] === page);\n  });\n  document.getElementById(\'page-title\').textContent = PAGE_TITLES[page] || page;\n  document.getElementById(\'topbar-actions\').innerHTML = \'\';\n  const fn = PAGES[page];\n  if (fn) fn();\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   PAGES\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nconst PAGES = { dashboard, automation, companies, contacts, emails, meetings, calls, analytics, chat, sms, integrations };\n\nasync function automation() {\n  const st = await api(\'GET\',\'/automation/status\');\n  const feed = await api(\'GET\',\'/automation/activity-feed\');\n  if (!st) return;\n  const p = st.pipeline, em = st.emails, ca = st.calls, mt = st.meetings, sc = st.schedule_utc, int_ = st.integrations;\n\n  const intBadge = (ok,lbl) => ok\n    ? `<span style="color:var(--green);font-size:.8rem">\xf0\x9f\x9f\xa2 ${lbl}</span>`\n    : `<span style="color:var(--red);font-size:.8rem">\xf0\x9f\x94\xb4 ${lbl} \xe2\x80\x94 configure in Integrations</span>`;\n\n  const feedIcon = t => ({email:\'\xf0\x9f\x93\xa7\',call:\'\xf0\x9f\x93\x9e\',sms:\'\xf0\x9f\x93\xb1\',meeting:\'\xf0\x9f\x93\x85\'}[t]||\'\xe2\x9a\xa1\');\n  const statusColor = s => s===\'sent\'||s===\'completed\'||s===\'queued\'?\'var(--green)\':s===\'replied\'?\'#a78bfa\':s===\'opened\'?\'var(--accent)\':\'var(--muted)\';\n\n  set(\'content\',`\n    <!-- INTEGRATION STATUS BAR -->\n    <div style="background:#1e293b;border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:20px;display:flex;flex-wrap:wrap;gap:16px;align-items:center">\n      <b style="font-size:.85rem">Integration Status:</b>\n      ${intBadge(int_.groq,\'Groq AI\')}\n      ${intBadge(int_.gmail,\'Gmail\')}\n      ${intBadge(int_.bland,\'Bland AI\')}\n      ${intBadge(int_.twilio,\'Twilio SMS\')}\n      ${!int_.groq||!int_.gmail||!int_.bland||!int_.twilio\n        ? `<button class="btn btn-sm btn-primary" onclick="goto(\'integrations\')" style="margin-left:auto">\xe2\x9a\x99\xef\xb8\x8f Configure \xe2\x86\x92</button>`\n        : `<span style="color:var(--green);margin-left:auto;font-size:.85rem;font-weight:600">\xe2\x9c\x85 Fully Configured \xe2\x80\x94 Automation Running</span>`}\n    </div>\n\n    <!-- PIPELINE METRICS -->\n    <div class="metrics-grid" style="margin-bottom:20px">\n      ${metric(\'\xf0\x9f\x94\xa5 Hot Leads\',p.hot_leads,\'var(--hot)\')}\n      ${metric(\'\xf0\x9f\x9f\xa1 Warm Leads\',p.warm_leads,\'var(--warm)\')}\n      ${metric(\'\xe2\x9d\x84\xef\xb8\x8f Cold Leads\',p.cold_leads,\'var(--cold)\')}\n      ${metric(\'\xf0\x9f\x92\xb0 Pipeline Value\',\'$\'+fmtM(p.pipeline_value),\'var(--green)\')}\n      ${metric(\'\xf0\x9f\x93\xa7 Cold Emails\',em.cold_sent)}\n      ${metric(\'\xf0\x9f\x93\xac Opened\',em.opened+\' (\'+em.open_rate+\'%)\')}\n      ${metric(\'\xe2\x86\xa9\xef\xb8\x8f Replied\',em.replied+\' (\'+em.reply_rate+\'%)\')}\n      ${metric(\'\xf0\x9f\x93\x9e Calls\',ca.total+\' / \'+ca.completed+\' done\')}\n    </div>\n\n    <!-- ACTION BUTTONS -->\n    <div class="section" style="margin-bottom:20px">\n      <h3>\xe2\x9a\xa1 Manual Triggers</h3>\n      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:4px">\n        <button class="btn btn-primary" onclick="runAuto(\'run-now\',\'\xf0\x9f\x94\x84 Full Automation Cycle\')">\xf0\x9f\x94\x84 Run Full Cycle</button>\n        <button class="btn btn-ghost" onclick="runAuto(\'score-now\',\'\xf0\x9f\x8e\xaf Scoring All Leads\')">\xf0\x9f\x8e\xaf Score All Leads</button>\n        <button class="btn btn-ghost" onclick="runAuto(\'email-now\',\'\xf0\x9f\x93\xa7 Sending Cold Emails\')">\xf0\x9f\x93\xa7 Send Cold Emails</button>\n        <button class="btn btn-ghost" onclick="runAuto(\'followup-now\',\'\xf0\x9f\x94\x81 Sending Follow-ups\')">\xf0\x9f\x94\x81 Follow-ups</button>\n        <button class="btn btn-ghost" onclick="runAuto(\'call-now\',\'\xf0\x9f\x93\x9e Calling Hot Leads\')">\xf0\x9f\x93\x9e Auto Call Leads</button>\n      </div>\n    </div>\n\n    <div class="row">\n      <!-- AUTOMATION SCHEDULE -->\n      <div class="col section">\n        <h3>\xf0\x9f\x95\x90 Automatic Schedule (UTC)</h3>\n        <table style="width:100%">\n          <thead><tr><th>Time</th><th>Action</th><th>Condition</th></tr></thead>\n          <tbody>\n            <tr><td><b>${sc.score}</b></td><td>\xf0\x9f\x8e\xaf Score all leads</td><td>Daily</td></tr>\n            <tr><td><b>${sc.cold_email}</b></td><td>\xf0\x9f\x93\xa7 Cold email hot leads</td><td>Score \xe2\x89\xa5 70, no prior email</td></tr>\n            <tr><td><b>${sc.follow_up}</b></td><td>\xf0\x9f\x94\x81 Follow-up email</td><td>Sent 3+ days ago, no reply</td></tr>\n            <tr><td><b>${sc.auto_call}</b></td><td>\xf0\x9f\x93\x9e AI phone call</td><td>Score \xe2\x89\xa5 80, not yet called</td></tr>\n            <tr><td><b>${sc.daily_report}</b></td><td>\xf0\x9f\x93\x8a Daily SMS report</td><td>Every day</td></tr>\n            <tr><td><b>${sc.weekly_report}</b></td><td>\xf0\x9f\x93\x8b Weekly SMS report</td><td>Mondays only</td></tr>\n          </tbody>\n        </table>\n        <div style="margin-top:12px;padding:10px;background:#0f172a;border-radius:6px;font-size:.8rem;color:var(--muted)">\n          \xf0\x9f\x92\xa1 Automation runs hourly. Checks the UTC hour and triggers the right action.\n          All actions log to SMS Logs page.\n        </div>\n      </div>\n\n      <!-- EMAIL FUNNEL -->\n      <div class="col section">\n        <h3>\xf0\x9f\x93\xa7 Email Funnel</h3>\n        ${funnelBar(\'Cold Sent\',em.cold_sent,em.cold_sent,\'var(--accent)\')}\n        ${funnelBar(\'Opened\',em.opened,em.cold_sent,\'var(--warm)\')}\n        ${funnelBar(\'Replied\',em.replied,em.cold_sent,\'var(--green)\')}\n        ${funnelBar(\'Follow-ups\',em.followups,em.cold_sent,\'#a78bfa\')}\n        <hr class="divider">\n        <div style="display:flex;justify-content:space-between;font-size:.85rem">\n          <span>Open Rate</span><b style="color:var(--warm)">${em.open_rate}%</b>\n        </div>\n        <div style="display:flex;justify-content:space-between;font-size:.85rem;margin-top:4px">\n          <span>Reply Rate</span><b style="color:var(--green)">${em.reply_rate}%</b>\n        </div>\n        <hr class="divider">\n        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">\n          <button class="btn btn-sm btn-ghost" onclick="goto(\'emails\')">View All Emails \xe2\x86\x92</button>\n        </div>\n      </div>\n    </div>\n\n    <!-- LIVE ACTIVITY FEED -->\n    <div class="section">\n      <h3>\xe2\x9a\xa1 Live Activity Feed</h3>\n      <div style="display:flex;flex-direction:column;gap:6px;max-height:400px;overflow-y:auto">\n        ${(feed||[]).length ? (feed||[]).map(f=>`\n          <div style="display:flex;align-items:center;gap:12px;padding:8px 12px;background:#0f172a;border-radius:8px;border-left:3px solid ${statusColor(f.status)}">\n            <span style="font-size:1.1rem">${feedIcon(f.type)}</span>\n            <div style="flex:1">\n              <div style="font-size:.85rem;font-weight:600">${esc(f.title||f.type)}</div>\n              <div style="font-size:.75rem;color:var(--muted)">${esc(f.target||\'\')} ${f.subtype?\'\xc2\xb7 \'+f.subtype:\'\'}</div>\n            </div>\n            <span class="badge badge-${f.status||\'draft\'}" style="flex-shrink:0">${f.status||\'\xe2\x80\x94\'}</span>\n            <span style="font-size:.7rem;color:var(--muted);flex-shrink:0">${fmtDt(f.created_at)}</span>\n          </div>`).join(\'\')\n        : \'<div class="empty">No activity yet \xe2\x80\x94 run automation to populate</div>\'}\n      </div>\n      <button class="btn btn-sm btn-ghost" style="margin-top:12px" onclick="automation()">\xf0\x9f\x94\x84 Refresh Feed</button>\n    </div>\n\n    <!-- HOW IT WORKS -->\n    <div class="section">\n      <h3>\xf0\x9f\x94\x81 How Full Automation Works</h3>\n      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:4px">\n        ${[\'\xf0\x9f\x8e\xaf Score Leads<br><small>All companies scored hourly using revenue, headcount, industry, buying signals</small>\',\n           \'\xf0\x9f\x93\xa7 Cold Outreach<br><small>AI writes personalised cold email via Groq \xe2\x86\x92 sends via Gmail automatically</small>\',\n           \'\xf0\x9f\x94\x81 Follow-Up<br><small>Auto follow-up 3 days after no reply. Tracks opened vs unopened</small>\',\n           \'\xf0\x9f\x93\x9e AI Calls<br><small>Bland AI calls hot leads (score\xe2\x89\xa580) with AI-generated script. Records + transcribes</small>\',\n           \'\xf0\x9f\x93\xb1 SMS Updates<br><small>Twilio SMS alerts for every event: new leads, emails sent, calls, meetings</small>\',\n           \'\xf0\x9f\x93\x8a Reports<br><small>Daily + Weekly SMS report: hot/warm/cold counts, pipeline value, top leads</small>\'\n          ].map(s=>`<div style="padding:12px;background:#0f172a;border-radius:8px;font-size:.82rem;line-height:1.5">${s}</div>`).join(\'\')}\n      </div>\n    </div>\n  `);\n}\n\nfunction funnelBar(label, val, total, color) {\n  const pct = total > 0 ? Math.round((val/total)*100) : 0;\n  return `<div style="margin-bottom:10px">\n    <div style="display:flex;justify-content:space-between;font-size:.8rem;margin-bottom:3px">\n      <span>${label}</span><span>${val} (${pct}%)</span>\n    </div>\n    <div style="height:10px;background:#1e293b;border-radius:5px;overflow:hidden">\n      <div style="height:100%;width:${pct}%;background:${color};border-radius:5px;transition:width .8s cubic-bezier(.34,1.2,.64,1)"></div>\n    </div>\n  </div>`;\n}\n\nasync function runAuto(action, label) {\n  toast(`\xe2\x9a\xa1 ${label} started...`, \'info\');\n  const r = await api(\'POST\', `/automation/${action}`);\n  if (r) {\n    toast(`\xe2\x9c\x85 ${r.message}`, \'success\');\n    setTimeout(automation, 2000); // refresh page after 2s\n  }\n}\n\nasync function dashboard() {\n  const [sum, cos, mtgs] = await Promise.all([\n    api(\'GET\',\'/analytics/summary\'),\n    api(\'GET\',\'/companies?limit=5\'),\n    api(\'GET\',\'/meetings?status=scheduled\'),\n  ]);\n  if (!sum) return;\n\n  set(\'content\', `\n    <div class="metrics-grid">\n      ${metric(\'\xf0\x9f\x8f\xa2 Companies\',sum.total_companies)}\n      ${metric(\'\xf0\x9f\x94\xa5 Hot Leads\',sum.hot_leads,\'var(--hot)\')}\n      ${metric(\'\xf0\x9f\x9f\xa1 Warm Leads\',sum.warm_leads,\'var(--warm)\')}\n      ${metric(\'\xf0\x9f\x93\xa7 Emails Sent\',sum.emails_sent)}\n      ${metric(\'\xf0\x9f\x93\xad Open Rate\',sum.open_rate+\'%\')}\n      ${metric(\'\xf0\x9f\x93\x85 Meetings\',sum.meetings_scheduled,\'var(--green)\')}\n      ${metric(\'\xf0\x9f\x93\x9e Calls\',sum.total_calls)}\n      ${metric(\'\xf0\x9f\x92\xb0 Pipeline\',\'$\'+fmt(sum.revenue_pipeline),\'var(--green)\')}\n    </div>\n    <div class="row">\n      <div class="col tbl-wrap">\n        <div class="tbl-head"><h3>\xf0\x9f\x94\xa5 Top Hot Leads</h3><button class="btn btn-sm btn-ghost" onclick="goto(\'companies\')">View All \xe2\x86\x92</button></div>\n        <table><thead><tr><th>Company</th><th>Score</th><th>Industry</th><th>Status</th></tr></thead>\n        <tbody>${(cos||[]).map(c=>`<tr>\n          <td><b>${esc(c.name)}</b></td>\n          <td>${scoreBadge(c.lead_score)}</td>\n          <td>${esc(c.industry||\'\xe2\x80\x94\')}</td>\n          <td><span class="badge badge-${c.status}">${c.status}</span></td>\n        </tr>`).join(\'\')}</tbody></table>\n      </div>\n      <div class="col tbl-wrap">\n        <div class="tbl-head"><h3>\xf0\x9f\x93\x85 Upcoming Meetings</h3><button class="btn btn-sm btn-ghost" onclick="goto(\'meetings\')">View All \xe2\x86\x92</button></div>\n        <table><thead><tr><th>Title</th><th>Company</th><th>When</th></tr></thead>\n        <tbody>${(mtgs||[]).slice(0,6).map(m=>`<tr>\n          <td><b>${esc(m.title)}</b></td>\n          <td>${esc(m.company_name||\'\xe2\x80\x94\')}</td>\n          <td style="font-size:.8rem;color:var(--muted)">${fmtDt(m.scheduled_at)}</td>\n        </tr>`).join(\'\')}</tbody></table>\n      </div>\n    </div>\n  `);\n}\n\nasync function companies() {\n  document.getElementById(\'topbar-actions\').innerHTML =\n    \'<button class="btn btn-primary btn-sm" onclick="openCompanyModal()">\xe2\x9e\x95 Add Company</button>\';\n  await renderCompanies();\n}\n\nasync function renderCompanies(search=\'\',status=\'\') {\n  let url = `/companies?limit=200`;\n  if (search) url += `&search=${encodeURIComponent(search)}`;\n  if (status) url += `&status=${encodeURIComponent(status)}`;\n  const cos = await api(\'GET\', url);\n  if (!cos) return;\n\n  set(\'content\',`\n    <div class="search-bar">\n      <input id="co-search" placeholder="\xf0\x9f\x94\x8d Search companies..." onkeyup="debounce(()=>renderCompanies(v(\'co-search\'),v(\'co-sf\')),400)" value="${esc(search)}">\n      <select id="co-sf" onchange="renderCompanies(v(\'co-search\'),this.value)">\n        <option value="">All Statuses</option>\n        ${[\'prospect\',\'qualified\',\'opportunity\',\'cold\',\'lost\'].map(s=>`<option value="${s}" ${s===status?\'selected\':\'\'}>${s}</option>`).join(\'\')}\n      </select>\n      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">\n        <input type="file" id="csv-file" accept=".csv" style="display:none" onchange="uploadCSV(this)">\n        <button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'csv-file\').click()">\xf0\x9f\x93\xa4 Import CSV</button>\n      </label>\n    </div>\n    <div class="tbl-wrap">\n      <div class="tbl-head"><h3>${cos.length} Companies</h3></div>\n      <table><thead><tr><th>Company</th><th>Industry</th><th>Score</th><th>Status</th><th>Employees</th><th>Revenue</th><th>Actions</th></tr></thead>\n      <tbody>${cos.map(c=>`<tr>\n        <td><b>${esc(c.name)}</b>${c.ai_summary?`<div style="font-size:.75rem;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.ai_summary)}</div>`:\'\'}</td>\n        <td>${esc(c.industry||\'\xe2\x80\x94\')}</td>\n        <td>${scoreBadge(c.lead_score)}</td>\n        <td><span class="badge badge-${c.status||\'prospect\'}">${c.status||\'prospect\'}</span></td>\n        <td>${c.employee_count?fmt(c.employee_count):\'\xe2\x80\x94\'}</td>\n        <td>${c.annual_revenue?\'$\'+fmtM(c.annual_revenue):\'\xe2\x80\x94\'}</td>\n        <td style="white-space:nowrap">\n          <button class="btn btn-sm btn-ghost" onclick="viewCompany(${c.id})">View</button>\n          <button class="btn btn-sm btn-ghost" onclick="scoreCompany(${c.id})">Score</button>\n          <button class="btn btn-sm btn-ghost" onclick="openEmailModal(${c.id})">Email</button>\n          <button class="btn btn-sm btn-danger" onclick="deleteCompany(${c.id},\'${esc(c.name)}\')">Del</button>\n        </td>\n      </tr>`).join(\'\')}</tbody></table>\n    </div>\n  `);\n}\n\nasync function viewCompany(cid) {\n  const co = await api(\'GET\', `/companies/${cid}`);\n  if (!co) return;\n\n  const techStr = Array.isArray(co.technologies) ? co.technologies.join(\', \')\n    : (co.technologies ? JSON.parse(co.technologies).join(\', \') : \'\xe2\x80\x94\');\n\n  set(\'content\',`\n    <button class="btn btn-ghost btn-sm" onclick="goto(\'companies\')" style="margin-bottom:16px">\xe2\x86\x90 Back</button>\n    <div class="section">\n      <div class="row" style="align-items:flex-start">\n        <div class="col">\n          <h2 style="font-size:1.3rem;margin-bottom:8px">${esc(co.name)} ${scoreBadge(co.lead_score)}</h2>\n          <p style="color:var(--muted);font-size:.85rem;margin-bottom:12px">${esc(co.description||\'\')}</p>\n          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:.85rem">\n            <div>\xf0\x9f\x8f\xad <b>Industry:</b> ${esc(co.industry||\'\xe2\x80\x94\')}</div>\n            <div>\xf0\x9f\x93\x8d <b>Location:</b> ${esc((co.city||\'\')+\' \'+(co.country||\'\'))}</div>\n            <div>\xf0\x9f\x91\xa5 <b>Employees:</b> ${co.employee_count?fmt(co.employee_count):\'\xe2\x80\x94\'}</div>\n            <div>\xf0\x9f\x92\xb0 <b>Revenue:</b> ${co.annual_revenue?\'$\'+fmtM(co.annual_revenue):\'\xe2\x80\x94\'}</div>\n            <div>\xf0\x9f\x8c\x90 <b>Website:</b> ${co.website?`<a href="https://${co.website}" target="_blank" style="color:var(--accent)">${co.website}</a>`:\'\xe2\x80\x94\'}</div>\n            <div>\xf0\x9f\x93\x8a <b>Status:</b> <span class="badge badge-${co.status||\'prospect\'}">${co.status||\'prospect\'}</span></div>\n            <div>\xf0\x9f\x92\xbb <b>Tech:</b> ${techStr}</div>\n          </div>\n          ${co.ai_summary?`<div style="margin-top:12px;padding:10px;background:#0f172a;border-radius:6px;font-size:.85rem">\xf0\x9f\xa4\x96 ${esc(co.ai_summary)}</div>`:\'\'}\n        </div>\n        <div style="display:flex;flex-direction:column;gap:8px;min-width:160px">\n          <button class="btn btn-primary btn-sm" onclick="scoreCompany(${cid},true)">\xf0\x9f\x8e\xaf Score Lead</button>\n          <button class="btn btn-ghost btn-sm" onclick="aiSummary(${cid})">\xf0\x9f\xa4\x96 AI Summary</button>\n          <button class="btn btn-ghost btn-sm" onclick="analyzeSignals(${cid})">\xf0\x9f\x93\xa1 Buying Signals</button>\n          <button class="btn btn-ghost btn-sm" onclick="openEmailModal(${cid})">\xf0\x9f\x93\xa7 Generate Email</button>\n          <button class="btn btn-ghost btn-sm" onclick="openMeetingModal(${cid})">\xf0\x9f\x93\x85 Schedule Meeting</button>\n          <button class="btn btn-ghost btn-sm" onclick="openCallModal(${cid})">\xf0\x9f\x93\x9e AI Call</button>\n          <button class="btn btn-ghost btn-sm" onclick="openContactModal(${cid})">\xf0\x9f\x91\xa4 Add Contact</button>\n          <button class="btn btn-ghost btn-sm" onclick="editCompanyModal(${cid})">\xe2\x9c\x8f\xef\xb8\x8f Edit</button>\n        </div>\n      </div>\n    </div>\n    ${companyTabs(co)}\n  `);\n}\n\nfunction companyTabs(co) {\n  const tabs = [\'Contacts\',\'Emails\',\'Meetings\',\'Calls\',\'Signals\',\'Scores\'];\n  return `\n    <div class="tabs">${tabs.map((t,i)=>`<div class="tab ${i===0?\'active\':\'\'}" onclick="switchTab(this,\'co-tab-${i}\')">${t}</div>`).join(\'\')}</div>\n    <div id="co-tab-0" class="tab-panel active">\n      ${co.contacts.length ? `<table><thead><tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th><th>Seniority</th><th>DM?</th></tr></thead>\n      <tbody>${co.contacts.map(c=>`<tr>\n        <td><b>${esc(c.first_name)} ${esc(c.last_name||\'\')}</b></td>\n        <td>${esc(c.title||\'\xe2\x80\x94\')}</td><td>${esc(c.email||\'\xe2\x80\x94\')}</td><td>${esc(c.phone||\'\xe2\x80\x94\')}</td>\n        <td>${c.seniority_level||\'\xe2\x80\x94\'}</td><td>${c.is_decision_maker?\'\xe2\x9c\x85\':\'\'}</td>\n      </tr>`).join(\'\')}</tbody></table>` : \'<div class="empty">No contacts \xe2\x80\x94 add one above</div>\'}\n    </div>\n    <div id="co-tab-1" class="tab-panel">\n      ${co.emails.length ? `<table><thead><tr><th>Subject</th><th>Type</th><th>Status</th><th>Recipient</th><th>Actions</th></tr></thead>\n      <tbody>${co.emails.map(e=>`<tr>\n        <td>${esc(e.subject)}</td>\n        <td>${(e.email_type||\'\').replace(/_/g,\' \')}</td>\n        <td><span class="badge badge-${e.status}">${e.status}</span></td>\n        <td>${esc(e.recipient_email||\'\')}</td>\n        <td>${e.status===\'draft\'?`<button class="btn btn-sm btn-success" onclick="sendEmail(${e.id})">Send</button>`:\'\xe2\x80\x94\'}</td>\n      </tr>`).join(\'\')}</tbody></table>` : \'<div class="empty">No emails yet</div>\'}\n    </div>\n    <div id="co-tab-2" class="tab-panel">\n      ${co.meetings.length ? `<table><thead><tr><th>Title</th><th>Type</th><th>When</th><th>Status</th><th>Actions</th></tr></thead>\n      <tbody>${co.meetings.map(m=>`<tr>\n        <td><b>${esc(m.title)}</b></td>\n        <td>${(m.meeting_type||\'\').replace(/_/g,\' \')}</td>\n        <td style="font-size:.8rem">${fmtDt(m.scheduled_at)}</td>\n        <td><span class="badge badge-${m.status||\'proposed\'}">${m.status||\'proposed\'}</span></td>\n        <td style="white-space:nowrap">\n          ${m.meeting_link?`<a href="${m.meeting_link}" target="_blank" class="btn btn-sm btn-ghost">\xf0\x9f\x93\xb9 Join</a>`:\'\'}\n          ${m.status!==\'completed\'?`<button class="btn btn-sm btn-success" onclick="completeMeeting(${m.id},${co.id})">Done</button>`:\'\'}\n          <button class="btn btn-sm btn-ghost" onclick="addToCalendar(${m.id},${co.id})">\xf0\x9f\x93\x85 Cal</button>\n        </td>\n      </tr>`).join(\'\')}</tbody></table>` : \'<div class="empty">No meetings yet</div>\'}\n    </div>\n    <div id="co-tab-3" class="tab-panel">\n      ${co.calls.length ? `<table><thead><tr><th>Phone</th><th>Objective</th><th>Status</th><th>Duration</th><th>Summary</th></tr></thead>\n      <tbody>${co.calls.map(c=>`<tr>\n        <td>${esc(c.phone_number)}</td>\n        <td>${c.objective||\'\xe2\x80\x94\'}</td>\n        <td><span class="badge badge-${c.status||\'queued\'}">${c.status||\'queued\'}</span></td>\n        <td>${c.duration_seconds?c.duration_seconds+\'s\':\'\xe2\x80\x94\'}</td>\n        <td style="font-size:.8rem;max-width:200px">${esc((c.summary||\'\').slice(0,80))}</td>\n      </tr>`).join(\'\')}</tbody></table>` : \'<div class="empty">No calls yet</div>\'}\n    </div>\n    <div id="co-tab-4" class="tab-panel">\n      ${co.buying_signals.length ? co.buying_signals.map(s=>`\n        <div style="padding:10px 0;border-bottom:1px solid var(--border)">\n          <b>${esc(s.signal_name)}</b>\n          <span class="signal-bar" style="margin-left:12px;color:var(--accent)">${\'\xe2\x96\x88\'.repeat(s.strength||5)}${\'\xe2\x96\x91\'.repeat(10-(s.strength||5))}</span>\n          <span style="font-size:.75rem;color:var(--muted);margin-left:6px">${s.strength}/10</span>\n          <div style="font-size:.8rem;color:var(--muted);margin-top:3px">${esc(s.signal_description||\'\')}</div>\n        </div>`).join(\'\')\n      : \'<div class="empty">Click "Buying Signals" to analyse</div>\'}\n    </div>\n    <div id="co-tab-5" class="tab-panel">\n      ${co.lead_score_details ? `\n        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">\n          ${[[\'Revenue\',co.lead_score_details.revenue_score],[\'Employees\',co.lead_score_details.employee_score],\n             [\'Industry\',co.lead_score_details.industry_score],[\'Buying Signals\',co.lead_score_details.buying_signal_score],\n             [\'Seniority\',co.lead_score_details.department_signal_score],[\'Email Activity\',co.lead_score_details.email_activity_score]\n            ].map(([lbl,val])=>`<div>\n            <div style="display:flex;justify-content:space-between;font-size:.8rem"><span>${lbl}</span><span>${val}/100</span></div>\n            <div class="score-bar-wrap"><div class="score-bar" style="width:${val}%"></div></div>\n          </div>`).join(\'\')}\n        </div>\n        <div style="margin-top:16px;text-align:center;font-size:1.1rem">\n          Total: <b style="color:var(--accent)">${co.lead_score_details.total_score}/100</b>\n          \xe2\x80\x94 Tier: <span class="badge badge-${co.lead_score_details.tier}">${co.lead_score_details.tier}</span>\n        </div>` : \'<div class="empty">Run "Score Lead" to see breakdown</div>\'}\n    </div>\n  `;\n}\n\nasync function contacts() {\n  const data = await api(\'GET\',\'/contacts\');\n  if (!data) return;\n  set(\'content\',`\n    <div class="tbl-wrap">\n      <div class="tbl-head"><h3>${data.length} Contacts</h3></div>\n      <table><thead><tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th><th>Dept</th><th>Seniority</th><th>DM</th></tr></thead>\n      <tbody>${data.map(c=>`<tr>\n        <td><b>${esc(c.first_name)} ${esc(c.last_name||\'\')}</b></td>\n        <td>${esc(c.title||\'\xe2\x80\x94\')}</td>\n        <td>${esc(c.email||\'\xe2\x80\x94\')}</td>\n        <td>${esc(c.phone||\'\xe2\x80\x94\')}</td>\n        <td>${esc(c.department||\'\xe2\x80\x94\')}</td>\n        <td>${c.seniority_level||\'\xe2\x80\x94\'}</td>\n        <td>${c.is_decision_maker?\'\xe2\x9c\x85\':\'\'}</td>\n      </tr>`).join(\'\')}</tbody></table>\n    </div>\n  `);\n}\n\nasync function emails() {\n  document.getElementById(\'topbar-actions\').innerHTML =\n    \'<button class="btn btn-primary btn-sm" onclick="openEmailModal()">\xf0\x9f\xa4\x96 Generate Email</button>\';\n  const [data,cos] = await Promise.all([api(\'GET\',\'/emails\'),api(\'GET\',\'/companies?limit=200\')]);\n  if (!data) return;\n  set(\'content\',`\n    <div class="tbl-wrap">\n      <div class="tbl-head">\n        <h3>${data.length} Emails</h3>\n        <div style="display:flex;gap:8px">\n          <select onchange="filterEmails(this.value,\'\')">\n            <option value="">All Statuses</option>\n            ${[\'draft\',\'sent\',\'opened\',\'replied\'].map(s=>`<option value="${s}">${s}</option>`).join(\'\')}\n          </select>\n        </div>\n      </div>\n      <table><thead><tr><th>Subject</th><th>Type</th><th>Status</th><th>Recipient</th><th>AI Model</th><th>Created</th><th>Actions</th></tr></thead>\n      <tbody>${data.map(e=>`<tr>\n        <td style="max-width:200px"><b>${esc(e.subject)}</b></td>\n        <td>${(e.email_type||\'\').replace(/_/g,\' \')}</td>\n        <td><span class="badge badge-${e.status}">${e.status}</span></td>\n        <td>${esc(e.recipient_email||\'\')}</td>\n        <td><span style="font-size:.75rem;color:var(--muted)">${e.ai_model_used||\'\xe2\x80\x94\'}</span></td>\n        <td style="font-size:.75rem;color:var(--muted)">${fmtDt(e.created_at)}</td>\n        <td>${e.status===\'draft\'?`<button class="btn btn-sm btn-success" onclick="sendEmail(${e.id})">\xf0\x9f\x93\xa4 Send</button>`:\'\xe2\x80\x94\'}</td>\n      </tr>`).join(\'\')}</tbody></table>\n    </div>\n  `);\n}\n\nasync function filterEmails(status) {\n  const url = status ? `/emails?status=${status}` : \'/emails\';\n  const data = await api(\'GET\', url);\n  if (!data) return;\n  document.querySelector(\'tbody\').innerHTML = data.map(e=>`<tr>\n    <td style="max-width:200px"><b>${esc(e.subject)}</b></td>\n    <td>${(e.email_type||\'\').replace(/_/g,\' \')}</td>\n    <td><span class="badge badge-${e.status}">${e.status}</span></td>\n    <td>${esc(e.recipient_email||\'\')}</td>\n    <td><span style="font-size:.75rem;color:var(--muted)">${e.ai_model_used||\'\xe2\x80\x94\'}</span></td>\n    <td style="font-size:.75rem;color:var(--muted)">${fmtDt(e.created_at)}</td>\n    <td>${e.status===\'draft\'?`<button class="btn btn-sm btn-success" onclick="sendEmail(${e.id})">\xf0\x9f\x93\xa4 Send</button>`:\'\xe2\x80\x94\'}</td>\n  </tr>`).join(\'\');\n}\n\nasync function meetings() {\n  document.getElementById(\'topbar-actions\').innerHTML =\n    \'<button class="btn btn-primary btn-sm" onclick="openMeetingModal()">\xe2\x9e\x95 Schedule Meeting</button>\';\n  const data = await api(\'GET\',\'/meetings\');\n  if (!data) return;\n  set(\'content\',`\n    <div class="tbl-wrap">\n      <div class="tbl-head"><h3>${data.length} Meetings</h3></div>\n      <table><thead><tr><th>Title</th><th>Company</th><th>Type</th><th>Scheduled</th><th>Duration</th><th>Status</th><th>Actions</th></tr></thead>\n      <tbody>${data.map(m=>`<tr>\n        <td><b>${esc(m.title)}</b></td>\n        <td>${esc(m.company_name||\'\xe2\x80\x94\')}</td>\n        <td>${(m.meeting_type||\'\').replace(/_/g,\' \')}</td>\n        <td>${fmtDt(m.scheduled_at)}</td>\n        <td>${m.duration_minutes||30}m</td>\n        <td><span class="badge badge-${m.status||\'proposed\'}">${m.status||\'proposed\'}</span></td>\n        <td style="white-space:nowrap">\n          ${m.meeting_link?`<a href="${m.meeting_link}" target="_blank" class="btn btn-sm btn-ghost">\xf0\x9f\x93\xb9</a>`:\'\'}\n          ${m.status!==\'completed\'?`<button class="btn btn-sm btn-success" onclick="completeMeeting(${m.id},${m.company_id})">\xe2\x9c\x85</button>`:\'\'}\n          <button class="btn btn-sm btn-ghost" onclick="addToCalendar(${m.id},${m.company_id})">\xf0\x9f\x93\x85</button>\n        </td>\n      </tr>`).join(\'\')}</tbody></table>\n    </div>\n  `);\n}\n\nasync function calls() {\n  document.getElementById(\'topbar-actions\').innerHTML =\n    \'<button class="btn btn-primary btn-sm" onclick="openCallModal()">\xf0\x9f\x93\x9e New AI Call</button>\';\n  const data = await api(\'GET\',\'/calls\');\n  if (!data) return;\n  set(\'content\',`\n    <div style="padding:12px;background:#1e3a5f;border-radius:8px;margin-bottom:16px;font-size:.85rem">\n      \xf0\x9f\x93\x9e <b>Bland AI Phone Calls</b> \xe2\x80\x94 The AI calls prospects and has real conversations. Requires BLAND_API_KEY in .env\n    </div>\n    <div class="tbl-wrap">\n      <div class="tbl-head"><h3>${data.length} Calls</h3></div>\n      <table><thead><tr><th>Company</th><th>Phone</th><th>Objective</th><th>Status</th><th>Duration</th><th>Summary</th><th>Actions</th></tr></thead>\n      <tbody>${data.map(c=>`<tr>\n        <td>${esc(c.company_name||\'\xe2\x80\x94\')}</td>\n        <td>${esc(c.phone_number)}</td>\n        <td>${c.objective||\'\xe2\x80\x94\'}</td>\n        <td><span class="badge badge-${c.status||\'queued\'}">${c.status||\'queued\'}</span></td>\n        <td>${c.duration_seconds?c.duration_seconds+\'s\':\'\xe2\x80\x94\'}</td>\n        <td style="font-size:.8rem;max-width:150px">${esc((c.summary||\'\').slice(0,60))}</td>\n        <td>\n          ${c.recording_url?`<a href="${c.recording_url}" target="_blank" class="btn btn-sm btn-ghost">\xf0\x9f\x8e\xa7</a>`:\'\'}\n          ${c.status===\'queued\'?`<button class="btn btn-sm btn-ghost" onclick="syncCall(${c.id})">\xf0\x9f\x94\x84</button>`:\'\'}\n        </td>\n      </tr>`).join(\'\')}</tbody></table>\n    </div>\n  `);\n}\n\nasync function analytics() {\n  const [sum,ea,ld,pipe] = await Promise.all([\n    api(\'GET\',\'/analytics/summary\'),\n    api(\'GET\',\'/analytics/email-activity\'),\n    api(\'GET\',\'/analytics/lead-distribution\'),\n    api(\'GET\',\'/analytics/pipeline\'),\n  ]);\n  if (!sum) return;\n  set(\'content\',`\n    <div class="metrics-grid">\n      ${metric(\'Total Companies\',sum.total_companies)}\n      ${metric(\'\xf0\x9f\x94\xa5 Hot Leads\',sum.hot_leads,\'var(--hot)\')}\n      ${metric(\'\xf0\x9f\x9f\xa1 Warm Leads\',sum.warm_leads,\'var(--warm)\')}\n      ${metric(\'\xe2\x9d\x84\xef\xb8\x8f Cold Leads\',sum.cold_leads,\'var(--cold)\')}\n      ${metric(\'Emails Sent\',sum.emails_sent)}\n      ${metric(\'Open Rate\',sum.open_rate+\'%\')}\n      ${metric(\'Reply Rate\',sum.reply_rate+\'%\')}\n      ${metric(\'\xf0\x9f\x92\xb0 Pipeline\',\'$\'+fmt(sum.revenue_pipeline),\'var(--green)\')}\n    </div>\n    <div class="row">\n      <div class="col section">\n        <h3>\xf0\x9f\x93\xa7 Email Activity</h3>\n        ${miniChart(ea||[],\'sent\',\'#3b82f6\')}\n      </div>\n      <div class="col section">\n        <h3>\xf0\x9f\x8f\xad Lead Distribution</h3>\n        <table><thead><tr><th>Industry</th><th>Count</th><th>Avg Score</th></tr></thead>\n        <tbody>${(ld||[]).map(r=>`<tr><td>${esc(r.industry)}</td><td>${r.count}</td><td>${r.avg_score}</td></tr>`).join(\'\')}</tbody></table>\n      </div>\n    </div>\n    <div class="section">\n      <h3>\xf0\x9f\x92\xb0 Revenue Pipeline</h3>\n      <table><thead><tr><th>Company</th><th>Score</th><th>Potential Revenue</th><th>Status</th></tr></thead>\n      <tbody>${(pipe||[]).map(c=>`<tr>\n        <td><b>${esc(c.name)}</b></td>\n        <td>${scoreBadge(c.lead_score)}</td>\n        <td style="color:var(--green)">$${fmt(c.potential_revenue)}</td>\n        <td><span class="badge badge-${c.status}">${c.status}</span></td>\n      </tr>`).join(\'\')}</tbody></table>\n    </div>\n  `);\n}\n\nasync function chat() {\n  const msgs = await api(\'GET\',\'/chat\');\n  set(\'content\',`\n    <div class="section" style="max-width:700px;margin:0 auto">\n      <div id="chat-msgs">${(msgs||[]).map(m=>\n        m.sender===\'user\'\n          ? `<div class="chat-user">\xf0\x9f\x91\xa4 ${esc(m.message)}</div>`\n          : `<div class="chat-bot">\xf0\x9f\xa4\x96 ${esc(m.message)}</div>`\n      ).join(\'\')}</div>\n      <div style="display:flex;gap:8px">\n        <input id="chat-input" placeholder="Ask anything\xe2\x80\xa6 try \'show leads\', \'analytics\', \'pipeline\'" onkeydown="if(event.key===\'Enter\')sendChat()" style="flex:1">\n        <button class="btn btn-primary" onclick="sendChat()">Send \xe2\x86\x92</button>\n        <button class="btn btn-ghost" onclick="clearChat()">\xf0\x9f\x97\x91</button>\n      </div>\n      <div style="margin-top:10px;font-size:.75rem;color:var(--muted)">\n        Commands: <code>show leads</code> \xc2\xb7 <code>analytics</code> \xc2\xb7 <code>pipeline</code> \xc2\xb7 <code>daily report</code> \xc2\xb7 <code>help</code>\n      </div>\n    </div>\n  `);\n  const el = document.getElementById(\'chat-msgs\');\n  if (el) el.scrollTop = el.scrollHeight;\n}\n\nasync function sendChat() {\n  const msg = (document.getElementById(\'chat-input\').value || \'\').trim();\n  if (!msg) return;\n  document.getElementById(\'chat-input\').value = \'\';\n  const box = document.getElementById(\'chat-msgs\');\n  if (box) box.innerHTML += `<div class="chat-user">\xf0\x9f\x91\xa4 ${esc(msg)}</div><div class="chat-bot" id="typing">\xf0\x9f\xa4\x96 ...</div>`;\n  if (box) box.scrollTop = box.scrollHeight;\n  const r = await api(\'POST\',\'/chat\',{message:msg});\n  const typing = document.getElementById(\'typing\');\n  if (typing && r) { typing.textContent = \'\xf0\x9f\xa4\x96 \' + r.reply; typing.id=\'\'; }\n  if (typing && !r) typing.textContent = \'\xf0\x9f\xa4\x96 Error \xe2\x80\x94 try again.\';\n  if (box) box.scrollTop = box.scrollHeight;\n}\n\nasync function clearChat() {\n  await api(\'DELETE\',\'/chat\');\n  chat();\n}\n\nasync function sms() {\n  const data = await api(\'GET\',\'/sms-logs?limit=100\');\n  if (!data) return;\n  set(\'content\',`\n    <div style="padding:12px;background:#1e3a5f;border-radius:8px;margin-bottom:16px;font-size:.85rem">\n      \xf0\x9f\x93\xb1 <b>Twilio SMS Notifications</b> \xe2\x80\x94 All 12 event types logged here. Set TWILIO_* variables in .env to enable.\n    </div>\n    <div class="tbl-wrap">\n      <div class="tbl-head"><h3>${data.length} SMS Notifications</h3></div>\n      <table><thead><tr><th>Event</th><th>To</th><th>Message Preview</th><th>Status</th><th>Time</th></tr></thead>\n      <tbody>${data.length ? data.map(l=>`<tr>\n        <td><b>${esc(l.event_type||\'\xe2\x80\x94\')}</b></td>\n        <td>${esc(l.to_number)}</td>\n        <td style="font-size:.8rem;max-width:250px">${esc((l.body||\'\').slice(0,80))}</td>\n        <td><span class="badge badge-${l.status===\'sent\'?\'sent\':\'error\'}">${l.status}</span></td>\n        <td style="font-size:.75rem;color:var(--muted)">${fmtDt(l.created_at)}</td>\n      </tr>`).join(\'\') : \'<tr><td colspan="5" class="empty">No SMS sent yet \xe2\x80\x94 configure Twilio in Integrations</td></tr>\'}</tbody></table>\n    </div>\n    <div class="section">\n      <h3>\xf0\x9f\x93\x8b SMS Event Types (12 total)</h3>\n      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:.85rem">\n        ${[[\'\xf0\x9f\x8f\xa2 company_added\',\'New company added\'],[\'\xf0\x9f\x94\xa5 hot_lead\',\'Lead score \xe2\x89\xa5 80\'],\n           [\'\xf0\x9f\x93\xa7 email_generated\',\'AI email created\'],[\'\xe2\x9c\x85 email_sent\',\'Email sent via Gmail\'],\n           [\'\xf0\x9f\x93\x85 meeting_scheduled\',\'Meeting created\'],[\'\xe2\x9c\x85 meeting_completed\',\'Meeting completed\'],\n           [\'\xf0\x9f\x93\x9e call_initiated\',\'Bland AI call\'],[\'\xf0\x9f\x93\xa4 csv_import\',\'CSV import done\'],\n           [\'\xf0\x9f\x93\x8a daily_report\',\'Daily at 6PM UTC\'],[\'\xe2\x8f\xb0 meeting_reminder_24h\',\'24h before\'],\n           [\'\xe2\x8f\xb0 meeting_reminder_1h\',\'1h before\'],[\'\xe2\x8f\xb0 meeting_reminder_10min\',\'10min before\']\n          ].map(([k,v])=>`<div style="padding:6px;background:#0f172a;border-radius:4px"><b>${k}</b> \xe2\x80\x94 ${v}</div>`).join(\'\')}\n      </div>\n    </div>\n  `);\n}\n\nasync function integrations() {\n  const st = await api(\'GET\',\'/integrations/status\');\n  if (!st) return;\n\n  const badge = (connected) =>\n    connected ? \'<span style="color:var(--green)">\xf0\x9f\x9f\xa2 Connected</span>\'\n              : \'<span style="color:var(--red)">\xf0\x9f\x94\xb4 Not configured</span>\';\n\n  set(\'content\',`\n    <div class="row">\n      <div class="col">\n        <div class="section">\n          <h3>\xf0\x9f\xa4\x96 Groq AI</h3>\n          <p>${badge(st.groq.connected)}</p>\n          <p style="font-size:.8rem;color:var(--muted);margin-top:6px">Model: ${st.groq.model}</p>\n          <code style="display:block;background:#0f172a;padding:8px;border-radius:4px;margin-top:8px;font-size:.75rem">GROQ_API_KEY=gsk_...</code>\n          <a href="https://console.groq.com" target="_blank" style="color:var(--accent);font-size:.8rem">Get free key \xe2\x86\x92</a>\n        </div>\n        <div class="section">\n          <h3>\xf0\x9f\x93\xa7 Gmail SMTP</h3>\n          <p>${badge(st.gmail.connected)}</p>\n          ${st.gmail.email?`<p style="font-size:.8rem;color:var(--muted)">${st.gmail.email}</p>`:\'\'}\n          <div style="display:flex;gap:8px;margin-top:10px">\n            <button class="btn btn-sm btn-ghost" onclick="testGmail()">\xf0\x9f\x94\x8d Test</button>\n            <button class="btn btn-sm btn-ghost" onclick="sendTestGmail()">\xf0\x9f\x93\xa7 Send Test</button>\n          </div>\n          <code style="display:block;background:#0f172a;padding:8px;border-radius:4px;margin-top:8px;font-size:.75rem">GMAIL_SENDER_EMAIL=you@gmail.com<br>GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx</code>\n          <a href="https://myaccount.google.com/apppasswords" target="_blank" style="color:var(--accent);font-size:.8rem">Create App Password \xe2\x86\x92</a>\n        </div>\n        <div class="section">\n          <h3>\xf0\x9f\x93\x9e Bland AI Calls</h3>\n          <p>${badge(st.bland_ai.connected)}</p>\n          <button class="btn btn-sm btn-ghost" onclick="testBland()" style="margin-top:10px">\xf0\x9f\x94\x8d Test</button>\n          <code style="display:block;background:#0f172a;padding:8px;border-radius:4px;margin-top:8px;font-size:.75rem">BLAND_API_KEY=org_...</code>\n          <a href="https://app.bland.ai" target="_blank" style="color:var(--accent);font-size:.8rem">Get key \xe2\x86\x92</a>\n        </div>\n      </div>\n      <div class="col">\n        <div class="section">\n          <h3>\xf0\x9f\x93\xb1 Twilio SMS (12 Events)</h3>\n          <p>${badge(st.twilio_sms.connected)}</p>\n          ${st.twilio_sms.from_number?`<p style="font-size:.8rem;color:var(--muted)">From: ${st.twilio_sms.from_number}</p>`:\'\'}\n          ${st.twilio_sms.admin_number?`<p style="font-size:.8rem;color:var(--muted)">Admin: ${st.twilio_sms.admin_number}</p>`:\'\'}\n          <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">\n            <input id="sms-test-num" placeholder="+14155550100" style="flex:1;min-width:150px">\n            <button class="btn btn-sm btn-ghost" onclick="testTwilio()">\xf0\x9f\x93\xb1 Test SMS</button>\n          </div>\n          <button class="btn btn-sm btn-ghost" onclick="sendDailyReport()" style="margin-top:8px;width:100%">\xf0\x9f\x93\x8a Send Daily Report Now</button>\n          <code style="display:block;background:#0f172a;padding:8px;border-radius:4px;margin-top:8px;font-size:.75rem">TWILIO_ACCOUNT_SID=ACxx<br>TWILIO_AUTH_TOKEN=xxx<br>TWILIO_FROM_NUMBER=+1xxx<br>TWILIO_ADMIN_NUMBER=+1xxx</code>\n          <a href="https://console.twilio.com" target="_blank" style="color:var(--accent);font-size:.8rem">Twilio Console \xe2\x86\x92</a>\n        </div>\n        <div class="section">\n          <h3>\xf0\x9f\x93\x85 Google Calendar</h3>\n          <p>${badge(st.google_calendar.connected)}</p>\n          ${!st.google_calendar.connected ? `\n            <button class="btn btn-sm btn-primary" onclick="connectGoogle()" style="margin-top:10px">\xf0\x9f\x94\x97 Connect Google Calendar</button>\n          ` : `\n            <div style="display:flex;gap:8px;margin-top:10px">\n              <button class="btn btn-sm btn-ghost" onclick="disconnectGoogle()">\xf0\x9f\x94\x8c Disconnect</button>\n            </div>\n          `}\n          <code style="display:block;background:#0f172a;padding:8px;border-radius:4px;margin-top:8px;font-size:.75rem">GOOGLE_CLIENT_ID=xxx<br>GOOGLE_CLIENT_SECRET=xxx</code>\n          <a href="https://console.cloud.google.com" target="_blank" style="color:var(--accent);font-size:.8rem">Google Cloud Console \xe2\x86\x92</a>\n        </div>\n      </div>\n    </div>\n    <div class="section" style="padding:14px">\n      <h3>\xf0\x9f\x93\x8b .env Quick Reference</h3>\n      <pre style="background:#0f172a;padding:12px;border-radius:6px;font-size:.75rem;overflow-x:auto">GROQ_API_KEY=gsk_...\nBLAND_API_KEY=org_...\nGMAIL_SENDER_EMAIL=you@gmail.com\nGMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx\nTWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\nTWILIO_AUTH_TOKEN=your_auth_token\nTWILIO_FROM_NUMBER=+14155551234\nTWILIO_ADMIN_NUMBER=+14155559999\nGOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com\nGOOGLE_CLIENT_SECRET=GOCSPX-xxx</pre>\n    </div>\n  `);\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   ACTIONS\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nasync function scoreCompany(cid, reload=false) {\n  const r = await api(\'POST\',`/companies/${cid}/score`);\n  if (!r) return;\n  toast(`Score: ${r.total_score}/100 \xe2\x80\x94 ${r.tier.toUpperCase()}${r.total_score>=80?\' \xf0\x9f\x94\xa5 Hot lead! SMS sent.\':\'\'}`, \'success\');\n  if (reload) viewCompany(cid);\n  else goto(\'companies\');\n}\n\nasync function aiSummary(cid) {\n  toast(\'Generating AI summary...\',\'info\');\n  const r = await api(\'POST\',`/companies/${cid}/ai-summary`);\n  if (r) { toast(\'Summary generated!\',\'success\'); viewCompany(cid); }\n}\n\nasync function analyzeSignals(cid) {\n  toast(\'Analysing buying signals...\',\'info\');\n  const r = await api(\'POST\',`/companies/${cid}/analyze-signals`);\n  if (r) { toast(`Found ${r.signals.length} signals!`,\'success\'); viewCompany(cid); }\n}\n\nasync function deleteCompany(cid, name) {\n  if (!confirm(`Delete ${name}? This cannot be undone.`)) return;\n  const r = await api(\'DELETE\',`/companies/${cid}`);\n  if (r) { toast(`${name} deleted`,\'success\'); goto(\'companies\'); }\n}\n\nasync function sendEmail(eid) {\n  const r = await api(\'POST\',`/emails/${eid}/send`);\n  if (!r) return;\n  const sr = r.send_result || {};\n  if (sr.status === \'sent\') toast(\'\xe2\x9c\x85 Email sent! SMS notification dispatched.\',\'success\');\n  else toast(sr.message || \'Check Gmail config in Integrations\',\'error\');\n  goto(\'emails\');\n}\n\nasync function completeMeeting(mid, cid) {\n  const r = await api(\'PUT\',`/meetings/${mid}`,{status:\'completed\'});\n  if (r) { toast(\'Meeting completed! SMS sent.\',\'success\'); if(cid) viewCompany(cid); else goto(\'meetings\'); }\n}\n\nasync function addToCalendar(mid, cid) {\n  toast(\'Adding to Google Calendar...\',\'info\');\n  const r = await api(\'POST\',`/meetings/${mid}/calendar`);\n  if (!r) return;\n  if (r.google_meet_link) toast(\'\xe2\x9c\x85 Calendar event created! Google Meet link added.\',\'success\');\n  else if (r.error) toast(r.error,\'error\');\n  if (cid) viewCompany(cid);\n}\n\nasync function syncCall(cid_) {\n  const r = await api(\'GET\',`/calls/${cid_}`);\n  if (r) { toast(`Call status: ${r.status}`,\'info\'); goto(\'calls\'); }\n}\n\nasync function uploadCSV(input) {\n  if (!input.files[0]) return;\n  const fd = new FormData();\n  fd.append(\'file\', input.files[0]);\n  const r = await api(\'POST\',\'/companies/upload-csv\',fd,true);\n  if (r) toast(`Import started \xe2\x80\x94 ${input.files[0].name}. SMS sent on completion.`,\'success\');\n  input.value = \'\';\n}\n\nasync function testGmail() {\n  const r = await api(\'POST\',\'/integrations/gmail/test\');\n  if (r) (r.success?toast:toastErr)(r.message);\n}\nasync function sendTestGmail() {\n  const r = await api(\'POST\',\'/integrations/gmail/send-test\');\n  if (r) toast(r.success?\'Test email sent!\':r.message, r.success?\'success\':\'error\');\n}\nasync function testBland() {\n  const r = await api(\'POST\',\'/integrations/bland/test\');\n  if (r) toast(r.message, r.success?\'success\':\'error\');\n}\nasync function testTwilio() {\n  const to = v(\'sms-test-num\');\n  if (!to) return toast(\'Enter a phone number\',\'error\');\n  const r = await api(\'POST\',\'/integrations/twilio/test\',{to_number:to});\n  if (r) toast(r.success?\'\xe2\x9c\x85 SMS sent!\':JSON.stringify(r.result), r.success?\'success\':\'error\');\n}\nasync function sendDailyReport() {\n  const r = await api(\'POST\',\'/integrations/twilio/daily-report\');\n  if (r) toast(\'\xf0\x9f\x93\x8a Daily report SMS sent!\',\'success\');\n}\nasync function connectGoogle() {\n  const r = await api(\'GET\',\'/integrations/google/auth-url\');\n  if (r && r.auth_url) {\n    const w = window.open(r.auth_url+\'&state=\'+(USER?USER.id:\'\'), \'_blank\', \'width=500,height=600\');\n    toast(\'Complete Google authorization in the popup, then refresh Integrations.\',\'info\');\n  } else toast(r?.message||\'GOOGLE_CLIENT_ID not set\',\'error\');\n}\nasync function disconnectGoogle() {\n  if (!confirm(\'Disconnect Google Calendar?\')) return;\n  const r = await api(\'POST\',\'/integrations/google/disconnect\');\n  if (r) { toast(\'Disconnected\',\'info\'); integrations(); }\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   MODALS\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nfunction openCompanyModal() {\n  [\'co-id\',\'co-name\',\'co-industry\',\'co-web\',\'co-city\',\'co-country\',\'co-desc\',\'co-tech\',\'co-linkedin\']\n    .forEach(id=>{ const el=document.getElementById(id); if(el) el.value=\'\'; });\n  document.getElementById(\'co-emp\').value=\'\';\n  document.getElementById(\'co-rev\').value=\'\';\n  document.getElementById(\'co-modal-title\').textContent=\'\xe2\x9e\x95 Add Company\';\n  openModal(\'company-modal\');\n}\n\nasync function editCompanyModal(cid) {\n  const co = await api(\'GET\',`/companies/${cid}`);\n  if (!co) return;\n  s(\'co-id\',cid); s(\'co-name\',co.name||\'\'); s(\'co-industry\',co.industry||\'\');\n  s(\'co-emp\',co.employee_count||\'\'); s(\'co-rev\',co.annual_revenue||\'\');\n  s(\'co-web\',co.website||\'\'); s(\'co-city\',co.city||\'\'); s(\'co-country\',co.country||\'\');\n  s(\'co-status\',co.status||\'prospect\'); s(\'co-desc\',co.description||\'\');\n  const techs = Array.isArray(co.technologies) ? co.technologies.join(\', \')\n    : (co.technologies||\'\');\n  s(\'co-tech\',techs); s(\'co-linkedin\',co.linkedin_url||\'\');\n  document.getElementById(\'co-modal-title\').textContent=\'\xe2\x9c\x8f\xef\xb8\x8f Edit Company\';\n  openModal(\'company-modal\');\n}\n\nasync function saveCompany() {\n  const name = v(\'co-name\').trim();\n  if (!name) return toast(\'Company name is required\',\'error\');\n  const cid = v(\'co-id\');\n  const payload = {\n    name, industry:v(\'co-industry\')||null,\n    employee_count:parseInt(v(\'co-emp\'))||null,\n    annual_revenue:parseInt(v(\'co-rev\'))||null,\n    website:v(\'co-web\')||null, city:v(\'co-city\')||null, country:v(\'co-country\')||null,\n    status:v(\'co-status\')||\'prospect\', description:v(\'co-desc\')||null,\n    technologies:v(\'co-tech\')||null, linkedin_url:v(\'co-linkedin\')||null,\n  };\n  let r;\n  if (cid) r = await api(\'PUT\',`/companies/${cid}`,payload);\n  else      r = await api(\'POST\',\'/companies\',payload);\n  if (!r) return;\n  closeModal(\'company-modal\');\n  toast(`${name} ${cid?\'updated\':\'added\'}! SMS notification sent.`,\'success\');\n  goto(\'companies\');\n}\n\nfunction openContactModal(companyId) {\n  document.getElementById(\'ct-company-id\').value = companyId;\n  [\'ct-fn\',\'ct-ln\',\'ct-email\',\'ct-phone\',\'ct-title\',\'ct-dept\'].forEach(id=>{\n    const el=document.getElementById(id); if(el) el.value=\'\';\n  });\n  document.getElementById(\'ct-dm\').checked = false;\n  openModal(\'contact-modal\');\n}\n\nasync function saveContact() {\n  const cid = v(\'ct-company-id\');\n  const fn  = v(\'ct-fn\').trim();\n  if (!fn || !cid) return toast(\'First name and company required\',\'error\');\n  const r = await api(\'POST\',\'/contacts\',{\n    company_id:parseInt(cid), first_name:fn, last_name:v(\'ct-ln\')||null,\n    email:v(\'ct-email\')||null, phone:v(\'ct-phone\')||null,\n    title:v(\'ct-title\')||null, department:v(\'ct-dept\')||null,\n    seniority_level:v(\'ct-sen\'), is_decision_maker:document.getElementById(\'ct-dm\').checked,\n  });\n  if (!r) return;\n  closeModal(\'contact-modal\');\n  toast(\'Contact added!\',\'success\');\n  viewCompany(parseInt(cid));\n}\n\nfunction openEmailModal(companyId) {\n  document.getElementById(\'em-company-id\').value = companyId||\'\';\n  document.getElementById(\'em-custom\').value=\'\';\n  document.getElementById(\'email-preview\').style.display=\'none\';\n  _genEmailData=null; _lastEmailId=null;\n  openModal(\'email-modal\');\n}\n\nasync function genEmail() {\n  const cid = parseInt(v(\'em-company-id\'));\n  if (!cid) { return toast(\'Select a company first\',\'error\'); }\n  toast(\'\xf0\x9f\xa4\x96 Generating email...\',\'info\');\n  const r = await api(\'POST\',\'/emails/generate\',{\n    company_id:cid, email_type:v(\'em-type\'), custom_instructions:v(\'em-custom\')});\n  if (!r) return;\n  _lastEmailId = r.id;\n  _genEmailData = r;\n  document.getElementById(\'em-subject\').value = r.subject;\n  document.getElementById(\'em-body\').value    = r.body;\n  document.getElementById(\'email-preview\').style.display=\'block\';\n  toast(\'Email generated! SMS notification sent.\',\'success\');\n}\n\nasync function sendGenEmail() {\n  if (!_lastEmailId) return toast(\'Generate an email first\',\'error\');\n  // Update with any edits first\n  await api(\'PUT\',`/emails/${_lastEmailId}`,{\n    subject:v(\'em-subject\'), body:v(\'em-body\')});\n  const r = await api(\'POST\',`/emails/${_lastEmailId}/send`);\n  if (!r) return;\n  const sr = r.send_result||{};\n  closeModal(\'email-modal\');\n  toast(sr.status===\'sent\'?\'\xe2\x9c\x85 Email sent! SMS dispatched.\':(sr.message||\'Check Gmail config\'), sr.status===\'sent\'?\'success\':\'error\');\n}\n\nasync function saveDraftEmail() {\n  if (!_lastEmailId) return;\n  await api(\'PUT\',`/emails/${_lastEmailId}`,{subject:v(\'em-subject\'),body:v(\'em-body\')});\n  closeModal(\'email-modal\');\n  toast(\'Draft saved!\',\'success\');\n}\n\nfunction openMeetingModal(companyId) {\n  document.getElementById(\'mtg-company-id\').value = companyId||\'\';\n  [\'mtg-title\',\'mtg-desc\'].forEach(id=>{const el=document.getElementById(id);if(el)el.value=\'\';});\n  document.getElementById(\'mtg-dur\').value=\'30\';\n  const today = new Date().toISOString().split(\'T\')[0];\n  document.getElementById(\'mtg-date\').value=today;\n  document.getElementById(\'mtg-time\').value=\'10:00\';\n  openModal(\'meeting-modal\');\n}\n\nasync function saveMeeting() {\n  const cid   = parseInt(v(\'mtg-company-id\'));\n  const title = v(\'mtg-title\').trim();\n  if (!title) return toast(\'Title required\',\'error\');\n  if (!cid)   return toast(\'Select a company first\',\'error\');\n  const scheduled_at = `${v(\'mtg-date\')} ${v(\'mtg-time\')}:00`;\n  const r = await api(\'POST\',\'/meetings\',{\n    company_id:cid, title, meeting_type:v(\'mtg-type\'),\n    description:v(\'mtg-desc\')||null,\n    scheduled_at, duration_minutes:parseInt(v(\'mtg-dur\'))||30,\n  });\n  if (!r) return;\n  closeModal(\'meeting-modal\');\n  toast(\'Meeting scheduled! SMS + 3 reminders set (24h, 1h, 10min).\',\'success\');\n  if (cid) viewCompany(cid); else goto(\'meetings\');\n}\n\nfunction openCallModal(companyId) {\n  document.getElementById(\'call-company-id\').value = companyId||\'\';\n  document.getElementById(\'call-phone\').value=\'\';\n  document.getElementById(\'call-script\').value=\'\';\n  openModal(\'call-modal\');\n}\n\nasync function makeCall() {\n  const phone = v(\'call-phone\').trim();\n  if (!phone) return toast(\'Phone number required (+14155550100)\',\'error\');\n  const cid = parseInt(v(\'call-company-id\'))||null;\n  const r = await api(\'POST\',\'/calls/make\',{\n    company_id:cid, phone_number:phone,\n    objective:v(\'call-obj\'), voice:v(\'call-voice\'),\n    custom_task:v(\'call-script\')||null,\n  });\n  if (!r) return;\n  const br = r.bland_result||{};\n  closeModal(\'call-modal\');\n  if (br.status===\'queued\') toast(`Call queued! Bland ID: ${br.call_id||\'\'}. SMS sent.`,\'success\');\n  else toast(br.message||\'Call failed \xe2\x80\x94 check BLAND_API_KEY\',\'error\');\n  goto(\'calls\');\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   SYSTEM STATUS\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nasync function loadStatus() {\n  try {\n    const r = await fetch(\'/health\').then(r=>r.json());\n    const el = document.getElementById(\'sys-status\');\n    if (!el) return;\n    el.innerHTML = \'<b>System</b>\'\n      + status_dot(r.groq,\'Groq AI\')\n      + status_dot(r.twilio,\'Twilio SMS\')\n      + status_dot(r.bland,\'Bland AI\')\n      + status_dot(r.gmail,\'Gmail\');\n  } catch(e) {}\n}\nfunction status_dot(ok,lbl){\n  return `<div style="margin-top:3px">${ok?\'\xf0\x9f\x9f\xa2\':\'\xf0\x9f\x94\xb4\'} ${lbl}</div>`;\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   UTILITIES\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\nfunction set(id,html){ const el=document.getElementById(id); if(el) el.innerHTML=html; }\nfunction v(id){ const el=document.getElementById(id); return el?(el.value||\'\'):\'\'; }\nfunction s(id,val){ const el=document.getElementById(id); if(el) el.value=val; }\nfunction esc(s){ if(!s) return \'\'; return String(s).replace(/&/g,\'&amp;\').replace(/</g,\'&lt;\').replace(/>/g,\'&gt;\').replace(/"/g,\'&quot;\'); }\nfunction fmt(n){ return Number(n||0).toLocaleString(); }\nfunction fmtM(n){ if(!n) return \'0\'; if(n>=1e9) return (n/1e9).toFixed(1)+\'B\'; if(n>=1e6) return (n/1e6).toFixed(0)+\'M\'; if(n>=1e3) return (n/1e3).toFixed(0)+\'K\'; return n; }\nfunction fmtDt(s){ if(!s) return \'\xe2\x80\x94\'; return s.slice(0,16).replace(\'T\',\' \'); }\nfunction metric(lbl,val,color=\'var(--accent)\'){ return `<div class="metric-card"><div class="metric-val" style="color:${color}">${val}</div><div class="metric-lbl">${lbl}</div></div>`; }\nfunction scoreBadge(s){ const tier=s>=70?\'hot\':s>=40?\'warm\':\'cold\'; return `<span class="badge badge-${tier}">${s}/100</span>`; }\nfunction miniChart(data,key,color){\n  if(!data.length) return \'<div class="empty">No data</div>\';\n  const max=Math.max(...data.map(d=>d[key]||0),1);\n  return \'<div style="display:flex;align-items:flex-end;gap:2px;height:60px">\'\n    +data.slice(-20).map(d=>{const h=Math.round(((d[key]||0)/max)*60);\n      return `<div style="flex:1;height:${h}px;background:${color};border-radius:2px 2px 0 0;min-height:2px" title="${d.date}: ${d[key]}"></div>`;\n    }).join(\'\')+\'</div>\';\n}\n\nlet _debounceTimer;\nfunction debounce(fn,ms){ clearTimeout(_debounceTimer); _debounceTimer=setTimeout(fn,ms); }\n\nfunction openModal(id){ document.getElementById(id).classList.add(\'open\'); }\nfunction closeModal(id){ document.getElementById(id).classList.remove(\'open\'); }\ndocument.addEventListener(\'click\', e=>{ if(e.target.classList.contains(\'modal-backdrop\')) e.target.classList.remove(\'open\'); });\n\nfunction switchTab(el,panelId){\n  el.closest(\'.section,#content\').querySelectorAll(\'.tab\').forEach(t=>t.classList.remove(\'active\'));\n  el.closest(\'.section,#content\').querySelectorAll(\'.tab-panel\').forEach(p=>p.classList.remove(\'active\'));\n  el.classList.add(\'active\');\n  const panel=document.getElementById(panelId);\n  if(panel) panel.classList.add(\'active\');\n}\n\nfunction toast(msg,type=\'info\'){\n  const box=document.getElementById(\'toast\');\n  const el=document.createElement(\'div\');\n  el.className=`toast-msg toast-${type}`;\n  el.textContent=msg;\n  box.appendChild(el);\n  setTimeout(()=>el.remove(), type===\'error\'?5000:3000);\n}\n\n/* \xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\n   BOOT\n\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90 */\n(function init(){\n  if (TOKEN && USER) {\n    showApp();\n  } else {\n    document.getElementById(\'login-screen\').style.display=\'flex\';\n    document.getElementById(\'app\').style.display=\'none\';\n  }\n})();\n</script>\n</body>\n</html>\n'.decode("utf-8")

# ══ FLASK APP ═════════════════════════════════════════════════════════════════
app=Flask(__name__,static_folder=None)
app.secret_key=SECRET_KEY
app.register_blueprint(api)

@app.route("/")
@app.route("/favicon.ico")
@app.route("/<path:subpath>")
def index(subpath=None): return Response(_HTML,mimetype="text/html")

@app.get("/health")
def health():
    try: cos=len(q("SELECT id FROM companies")); db_ok=True
    except Exception: cos,db_ok=0,False
    return jsonify({"status":"healthy" if db_ok else "degraded","database":db_ok,"companies":cos,
        "groq":bool(GROQ_API_KEY),"twilio":bool(TWILIO_SID),"bland":bool(BLAND_API_KEY),"gmail":bool(GMAIL_EMAIL)})

@app.errorhandler(404)
def _404(e): return Response(_HTML,mimetype="text/html"),200
@app.errorhandler(405)
def _405(e): return jsonify({"ok":False,"error":"Method not allowed"}),405
@app.errorhandler(500)
def _500(e):
    import traceback
    logger.error(f"500: {e}\n{traceback.format_exc()}")
    return jsonify({"ok":False,"error":f"Server error: {e}"}),500

# Startup at module import — works with gunicorn --preload
try: init_db(); logger.info("✅ DB ready")
except Exception as _e: logger.error(f"DB:{_e}")
try: start(); logger.info("✅ Scheduler ready")
except Exception as _e: logger.warning(f"Sched:{_e}")

if __name__=="__main__":
    _port=int(os.environ.get("PORT",5000))
    logger.info(f"http://localhost:{_port} | admin@salesai.com / Admin@123456")
    app.run(host="0.0.0.0",port=_port,debug=False,use_reloader=False)
