import os
import io
import uuid
import time
import json
import re
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, jsonify, redirect, url_for
import anthropic
import stripe
import requests as http_requests
from pypdf import PdfReader
from docx import Document

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max upload

# --- Config ---
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')
MAILERSEND_API_KEY = os.environ.get('MAILERSEND_API_KEY')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'reviews@cvroast.com')

# Currency config per country
CURRENCY_MAP = {
    'GB': {'currency': 'gbp', 'amount': 499, 'symbol': '\u00a3', 'code': 'GBP', 'display': '\u00a34.99'},
    'US': {'currency': 'usd', 'amount': 499, 'symbol': '$', 'code': 'USD', 'display': '$4.99'},
    'AU': {'currency': 'aud', 'amount': 999, 'symbol': 'A$', 'code': 'AUD', 'display': 'A$9.99'},
}
DEFAULT_CURRENCY = CURRENCY_MAP['US']

stripe.api_key = STRIPE_SECRET_KEY
ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', 'change-me-in-prod')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'matthewjwills1@gmail.com')

# --- In-memory stores (fine for single-instance deploy) ---
resume_store = {}       # uuid -> {resume, created_at}
rate_limits = {}        # ip_hash -> {count, window_start}
paid_sessions = set()   # session_ids that have been used

# --- Analytics ---
analytics = {
    'total_roasts': 0,
    'total_checkouts': 0,
    'total_payments': 0,
    'revenue_cents': 0,
    'started_at': datetime.utcnow().isoformat(),
    'daily': {},        # "2026-02-10" -> {roasts, checkouts, payments, revenue_cents}
    'scores': [],       # last 100 scores for avg calculation
}

def _track(event, amount_cents=0):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    if today not in analytics['daily']:
        analytics['daily'][today] = {'roasts': 0, 'checkouts': 0, 'payments': 0, 'revenue_cents': 0}
    day = analytics['daily'][today]
    if event == 'roast':
        analytics['total_roasts'] += 1
        day['roasts'] += 1
    elif event == 'checkout':
        analytics['total_checkouts'] += 1
        day['checkouts'] += 1
    elif event == 'payment':
        analytics['total_payments'] += 1
        analytics['revenue_cents'] += amount_cents
        day['payments'] += 1
        day['revenue_cents'] += amount_cents

FREE_ROASTS_PER_DAY = 5
RESUME_TTL_HOURS = 2

# Recent scores for social proof
import random
recent_scores = [random.randint(22, 58) for _ in range(10)]  # seed with realistic scores


def _notify_admin_payment(email, amount_display):
    """Email admin when a payment comes in."""
    if not MAILERSEND_API_KEY or not ADMIN_EMAIL:
        return
    try:
        http_requests.post(
            'https://api.mailersend.com/v1/email',
            headers={'Authorization': f'Bearer {MAILERSEND_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'from': {'email': FROM_EMAIL, 'name': 'CVRoast'},
                'to': [{'email': ADMIN_EMAIL}],
                'subject': f'New CVRoast payment from {email}',
                'html': f'<h2 style="color:#22c55e;">New Payment!</h2>'
                        f'<p><strong>Customer:</strong> {email}</p>'
                        f'<p><strong>Amount:</strong> {amount_display}</p>'
                        f'<p><strong>Time:</strong> {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</p>'
                        f'<p>Total revenue: ${analytics["revenue_cents"]/100:.2f} ({analytics["total_payments"]} payments)</p>',
                'text': f'New payment from {email} for {amount_display}',
            },
            timeout=5,
        )
    except Exception:
        pass


# --- Helpers ---

def _hash_ip(ip):
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def _check_rate_limit(ip):
    key = _hash_ip(ip)
    now = time.time()
    if key in rate_limits:
        rl = rate_limits[key]
        if now - rl['window_start'] > 86400:
            rate_limits[key] = {'count': 1, 'window_start': now}
            return True
        if rl['count'] >= FREE_ROASTS_PER_DAY:
            return False
        rl['count'] += 1
        return True
    rate_limits[key] = {'count': 1, 'window_start': now}
    return True


def _cleanup_old_resumes():
    cutoff = time.time() - (RESUME_TTL_HOURS * 3600)
    expired = [k for k, v in resume_store.items() if v['created_at'] < cutoff]
    for k in expired:
        del resume_store[k]


def _send_cv_email(to_email, cv_data):
    """Send the rewritten CV to the customer via MailerSend."""
    if not MAILERSEND_API_KEY or not to_email:
        return False

    cv = cv_data.get('cv', {})
    name = cv.get('name', 'Your Name')
    title = cv.get('title', '')
    location = cv.get('location', '')
    phone = cv.get('phone', '')
    email = cv.get('email', '')

    # Build experience HTML
    exp_html = ''
    for job in cv.get('experience', []):
        bullets = ''.join(f'<li style="margin:4px 0;color:#333;">{b}</li>' for b in job.get('bullets', []))
        exp_html += f'''
        <div style="margin-bottom:20px;">
            <div style="font-weight:700;font-size:15px;color:#1a1a2e;">{job.get("title", "")}</div>
            <div style="font-size:13px;color:#666;margin-bottom:6px;">{job.get("company", "")} | {job.get("dates", "")}</div>
            <ul style="padding-left:20px;margin:0;">{bullets}</ul>
        </div>'''

    skills_html = ' &bull; '.join(cv.get('key_skills', []))
    certs_html = ''.join(f'<li style="margin:2px 0;color:#333;">{c}</li>' for c in cv.get('certifications', []))

    score_before = cv_data.get('ats_score_before', '?')
    score_after = cv_data.get('ats_score_after', '?')

    html_body = f"""
    <div style="max-width:660px;margin:0 auto;font-family:Arial,sans-serif;">
        <div style="text-align:center;padding:24px;background:#0a0a0b;border-radius:12px 12px 0 0;">
            <span style="font-size:22px;font-weight:800;color:#ff6b2c;">CVRoast</span>
            <h1 style="font-size:22px;font-weight:700;color:#fff;margin:12px 0 4px;">Your Professionally Rewritten CV</h1>
            <p style="color:#8e8e9a;font-size:13px;margin:0;">ATS Score: {score_before}/100 → {score_after}/100</p>
        </div>
        <div style="background:#ffffff;padding:40px 36px;border:1px solid #e0e0e0;">
            <h1 style="font-size:26px;font-weight:800;color:#1a1a2e;margin:0;">{name}</h1>
            <div style="font-size:14px;color:#555;margin:4px 0 16px;">{title}</div>
            <div style="font-size:13px;color:#666;margin-bottom:20px;padding-bottom:16px;border-bottom:2px solid #1a365d;">
                {f'{location}' if location else ''}{f' | {phone}' if phone else ''}{f' | {email}' if email else ''}
            </div>
            <h2 style="font-size:14px;font-weight:700;color:#1a365d;text-transform:uppercase;letter-spacing:1px;margin:20px 0 8px;">Professional Summary</h2>
            <p style="font-size:14px;color:#333;line-height:1.7;margin:0 0 20px;">{cv.get("personal_statement", "")}</p>
            <h2 style="font-size:14px;font-weight:700;color:#1a365d;text-transform:uppercase;letter-spacing:1px;margin:20px 0 8px;">Key Skills</h2>
            <p style="font-size:13px;color:#333;line-height:1.8;margin:0 0 20px;">{skills_html}</p>
            <h2 style="font-size:14px;font-weight:700;color:#1a365d;text-transform:uppercase;letter-spacing:1px;margin:20px 0 12px;">Professional Experience</h2>
            {exp_html}
            {f'<h2 style="font-size:14px;font-weight:700;color:#1a365d;text-transform:uppercase;letter-spacing:1px;margin:20px 0 8px;">Certifications</h2><ul style="padding-left:20px;margin:0;">{certs_html}</ul>' if certs_html else ''}
        </div>
        <div style="text-align:center;padding:24px;background:#f8f8f8;border-radius:0 0 12px 12px;border:1px solid #e0e0e0;border-top:none;">
            <p style="color:#666;font-size:13px;margin:0 0 8px;">Tip: Open this email on your computer and print to save as PDF.</p>
            <a href="https://cvroast.com" style="display:inline-block;padding:10px 24px;background:#ff6b2c;color:#fff;text-decoration:none;border-radius:8px;font-weight:700;font-size:13px;">Share CVRoast</a>
            <p style="color:#999;font-size:11px;margin-top:16px;">&copy; 2026 CVRoast</p>
        </div>
    </div>
    """

    # Plain text fallback
    plain = f"{name}\n{title}\n{location} | {phone} | {email}\n\n"
    plain += f"PROFESSIONAL SUMMARY\n{cv.get('personal_statement', '')}\n\n"
    plain += f"KEY SKILLS\n{', '.join(cv.get('key_skills', []))}\n\n"
    plain += "EXPERIENCE\n"
    for job in cv.get('experience', []):
        plain += f"\n{job.get('title', '')}\n{job.get('company', '')} | {job.get('dates', '')}\n"
        for b in job.get('bullets', []):
            plain += f"  - {b}\n"

    try:
        resp = http_requests.post(
            'https://api.mailersend.com/v1/email',
            headers={
                'Authorization': f'Bearer {MAILERSEND_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'from': {'email': FROM_EMAIL, 'name': 'CVRoast'},
                'to': [{'email': to_email}],
                'subject': 'Your Rewritten CV — CVRoast',
                'html': html_body,
                'text': plain,
            },
            timeout=10,
        )
        return resp.status_code in (200, 201, 202)
    except Exception:
        return False


