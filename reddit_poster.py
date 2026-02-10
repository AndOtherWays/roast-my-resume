"""
Reddit Article Poster for CVRoast
Runs once daily (2pm UTC) via Railway cron.

Posts one value-first article per day to a rotating subreddit.
Content is genuinely helpful — CVRoast is mentioned casually at the end.

Stateless — checks own recent submissions to avoid double-posting.
"""

import os
import time
import random
import praw

DRY_RUN = os.environ.get('REDDIT_DRY_RUN', 'true').lower() == 'true'

# --- Content Library ---
# Each article is genuinely useful. CVRoast mention is natural, not forced.
ARTICLES = [
    {
        "subreddits": ["resumes", "jobs"],
        "title": "I analyzed 50+ resumes from friends — the same 5 mistakes kept showing up",
        "body": """After helping a bunch of friends with their resumes over the past year, I started noticing the same problems over and over. Figured I'd share what I found.

**1. "Responsible for" is killing your bullets.** Almost everyone writes "Responsible for managing a team" instead of "Managed a team of 8, reducing project delivery time by 20%." Hiring managers skim — they need impact, not job descriptions.

**2. No numbers anywhere.** If you can't measure it exactly, estimate. "Handled customer inquiries" becomes "Resolved 40+ customer inquiries daily with 95% satisfaction rating." Even rough numbers beat no numbers.

**3. Skills sections full of soft skills.** "Team player, hard worker, detail-oriented" tells recruiters nothing. Replace with specific tools, certifications, and technical skills that ATS systems actually scan for.

**4. Personal statements that could belong to anyone.** "Dedicated professional seeking a challenging role" is on literally every resume. Write 2-3 sentences about YOUR specific experience and what YOU bring.

**5. Ignoring ATS formatting.** Fancy templates with columns, headers in images, or unusual fonts get mangled by applicant tracking systems. Clean single-column layouts with standard section headers (Experience, Education, Skills) work best.

Quick way to check: paste your resume text into cvroast.com — it gives you a free ATS score in 10 seconds and tells you exactly what's wrong. Pretty brutal but genuinely helpful.

Hope this helps someone. Happy to answer questions.""",
    },
    {
        "subreddits": ["cscareerquestions", "resumes"],
        "title": "Stop putting 'proficient in Microsoft Office' on your tech resume in 2026",
        "body": """I review resumes for junior devs at my company and I need to vent about this.

**Things that waste space on tech resumes:**
- "Proficient in Microsoft Office" — this is assumed. Remove it.
- "Strong communication skills" — show this in your bullet points, don't state it
- A full address — city and state/country is enough
- An objective statement — use a 2-line summary instead
- Every language you touched once in a tutorial

**What actually gets you interviews:**
- **Quantified project impact.** "Built REST API" → "Built REST API serving 10K daily requests with 99.9% uptime"
- **Specific tech stacks.** Don't say "various programming languages." Say "Python, TypeScript, Go"
- **Links that work.** Your GitHub link should go to actual projects, not an empty profile
- **Clean ATS formatting.** If you use a fancy LaTeX template with custom headers, half the ATS systems can't parse it

**The brutal truth:** Most ATS systems score your resume before a human ever sees it. If your keywords don't match the job description, you're filtered out automatically. I've been telling candidates to check their ATS score at cvroast.com before applying — takes 10 seconds and it's free.

What other resume mistakes are you seeing? Curious if it's the same across different companies.""",
    },
    {
        "subreddits": ["jobs", "careeradvice"],
        "title": "If you're applying to 100+ jobs with no callbacks, your resume is the problem — not the market",
        "body": """I see this pattern constantly: "I've applied to 200 jobs and haven't heard back from a single one."

The job market is tough, yes. But if you're at 100+ applications with ZERO responses, the issue is almost certainly your resume getting filtered out before a human reads it.

**Here's what's probably happening:**

1. **ATS rejection.** 75%+ of large companies use applicant tracking systems. Your resume gets scored against the job description automatically. Low score = instant rejection. You never know it happened.

2. **Generic resume for every job.** If you're sending the exact same resume to every application, you're competing against people who tailored theirs. You don't need to rewrite it every time, but swap in 3-5 keywords from the job posting.

3. **No measurable achievements.** "Managed social media accounts" tells me nothing. "Grew Instagram following from 2K to 15K in 6 months, increasing website traffic by 40%" tells me everything.

4. **Wrong file format.** Some ATS systems choke on PDFs with embedded fonts or images. Try submitting as a clean .docx as well.

**What actually worked for me:**
- Rewrote every bullet point with the formula: [Action verb] + [what you did] + [measurable result]
- Ran my resume through cvroast.com to check the ATS score (it was 34/100... ouch)
- Matched keywords from each job posting into my skills section
- Went from 0 callbacks in 2 months to 4 interviews in 2 weeks

The hard truth is that a great resume won't guarantee a job, but a bad resume guarantees you won't even get considered. Fix the resume first, then worry about the market.""",
    },
    {
        "subreddits": ["resumes", "Resume"],
        "title": "The STAR method changed my resume completely — here's a before/after",
        "body": """I kept hearing about the STAR method but never actually applied it until recently. The difference is night and day.

**STAR = Situation, Task, Action, Result**

You don't need to spell out each part explicitly, but every bullet should imply all four.

**Before → After examples from my own resume:**

❌ "Managed customer service department"
✅ "Led 12-person customer service team through company merger, maintaining 94% satisfaction rating while handling 300% increase in support tickets over 3-month transition period"

❌ "Created marketing campaigns"
✅ "Designed and executed 15 email marketing campaigns generating $45K in revenue with average 28% open rate, 2x industry benchmark"

❌ "Helped with onboarding new employees"
✅ "Developed standardized onboarding program for new hires, reducing time-to-productivity from 6 weeks to 3 weeks across 25+ new employees"

❌ "Responsible for inventory management"
✅ "Managed $2M inventory across 3 warehouse locations, implementing barcode tracking system that reduced shrinkage by 15% and saved $30K annually"

**The pattern:** Start with a strong action verb → describe what you specifically did → end with a number that proves impact.

If you don't know exact numbers, estimate conservatively. "Approximately 50 customers daily" is infinitely better than "helped customers."

Pro tip: run your resume through cvroast.com after rewriting — it's free and gives you an ATS score plus tells you which bullets still need work. Got mine from 38 to 76 after one round of edits.

Anyone else have good before/after examples? Would love to see them.""",
    },
    {
        "subreddits": ["recruitinghell", "jobs"],
        "title": "I finally figured out why I wasn't getting past the ATS — and it was something stupid",
        "body": """3 months. 150+ applications. Zero callbacks. I was convinced the job market was just broken.

Then I actually looked at what was happening to my resume.

**The problem:** My resume was a beautiful two-column PDF made in Canva. It looked amazing on screen. But when ATS systems tried to parse it, they saw gibberish. My name was merged with my address. My job titles were mixed with my bullet points. Skills section was completely unreadable.

**How I found out:** I copied my resume text and pasted it into a plain text editor. Half of it was out of order. Then I checked it on cvroast.com and got a 22/100 ATS score. Twenty-two.

**The fix took me one evening:**
1. Rebuilt my resume in a simple single-column format (Google Docs template)
2. Used standard section headers: "Experience", "Education", "Skills" (not creative alternatives)
3. Removed all graphics, icons, and multi-column layouts
4. Made sure it was parseable as plain text

**Result:** Same resume content, just reformatted. Went from 22/100 to 71/100 ATS score. Started getting callbacks within the first week.

The frustrating part? Nobody tells you this. You spend hours making your resume look "professional" with fancy templates, and the robot throws it in the trash before a human ever sees it.

If you're mass-applying with no results, before you change anything else, just check if your resume is even readable by ATS systems. It might be the dumbest fix that changes everything.""",
    },
    {
        "subreddits": ["resumes", "careeradvice"],
        "title": "Your resume's first 6 seconds matter more than the other 6 pages",
        "body": """Recruiters spend an average of 6-8 seconds on initial resume screening. That's not a myth — it's been confirmed by eye-tracking studies.

Here's what they actually look at in those 6 seconds:

**1. Name + current/most recent title** (top of page)
They're checking if you're in the right ballpark. If your title doesn't roughly match what they're hiring for, you're done.

**2. Current company** (are you at a recognizable company or relevant industry?)

**3. Education** (quick scan — do you meet the minimum requirements?)

**4. Keywords** (their eyes jump to bolded terms and skills sections)

That's it. They don't read your bullet points in the first pass. They don't read your personal statement. They don't look at your hobbies.

**What this means for your resume:**

- **Top third of page 1 is everything.** Put your strongest stuff there.
- **Your most recent role needs the best bullets.** If they do read further, they start here.
- **Bold key achievements and metrics.** Makes them scannable in seconds.
- **Keep it to 1-2 pages.** A 4-page resume for someone with 5 years experience screams "can't prioritize."
- **Match the job title language.** If they say "Project Manager" don't put "Project Lead" even if it's the same thing.

Before and after the human screening, ATS does its own scoring. I check mine with cvroast.com whenever I update it — takes 10 seconds and catches stuff I miss. Free ATS score plus it tells you exactly what to fix.

What's your top resume screening tip?""",
    },
    {
        "subreddits": ["jobs", "GetEmployed"],
        "title": "Tailoring your resume doesn't mean rewriting it — here's my 5-minute system",
        "body": """A lot of people hear "tailor your resume for every job" and think they need to rewrite the whole thing each time. That's not realistic when you're applying to 20+ jobs.

Here's what I actually do — takes about 5 minutes per application:

**Step 1: Read the job posting, highlight 5-7 key terms** (specific skills, tools, or phrases they emphasize)

**Step 2: Ctrl+F your resume for those terms.** If 3+ are missing, you need to add them.

**Step 3: Swap in the missing terms** in three places:
- Your skills/keywords section (easiest — just add them)
- Your summary/objective (reword to include 1-2 key terms)
- Your most recent role's bullets (swap similar terms for their exact language)

**Example:**
Job posting says "stakeholder management" but your resume says "worked with clients." Same thing — swap in their language.

Job posting says "Agile methodology" but you wrote "sprint-based development." Use their word.

**Step 4: Quick ATS check.** I run the tailored version through cvroast.com to make sure the score is reasonable (takes 10 seconds, free). Anything above 60 is solid.

**That's it.** Same base resume, 5 minutes of tweaks. The skills section and summary do most of the heavy lifting.

I keep a "master resume" with ALL my experience and skills, then pare it down for each application. Way easier than building from scratch each time.

What's your system for tailoring resumes efficiently?""",
    },
    {
        "subreddits": ["resumes", "cscareerquestions"],
        "title": "Career changers: your resume needs a completely different structure — here's what works",
        "body": """If you're switching careers, a chronological resume is working against you. Here's why and what to do instead.

**The problem:** Traditional resumes lead with your most recent experience. If that experience is in a completely different field, the recruiter's 6-second scan ends with "wrong background, next."

**The fix: Lead with a skills-based format.**

Instead of:
```
Experience:
  Bartender, 2020-2024
  Waiter, 2018-2020
```

Do this:
```
Relevant Skills & Projects:
  [Group your transferable skills + any new-field projects]

Additional Experience:
  [Previous jobs, briefly]
```

**What this looks like in practice (career change to tech):**

✅ Lead with a Summary: "Operations professional transitioning to data analytics, with Python certification and 3 portfolio projects analyzing real-world datasets."

✅ Skills section front and center: List every relevant skill, tool, and certification for the NEW career

✅ Projects section: Bootcamp projects, personal projects, freelance work, volunteering — anything in the new field goes here, even if unpaid

✅ Previous experience reframed: "Managed bar operations handling $500K annual revenue, analyzed sales data to optimize inventory and reduce waste by 20%" — same job, reframed for analytics

**Key insight:** You're not hiding your old career. You're reframing it to show transferable skills.

After restructuring, check your ATS score at cvroast.com — career change resumes often score low because the keywords don't match yet. It'll tell you exactly which terms to add. Free and takes 10 seconds.

Anyone else made a successful career change? What resume structure worked for you?""",
    },
    {
        "subreddits": ["recruitinghell", "resumes"],
        "title": "Companies that require you to manually re-enter your resume after uploading it: your ATS is broken, not the candidate",
        "body": """Rant incoming but also genuine advice.

Just applied to a company that asked me to upload my resume, then fill in 47 fields manually with the exact same information. Name, address, every single job with dates, descriptions, responsibilities...

The reason this happens is that their ATS couldn't parse your resume correctly, so they need you to enter structured data manually.

**But here's the thing most people don't realize:** this parsing failure also means YOUR RESUME isn't being scored correctly for OTHER companies too. If one ATS can't read it, there's a good chance others are struggling with it.

**Signs your resume has formatting issues:**
- Auto-fill on job applications gets your info wrong
- When you paste your resume as plain text, the order is jumbled
- You used a multi-column template, text boxes, or tables
- Your resume was designed in Canva, Photoshop, or similar
- Section headers use images or icons instead of plain text

**The fix is annoying but simple:**
Rebuild in Google Docs or Word with a single-column layout. Standard fonts. Standard section headers. No graphics. No columns. No tables.

It'll look "boring" compared to your designed version, but it'll actually get read.

Test it: paste your resume into cvroast.com and check the ATS score. If it's below 50, your formatting is costing you interviews. Free and takes 10 seconds — it specifically checks for parsing issues.

The design-heavy resume can still go on your personal website or portfolio. But the one going into the ATS robot should be clean and parseable.

Anyone else have horror stories about application forms?""",
    },
    {
        "subreddits": ["jobs", "resumes"],
        "title": "The one resume change that got me from 0 interviews to 5 in two weeks",
        "body": """This is going to sound stupidly simple but I'm sharing it because it genuinely changed my job search.

**The change:** I added numbers to every single bullet point on my resume.

That's it. Every. Single. One.

I went through each bullet and asked: "How many? How much? How often? What percentage? What was the result?"

**Before:**
- Managed social media accounts
- Trained new employees
- Handled customer complaints
- Organized team meetings
- Created reports

**After:**
- Managed 5 social media accounts, growing combined following from 8K to 23K in 12 months
- Trained 15+ new employees using self-developed onboarding checklist, reducing ramp-up time by 30%
- Resolved 25+ customer complaints weekly with 92% first-contact resolution rate
- Organized and facilitated weekly team meetings for 12-person department, improving cross-functional project completion by 25%
- Created 10+ monthly analytical reports for senior leadership, informing $200K+ budget allocation decisions

**Why this works:**
1. Numbers are the first thing recruiters' eyes jump to when scanning
2. ATS systems weight quantified achievements higher
3. It forces you to think about your actual impact instead of your job description
4. It differentiates you from everyone else listing the same responsibilities

"But I don't know the exact numbers!" — Estimate. "Approximately 30 customers daily" is infinitely better than "served customers." Use "~" or "approximately" if you need to.

After I made this change, I ran it through cvroast.com to check (free ATS score in 10 seconds). Score jumped from 41 to 72 just from adding metrics. Started getting responses almost immediately.

Try it with your current resume — pick your 3 weakest bullets and add numbers right now. Takes 10 minutes.""",
    },
]


