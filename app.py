import os
import uuid
import time
import json
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, jsonify, redirect, url_for
import anthropic
import stripe
import requests as http_requests

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

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


def _send_review_email(to_email, review_markdown):
    """Send the full review to the customer via MailerSend."""
    if not MAILERSEND_API_KEY or not to_email:
        return False

    # Convert markdown to simple HTML for email
    review_html = review_markdown.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    review_html = (review_html
        .replace('## ', '<h2 style="color:#ff6b2c;font-size:20px;margin:28px 0 8px;border-bottom:1px solid #2a2a30;padding-bottom:8px;">')
        .replace('\n\n', '</p><p style="margin:0 0 12px;color:#e8e8ed;line-height:1.7;">')
        .replace('\n', '<br>')
        .replace('**', '<strong>', 1))
    # Close unclosed h2 tags (each heading ends at newline)
    import re
    review_html = re.sub(r'(<h2[^>]*>)([^<]+)', r'\1\2</h2>', review_html)
    review_html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', review_html)

    html_body = f"""
    <div style="max-width:640px;margin:0 auto;font-family:'Inter',Arial,sans-serif;background:#0a0a0b;color:#e8e8ed;padding:40px 32px;border-radius:12px;">
        <div style="text-align:center;margin-bottom:32px;">
            <span style="font-size:24px;font-weight:800;color:#ff6b2c;">CVRoast</span>
            <h1 style="font-size:28px;font-weight:800;margin:16px 0 8px;color:#e8e8ed;">Your Full Resume Review</h1>
            <p style="color:#8e8e9a;font-size:14px;">Here's your detailed analysis. Save this email for reference.</p>
        </div>
        <div style="background:#131316;border:1px solid #2a2a30;border-radius:12px;padding:32px;line-height:1.8;">
            <p style="margin:0 0 12px;color:#e8e8ed;line-height:1.7;">{review_html}</p>
        </div>
        <div style="text-align:center;margin-top:32px;padding-top:24px;border-top:1px solid #2a2a30;">
            <p style="color:#8e8e9a;font-size:13px;">Got a friend who needs a resume reality check?</p>
            <a href="https://cvroast.com" style="display:inline-block;margin-top:8px;padding:12px 28px;background:#ff6b2c;color:#fff;text-decoration:none;border-radius:8px;font-weight:700;font-size:14px;">Share CVRoast</a>
            <p style="color:#8e8e9a;font-size:11px;margin-top:20px;">&copy; 2026 CVRoast &middot; <a href="https://cvroast.com/privacy" style="color:#8e8e9a;">Privacy</a></p>
        </div>
    </div>
    """

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
                'subject': 'Your Full Resume Review — CVRoast',
                'html': html_body,
                'text': f"Your Full Resume Review\n\n{review_markdown}\n\n---\nGet another roast at https://cvroast.com",
            },
            timeout=10,
        )
        return resp.status_code in (200, 201, 202)
    except Exception:
        return False


# --- Routes ---

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
                        'name': 'Full Resume Rewrite & ATS Score',
                        'description': 'Detailed review, rewritten bullet points, ATS compatibility score, and personalized career tips.',
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
            max_tokens=3000,
            messages=[{
                "role": "user",
                "content": f"""You are an expert resume writer, career coach, and ATS (Applicant Tracking System) specialist with 15 years of experience.

Provide a comprehensive, premium resume review. Use this exact markdown structure:

## ATS Compatibility Score: XX/100

[2-3 sentences explaining the score — what's working and what's not for automated screening systems]

## Top 5 Issues Holding You Back

1. **[Issue]** — [Specific explanation with example from their resume]
2. **[Issue]** — [Specific explanation]
3. **[Issue]** — [Specific explanation]
4. **[Issue]** — [Specific explanation]
5. **[Issue]** — [Specific explanation]

## Rewritten Bullet Points

Take their 5 weakest bullet points and rewrite them using the STAR method with quantified impact. Show before → after for each:

**Before:** [their original bullet]
**After:** [your improved version with metrics]

(Repeat 5 times)

## Quick Wins (5-Minute Fixes)

Three specific changes they can make RIGHT NOW:
1. [Quick fix]
2. [Quick fix]
3. [Quick fix]

## Industry-Specific Tips

Based on their apparent industry/role, give 2 targeted recommendations that most generic advice misses.

---

Be specific to THIS resume. Reference their actual content. Be direct and actionable — they paid for this, give them real value.

Resume:
{resume_text}"""
            }]
        )

        review_text = response.content[0].text

        # Email the review to the customer
        emailed = False
        if customer_email:
            emailed = _send_review_email(customer_email, review_text)

        return jsonify({'review': review_text, 'emailed': emailed})

    except Exception as e:
        paid_sessions.discard(session_id)  # Allow retry on failure
        return jsonify({'error': 'Review generation failed. Please refresh to try again.'}), 500


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