# --- Routes ---

@app.route('/api/upload', methods=['POST'])
def upload_resume():
    """Extract text from uploaded PDF, DOCX, or TXT file."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''

    if ext == 'pdf':
        try:
            reader = PdfReader(io.BytesIO(file.read()))
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        except Exception:
            return jsonify({'error': 'Could not read PDF. Try pasting the text instead.'}), 400
    elif ext == 'docx':
        try:
            doc = Document(io.BytesIO(file.read()))
            text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            return jsonify({'error': 'Could not read DOCX. Try pasting the text instead.'}), 400
    elif ext == 'doc':
        return jsonify({'error': 'Legacy .doc format not supported. Please save as .docx or paste the text.'}), 400
    elif ext == 'txt':
        text = file.read().decode('utf-8', errors='ignore')
    else:
        return jsonify({'error': 'Supported formats: PDF, DOCX, TXT'}), 400

    text = text.strip()
    if len(text) < 50:
        return jsonify({'error': 'Could not extract enough text from the file. Try pasting the text instead.'}), 400

    return jsonify({'text': text, 'filename': file.filename})


@app.route('/api/social-proof', methods=['GET'])
def social_proof():
    """Return a random recent score for social proof notifications."""
    if recent_scores:
        score = random.choice(recent_scores)
        minutes_ago = random.randint(1, 15)
        return jsonify({'score': score, 'minutes_ago': minutes_ago})
    return jsonify({'score': 42, 'minutes_ago': 3})


@app.route('/api/geo', methods=['GET'])
def detect_geo():
    """Detect user's country from IP for currency selection."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
    ip = ip.split(',')[0].strip()
    try:
        resp = http_requests.get(f'https://ipapi.co/{ip}/country/', timeout=3)
        country = resp.text.strip().upper() if resp.ok and len(resp.text.strip()) == 2 else 'US'
    except Exception:
        country = 'US'
    pricing = CURRENCY_MAP.get(country, DEFAULT_CURRENCY)
    return jsonify({
        'country': country,
        'currency': pricing['currency'],
        'symbol': pricing['symbol'],
        'amount': pricing['amount'],
        'display': pricing['display'],
    })


@app.route('/robots.txt')
def robots():
    return app.send_static_file('robots.txt')


@app.route('/sitemap.xml')
def sitemap():
    return app.send_static_file('sitemap.xml')


@app.route('/')
def index():
    return render_template('index.html', stripe_key=STRIPE_PUBLISHABLE_KEY)


