"""Build the approval leaderboard from GitHub issue history."""

from collections import Counter
from datetime import date, datetime, timezone
from html import escape
from itertools import batched
import json
import os
from pathlib import Path
from urllib.parse import quote

import requests

from src.updater import (
    APPROVE_THEME_LABEL,
    DEFAULT_GITHUB_REPOSITORY,
    RateLimiter,
    TopContributor,
    create_github_session,
    fetch_avatar_data_uri,
    print_github_warning,
    render_leaderboard_svg,
    resolve_contributor_profile,
)

APPROVE_QUEUE_LABEL = 'approve-queue'
APPROVAL_LABELS = {APPROVE_THEME_LABEL, 'add-game', 'add-movie'}
# The retired labels were migrated to approve-theme in August 2023.
APPROVAL_LABEL_MIGRATION = '2023-08-08T00:00:00Z'
APPROVAL_BOT = 'lizardbyte-bot'
REST_TIMELINE_EVENTS = {'labeled', 'unlabeled', 'closed', 'reopened'}
REST_REQUEST_RESERVE = 50
TITLE = 'Approval Leaderboard'
BASENAME = 'approval_leaderboard'
EMPTY_LABEL = 'No attributed theme approvals'

ISSUES_QUERY = '''
query ApprovedIssues($owner: String!, $name: String!, $label: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $cursor, states: [CLOSED], labels: [$label]) {
      nodes { id number closedAt updatedAt stateReason }
      pageInfo { hasNextPage endCursor }
    }
  }
}
'''

TIMELINE_QUERY = '''
fragment History on Issue {
  timelineItems(first: 30, after: CURSOR, itemTypes: [
    LABELED_EVENT, UNLABELED_EVENT, ISSUE_COMMENT, CLOSED_EVENT, REOPENED_EVENT
  ]) {
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on LabeledEvent { createdAt label { name } actor { ...ApprovalActor } }
      ... on UnlabeledEvent { createdAt label { name } }
      ... on IssueComment { id createdAt body author { ...ApprovalActor } }
      ... on ClosedEvent { createdAt }
      ... on ReopenedEvent { createdAt }
    }
  }
}
fragment ApprovalActor on Actor {
  login
  ... on User { databaseId }
}
'''

REACTIONS_QUERY = '''
fragment History on IssueComment {
  reactions(first: 30, after: CURSOR, content: THUMBS_UP) {
    pageInfo { hasNextPage endCursor }
    nodes { user { login } }
  }
}
'''


def _graphql(session: requests.Session, query: str, **variables) -> dict:
    """Reject partial results as well as HTTP failures."""
    response = session.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('errors'):
        raise requests.RequestException(f'GitHub GraphQL: {payload["errors"][0]["message"]}')
    return payload['data']


def _approved_issues(session: requests.Session, repository: str):
    """Yield completed issues carrying the final approval label."""
    owner, name = repository.split('/')
    cursor = None
    while True:
        data = _graphql(session, ISSUES_QUERY, owner=owner, name=name, label=APPROVE_THEME_LABEL, cursor=cursor)
        page = data['repository']['issues']
        yield from (issue for issue in page['nodes'] if issue['stateReason'] == 'COMPLETED')
        if not page['pageInfo']['hasNextPage']:
            return
        cursor = page['pageInfo']['endCursor']


def _load_connections(session: requests.Session, ids: list[str], fragment: str, field: str) -> dict:
    """Batch the first page for several nodes and finish any longer connections."""
    batch_query = 'query Histories($ids: [ID!]!) { nodes(ids: $ids) { id ...History } }'
    next_query = 'query HistoryPage($id: ID!, $cursor: String!) { node(id: $id) { ...History } }'
    connections = {}
    for batch in batched(ids, 50):
        data = _graphql(session, batch_query + fragment.replace('CURSOR', 'null'), ids=batch)
        for node in filter(None, data['nodes']):
            node_id = node['id']
            page = node[field]
            items = list(filter(None, page['nodes']))
            while page['pageInfo']['hasNextPage']:
                data = _graphql(
                    session, next_query + fragment.replace('CURSOR', '$cursor'),
                    id=node_id, cursor=page['pageInfo']['endCursor'],
                )
                page = data['node'][field]
                items.extend(filter(None, page['nodes']))
            connections[node_id] = items
    return connections


