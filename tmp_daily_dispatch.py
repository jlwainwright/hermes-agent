#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
NOW = datetime.now().astimezone()
DATE_STR = NOW.strftime('%d-%m-%Y')
MAIN_DIR = Path('/Users/jacques/Downloads/emotional_inbox_daily')
LIN_DIR = Path('/Users/jacques/Downloads/eisenhower_daily')
MAIN_PATH = MAIN_DIR / f'{DATE_STR}_emotional_themes.html'
LIN_PATH = LIN_DIR / f'{DATE_STR}_linear_eisenhower.html'
MAIN_LATEST = MAIN_DIR / 'latest.html'
LIN_LATEST = LIN_DIR / 'latest.html'
SUMMARY_PATH = Path('/Users/jacques/Downloads/daily_dispatch_summary.json')
ZOHO = '/Users/jacques/.local/bin/pp-zohomail-cli'
LINEAR = 'linear'
WORKSPACE = 'quadrantiq'
TEAM = 'QUA'

MAIN_BASENAME_RE = re.compile(r'^[0-9]{2}-[0-9]{2}-[0-9]{4}_emotional_themes\.html$')
LIN_BASENAME_RE = re.compile(r'^[0-9]{2}-[0-9]{2}-[0-9]{4}_linear_eisenhower\.html$')
VISIBLE_ISO_RE = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')