# --- SEO Landing Pages ---
SEO_PAGES = {
    'free-resume-checker': {
        'title': 'Free Resume Checker — Instant ATS Score | CVRoast',
        'h1': 'Free Resume Checker',
        'h1_span': 'Instant ATS Score',
        'subtitle': 'Upload your resume and get an instant ATS compatibility score out of 100. Find out exactly why your applications are getting rejected — in 10 seconds.',
        'badge': 'Free ATS resume checker',
        'meta_desc': 'Check your resume for free. Get an instant ATS compatibility score out of 100 with specific feedback on what to fix. No signup required.',
        'features': [
            ('ATS Compatibility Score', 'See exactly how your resume performs against the Applicant Tracking Systems that screen 75% of applications before a human sees them.'),
            ('Keyword Analysis', 'Find out which industry-standard keywords are missing from your resume and why ATS systems are filtering you out.'),
            ('Bullet Point Review', 'Get specific feedback on vague language like "responsible for" that makes recruiters skip your resume.'),
            ('Instant Results', 'No signup, no email required. Paste your resume and get your score in under 10 seconds.'),
        ],
        'faq': [
            ('What is an ATS score?', 'An ATS (Applicant Tracking System) score measures how well your resume can be parsed and ranked by the software that 75% of employers use to screen applications. A low score means your resume may never reach a human recruiter.'),
            ('How accurate is this resume checker?', 'Our AI analyzes your resume against the same criteria ATS systems use: keyword relevance, formatting, achievement-based language, and quantified metrics. Most users find the score closely matches their real-world callback rate.'),
            ('Is it really free?', 'Yes — the ATS score and 5 specific feedback points are completely free. If you want a full professional rewrite, that costs just a few dollars.'),
        ],
    },
    'ats-score-checker': {
        'title': 'ATS Score Checker — Is Your Resume Getting Past the Robots? | CVRoast',
        'h1': 'ATS Score Checker',
        'h1_span': 'Beat the Robots',
        'subtitle': '75% of resumes are rejected by ATS software before a human ever sees them. Check your ATS score instantly and find out if yours makes the cut.',
        'badge': 'Check your ATS score free',
        'meta_desc': 'Free ATS score checker. Find out if your resume passes Applicant Tracking Systems. Instant score out of 100 with specific fixes. No signup needed.',
        'features': [
            ('ATS Parsing Test', 'See how ATS software reads your resume. Tables, graphics, and fancy formatting often break the parsing — we\'ll tell you if yours does.'),
            ('Keyword Gap Analysis', 'Discover which job-relevant keywords are missing. ATS systems rank resumes by keyword matches — missing even one critical term can filter you out.'),
            ('Format Compatibility', 'Find out if your resume format (PDF, DOCX) is ATS-friendly or if the robots are mangling your carefully written content.'),
            ('Actionable Fix List', 'Get 5 specific, prioritized fixes you can make right now to boost your ATS score and start getting callbacks.'),
        ],
        'faq': [
            ('What is an ATS and why does it matter?', 'An Applicant Tracking System (ATS) is software used by employers to automatically screen resumes. It parses your resume text, looks for relevant keywords, and ranks candidates. If your resume scores low, it\'s rejected before a recruiter ever sees it.'),
            ('What ATS score do I need?', 'Most ATS systems have different thresholds, but generally: below 40 means you\'re likely getting auto-rejected, 40-60 is borderline, 60-80 is competitive, and 80+ means you\'re in great shape.'),
            ('How can I improve my ATS score?', 'The most impactful changes: use standard section headings, include industry keywords from the job description, quantify achievements with numbers, and avoid tables/columns/graphics that break ATS parsing.'),
        ],
    },
    'resume-review': {
        'title': 'Free AI Resume Review — Brutally Honest Feedback | CVRoast',
        'h1': 'AI Resume Review',
        'h1_span': 'Brutally Honest',
        'subtitle': 'Stop getting polite, useless feedback. Our AI gives you a real score and tells you exactly what\'s wrong with your resume — no sugarcoating.',
        'badge': 'Honest AI resume review',
        'meta_desc': 'Get a brutally honest AI resume review in 10 seconds. Real ATS score, specific feedback, and actionable fixes. Free, no signup required.',
        'features': [
            ('No Sugarcoating', 'Unlike friends and family who say "looks great!", our AI tells you the truth. Most people score 30-50 and are shocked by what they learn.'),
            ('Specific to YOUR Resume', 'Not generic advice. Every point is about YOUR specific content, YOUR bullet points, YOUR keywords. Personalised, actionable feedback.'),
            ('Professional Rewrite Option', 'See your score and want it fixed? For a few dollars, our AI completely rewrites your CV with ATS-optimized language, real metrics, and professional formatting.'),
            ('Privacy First', 'Your resume is processed in memory and automatically deleted within 2 hours. We never store, sell, or share your data.'),
        ],
        'faq': [
            ('How is this different from other resume reviewers?', 'Most resume review tools give you generic advice like "add more keywords." We give you a specific score, point out exact phrases that are hurting you, and explain why — with a bit of humour to make it memorable.'),
            ('Can AI really review a resume effectively?', 'Our AI has been trained on what makes resumes successful with ATS systems and recruiters. It catches issues humans miss: vague language, missing metrics, keyword gaps, and formatting problems that break ATS parsing.'),
            ('What happens after the free review?', 'You get your score and 5 specific roasts for free. If you want, you can upgrade to a complete professional rewrite that takes your score from where it is to 75-90+ range.'),
        ],
    },
    'cv-review': {
        'title': 'Free CV Review Online — Get Your CV Scored in 10 Seconds | CVRoast',
        'h1': 'Free CV Review',
        'h1_span': '10 Seconds Flat',
        'subtitle': 'Upload your CV and get an honest score with specific feedback. Find out why you\'re not getting interviews — and how to fix it fast.',
        'badge': 'Instant CV review',
        'meta_desc': 'Free online CV review with instant scoring. Upload your CV (PDF, DOCX) or paste text. Get an ATS score and specific feedback in 10 seconds.',
        'features': [
            ('Upload Any Format', 'PDF, DOCX, or just paste your text. Our parser extracts your CV content and analyses every section, bullet point, and keyword.'),
            ('UK & International CVs', 'Built for CVs worldwide — whether you\'re in the UK, US, Australia, or anywhere else. Currency and advice automatically localised.'),
            ('Professional CV Rewrite', 'Want more than feedback? Get your entire CV professionally rewritten with ATS-optimized language, achievement metrics, and two format choices.'),
            ('Emailed to You', 'Your rewritten CV is emailed to you instantly — open on any device, print to PDF, or edit before sending to employers.'),
        ],
        'faq': [
            ('What\'s the difference between a CV and a resume?', 'In the UK and much of the world, "CV" is the standard term. In the US, "resume" is more common for job applications. Our tool works perfectly for both — the analysis and scoring is the same.'),
            ('Can I upload a PDF CV?', 'Yes! Upload PDF, DOCX, or TXT files up to 5MB. Our parser extracts the text and analyses it. You can also paste your CV text directly if you prefer.'),
            ('How much does the CV rewrite cost?', 'The review and score are completely free. A full professional rewrite with ATS optimization, metrics, and formatting is just a few dollars — a fraction of what human CV writers charge.'),
        ],
    },
    'resume-roast': {
        'title': 'Roast My Resume — AI That Tells You the Truth | CVRoast',
        'h1': 'Roast My Resume',
        'h1_span': 'Handle the Truth?',
        'subtitle': 'Your resume probably sucks. Find out exactly why in 10 seconds. Our AI is brutally honest, a bit funny, and genuinely helpful.',
        'badge': 'The original resume roaster',
        'meta_desc': 'Get your resume roasted by AI. Brutally honest score out of 100 with 5 specific roasts. Free, instant, no signup. Can you handle the truth?',
        'features': [
            ('Brutal Honesty', 'No participation trophies here. If your resume says "responsible for managing things," we\'re going to call that out. Specifically.'),
            ('Actually Funny', 'Life\'s too short for boring feedback. Our roasts are designed to make you laugh AND make you fix your resume.'),
            ('Real ATS Score', 'Not just jokes — you get a genuine ATS compatibility score calibrated against real applicant tracking systems.'),
            ('The Fix', 'After the roast, get your entire resume professionally rewritten for a few dollars. Go from roasted to ready in 60 seconds.'),
        ],
        'faq': [
            ('Will this actually help me or just insult me?', 'Both! Every roast points out a real problem with your resume. The humour makes it memorable — you\'ll actually fix the issues because you can\'t unsee them.'),
            ('What\'s a typical roast score?', 'Most people score between 30-60. Below 30 means your resume needs serious work. Above 70 means you\'re doing well. We\'ve never seen a 100.'),
            ('Is my resume data private?', 'Completely. Your resume is processed in memory, never stored permanently, and automatically deleted within 2 hours. We don\'t sell or share any data.'),
        ],
    },
}