def _rest_items(session: requests.Session, url: str, limiter: RateLimiter, **params):
    """Read every page of a GitHub REST list."""
    params = {'per_page': 100, **params}
    while url:
        limiter.wait()
        response = session.get(url, params=params, timeout=15)
        response.raise_for_status()
        yield from response.json()
        url = response.links.get('next', {}).get('url')
        params = None


def _rest_actor(actor: dict | None) -> dict:
    """Convert a REST account to the actor shape used by approval timelines."""
    actor = actor or {}
    return {
        'login': actor.get('login', ''),
        'databaseId': actor.get('id') if actor.get('type') == 'User' else None,
    }


def _rest_label_event(event: dict) -> dict:
    """Convert a REST label or issue state event to the shared timeline format."""
    return {
        '__typename': f'{event["event"].capitalize()}Event',
        'createdAt': event['created_at'],
        'label': event.get('label'),
        'actor': _rest_actor(event.get('actor')),
    }


def _load_legacy_timeline(session: requests.Session, repository: str, number: int, limiter: RateLimiter) -> list:
    """REST preserves the names of the retired add-game and add-movie labels."""
    url = f'https://api.github.com/repos/{repository}/issues/{number}/timeline'
    return [
        _rest_label_event(event)
        for event in _rest_items(session, url, limiter)
        if event['event'] in REST_TIMELINE_EVENTS
    ]


def _person(actor: dict | None) -> dict | None:
    """Identify a human GitHub account by its stable numeric ID."""
    if not actor or not actor.get('databaseId') or actor['login'].casefold() == APPROVAL_BOT:
        return None
    return {'id': actor['databaseId'], 'login': actor['login']}


def _is_approval_command(body: str) -> bool:
    """Recognize the approval command using the bot's space-separated syntax."""
    mention, _, arguments = body.strip().partition(' ')
    command, _, _ = arguments.lstrip(' ').partition(' ')
    return mention == '@LizardByte-bot' and command.lower() == 'approve'


def _confirmed_commands(session: requests.Session, timelines: dict) -> set[str]:
    """Verify that the approval bot acknowledged each candidate command."""
    comment_ids = [
        event['id']
        for timeline in timelines.values()
        for event in timeline
        if event['__typename'] == 'IssueComment' and _is_approval_command(event['body'])
    ]
    reactions = _load_connections(session, comment_ids, REACTIONS_QUERY, 'reactions')
    return {
        comment_id for comment_id, thumbs_up in reactions.items()
        if any((reaction['user'] or {}).get('login', '').casefold() == APPROVAL_BOT for reaction in thumbs_up)
    }


def _label_approver(event: dict, previous_approver: dict | None) -> dict | None:
    """Clear removed labels and trace bot-applied labels to the earlier approval."""
    if event['__typename'] == 'UnlabeledEvent':
        return None
    actor = event.get('actor') or {}
    if actor.get('login', '').casefold() == APPROVAL_BOT:
        return previous_approver
    return _person(actor)


def _find_approver(timeline: list, closed_at: str, confirmed_commands: set[str]) -> dict | None:
    """Follow the approval label and queue through the issue's final close."""
    command = queued = approver = None
    for event in timeline:
        kind = event['__typename']
        if event['createdAt'] > closed_at or (kind == 'ClosedEvent' and event['createdAt'] == closed_at):
            break
        match kind:
            case 'ReopenedEvent':
                command = queued = approver = None
            case 'IssueComment' if event['id'] in confirmed_commands:
                command = _person(event['author'])
            case 'LabeledEvent' | 'UnlabeledEvent':
                label = event['label']['name']
                if label == APPROVE_QUEUE_LABEL:
                    queued = _label_approver(event, command)
                    command = None
                elif label in APPROVAL_LABELS:
                    approver = _label_approver(event, queued)
                    command = None
    return approver