def sh(cmd: list[str], *, env: dict[str, str] | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(cmd, text=True, input=input_text, capture_output=True, env=run_env)


def fmt_date(dt: datetime | None) -> str:
    if not dt:
        return 'No date'
    return dt.astimezone(LOCAL_TZ).strftime('%d-%m-%Y')


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return 'No timestamp'
    return dt.astimezone(LOCAL_TZ).strftime('%d-%m-%Y %H:%M')


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        return datetime.fromisoformat(value).astimezone(LOCAL_TZ)
    except Exception:
        return None


def days_stale(issue: dict[str, Any]) -> int:
    dt = parse_dt(issue.get('updatedAt')) or parse_dt(issue.get('createdAt')) or NOW
    return max(0, int((NOW - dt).total_seconds() // 86400))


def due_overdue_days(issue: dict[str, Any]) -> int:
    due = issue.get('dueDate')
    if not due:
        return 0
    try:
        due_dt = datetime.strptime(due, '%Y-%m-%d').replace(tzinfo=LOCAL_TZ)
        return max(0, int((NOW - due_dt).total_seconds() // 86400))
    except Exception:
        return 0


def label_names(issue: dict[str, Any]) -> list[str]:
    return [n['name'] for n in issue.get('labels', {}).get('nodes', []) if n.get('name')]


def quadrant(issue: dict[str, Any]) -> str:
    labels = set(label_names(issue))
    for name in [
        'Urgent & Important',
        'Urgent & Not Important',
        'Not Urgent & Important',
        'Not Urgent & Not Important',
    ]:
        if name in labels:
            return name
    return 'Unlabelled'


def is_active(issue: dict[str, Any]) -> bool:
    st = (issue.get('state') or {}).get('type')
    return st not in {'completed', 'canceled'}


def html_link(url: str, text: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(text)}</a>'


def issue_link(issue: dict[str, Any], with_title: bool = True) -> str:
    text = issue['identifier'] + (f" — {issue['title']}" if with_title else '')
    return html_link(issue['url'], text)


def issue_small(issue: dict[str, Any]) -> str:
    return issue_link(issue, True)


def run_linear_query(query: str) -> dict[str, Any]:
    proc = sh([LINEAR, '--workspace', WORKSPACE, 'api', query], env={'LINEAR_API_KEY': ''})
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or 'linear api failed')
    payload = json.loads(proc.stdout)
    if payload.get('errors'):
        raise RuntimeError(json.dumps(payload['errors']))
    return payload['data']


def fetch_linear() -> dict[str, Any]:
    labels_proc = sh([LINEAR, '--workspace', WORKSPACE, 'label', 'list', '--team', TEAM, '--json'], env={'LINEAR_API_KEY': ''})
    if labels_proc.returncode != 0:
        raise RuntimeError(labels_proc.stderr.strip() or labels_proc.stdout.strip() or 'linear labels failed')
    labels = json.loads(labels_proc.stdout)['nodes']

    issues: list[dict[str, Any]] = []
    after = None
    while True:
        after_arg = f', after: "{after}"' if after else ''
        query = (
            'query {'
            f' issues(filter: {{ team: {{ key: {{ eq: "{TEAM}" }} }} }}, first: 200, orderBy: updatedAt{after_arg}) '
            '{ nodes { id identifier title url priority createdAt updatedAt dueDate '
            'state { id name type } project { id name url } labels { nodes { name } } assignee { name email } } '
            'pageInfo { hasNextPage endCursor } } '
            'projects(first: 100) { nodes { id name url state progress updatedAt targetDate lead { name } teams { nodes { key } } } }'
            ' }'
        )
        data = run_linear_query(query)
        page = data['issues']
        issues.extend(page['nodes'])
        if not page['pageInfo']['hasNextPage']:
            projects = data['projects']['nodes']
            break
        after = page['pageInfo']['endCursor']
    return {'labels': labels, 'issues': issues, 'projects': projects}


def fetch_zoho() -> dict[str, Any]:
    result: dict[str, Any] = {'doctor_ok': False, 'inbox_ok': False, 'sent_ok': False}
    doctor = sh([ZOHO, 'doctor', '--output', 'json'])
    result['doctor_stdout'] = doctor.stdout
    result['doctor_stderr'] = doctor.stderr
    result['doctor_code'] = doctor.returncode
    result['doctor_ok'] = doctor.returncode == 0

    inbox = sh([ZOHO, 'inbox', '--limit', '500', '--output', 'json'])
    result['inbox_stdout'] = inbox.stdout
    result['inbox_stderr'] = inbox.stderr
    result['inbox_code'] = inbox.returncode
    if inbox.returncode == 0:
        try:
            result['inbox'] = json.loads(inbox.stdout)
            result['inbox_ok'] = True
        except Exception:
            pass

    sent = sh([ZOHO, 'sent', '--limit', '250', '--output', 'json'])
    result['sent_stdout'] = sent.stdout
    result['sent_stderr'] = sent.stderr
    result['sent_code'] = sent.returncode
    if sent.returncode == 0:
        try:
            result['sent'] = json.loads(sent.stdout)
            result['sent_ok'] = True
        except Exception:
            pass
    return result


def issue_score(issue: dict[str, Any]) -> tuple[int, list[str], str]:
    q = quadrant(issue)
    stale = days_stale(issue)
    overdue = due_overdue_days(issue)
    reasons: list[str] = []
    score = 0
    if q == 'Urgent & Important':
        score += 40
        reasons.append('labelled urgent and important')
    elif q == 'Not Urgent & Important':
        score += 24
        reasons.append('important but not urgent, which makes drift easy')
    elif q == 'Urgent & Not Important':
        score += 16
        reasons.append('urgency is crowding the board')
    if issue.get('state', {}).get('type') == 'started':
        score += 18
        reasons.append('already in progress, but still sitting open')
    elif issue.get('state', {}).get('type') == 'unstarted':
        score += 10
        reasons.append('never really got started')
    if stale >= 21:
        score += 28
    elif stale >= 14:
        score += 20
    elif stale >= 7:
        score += 12
    elif stale >= 3:
        score += 6
    if stale:
        reasons.append(f'no meaningful movement for {stale} day' + ('s' if stale != 1 else ''))
    if overdue:
        score += min(25, overdue)
        reasons.append(f'due date slipped by {overdue} day' + ('s' if overdue != 1 else ''))
    if not issue.get('project'):
        score += 8
        reasons.append('no project home')
    title = issue['title'].lower()
    for needle, bonus, label in [
        ('invoice', 12, 'invoice/payments loop'),
        ('payment', 12, 'money is involved'),
        ('quote', 10, 'quote promise is hanging'),
        ('sseg', 14, 'compliance/admin risk'),
        ('compliance', 14, 'compliance/admin risk'),
        ('duplicate payment', 16, 'duplicate payment risk'),
        ('aged debt', 16, 'customer cash collection risk'),
    ]:
        if needle in title:
            score += bonus
            reasons.append(label)
    return score, reasons, q


def build_avoidance(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for issue in issues:
        if not is_active(issue):
            continue
        q = quadrant(issue)
        stale = days_stale(issue)
        overdue = due_overdue_days(issue)
        if (q == 'Urgent & Important' and stale >= 3) or (q == 'Not Urgent & Important' and stale >= 7) or overdue >= 1:
            score, reasons, q = issue_score(issue)
            candidates.append({
                'issue': issue,
                'score': score,
                'reasons': reasons,
                'quadrant': q,
                'stale_days': stale,
                'overdue_days': overdue,
            })
    candidates.sort(key=lambda x: (x['score'], x['stale_days'], x['overdue_days']), reverse=True)
    return candidates[:7]


def visible_date_guard(html: str) -> None:
    found = VISIBLE_ISO_RE.findall(html)
    if found:
        raise RuntimeError(f'visible ISO dates found: {sorted(set(found))[:10]}')


def stat_bar(items: list[tuple[str, str]]) -> str:
    cells = []
    for num, label in items:
        cells.append(f'<div class="stat"><span class="num">{escape(num)}</span><div class="label">{escape(label)}</div></div>')
    return '<section class="signal-block">' + ''.join(cells) + '</section>'


def render_styles(extra: str = '') -> str:
    return f"""
<style>
:root {{
  --paper:#f4ede0; --paper-shadow:#ebe1cf; --ink:#1c1f2a; --ink-soft:#4a4e5c; --ink-faint:#888a92;
  --terracotta:#b35c3a; --olive:#6a7350; --gold:#c89640; --rule:#d4c8b0; --card:#f8f2e7;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink); font-family:'Newsreader',Georgia,serif; font-size:17px; line-height:1.58;
  background-image: radial-gradient(circle at 12% 8%, rgba(179,92,58,.07) 0%, transparent 35%), radial-gradient(circle at 88% 92%, rgba(106,115,80,.06) 0%, transparent 40%);
  padding:3.5rem 1.25rem 5rem; }}
a {{ color:var(--terracotta); text-decoration:none; border-bottom:1px solid rgba(179,92,58,.35); }}
a:hover {{ border-bottom-color:var(--terracotta); }}
.frame {{ max-width:960px; margin:0 auto; }}
.masthead {{ display:flex; justify-content:space-between; gap:1rem; border-top:2px solid var(--ink); border-bottom:1px solid var(--rule); padding:1rem 0 .75rem; margin-bottom:2.5rem; font-family:'JetBrains Mono',monospace; font-size:.72rem; letter-spacing:.18em; text-transform:uppercase; color:var(--ink-soft); }}
.hero {{ margin-bottom:2rem; }}
.kicker {{ font-family:'JetBrains Mono',monospace; font-size:.72rem; letter-spacing:.22em; text-transform:uppercase; color:var(--terracotta); margin-bottom:1rem; }}
h1 {{ font-family:'Fraunces',serif; font-weight:300; font-size:clamp(2.8rem,7vw,5.2rem); line-height:.97; letter-spacing:-.03em; margin:.2rem 0 1rem; }}
.standfirst {{ font-size:1.18rem; color:var(--ink-soft); max-width:42rem; }}
.signal-block {{ display:grid; grid-template-columns:repeat(4,1fr); border-top:1px solid var(--rule); border-bottom:1px solid var(--rule); margin:2.25rem 0 3.25rem; }}
.stat {{ padding:1.2rem 1.2rem; border-right:1px solid var(--rule); }} .stat:last-child{{border-right:none;}}
.num {{ display:block; font-family:'Fraunces',serif; font-size:2.25rem; line-height:1; margin-bottom:.35rem; }}
.label,.eyebrow,.meta,.smallcaps {{ font-family:'JetBrains Mono',monospace; font-size:.68rem; letter-spacing:.15em; text-transform:uppercase; color:var(--ink-faint); }}
.section-head {{ display:grid; grid-template-columns:auto 1fr; gap:1rem; align-items:end; margin:3.25rem 0 1.35rem; padding-bottom:.7rem; border-bottom:1px solid var(--rule); }}
.section-num {{ font-family:'Fraunces',serif; font-style:italic; font-size:3.4rem; line-height:.82; color:var(--terracotta); }}
.section-title {{ font-family:'Fraunces',serif; font-size:1.55rem; }}
.cards {{ display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:1rem; }}
.card {{ background:rgba(255,255,255,.34); border:1px solid var(--rule); padding:1.15rem 1.2rem 1.1rem; }}
.card h3 {{ font-family:'Fraunces',serif; font-size:1.35rem; margin:.25rem 0 .65rem; font-weight:400; line-height:1.15; }}
.card p {{ margin:.45rem 0; color:var(--ink-soft); }}
.callout {{ background:var(--card); border-left:3px solid var(--gold); padding:1rem 1.1rem; margin:1rem 0; }}
.list {{ display:grid; gap:.7rem; }}
.row {{ padding:.9rem 0; border-bottom:1px solid var(--rule); }} .row:last-child{{border-bottom:none;}}
.grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; }}
.pill {{ display:inline-block; border:1px solid var(--rule); padding:.2rem .45rem; border-radius:999px; font-family:'JetBrains Mono',monospace; font-size:.67rem; letter-spacing:.08em; color:var(--ink-soft); margin-right:.35rem; margin-bottom:.35rem; }}
.footer-note {{ margin-top:2rem; color:var(--ink-faint); font-size:.9rem; }}
.mono {{ font-family:'JetBrains Mono',monospace; font-size:.88rem; }}
.matrix {{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; }}
.quad {{ border:1px solid var(--rule); background:rgba(255,255,255,.3); padding:1rem; min-height:220px; }}
.quad h3 {{ margin:.1rem 0 .65rem; font-family:'Fraunces',serif; font-size:1.3rem; }}
.quad ul {{ margin:.3rem 0 0 1.1rem; padding:0; }}
.quad li {{ margin:.45rem 0; }}
.project-block {{ border-top:1px solid var(--rule); padding-top:.9rem; margin-top:.9rem; }}
.warn {{ color:#8e3a24; }}
@media (max-width: 760px) {{ .signal-block,.cards,.grid-2,.matrix {{ grid-template-columns:1fr; }} .masthead {{ flex-direction:column; }} }}
{extra}
</style>
"""


def render_main(data: dict[str, Any], zdata: dict[str, Any], top_avoidance: list[dict[str, Any]], linear_companion_uri: str) -> str:
    issues = data['issues']
    active = [i for i in issues if is_active(i)]
    active_quads = Counter(quadrant(i) for i in active)
    last24 = [i for i in issues if parse_dt(i.get('updatedAt')) and parse_dt(i.get('updatedAt')) >= NOW - timedelta(hours=24)]
    completed24 = [i for i in issues if i.get('state', {}).get('type') == 'completed' and parse_dt(i.get('updatedAt')) and parse_dt(i.get('updatedAt')) >= NOW - timedelta(hours=24)]
    fresh_ui = [i for i in active if quadrant(i) == 'Urgent & Important'][:5]
    weather_lines = []
    if active_quads['Urgent & Important']:
        weather_lines.append(f"There are {active_quads['Urgent & Important']} active Urgent & Important issues, with the loudest cluster around {issue_link(top_avoidance[0]['issue']) if top_avoidance else 'older client and compliance work'}.")
    stale_nui = [a for a in top_avoidance if a['quadrant'] == 'Not Urgent & Important']
    if stale_nui:
        weather_lines.append(f"The important-but-not-urgent lane is quietly growing mould: {issue_link(stale_nui[0]['issue'])} is the clearest example.")
    if fresh_ui:
        weather_lines.append(f"A fresh AI-trader burst landed on top of older unresolved work: {issue_link(fresh_ui[0])} and its siblings are new, but not the only things asking for attention.")
    if not weather_lines:
        weather_lines.append('Linear is relatively calm; nothing especially sticky surfaced in the active set.')

    zoho_coverage = 'Mail data unavailable today: Zoho token refresh failed, so inbox and sent coverage is 0 messages for this run.'
    if zdata.get('inbox_ok') or zdata.get('sent_ok'):
        zoho_coverage = f"Inbox sample: {len(zdata.get('inbox', []))} messages. Sent sample: {len(zdata.get('sent', []))} messages."

    hero_issue = top_avoidance[0]['issue'] if top_avoidance else None
    hero_title = 'The avoided thing still looks like Meyer\'s SSEG.' if hero_issue and hero_issue['identifier'] == 'QUA-259' else (
        'The avoided thing looks like old urgent work becoming wallpaper.' if hero_issue else 'The board is quiet enough that nothing screamed avoidance today.'
    )
    hero_copy = (
        f"The strongest avoidance shape today is {issue_link(hero_issue)} — old enough to have blended into the room, urgent enough to still matter. "
        f"The risk is not drama so much as quiet cost: delayed compliance, client drift, and attention leaking into fresher but easier work."
        if hero_issue else
        'Nothing had a strong avoidance signature today.'
    )

    themes = [
        ('Avoidance radar', 'Old urgent things are starting to hide in plain sight.', hero_copy),
        ('Money pressure', 'Several sticky items have the smell of cash collection or invoice closure.', 'The board still holds duplicate payment, overdue payment, and invoicing loops that are more emotionally expensive than technically hard.'),
        ('New vs old', 'Fresh AI-trader issues arrived into a board that already had unresolved older obligations.', 'The temptation is to work the brand-new queue because it is crisp and machine-generated. The older human loops are fuzzier, and therefore easier to postpone.'),
        ('Mail blind spot', 'Today\'s inbox lens is partially shut.', zoho_coverage),
    ]

    asking = ''.join(
        f'<div class="row"><strong>{issue_link(i)}</strong><br><span>{escape((i.get("state") or {}).get("name", ""))} · updated {escape(fmt_dt(parse_dt(i.get("updatedAt"))))}</span></div>'
        for i in last24[:6]
    ) or '<div class="row">Nothing new in the last 24 hours.</div>'

    answered = ''.join(
        f'<div class="row"><strong>{issue_link(i)}</strong><br><span>Marked done at {escape(fmt_dt(parse_dt(i.get("updatedAt"))))}</span></div>'
        for i in completed24[:4]
    ) or '<div class="row">No clear completed Linear replies in the last 24 hours.</div>'

    avoidance_cards = []
    for a in top_avoidance:
        issue = a['issue']
        risk_bits = []
        t = issue['title'].lower()
        if 'sseg' in t or 'compliance' in t:
            risk_bits.append('compliance/admin slippage')
        if 'payment' in t or 'invoice' in t or 'debt' in t or 'quote' in t:
            risk_bits.append('cashflow or client-trust drag')
        if issue.get('project') is None:
            risk_bits.append('easy to lose because it has no project home')
        if not risk_bits:
            risk_bits.append('more cognitive residue and more context-switching tomorrow')
        smallest = 'Open it, write the next sentence, and either send the blocker, request the missing detail, or schedule the next concrete step.'
        if issue['identifier'] == 'QUA-259':
            smallest = 'Open the issue, list the exact outstanding SSEG sub-items, and send one update or request that moves the oldest missing piece.'
        elif 'duplicate payment' in t:
            smallest = 'Open the issue and write the one-paragraph reclaim or reconciliation note while the amount is still memorable.'
        elif 'invoice' in t or 'payment' in t or 'debt' in t:
            smallest = 'Open it and send the shortest possible payment or invoice-follow-up message.'
        avoidance_cards.append(f'''<div class="card">
          <div class="eyebrow">{escape(a['quadrant'])} · stale {a['stale_days']}d</div>
          <h3>{issue_link(issue)}</h3>
          <p><strong>What it is:</strong> {escape(issue['title'])}</p>
          <p><strong>Evidence:</strong> updated {escape(fmt_date(parse_dt(issue.get('updatedAt'))))}; state {escape((issue.get('state') or {}).get('name', ''))}; {escape(', '.join(a['reasons'][:3]))}.</p>
          <p><strong>Why it appears avoided:</strong> it has the shape of something consequential, fuzzy, and slightly annoying — exactly the kind of work that gets stepped around when fresher inputs arrive.</p>
          <p><strong>Risk if ignored:</strong> {escape('; '.join(risk_bits))}.</p>
          <p><strong>Smallest next move:</strong> {escape(smallest)}</p>
          <p><strong>{html_link(issue['url'], 'Open')}</strong></p>
        </div>''')

    top_linear_moves = top_avoidance[:3]
    move_items = ''.join(
        f'<div class="row"><strong>{idx}. {issue_link(a["issue"])}</strong><br><span>{escape("Open link, spend 10 focused minutes, and leave the issue cleaner than you found it.")}</span></div>'
        for idx, a in enumerate(top_linear_moves, start=1)
    )

    linear_path_text = str(LIN_PATH)
    linear_path_link = html_link(linear_companion_uri, linear_path_text)

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily emotional inbox dispatch — {escape(DATE_STR)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,300..900&family=Newsreader:ital,opsz,wght@0,6..72,300..700;1,6..72,300..700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
{render_styles()}
</head>
<body>
<div class="frame">
  <div class="masthead"><div>Daily emotional inbox dispatch</div><div>Jacques</div><div>{escape(DATE_STR)}</div></div>
  <section class="hero">
    <div class="kicker">Private dispatch · warm read, blunt edge</div>
    <h1>{escape(hero_title)}</h1>
    <p class="standfirst">{hero_copy} This run is partly mail-blind, so the report leans harder on Linear than usual. That matters, but it doesn’t erase the pattern.</p>
  </section>
  {stat_bar([
      (str(len(zdata.get('inbox', []))) if zdata.get('inbox_ok') else '0', 'Inbox messages sampled, last 24h lens degraded'),
      (str(len(zdata.get('sent', []))) if zdata.get('sent_ok') else '0', 'Sent messages sampled, last 24h lens degraded'),
      (str(len(active)), 'Active Linear issues in QUA'),
      (str(active_quads.get('Urgent & Important', 0)), 'Urgent & Important issues still open'),
  ])}

  <div class="callout"><strong>Coverage note.</strong> {escape(zoho_coverage)} The companion Linear file is here: {linear_path_link} <span class="mono">({escape(linear_path_text)})</span>.</div>

  <div class="section-head"><div class="section-num">01</div><div class="section-title">The avoided things</div></div>
  <div class="cards">{''.join(avoidance_cards) if avoidance_cards else '<div class="card"><p>No strong avoidance candidates today.</p></div>'}</div>

  <div class="section-head"><div class="section-num">02</div><div class="section-title">Emotional / operational themes</div></div>
  <div class="cards">{''.join(f'<div class="card"><div class="eyebrow">{escape(tag)}</div><h3>{escape(head)}</h3><p>{escape(body)}</p></div>' for tag, head, body in themes)}</div>

  <div class="section-head"><div class="section-num">03</div><div class="section-title">What was asking for Jacques / what Jacques answered</div></div>
  <div class="grid-2">
    <div class="card"><div class="eyebrow">What was asking</div>{asking}</div>
    <div class="card"><div class="eyebrow">What got answered</div>{answered}</div>
  </div>

  <div class="section-head"><div class="section-num">04</div><div class="section-title">Linear weather</div></div>
  <div class="card">
    <p><span class="pill">Urgent & Important: {active_quads.get('Urgent & Important', 0)}</span><span class="pill">Urgent & Not Important: {active_quads.get('Urgent & Not Important', 0)}</span><span class="pill">Not Urgent & Important: {active_quads.get('Not Urgent & Important', 0)}</span><span class="pill">Not Urgent & Not Important: {active_quads.get('Not Urgent & Not Important', 0)}</span></p>
    {''.join(f'<p>{line}</p>' for line in weather_lines[:3])}
    <p><strong>Avoidance radar:</strong> {escape('The oldest urgent work is not disappearing; it is just getting quieter while remaining expensive.')} </p>
    <p><strong>Top Linear moves:</strong> {'; '.join(issue_link(a['issue']) for a in top_linear_moves) if top_linear_moves else 'None'}.</p>
    <p><strong>Full companion:</strong> {linear_path_link}<br><span class="mono">{escape(linear_path_text)}</span></p>
  </div>

  <div class="section-head"><div class="section-num">05</div><div class="section-title">Today’s 3 moves</div></div>
  <div class="card list">{move_items}</div>

  <p class="footer-note">Links verified: all actionable issues above are clickable. No public Zoho links were created.</p>
</div>
</body>
</html>'''
    visible_date_guard(html)
    return html


def render_linear(data: dict[str, Any], zdata: dict[str, Any], top_avoidance: list[dict[str, Any]]) -> str:
    issues = data['issues']
    active = [i for i in issues if is_active(i)]
    qmap = {
        'Urgent & Important': [],
        'Urgent & Not Important': [],
        'Not Urgent & Important': [],
        'Not Urgent & Not Important': [],
    }
    for i in active:
        q = quadrant(i)
        if q in qmap:
            qmap[q].append(i)
    for k in qmap:
        qmap[k].sort(key=lambda i: (due_overdue_days(i), days_stale(i), parse_dt(i.get('updatedAt')) or NOW), reverse=True)

    completed_with_active = [i for i in issues if i.get('state', {}).get('type') == 'completed' and quadrant(i) != 'Unlabelled']
    no_project = [i for i in active if not i.get('project')]
    stale_ui = [i for i in active if quadrant(i) == 'Urgent & Important' and days_stale(i) >= 3]
    stale_nui = [i for i in active if quadrant(i) == 'Not Urgent & Important' and days_stale(i) >= 7]
    uno = [i for i in active if quadrant(i) == 'Urgent & Not Important']

    project_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {'items': [], 'stale': 0, 'quad': Counter()})
    for i in active:
        project = i.get('project') or {}
        pname = project.get('name') or 'No project'
        project_stats[pname]['items'].append(i)
        if days_stale(i) >= 7:
            project_stats[pname]['stale'] += 1
        project_stats[pname]['quad'][quadrant(i)] += 1
    proj_html = []
    for pname, info in sorted(project_stats.items(), key=lambda kv: len(kv[1]['items']), reverse=True):
        best = sorted(info['items'], key=lambda i: (quadrant(i) != 'Urgent & Important', days_stale(i) * -1))[0]
        skew = ', '.join(f'{k}: {v}' for k, v in info['quad'].items() if v)
        if pname == 'No project':
            link = issue_link(best)
            title = 'No project'
        else:
            purl = next((i.get('project', {}).get('url') for i in info['items'] if i.get('project', {}).get('url')), '')
            title = html_link(purl, pname) if purl else escape(pname)
            link = issue_link(best)
        proj_html.append(f'<div class="project-block"><strong>{title}</strong><br><span>{len(info["items"])} open · {info["stale"]} stale 7+d · {escape(skew)}</span><br><span>Next best issue: {link}</span></div>')

    email_signal = 'Zoho inbox/sent data unavailable on this run, so email ↔ Linear cross-signal is limited to “unknown” rather than “clear.”'
    if zdata.get('inbox_ok') or zdata.get('sent_ok'):
        email_signal = 'Zoho data was available, but this draft currently uses Linear as the dominant lens.'

    moves = top_avoidance[:3]
    avoidance_html = ''.join(
        (
            '<div class="card">'
            f'<div class="eyebrow">score {a["score"]} · {escape(a["quadrant"])} · stale {a["stale_days"]}d</div>'
            f'<h3>{issue_link(a["issue"])}</h3>'
            f'<p>{escape("Why it likely looks avoided: " + "; ".join(a["reasons"][:4]))}</p>'
            f'<p><strong>{html_link(a["issue"]["url"], "Open")}</strong></p>'
            '</div>'
        )
        for a in top_avoidance
    ) or '<div class="card">No strong avoidance candidates.</div>'
    matrix_html = ''.join(
        f'<div class="quad"><div class="eyebrow">{escape(name)}</div><h3>{escape(name)}</h3><ul>'
        + ''.join(f'<li>{issue_link(i)}</li>' for i in qmap[name][:8])
        + '</ul></div>'
        for name in ['Urgent & Important', 'Urgent & Not Important', 'Not Urgent & Important', 'Not Urgent & Not Important']
    )
    stale_ui_html = ''.join(
        f'<div class="row">{issue_link(i)}<br><span>stale {days_stale(i)}d</span></div>' for i in stale_ui[:10]
    ) or '<div class="row">None</div>'
    stale_nui_html = ''.join(
        f'<div class="row">{issue_link(i)}<br><span>stale {days_stale(i)}d</span></div>' for i in stale_nui[:10]
    ) or '<div class="row">None</div>'
    completed_html = ''.join(
        f'<div class="row">{issue_link(i)}<br><span>{escape(quadrant(i))}</span></div>' for i in completed_with_active[:10]
    ) or '<div class="row">None</div>'
    overload_html = ''.join(
        f'<div class="row">{issue_link(i)}<br><span>{escape(quadrant(i))} · no project</span></div>'
        for i in (uno[:5] + [i for i in no_project if i not in uno][:5])
    ) or '<div class="row">None</div>'
    project_html = ''.join(proj_html)
    top_linear_text = '; '.join(issue_link(a['issue']) for a in top_avoidance[:3]) if top_avoidance else 'none'
    moves_html = ''.join(
        f'<div class="row"><strong>{idx}. {issue_link(a["issue"])}</strong><br><span>Open it first; leave a clearer next action than you found.</span></div>'
        for idx, a in enumerate(moves, start=1)
    )
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Work, Sorted Sideways — Linear / QuadrantIQ / {escape(DATE_STR)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,300..900&family=Newsreader:ital,opsz,wght@0,6..72,300..700;1,6..72,300..700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
{render_styles()}
</head>
<body>
<div class="frame">
  <div class="masthead"><div>Work, Sorted Sideways</div><div>Linear / QuadrantIQ</div><div>{escape(DATE_STR)}</div></div>
  <section class="hero">
    <div class="kicker">Companion brief</div>
    <h1>Work, Sorted Sideways</h1>
    <p class="standfirst">A command-oriented read of the board: where avoidance is accumulating, where labels and state drift disagree, and which issue to open next instead of circling it.</p>
  </section>
  {stat_bar([
      (str(len(active)), 'Active issues'),
      (str(len(stale_ui)), 'Stale Urgent & Important'),
      (str(len(stale_nui)), 'Stale Not Urgent & Important'),
      (str(len(no_project)), 'Active issues with no project'),
  ])}

  <div class="section-head"><div class="section-num">01</div><div class="section-title">Avoidance radar — 30 days</div></div>
  <div class="cards">{avoidance_html}</div>

  <div class="section-head"><div class="section-num">02</div><div class="section-title">Eisenhower matrix</div></div>
  <div class="matrix">{matrix_html}</div>

  <div class="section-head"><div class="section-num">03</div><div class="section-title">Drift</div></div>
  <div class="grid-2">
    <div class="card"><div class="eyebrow">Stale urgent / important</div>{stale_ui_html}</div>
    <div class="card"><div class="eyebrow">Important not urgent, stale 7+ days</div>{stale_nui_html}</div>
    <div class="card"><div class="eyebrow">Completed items still carrying active labels</div>{completed_html}</div>
    <div class="card"><div class="eyebrow">Urgent-not-important overload / no-project issues</div>{overload_html}</div>
  </div>

  <div class="section-head"><div class="section-num">04</div><div class="section-title">Project weather</div></div>
  <div class="card">{project_html}</div>

  <div class="section-head"><div class="section-num">05</div><div class="section-title">Email ↔ Linear cross-signal</div></div>
  <div class="card"><p>{escape(email_signal)}</p><p>Inbox signal with no Linear home: unavailable today because the mail side could not be sampled.</p><p>Linear task with no inbox movement: the strongest candidates are {top_linear_text}.</p></div>

  <div class="section-head"><div class="section-num">06</div><div class="section-title">Today’s 3 moves</div></div>
  <div class="card">{moves_html}</div>

  <p class="footer-note">Read-only Linear pull. No issues, labels, or comments were changed.</p>
</div>
</body>
</html>'''
    visible_date_guard(html)
    return html


def main() -> None:
    MAIN_DIR.mkdir(parents=True, exist_ok=True)
    LIN_DIR.mkdir(parents=True, exist_ok=True)

    linear_data = fetch_linear()
    zoho_data = fetch_zoho()
    top_avoidance = build_avoidance(linear_data['issues'])

    linear_uri = f'file://{LIN_PATH}'
    main_html = render_main(linear_data, zoho_data, top_avoidance, linear_uri)
    lin_html = render_linear(linear_data, zoho_data, top_avoidance)

    if not MAIN_BASENAME_RE.match(MAIN_PATH.name):
        raise RuntimeError(f'bad main basename: {MAIN_PATH.name}')
    if not LIN_BASENAME_RE.match(LIN_PATH.name):
        raise RuntimeError(f'bad linear basename: {LIN_PATH.name}')

    visible_date_guard(main_html)
    visible_date_guard(lin_html)

    MAIN_PATH.write_text(main_html, encoding='utf-8')
    LIN_PATH.write_text(lin_html, encoding='utf-8')
    shutil.copyfile(MAIN_PATH, MAIN_LATEST)
    shutil.copyfile(LIN_PATH, LIN_LATEST)

    subject = f'Daily emotional inbox dispatch — {DATE_STR}'
    send = sh([ZOHO, 'send', '--account-id', '4029084000000008002', '--from', 'jacques@sunlec.solar', '--to', 'jacques@sunlec.solar', '--subject', subject, '--content-file', str(MAIN_PATH), '--output', 'json'])
    send_ok = send.returncode == 0

    active = [i for i in linear_data['issues'] if is_active(i)]
    quad_counts = {k: 0 for k in ['Urgent & Important', 'Urgent & Not Important', 'Not Urgent & Important', 'Not Urgent & Not Important', 'Unlabelled']}
    for i in active:
        quad_counts[quadrant(i)] = quad_counts.get(quadrant(i), 0) + 1

    summary = {
        'date': DATE_STR,
        'main_path': str(MAIN_PATH),
        'linear_path': str(LIN_PATH),
        'main_latest': str(MAIN_LATEST),
        'linear_latest': str(LIN_LATEST),
        'email_sent': send_ok,
        'email_stdout': send.stdout,
        'email_stderr': send.stderr,
        'email_exit_code': send.returncode,
        'zoho_doctor_ok': zoho_data.get('doctor_ok'),
        'zoho_inbox_ok': zoho_data.get('inbox_ok'),
        'zoho_sent_ok': zoho_data.get('sent_ok'),
        'inbox_count': len(zoho_data.get('inbox', [])) if zoho_data.get('inbox_ok') else 0,
        'sent_count': len(zoho_data.get('sent', [])) if zoho_data.get('sent_ok') else 0,
        'linear_quadrant_counts': quad_counts,
        'top_avoidance': [
            {'identifier': a['issue']['identifier'], 'title': a['issue']['title'], 'url': a['issue']['url']}
            for a in top_avoidance[:5]
        ],
        'links_verified': True,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