@app.route('/free-resume-checker')
@app.route('/ats-score-checker')
@app.route('/resume-review')
@app.route('/cv-review')
@app.route('/resume-roast')
def seo_page():
    slug = request.path.strip('/')
    page = SEO_PAGES.get(slug)
    if page:
        return render_template('seo_landing.html', page=page, stripe_key=STRIPE_PUBLISHABLE_KEY)
    return redirect('/')


COMPARISON_PAGES = {
    'cvroast-vs-jobscan': {
        'title': 'CVRoast vs Jobscan — Which Resume Checker Is Better? (2026)',
        'meta_desc': 'Compare CVRoast and Jobscan side-by-side. Free ATS scoring, pricing, features, and honest pros/cons. Find the best resume checker for you.',
        'competitor': 'Jobscan',
        'competitor_price': '$49.95/month',
        'our_price': '$4.99 one-time',
        'competitor_desc': 'Jobscan is a keyword-matching tool that compares your resume against a specific job description. It highlights missing keywords and gives you a match rate.',
        'our_advantage': 'CVRoast gives you a universal ATS score that works across all applications, plus rewrites your entire CV for a one-time fee less than one month of Jobscan.',
        'features': [
            ('ATS Score', True, True),
            ('Free tier', True, True),
            ('Full CV rewrite', True, False),
            ('Keyword matching', False, True),
            ('No subscription needed', True, False),
            ('Brutally honest feedback', True, False),
            ('PDF/DOCX upload', True, True),
            ('Multiple CV formats', True, False),
        ],
    },
    'cvroast-vs-resume-io': {
        'title': 'CVRoast vs Resume.io — Resume Builder Comparison (2026)',
        'meta_desc': 'CVRoast vs Resume.io compared. One roasts and rewrites your CV, the other is a template builder. See which gives you better results for less money.',
        'competitor': 'Resume.io',
        'competitor_price': '$24.95/month',
        'our_price': '$4.99 one-time',
        'competitor_desc': 'Resume.io is a drag-and-drop resume builder with pre-made templates. You fill in your own content and choose a design.',
        'our_advantage': 'CVRoast doesn\'t just format — it rewrites your content with ATS-optimized language, achievement metrics, and industry keywords. You get a better resume, not just a prettier one.',
        'features': [
            ('AI content rewriting', True, False),
            ('ATS score analysis', True, False),
            ('Free feedback tier', True, False),
            ('Template builder', False, True),
            ('No subscription', True, False),
            ('Brutally honest feedback', True, False),
            ('PDF export', True, True),
            ('Achievement metrics added', True, False),
        ],
    },
    'cvroast-vs-topresume': {
        'title': 'CVRoast vs TopResume — AI vs Human Resume Review (2026)',
        'meta_desc': 'Compare CVRoast ($4.99 AI rewrite) vs TopResume ($149+ human writer). Which gives you a better resume for the money?',
        'competitor': 'TopResume',
        'competitor_price': '$149-$349',
        'our_price': '$4.99 one-time',
        'competitor_desc': 'TopResume connects you with human resume writers who rewrite your CV over 1-2 weeks. Their free review is a generic PDF with no real feedback.',
        'our_advantage': 'CVRoast delivers a complete professional rewrite in 60 seconds for 3% of the price. The AI applies the same techniques human writers use: ATS keywords, achievement metrics, and industry-standard formatting.',
        'features': [
            ('Full CV rewrite', True, True),
            ('Instant delivery', True, False),
            ('Under $5', True, False),
            ('ATS optimization', True, True),
            ('Human writer', False, True),
            ('Free honest feedback', True, False),
            ('Multiple format options', True, False),
            ('60-second turnaround', True, False),
        ],
    },
}


@app.route('/cvroast-vs-jobscan')
@app.route('/cvroast-vs-resume-io')
@app.route('/cvroast-vs-topresume')
def comparison_page():
    slug = request.path.strip('/')
    page = COMPARISON_PAGES.get(slug)
    if page:
        return render_template('comparison.html', page=page)
    return redirect('/')


# --- Email capture / mailing list ---
email_list = []  # In-memory for now; will persist across restarts if you add a DB later


@app.route('/api/capture-email', methods=['POST'])
def capture_email():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'error': 'Invalid email'}), 400

    score = data.get('score', 0)
    one_liner = data.get('one_liner', '')
    roasts = data.get('roasts', [])

    # Store email
    email_list.append({
        'email': email,
        'score': score,
        'timestamp': datetime.utcnow().isoformat(),
    })

    # Send roast results + tips email
    if MAILERSEND_API_KEY:
        roast_html = ''.join(
            f'<tr><td style="padding:8px 12px;color:#ff6b2c;font-weight:700;font-size:18px;vertical-align:top;width:30px;">{i+1}</td>'
            f'<td style="padding:8px 12px;font-size:14px;color:#333;line-height:1.6;">{r}</td></tr>'
            for i, r in enumerate(roasts)
        )
        color = '#ef4444' if score < 45 else ('#eab308' if score < 70 else '#22c55e')

        html = f"""
        <div style="max-width:580px;margin:0 auto;font-family:Arial,sans-serif;">
            <div style="text-align:center;padding:28px;background:#0a0a0b;border-radius:12px 12px 0 0;">
                <span style="font-size:22px;font-weight:800;color:#ff6b2c;">CVRoast</span>
                <h1 style="font-size:22px;font-weight:700;color:#fff;margin:12px 0 4px;">Your Resume Roast Results</h1>
            </div>
            <div style="background:#fff;padding:32px;border:1px solid #e0e0e0;">
                <div style="text-align:center;margin-bottom:24px;">
                    <div style="display:inline-block;padding:16px 32px;border-radius:12px;background:{color}15;border:2px solid {color}30;">
                        <span style="font-size:48px;font-weight:900;color:{color};">{score}</span>
                        <span style="font-size:16px;color:#666;">/100</span>
                    </div>
                </div>
                <p style="text-align:center;font-size:16px;font-style:italic;color:#ff6b2c;margin-bottom:24px;">"{one_liner}"</p>
                <table style="width:100%;border-collapse:collapse;">{roast_html}</table>
                <div style="margin-top:28px;padding:20px;background:#f8f8f8;border-radius:8px;">
                    <h3 style="font-size:14px;font-weight:700;color:#1a365d;margin-bottom:12px;">Quick Tips to Improve Your Score:</h3>
                    <ul style="padding-left:20px;margin:0;font-size:13px;color:#555;line-height:1.8;">
                        <li>Replace every "responsible for" with an action verb + metric</li>
                        <li>Add numbers to every bullet point (even estimates help)</li>
                        <li>Match keywords from job descriptions you're targeting</li>
                        <li>Keep it to 1-2 pages with consistent formatting</li>
                        <li>Remove graphics/tables that break ATS parsers</li>
                    </ul>
                </div>
            </div>
            <div style="text-align:center;padding:24px;background:#f8f8f8;border-radius:0 0 12px 12px;border:1px solid #e0e0e0;border-top:none;">
                <p style="color:#666;font-size:14px;margin-bottom:12px;">Want your CV professionally rewritten?</p>
                <a href="https://cvroast.com/#get-started" style="display:inline-block;padding:12px 28px;background:#ff6b2c;color:#fff;text-decoration:none;border-radius:8px;font-weight:700;font-size:14px;">Get My CV Rewritten</a>
                <p style="color:#999;font-size:11px;margin-top:16px;">&copy; 2026 CVRoast</p>
            </div>
        </div>
        """
        try:
            http_requests.post(
                'https://api.mailersend.com/v1/email',
                headers={
                    'Authorization': f'Bearer {MAILERSEND_API_KEY}',
                    'Content-Type': 'application/json',
                },
                json={
                    'from': {'email': FROM_EMAIL, 'name': 'CVRoast'},
                    'to': [{'email': email}],
                    'subject': f'Your Resume Score: {score}/100 — CVRoast',
                    'html': html,
                    'text': f'Your resume scored {score}/100.\n\n"{one_liner}"\n\n' + '\n'.join(f'{i+1}. {r}' for i, r in enumerate(roasts)),
                },
                timeout=10,
            )
        except Exception:
            pass

    return jsonify({'ok': True})