def _rest_command_acknowledged(event: dict, session: requests.Session, repo_url: str, limiter: RateLimiter) -> bool:
    """Check whether a REST timeline comment was an approval acknowledged by the bot."""
    if event['event'] != 'commented' or not _is_approval_command(event.get('body') or ''):
        return False
    if (event.get('reactions') or {}).get('+1') == 0:
        return False
    url = f'{repo_url}/issues/comments/{event["id"]}/reactions'
    return any(
        (reaction.get('user') or {}).get('login', '').casefold() == APPROVAL_BOT
        for reaction in _rest_items(session, url, limiter, content='+1')
    )


def _rest_approver(session: requests.Session, repo_url: str, number: str, closed_at: str,
                   limiter: RateLimiter) -> dict | None:
    """Replay REST evidence for an issue whose GraphQL history gave no approver."""
    timeline = []
    confirmed = set()
    for event in _rest_items(session, f'{repo_url}/issues/{number}/timeline', limiter):
        if event['event'] in REST_TIMELINE_EVENTS:
            timeline.append(_rest_label_event(event))
        elif (created_at := event.get('created_at')) and created_at <= closed_at and _rest_command_acknowledged(
                event, session, repo_url, limiter):
            comment_id = str(event['id'])
            timeline.append({
                '__typename': 'IssueComment',
                'id': comment_id,
                'createdAt': created_at,
                'author': _rest_actor(event.get('user')),
            })
            confirmed.add(comment_id)
    return _find_approver(timeline, closed_at, confirmed)


def _recheck_unattributed(session: requests.Session, repository: str, history: dict, limiter: RateLimiter) -> None:
    """Retry unresolved approvals against REST while preserving progress across runs."""
    pending = [
        (number, entry)
        for number, entry in history.items()
        if entry['approver'] is None and not entry.get('rest_checked')
    ]
    if not pending:
        return
    pending.sort(key=lambda pair: pair[1]['closed_at'], reverse=True)
    try:
        response = session.get('https://api.github.com/rate_limit', timeout=15)
        response.raise_for_status()
        remaining = response.json()['resources']['core']['remaining']
        # Most issues need a timeline request and one reaction request.
        limit = max(0, remaining - REST_REQUEST_RESERVE) // 2
        repo_url = f'https://api.github.com/repos/{repository}'
        for number, entry in pending[:limit]:
            entry['approver'] = _rest_approver(session, repo_url, number, entry['closed_at'], limiter)
            entry['rest_checked'] = True
    except requests.RequestException as exc:
        print_github_warning(f'Unable to finish rechecking unattributed approvals: {exc}')


def _load_approval_history(session: requests.Session, repository: str, previous: dict) -> dict:
    """Reuse unchanged attribution and backfill any newly discovered history."""
    history = {}
    changed = []
    seen = set()
    for issue in _approved_issues(session, repository):
        number = str(issue['number'])
        if number in seen:
            continue
        seen.add(number)
        cached = previous.get(number)
        if cached and cached['updated_at'] == issue['updatedAt']:
            history[number] = {**cached, 'closed_at': issue['closedAt']}
        else:
            changed.append(issue)

    modern_ids = [issue['id'] for issue in changed if issue['closedAt'] >= APPROVAL_LABEL_MIGRATION]
    timelines = _load_connections(session, modern_ids, TIMELINE_QUERY, 'timelineItems')
    confirmed = _confirmed_commands(session, timelines)
    limiter = RateLimiter(max_requests_per_second=4)
    for issue in changed:
        if issue['closedAt'] < APPROVAL_LABEL_MIGRATION:
            timeline = _load_legacy_timeline(session, repository, issue['number'], limiter)
        else:
            timeline = timelines.get(issue['id'])
            if timeline is None:
                raise requests.RequestException(f'GitHub returned no timeline for issue #{issue["number"]}')
        history[str(issue['number'])] = {
            'updated_at': issue['updatedAt'],
            'closed_at': issue['closedAt'],
            'approver': _find_approver(timeline, issue['closedAt'], confirmed),
        }
    _recheck_unattributed(session, repository, history, limiter)
    return history


