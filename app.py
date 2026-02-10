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
PRICE_CENTS = 499  # $4.99
MAILERSEND_API_KEY = os.environ.get('MAILERSEND_API_KEY')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'reviews@cvroast.com')

stripe.api_key = STRIPE_SECRET_KEY
ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', 'change-me-in-prod')

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


@app.route('/')
def index():
    return render_template('index.html', stripe_key=STRIPE_PUBLISHABLE_KEY)


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
        analytics['scores'].append(result.get('score', 0))
        analytics['scores'] = analytics['scores'][-100:]  # keep last 100
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

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': 'Professional CV Rewrite',
                        'description': 'Complete CV rewrite with ATS-optimized keywords, achievement metrics, and professional formatting.',
                    },
                    'unit_amount': PRICE_CENTS,
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
    _track('payment', PRICE_CENTS)

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