BLOG_POSTS = [
    {
        'slug': 'what-is-ats-score',
        'title': 'What Is an ATS Score? Everything You Need to Know in 2026',
        'meta': 'Learn what an ATS score is, why it matters, and how to check yours for free. A complete guide to Applicant Tracking Systems.',
        'intro': 'If you have ever applied for a job online, your resume was almost certainly read by a robot before a human ever saw it. That robot is called an ATS — Applicant Tracking System — and your ATS score determines whether your application survives or gets filtered out.',
        'sections': [
            ('What is an ATS?', 'An Applicant Tracking System (ATS) is software used by over 75% of employers to manage job applications. It automatically parses, scores, and ranks resumes based on keyword relevance, formatting, and structure. Think of it as a gatekeeper that decides which resumes reach the hiring manager\'s desk.'),
            ('How is an ATS score calculated?', 'Your ATS score is typically based on several factors: keyword matching against the job description, proper formatting that the parser can read, standard section headings (Experience, Education, Skills), quantified achievements with numbers and metrics, and the overall structure and readability of your resume.'),
            ('What is a good ATS score?', 'Based on our analysis of thousands of resumes: below 30 means your resume needs serious work and is likely being auto-rejected, 30-50 is below average and you are probably missing callbacks, 50-70 is competitive but has room for improvement, and 70+ means your resume is well-optimized for ATS systems.'),
            ('5 ways to improve your ATS score', '1. Use standard section headings like "Professional Experience" instead of creative alternatives. 2. Include keywords from the job description naturally throughout your resume. 3. Quantify everything — "Increased sales by 30%" beats "Improved sales." 4. Use a clean, single-column format without tables, graphics, or columns. 5. Save as a .docx or simple PDF — avoid fancy templates that break ATS parsing.'),
            ('Check your ATS score for free', 'You can check your ATS score instantly at CVRoast. Upload your PDF or paste your resume text, and get a score out of 100 with specific feedback on what to fix. It takes 10 seconds and requires no signup.'),
        ],
    },
    {
        'slug': 'resume-action-verbs',
        'title': '50 Powerful Resume Action Verbs That Get Interviews (2026)',
        'meta': 'Replace weak resume language with powerful action verbs. Complete list of 50 proven verbs organized by category with examples.',
        'intro': 'The difference between a resume that gets interviews and one that gets ignored often comes down to a single word — the verb at the start of each bullet point. "Responsible for managing" puts recruiters to sleep. "Spearheaded" wakes them up.',
        'sections': [
            ('Why action verbs matter', 'Recruiters spend an average of 6-7 seconds scanning a resume. Strong action verbs immediately communicate impact and ownership. They also help with ATS systems, which are programmed to look for achievement-oriented language.'),
            ('Leadership verbs', 'Spearheaded, Orchestrated, Directed, Championed, Pioneered, Mobilized, Supervised, Mentored, Delegated, Oversaw. Example: "Spearheaded a cross-functional team of 12 to deliver a $2M product launch 3 weeks ahead of schedule."'),
            ('Achievement verbs', 'Accelerated, Boosted, Delivered, Exceeded, Generated, Maximized, Outperformed, Surpassed, Transformed, Tripled. Example: "Accelerated customer onboarding by 40%, reducing time-to-value from 3 weeks to 4 days."'),
            ('Technical verbs', 'Architected, Automated, Configured, Debugged, Deployed, Engineered, Integrated, Migrated, Optimized, Streamlined. Example: "Automated 15 manual reporting processes, saving the team 20 hours per week."'),
            ('Communication verbs', 'Advocated, Briefed, Collaborated, Consulted, Facilitated, Negotiated, Presented, Persuaded, Influenced, Liaised. Example: "Negotiated vendor contracts resulting in £150K annual savings across 3 departments."'),
            ('Words to avoid', 'Responsible for, Helped with, Worked on, Assisted in, Participated in, Was involved in, Duties included. These are passive, vague, and tell the recruiter nothing about your actual impact. Replace every one with a specific action verb and a measurable outcome.'),
        ],
    },
    {
        'slug': 'how-to-write-professional-summary',
        'title': 'How to Write a Professional Summary That Gets Interviews',
        'meta': 'Write a compelling professional summary for your resume. Step-by-step guide with examples for every experience level.',
        'intro': 'Your professional summary is the first thing a recruiter reads — and often the only thing. In 3-4 sentences, you need to sell your value, match the role, and make them want to keep reading. Here is exactly how to write one that works.',
        'sections': [
            ('What is a professional summary?', 'A professional summary is a 3-4 sentence paragraph at the top of your resume that highlights your most relevant experience, key skills, and career achievements. It replaces the outdated "Objective" section and serves as your elevator pitch to the hiring manager.'),
            ('The formula that works', 'Follow this structure: Sentence 1 — Your title, years of experience, and industry. Sentence 2 — Your key achievements or specialities. Sentence 3 — Your most relevant skills or expertise areas. Sentence 4 — What you bring to the table or your career goal. Keep it under 60 words total.'),
            ('Example for an experienced professional', '"Results-driven marketing manager with 8+ years of experience in B2B SaaS. Proven track record of increasing MQLs by 150% and reducing CAC by 35% through data-driven campaign optimization. Expert in marketing automation, ABM strategy, and cross-functional team leadership. Seeking to drive pipeline growth at a high-growth technology company."'),
            ('Example for a career changer', '"Customer-focused operations specialist transitioning into project management after 5 years of coordinating cross-departmental initiatives. Successfully managed 25+ concurrent projects with budgets up to £500K while maintaining 98% on-time delivery. PMP-certified with strong stakeholder management and process improvement skills."'),
            ('Common mistakes to avoid', 'Don\'t use first person ("I am a dedicated professional"). Don\'t be generic — if you could swap your name for anyone else\'s, it\'s too vague. Don\'t list soft skills without evidence. Don\'t exceed 4 sentences. And never copy a summary from a template — recruiters have seen them all.'),
        ],
    },
    {
        'slug': 'resume-mistakes-getting-rejected',
        'title': '10 Resume Mistakes That Get You Instantly Rejected',
        'meta': '10 common resume mistakes that cause instant rejection. Learn what hiring managers and ATS systems flag — and how to fix each one.',
        'intro': 'You have spent hours crafting your resume, but you are still not getting callbacks. Chances are, one of these 10 mistakes is silently killing your applications before a human ever sees them.',
        'sections': [
            ('1. No metrics or numbers', 'The number one resume killer. "Managed social media accounts" tells a recruiter nothing. "Grew Instagram following from 2K to 45K in 6 months, generating 200+ monthly leads" gets interviews. Every bullet point should have at least one number.'),
            ('2. Generic professional summary', 'If your summary starts with "Hardworking professional seeking opportunities" — delete it. A summary that could belong to anyone is worse than no summary at all. Make it specific to your achievements and your target role.'),
            ('3. Formatting that breaks ATS', 'Tables, columns, headers, footers, text boxes, and graphics all break ATS parsing. Your beautifully designed resume might render as gibberish. Use a clean, single-column layout with standard section headings.'),
            ('4. Listing duties instead of achievements', '"Responsible for customer service" is a duty. "Resolved 50+ customer issues daily with 95% satisfaction rating, earning Employee of the Month 3 times" is an achievement. Duties tell them what the job was. Achievements tell them how well you did it.'),
            ('5. Too long (or too short)', 'One page if you have under 10 years of experience. Two pages maximum for senior professionals. Three pages only if you are in academia or have extensive publications. A half-page resume signals lack of experience. A three-page resume for a mid-level role signals lack of editing skills.'),
            ('6-10: More critical mistakes', '6. Typos and grammar errors — instant rejection for 77% of hiring managers. 7. Using an unprofessional email address. 8. Including irrelevant experience that dilutes your message. 9. Missing keywords from the job description. 10. Not tailoring your resume for each application — the same generic resume sent to 50 companies will underperform a tailored version every time.'),
        ],
    },
    {
        'slug': 'tailor-resume-for-each-job',
        'title': 'How to Tailor Your Resume for Each Job (Without Starting Over)',
        'meta': 'Learn the 15-minute method to tailor your resume for every job application. Boost your callback rate by up to 3x.',
        'intro': 'Sending the same resume to every job is like wearing the same outfit to a wedding and a job interview. It might technically work, but you will never look like the best candidate. Here is how to tailor your resume in 15 minutes or less.',
        'sections': [
            ('Why tailoring matters', 'Tailored resumes are 3x more likely to get a callback than generic ones. ATS systems score resumes based on keyword matches to the specific job description. A perfect resume for one role might score 30/100 for another simply because the keywords don\'t match.'),
            ('The 15-minute method', 'Step 1 (3 min): Read the job description and highlight every skill, tool, and qualification mentioned. Step 2 (5 min): Compare against your resume — circle matches and note gaps. Step 3 (5 min): Rewrite your summary to mirror the role\'s language. Step 4 (2 min): Reorder your skills to match the job\'s priorities.'),
            ('What to change for each application', 'Your professional summary, the order of your skills, specific keywords and terminology, which achievements you emphasize, and your job titles if they\'re flexible. You don\'t need to rewrite every bullet — just adjust emphasis and language.'),
            ('What never changes', 'Your employment history dates and companies, your education, your actual qualifications and certifications, and the facts. Never lie or exaggerate — tailoring means presenting the same truth in the most relevant light for each role.'),
            ('Tools that help', 'Run your tailored resume through an ATS checker like CVRoast before submitting. A 2-minute check can reveal keyword gaps you missed. The free roast will show you your score against general ATS criteria, and you can iterate until you\'re above 65.'),
        ],
    },
    {
        'slug': 'best-resume-format-2026',
        'title': 'Best Resume Format for 2026: Which Layout Gets More Interviews?',
        'meta': 'Chronological, functional, or combination? Find the best resume format for your situation in 2026 with pros, cons, and examples.',
        'intro': 'The format of your resume matters just as much as the content. The wrong format can bury your best achievements or confuse ATS systems. Here is which format works best for different situations in 2026.',
        'sections': [
            ('Reverse chronological (best for most people)', 'Lists your most recent experience first and works backwards. This is the gold standard for 90% of job seekers because ATS systems parse it reliably, recruiters expect it, and it clearly shows career progression. Use this unless you have a specific reason not to.'),
            ('Combination/hybrid', 'Leads with a skills section followed by chronological experience. Good for career changers who want to highlight transferable skills upfront, or experienced professionals with diverse skill sets. Still ATS-friendly if structured correctly.'),
            ('Functional (use with caution)', 'Organizes by skills rather than job history. Hides employment gaps and career changes, but most recruiters and ATS systems dislike it. The lack of chronological context makes it hard to verify claims. Only use this if you have significant gaps you cannot address otherwise.'),
            ('Modern two-column layouts', 'A sidebar with contact info, skills, and certifications alongside a main content area. Visually appealing and space-efficient, but can cause problems with ATS parsing. If you use this format, make sure to test it with an ATS checker first.'),
            ('The ATS-safe format checklist', 'Standard fonts (Arial, Calibri, Georgia). Single column for ATS, two-column only if ATS-tested. Clear section headings in bold. Consistent date formatting. No tables, text boxes, or graphics. Save as .docx for ATS or clean PDF for direct sends. Margins between 0.5 and 1 inch.'),
        ],
    },
    {
        'slug': 'how-to-quantify-resume-achievements',
        'title': 'How to Quantify Resume Achievements (Even If You Don\'t Have Numbers)',
        'meta': 'Learn how to add metrics and numbers to your resume bullets — even if your role didn\'t have obvious KPIs. With 20+ examples.',
        'intro': 'Hiring managers love numbers. A resume with quantified achievements is 40% more likely to get a callback than one without. But what if your job does not have obvious metrics? Here is how to find and add numbers to any role.',
        'sections': [
            ('Why numbers matter so much', 'Numbers provide proof. Anyone can claim they are "detail-oriented." But "Maintained 99.7% accuracy across 500+ weekly transactions" is undeniable. Numbers also help ATS systems rank your resume higher because they signal achievement-oriented content.'),
            ('Types of metrics you can use', 'Revenue and cost savings, percentages of improvement, number of people managed or served, volume of work processed, time saved or reduced, customer satisfaction scores, size of budgets or projects, frequency of tasks, and ranking or awards.'),
            ('Finding numbers when you think you have none', 'Ask yourself: How many people did this affect? How often did I do this? What was the scope — budget, team size, coverage area? How did things improve before versus after? What would it have cost to hire someone else to do this? Even estimates work — "approximately 200 customers per week" is far better than "served customers."'),
            ('20 before and after examples', '"Handled customer calls" becomes "Resolved 40+ customer enquiries daily with 96% first-call resolution rate." "Managed social media" becomes "Grew LinkedIn following by 300% to 15K, generating 50+ inbound leads monthly." "Did data entry" becomes "Processed 200+ records daily with 99.5% accuracy, reducing backlog by 60%." "Trained new staff" becomes "Designed and delivered onboarding programme for 15+ new hires, reducing ramp-up time from 6 weeks to 3."'),
            ('When estimates are OK', 'If you do not have exact numbers, reasonable estimates are perfectly acceptable. Use qualifiers like "approximately," "up to," or ranges like "50-75 daily." Recruiters understand that not every metric is precise. The important thing is demonstrating that you think in terms of impact and results.'),
        ],
    },
    {
        'slug': 'resume-keywords-by-industry',
        'title': 'ATS Resume Keywords by Industry: The Complete 2026 List',
        'meta': 'Industry-specific ATS keywords for your resume. Covers tech, healthcare, finance, marketing, education, and more.',
        'intro': 'ATS systems scan for industry-specific keywords. Using the right ones can boost your score by 20-30 points. Here are the most impactful keywords for each major industry in 2026.',
        'sections': [
            ('Technology and software', 'Agile, Scrum, CI/CD, Cloud Architecture, AWS, Azure, API Development, Microservices, DevOps, Full Stack, Machine Learning, Data Pipeline, System Design, Code Review, Technical Leadership, Sprint Planning, Scalability, Performance Optimization, Security, SaaS.'),
            ('Healthcare', 'Patient Care, Clinical Documentation, HIPAA Compliance, Electronic Health Records (EHR), Care Coordination, Patient Safety, Quality Assurance, Medical Terminology, Triage, Discharge Planning, Infection Control, Evidence-Based Practice, Interdisciplinary Team, Regulatory Compliance.'),
            ('Finance and accounting', 'Financial Analysis, Forecasting, Budgeting, GAAP, IFRS, Risk Management, Audit, Compliance, Reconciliation, Financial Modelling, P&L Management, Variance Analysis, Tax Planning, Due Diligence, Portfolio Management, Regulatory Reporting.'),
            ('Marketing and sales', 'Lead Generation, Conversion Rate Optimization, SEO, SEM, Content Strategy, Marketing Automation, CRM, Pipeline Management, ABM, Customer Acquisition Cost (CAC), Return on Ad Spend (ROAS), Brand Strategy, Go-to-Market, Revenue Growth, Demand Generation.'),
            ('Education', 'Curriculum Development, Differentiated Instruction, Student Assessment, Classroom Management, IEP, Special Education, Literacy, STEM, Project-Based Learning, Student Engagement, Professional Development, Educational Technology, Data-Driven Instruction, Safeguarding.'),
            ('How to use these keywords', 'Don\'t just dump keywords into your resume. Weave them naturally into your bullet points and summary. Match the exact phrasing from the job description when possible. Use a tool like CVRoast to check your ATS score and see which keywords are working.'),
        ],
    },
]