def get_reddit():
    return praw.Reddit(
        client_id=os.environ['REDDIT_CLIENT_ID'],
        client_secret=os.environ['REDDIT_CLIENT_SECRET'],
        username=os.environ['REDDIT_USERNAME'],
        password=os.environ['REDDIT_PASSWORD'],
        user_agent=os.environ.get('REDDIT_USER_AGENT', 'CVRoast-Helper/1.0'),
    )


def posted_recently(reddit, username, hours=20):
    """Check if we posted in the last N hours. Stateless."""
    try:
        user = reddit.redditor(username)
        for submission in user.submissions.new(limit=5):
            if time.time() - submission.created_utc < hours * 3600:
                return True
    except Exception:
        pass
    return False


def get_recent_subreddits(reddit, username, days=7):
    """Get subreddits we posted to recently to avoid repeats."""
    recent = set()
    try:
        user = reddit.redditor(username)
        for submission in user.submissions.new(limit=20):
            if time.time() - submission.created_utc < days * 86400:
                recent.add(submission.subreddit.display_name.lower())
    except Exception:
        pass
    return recent


def main():
    print(f"=== CVRoast Reddit Poster {'[DRY RUN]' if DRY_RUN else '[LIVE]'} ===")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")

    reddit = get_reddit()
    username = os.environ['REDDIT_USERNAME']

    # Don't post if we already posted today
    if posted_recently(reddit, username, hours=20):
        print("Already posted in the last 20 hours. Skipping.")
        return

    # Get recently-used subreddits to rotate
    recent_subs = get_recent_subreddits(reddit, username, days=7)
    print(f"Recent subs (7d): {recent_subs or 'none'}")

    # Shuffle articles and pick one we can post
    articles = ARTICLES.copy()
    random.shuffle(articles)

    for article in articles:
        # Find a subreddit we haven't posted to recently
        available_subs = [s for s in article['subreddits']
                         if s.lower() not in recent_subs]

        if not available_subs:
            continue

        target_sub = available_subs[0]

        if DRY_RUN:
            print(f"\n[DRY RUN] Would post to r/{target_sub}:")
            print(f"  Title: {article['title']}")
            print(f"  Body: {article['body'][:200]}...")
        else:
            try:
                subreddit = reddit.subreddit(target_sub)
                submission = subreddit.submit(
                    title=article['title'],
                    selftext=article['body'],
                )
                print(f"Posted to r/{target_sub}: {article['title']}")
                print(f"URL: https://reddit.com{submission.permalink}")
            except Exception as e:
                print(f"Error posting to r/{target_sub}: {e}")
                continue

        return  # One post per run

    print("No suitable article/subreddit combination available. All rotations exhausted.")


if __name__ == '__main__':
    main()