def _format_last_approval(closed_at: str, today: date) -> str:
    """Express the most recent completion in whole days, weeks, months, or years."""
    days = (today - datetime.fromisoformat(closed_at).date()).days
    if days <= 0:
        return 'Today'
    for length, unit in ((365, 'year'), (30, 'month'), (7, 'week')):
        if days >= length:
            count = days // length
            return f'{count} {unit}{"s" if count != 1 else ""} ago'
    return f'{days} day{"s" if days != 1 else ""} ago'


def _rank_approvers(history: dict, session: requests.Session, today: date) -> tuple[list[TopContributor], int]:
    """Rank approvers by count and load their GitHub profiles and avatars."""
    counts = Counter()
    logins = {}
    last_approvals = {}
    for entry in sorted(history.values(), key=lambda entry: entry['updated_at'], reverse=True):
        if approver := entry['approver']:
            user_id = approver['id']
            counts[user_id] += 1
            logins.setdefault(user_id, approver['login'])
            last_approvals[user_id] = max(last_approvals.get(user_id, ''), entry['closed_at'])
    ranked = sorted(counts, key=lambda user_id: (-counts[user_id], logins[user_id].casefold()))
    approvers = []
    for user_id in ranked:
        profile = resolve_contributor_profile(str(user_id), session, fallback_login=logins[user_id])
        approvers.append(TopContributor(
            user_id=str(user_id),
            count=counts[user_id],
            profile=profile,
            avatar_data_uri=fetch_avatar_data_uri(profile, session),
            count_detail=_format_last_approval(last_approvals[user_id], today),
        ))
    return approvers, len(history) - counts.total()


def render_approval_svg(approvers: list[TopContributor], summary: str) -> str:
    """Render the approval ranking in the contribution leaderboard's style."""
    return render_leaderboard_svg(
        [{'contributors': approvers}],
        title=TITLE,
        description='People ranked by completed theme approvals, with the time since their most recent approval.',
        subtitle=summary,
        overall_limit=max(2, len(approvers)),
        empty_label=EMPTY_LABEL,
    )


def _render_approval_page(approvers: list[TopContributor], summary: str) -> str:
    """Render an accessible table with links to approver profiles."""
    lines = [
        '---', 'layout: page', f'title: {TITLE}', f'permalink: /{BASENAME}.html', '---',
        '', summary, '',
    ]
    if approvers:
        lines.extend(['| Approver | Approvals | Last approval |', '| --- | ---: | --- |'])
        for approver in approvers:
            login = approver.profile.login
            profile = f'<a href="https://github.com/{quote(login)}">@{escape(login)}</a>'
            lines.append(f'| {profile} | {approver.count:,} | {approver.count_detail} |')
    else:
        lines.append(f'{EMPTY_LABEL}.')
    return '\n'.join(lines) + '\n'


def write_approval_leaderboard(database_root: Path = Path('database')) -> None:
    """Publish the complete leaderboard and save attribution for the next refresh."""
    repository = os.environ.get('GITHUB_REPOSITORY', DEFAULT_GITHUB_REPOSITORY)
    history_file = database_root / 'approval_history.json'
    previous = json.loads(history_file.read_text(encoding='utf-8')) if history_file.exists() else {}
    today = datetime.now(timezone.utc).date()
    try:
        with create_github_session() as session:
            history = _load_approval_history(session, repository, previous)
            approvers, unattributed = _rank_approvers(history, session, today)
    except requests.RequestException as exc:
        print_github_warning(f'Unable to refresh approval leaderboard: {exc}')
        return

    total = len(history)
    summary = f'{total:,} total approvals · {total - unattributed:,} attributed · {unattributed:,} unattributed'
    summary += f' · Updated {today:%Y-%m-%d} UTC'
    chart = render_approval_svg(approvers, summary)
    database_root.mkdir(parents=True, exist_ok=True)
    (database_root / f'{BASENAME}.svg').write_text(chart, encoding='utf-8')
    (database_root / f'{BASENAME}.md').write_text(_render_approval_page(approvers, summary), encoding='utf-8')
    history_file.write_text(json.dumps(history, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')


if __name__ == '__main__':
    write_approval_leaderboard()