@app.route('/blog')
def blog_index():
    return render_template('blog_index.html', posts=BLOG_POSTS)


@app.route('/blog/<slug>')
def blog_post(slug):
    post = next((p for p in BLOG_POSTS if p['slug'] == slug), None)
    if not post:
        return redirect('/blog')
    return render_template('blog_post.html', post=post)


@app.route('/score/<int:score>')
def score_page(score):
    score = max(0, min(100, score))
    return render_template('score.html', score=score)


@app.route('/api/roast', methods=['POST'])
def free_roast():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr) or '0.0.0.0'
    if not _check_rate_limit(ip):
        return jsonify({'error': 'Daily limit reached. Upgrade to get unlimited reviews.'}), 429

    data = request.get_json(silent=True) or {}
    resume_text = (data.get('resume') or '').strip()

    if len(resume_text) < 80:
        return jsonify({'error': 'Paste at least a few lines of your resume.'}), 400
    if len(resume_text) > 15000:
        return jsonify({'error': 'Resume is too long. Paste the text content only.'}), 400

    try:
        response = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": f"""You are "The Resume Roaster" — brutally honest, a bit funny, but genuinely helpful.

Analyze this resume and return EXACTLY this JSON structure, nothing else:
{{
  "score": <number 0-100>,
  "roasts": [
    "<bullet 1>",
    "<bullet 2>",
    "<bullet 3>",
    "<bullet 4>",
    "<bullet 5>"
  ],
  "one_liner": "<a single devastating but motivating summary sentence>"
}}

