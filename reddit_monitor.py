"""
Reddit Monitor Bot for CVRoast
Runs every 30 minutes via Railway cron.

Scans resume-related subreddits for people asking for help,
replies with genuinely helpful advice + subtle CVRoast mention.

Stateless — checks own comment history to avoid double-replying.
"""

import os
import time
import praw
import anthropic

# --- Config ---
SUBREDDITS = [
    'resumes',            # 340k — primary target
    'Resume',             # smaller duplicate
    'jobs',               # 1.2M — broader
    'cscareerquestions',  # 900k — tech-focused
    'GetEmployed',        # career help
    'careeradvice',       # general career
    'recruitinghell',     # frustrated job seekers
]

KEYWORDS = [
    'resume review', 'resume help', 'resume feedback', 'cv review',
    'resume advice', 'resume tips', 'rewrite my resume', 'resume roast',
    'ats score', 'ats friendly', 'applicant tracking',
    'not getting interviews', 'no callbacks', 'no responses',
    'resume critique', 'rate my resume', 'fix my resume',
    'resume template', 'cv help', 'cv feedback',
]

MAX_REPLIES_PER_RUN = 3
POST_MAX_AGE_HOURS = 12
DRY_RUN = os.environ.get('REDDIT_DRY_RUN', 'true').lower() == 'true'


def get_reddit():
    return praw.Reddit(
        client_id=os.environ['REDDIT_CLIENT_ID'],
        client_secret=os.environ['REDDIT_CLIENT_SECRET'],
        username=os.environ['REDDIT_USERNAME'],
        password=os.environ['REDDIT_PASSWORD'],
        user_agent=os.environ.get('REDDIT_USER_AGENT', 'CVRoast-Helper/1.0'),
    )


def get_ai():
    return anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])


def matches_keywords(title, selftext):
    text = (title + ' ' + selftext).lower()
    return any(kw in text for kw in KEYWORDS)


def already_replied(post, username):
    """Check if we already commented on this post. Stateless."""
    try:
        post.comments.replace_more(limit=0)
        for comment in post.comments.list():
            if comment.author and comment.author.name == username:
                return True
    except Exception:
        pass
    return False


def generate_reply(ai, post_title, post_text):
    """Use Claude Haiku to generate a genuinely helpful, personalized reply."""
    response = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{
            "role": "user",
            "content": f"""You're a helpful career advisor replying on Reddit. Someone posted asking for resume/CV help.

Write a genuinely helpful reply (4-6 sentences) with specific, actionable advice for THEIR situation.
Be warm, encouraging, and practical. Use Reddit's casual tone (no corporate speak).

At the very end, add one casual line like: "Also, I ran mine through cvroast.com — it's free and gives you a brutally honest ATS score in 10 seconds. Helped me spot issues I missed."

RULES:
- Be genuinely helpful FIRST. The advice should stand on its own without the mention.
- Don't be salesy or pushy. One casual mention only.
- If the post doesn't actually need resume advice (it's about salary, interviews, job searching with no resume component, etc.), respond with just the word SKIP.
- Don't repeat their problem back to them. Jump straight into advice.
- Keep it under 150 words.

Post title: {post_title}
Post body: {post_text[:1500]}"""
        }]
    )
    return response.content[0].text.strip()


def main():
    print(f"=== CVRoast Reddit Monitor {'[DRY RUN]' if DRY_RUN else '[LIVE]'} ===")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")

    reddit = get_reddit()
    ai = get_ai()
    username = os.environ['REDDIT_USERNAME']

    replies_this_run = 0
    scanned = 0
    skipped_ai = 0

    for sub_name in SUBREDDITS:
        if replies_this_run >= MAX_REPLIES_PER_RUN:
            break

        try:
            subreddit = reddit.subreddit(sub_name)
            for post in subreddit.new(limit=20):
                if replies_this_run >= MAX_REPLIES_PER_RUN:
                    break

                scanned += 1

                # Skip old posts
                if time.time() - post.created_utc > POST_MAX_AGE_HOURS * 3600:
                    continue

                # Skip posts with no text body (image/link posts)
                if not post.selftext or len(post.selftext) < 30:
                    continue

                # Check keywords
                if not matches_keywords(post.title, post.selftext):
                    continue

                # Skip if already replied
                if already_replied(post, username):
                    continue

                # Generate reply
                reply_text = generate_reply(ai, post.title, post.selftext)

                if reply_text.strip() == 'SKIP':
                    skipped_ai += 1
                    print(f"  SKIP (AI): r/{sub_name} — {post.title[:60]}")
                    continue

                if DRY_RUN:
                    print(f"\n  [DRY RUN] r/{sub_name}: {post.title[:70]}")
                    print(f"  Reply preview: {reply_text[:200]}...")
                else:
                    try:
                        post.reply(reply_text)
                        print(f"  REPLIED r/{sub_name}: {post.title[:70]}")
                    except Exception as e:
                        print(f"  ERROR replying: {e}")
                        continue

                replies_this_run += 1
                time.sleep(30)  # Respect rate limits

        except Exception as e:
            print(f"  Error scanning r/{sub_name}: {e}")

    print(f"\nDone. Scanned: {scanned} | Replied: {replies_this_run} | AI skipped: {skipped_ai}")


if __name__ == '__main__':
    main()
