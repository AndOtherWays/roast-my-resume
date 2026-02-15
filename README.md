<div align="center">

# CVRoast

### Your resume probably sucks. Find out why in 10 seconds.

**Free AI-powered resume roaster that scores your CV, roasts it with brutal honesty, and rewrites it into an interview-winning document.**

[![Live at cvroast.com](https://img.shields.io/badge/Try_it_free-cvroast.com-ff6b2c?style=for-the-badge&logoColor=white)](https://cvroast.com)

[![Flask](https://img.shields.io/badge/Flask-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Claude AI](https://img.shields.io/badge/Claude_AI-191919?style=flat-square&logo=anthropic&logoColor=white)](https://anthropic.com)
[![Stripe](https://img.shields.io/badge/Stripe-635BFF?style=flat-square&logo=stripe&logoColor=white)](https://stripe.com)
[![Railway](https://img.shields.io/badge/Railway-0B0D0E?style=flat-square&logo=railway&logoColor=white)](https://railway.com)
[![Python](https://img.shields.io/badge/Python_3.11-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

---

[**Try it now**](https://cvroast.com) | [Blog](https://cvroast.com/blog) | [Free ATS Checker](https://cvroast.com/free-resume-checker) | [Embed Widget](https://cvroast.com/embed)

</div>

---

## What is CVRoast?

[**CVRoast**](https://cvroast.com) is an AI resume reviewer that gives you a brutally honest ATS score out of 100 with 5 specific roasts about what is wrong with your resume. It is free, instant, and requires no signup.

If you want more than feedback, you can upgrade to a **full professional rewrite** for $4.99 -- your entire CV gets rewritten with ATS-optimized keywords, quantified achievements, and professional formatting, then emailed directly to you.

**75% of resumes are rejected by Applicant Tracking Systems before a human ever sees them.** CVRoast tells you exactly why yours is one of them.

## How It Works

```
1. Paste your resume (or upload PDF/DOCX)
2. Get roasted in ~10 seconds -- ATS score + 5 brutal but helpful critiques
3. (Optional) Pay $4.99 for a complete professional rewrite
4. Rewritten CV arrives in your inbox, ready to send to employers
```

### The Free Roast

Every user gets a score out of 100 and 5 specific, personalized roasts. Not generic advice -- real feedback about YOUR resume. The kind of feedback recruiters think but are too polite to say.

### The Paid Rewrite

For $4.99 (one-time, no subscription), Claude AI completely rewrites your CV:

- Achievement-focused bullet points with estimated metrics
- ATS-optimized keywords and professional formatting
- Before/after ATS score comparison
- Personalized tips to push your score even higher
- Emailed to you instantly in a clean, professional format

## Features

| Feature | Details |
|---|---|
| **Instant ATS Score** | Score out of 100 calibrated against real ATS systems |
| **5 Personalized Roasts** | Specific to your resume -- not generic advice |
| **File Upload** | PDF, DOCX, and TXT support (up to 5MB) |
| **Full CV Rewrite** | Complete professional rewrite for $4.99 |
| **Multi-Currency** | Auto-detects country -- supports GBP, USD, AUD |
| **Email Delivery** | Rewritten CV emailed in a clean HTML format |
| **Privacy First** | Resumes processed in memory, auto-deleted within 2 hours |
| **SEO Blog** | 8 in-depth articles on resume optimization |
| **Role-Specific Pages** | 20 industry-specific resume checker pages |
| **Competitor Comparisons** | 7 detailed comparison pages vs. Jobscan, Zety, TopResume, etc. |
| **Embeddable Widget** | Drop a "Check Your Resume" button on any website |
| **Social Proof** | Real-time score notifications from recent users |
| **Rate Limiting** | 5 free roasts per day per IP |

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.11 + Flask |
| **AI (Free Roast)** | Claude Haiku 4.5 -- fast, cheap, funny |
| **AI (Paid Rewrite)** | Claude Sonnet 4.5 -- thorough, professional |
| **Payments** | Stripe Checkout with multi-currency support |
| **Email** | MailerSend transactional emails |
| **File Parsing** | pypdf (PDF) + python-docx (DOCX) |
| **Hosting** | Railway (Gunicorn, 2 workers) |
| **Geo Detection** | ipapi.co for currency auto-selection |

## Architecture

```
                    +------------------+
                    |   cvroast.com    |
                    |  (Single Page)   |
                    +--------+---------+
                             |
                    +--------v---------+
                    |   Flask App      |
                    |                  |
                    |  /api/upload     |  PDF/DOCX parsing
                    |  /api/roast      |  Free AI roast (Haiku 4.5)
                    |  /api/checkout   |  Stripe session
                    |  /api/full-review|  Paid rewrite (Sonnet 4.5)
                    |                  |
                    +--+-----+-----+--+
                       |     |     |
              +--------+  +--+--+  +--------+
              |           |     |           |
         Anthropic     Stripe  MailerSend  ipapi
         Claude API    Payments  Email     Geo
```

The entire application runs as a single Flask process with in-memory storage. No database required. Resumes are stored temporarily (2-hour TTL) and automatically cleaned up. This keeps the architecture simple and the cold-start fast.

## Content Pages

CVRoast includes a full SEO content strategy built into the application:

- **5 SEO landing pages** -- free resume checker, ATS score checker, resume review, CV review, resume roast
- **8 blog posts** -- ATS scores, action verbs, professional summaries, common mistakes, and more
- **20 role-specific pages** -- nurses, software engineers, teachers, accountants, executives, and more
- **7 competitor comparison pages** -- vs. Jobscan, Resume.io, TopResume, Zety, Kickresume, Resume Worded, Enhancv
- **Embeddable widget** -- a one-line JS embed for other websites
- **RSS feed** at `/feed.xml`

## Local Development

```bash
# Clone
git clone https://github.com/AndOtherWays/roast-my-resume.git
cd roast-my-resume

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env with your API keys

# Run
python app.py
```

### Required Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `STRIPE_SECRET_KEY` | Stripe secret key |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `MAILERSEND_API_KEY` | MailerSend API key for email delivery |
| `SECRET_KEY` | Flask session secret |
| `BASE_URL` | Your app URL (default: `http://localhost:5000`) |

## Deployment

CVRoast is deployed on [Railway](https://railway.com) with automatic deploys from the `main` branch.

```bash
# Railway will auto-detect Python and use:
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

The `railway.json` and `Procfile` are both included for platform compatibility.

## Privacy

- Resumes are processed **in memory only** -- never written to disk
- Automatic deletion after **2 hours**
- No user accounts, no tracking cookies, no data selling
- Stripe handles all payment data -- CVRoast never sees card numbers
- Full privacy policy at [cvroast.com/privacy](https://cvroast.com/privacy)

---

<div align="center">

### Stop getting ghosted by recruiters.

**[Get your resume roasted free at cvroast.com](https://cvroast.com)**

*Most resumes score 30-50. What's yours?*

---

Built by [AndOtherWays](https://github.com/AndOtherWays)

</div>