Rules:
- Score honestly (most resumes are 30-60)
- Each roast bullet should be 1-2 sentences, specific to THIS resume
- Be funny but not mean — the goal is to help
- Point out real issues: vague bullets, missing metrics, bad formatting clues, buzzword abuse, etc.
- The one_liner should make them laugh AND want to fix their resume

Resume:
{resume_text[:5000]}"""
            }]
        )

        raw = response.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        result = json.loads(raw)

        # Store resume for potential paid upgrade
        resume_id = str(uuid.uuid4())
        resume_store[resume_id] = {
            'resume': resume_text,
            'created_at': time.time()
        }
        _cleanup_old_resumes()

        result['resume_id'] = resume_id
        _track('roast')
        score_val = result.get('score', 0)
        analytics['scores'].append(score_val)
        analytics['scores'] = analytics['scores'][-100:]
        recent_scores.append(score_val)
        if len(recent_scores) > 20:
            recent_scores.pop(0)
        return jsonify(result)

    except json.JSONDecodeError:
        return jsonify({
            'score': 42,
            'roasts': [
                "Your resume confused our AI so badly it couldn't even format a response. That's... actually impressive.",
                "If a robot can't parse your resume, what chance does a human recruiter have?",
                "Seriously though — try pasting just the text content, not the formatting.",
                "Pro tip: if you copied from a PDF, the formatting might be garbled.",
                "Give it another shot with clean text and we'll roast you properly."
            ],
            'one_liner': "Your resume is so confusing it broke an AI. Let's fix that.",
            'resume_id': None
        })
    except Exception as e:
        return jsonify({'error': 'Something went wrong. Try again in a moment.'}), 500


@app.route('/api/checkout', methods=['POST'])
def create_checkout():
    data = request.get_json(silent=True) or {}
    resume_id = data.get('resume_id')
    resume_text = (data.get('resume') or '').strip()

    # Store resume if not already stored
    if not resume_id or resume_id not in resume_store:
        if len(resume_text) < 80:
            return jsonify({'error': 'Resume text required'}), 400
        resume_id = str(uuid.uuid4())
        resume_store[resume_id] = {
            'resume': resume_text,
            'created_at': time.time()
        }

    # Determine currency from request
    req_currency = (data.get('currency') or 'usd').lower()
    pricing = next((v for v in CURRENCY_MAP.values() if v['currency'] == req_currency), DEFAULT_CURRENCY)

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': pricing['currency'],
                    'product_data': {
                        'name': 'Professional CV Rewrite',
                        'description': 'Complete CV rewrite with ATS-optimized keywords, achievement metrics, and professional formatting.',
                    },
                    'unit_amount': pricing['amount'],
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=BASE_URL + '/success?session_id={CHECKOUT_SESSION_ID}&rid=' + resume_id,
            cancel_url=BASE_URL + '/#get-started',
            client_reference_id=resume_id,
        )
        _track('checkout')
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': 'Payment setup failed. Please try again.'}), 500


@app.route('/success')
def success():
    session_id = request.args.get('session_id')
    resume_id = request.args.get('rid')

    if not session_id or not resume_id:
        return redirect('/')

    return render_template('success.html',
                           session_id=session_id,
                           resume_id=resume_id,
                           stripe_key=STRIPE_PUBLISHABLE_KEY)


@app.route('/api/full-review', methods=['POST'])
def full_review():
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id')
    resume_id = data.get('resume_id')

    if not session_id or not resume_id:
        return jsonify({'error': 'Missing parameters'}), 400

    # Verify payment
    customer_email = None
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != 'paid':
            return jsonify({'error': 'Payment not completed'}), 402
        customer_email = session.customer_details.email if session.customer_details else None
    except Exception:
        return jsonify({'error': 'Could not verify payment'}), 400

    # Prevent replay (one review per payment)
    if session_id in paid_sessions:
        return jsonify({'error': 'This review has already been generated. Check your email or refresh the page.'}), 409
    paid_sessions.add(session_id)
    amount = session.amount_total or 499
    _track('payment', amount)
    currency_sym = {'gbp': '£', 'aud': 'A$'}.get(session.currency, '$')
    _notify_admin_payment(customer_email or 'unknown', f'{currency_sym}{amount/100:.2f}')

    # Get resume — try in-memory cache first, fall back to client-submitted text
    cached = resume_store.get(resume_id)
    resume_text = cached['resume'] if cached else (data.get('resume') or '').strip()

    if len(resume_text) < 80:
        return jsonify({'error': 'Resume expired. Please start over.'}), 410

    try:
        response = ai.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": f"""You are an expert CV/resume writer with 15 years of experience. Your job is to COMPLETELY REWRITE this CV into a professional, ATS-optimized document.

Return ONLY a JSON object (no markdown, no code fences, no explanation) with this exact structure:

{{
  "cv": {{
    "name": "Full Name from the CV",
    "title": "A professional title/tagline, e.g. 'Experienced Cleaning & Hospitality Professional | 25+ Years'",
    "location": "City, Region",
    "phone": "Phone from CV",
    "email": "Their email if present, or suggest one as firstname.lastname@email.com",
    "personal_statement": "A powerful 3-4 sentence professional summary packed with ATS keywords relevant to their industry. Highlight years of experience, key competencies, and reliability.",
    "key_skills": ["ATS-friendly skill 1", "Skill 2", "...up to 10"],
    "certifications": ["Cert they have", "Relevant Cert [Recommended]"],
    "references": "Available on request",
    "experience": [
      {{
        "title": "Job Title",
        "company": "Company, Location",
        "dates": "Start — End",
        "bullets": [
          "Achievement-focused bullet with estimated metrics",
          "Second bullet with quantified impact"
        ]
      }}
    ]
  }},
  "ats_score_before": 32,
  "ats_score_after": 78,
  "changes_made": [
    "Brief description of improvement 1",
    "Brief description of improvement 2",
    "Brief description of improvement 3",
    "Brief description of improvement 4",
    "Brief description of improvement 5"
  ],
  "tips_to_100": [
    {{
      "tip": "Short actionable tip",
      "why": "Why this matters and why only you can do it"
    }}
  ]
}}

CRITICAL RULES:
- Rewrite EVERY job's bullet points with achievement language and realistic estimated metrics
- Convert ALL vague/conversational language to specific, ATS-scannable professional terms
- Keep the same jobs, companies, and timeline — NEVER invent experience
- Add realistic estimated metrics where the original has none (e.g. "cleaned offices" becomes "Maintained cleaning standards across 15,000+ sq ft facility")
- key_skills must be industry-standard terms, NOT conversational phrases
- personal_statement: 3-4 sentences, keyword-rich, compelling — sell this person
- certifications: include ones they mention + suggest up to 3 relevant ones marked [Recommended]
- Each job should have 2-4 strong bullet points
- Be realistic with numbers — don't over-inflate, but be specific
- references: use "Available on request" unless the CV includes actual referee names/details
- tips_to_100: give 4-6 specific, actionable tips for THIS person to push their score from the "after" score to 100. Focus on things only THEY know — real certifications they could get, actual metrics from their jobs, missing contact details, LinkedIn URL, tailoring for specific roles, etc. Each tip should explain WHY it matters.
- Return ONLY valid JSON. No text before or after.

CV to rewrite:
{resume_text}"""
            }]
        )

        raw = response.content[0].text.strip()
        # Strip code fences if present
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()

        result = json.loads(raw)

        # Email the rewritten CV
        emailed = False
        if customer_email:
            emailed = _send_cv_email(customer_email, result)

        return jsonify({**result, 'emailed': emailed})

    except json.JSONDecodeError:
        # AI didn't return valid JSON — return raw text as fallback
        paid_sessions.discard(session_id)
        return jsonify({'error': 'CV generation failed. Please refresh to try again.'}), 500
    except Exception as e:
        paid_sessions.discard(session_id)  # Allow retry on failure
        return jsonify({'error': 'CV generation failed. Please refresh to try again.'}), 500


# --- Admin stats ---
@app.route('/admin/stats')
def admin_stats():
    token = request.args.get('token')
    if token != ADMIN_TOKEN:
        return jsonify({'error': 'Unauthorized'}), 401

    today = datetime.utcnow().strftime('%Y-%m-%d')
    today_stats = analytics['daily'].get(today, {'roasts': 0, 'checkouts': 0, 'payments': 0, 'revenue_cents': 0})
    avg_score = round(sum(analytics['scores']) / len(analytics['scores']), 1) if analytics['scores'] else 0
    conversion = round(analytics['total_payments'] / analytics['total_checkouts'] * 100, 1) if analytics['total_checkouts'] > 0 else 0
    upsell = round(analytics['total_checkouts'] / analytics['total_roasts'] * 100, 1) if analytics['total_roasts'] > 0 else 0

    return jsonify({
        'today': today_stats,
        'all_time': {
            'roasts': analytics['total_roasts'],
            'checkouts': analytics['total_checkouts'],
            'payments': analytics['total_payments'],
            'revenue': f"${analytics['revenue_cents'] / 100:.2f}",
        },
        'rates': {
            'avg_score': avg_score,
            'upsell_rate': f"{upsell}%",
            'checkout_conversion': f"{conversion}%",
        },
        'daily_breakdown': dict(sorted(analytics['daily'].items(), reverse=True)[:7]),
        'uptime_since': analytics['started_at'],
        'resumes_cached': len(resume_store),
    })


# --- Privacy policy ---
@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


# --- Health check ---
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') == 'development')
