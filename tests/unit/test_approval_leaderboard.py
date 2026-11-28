import json
from unittest.mock import Mock
from xml.etree import ElementTree

import requests

from src import approval_leaderboard as leaderboard
from src.updater import ContributorProfile, TopContributor


OPENED = '2026-01-01T00:00:00Z'
CLOSED = '2026-01-04T00:00:00Z'
REVIEWER = {'login': 'reviewer', 'databaseId': 1}
OTHER_REVIEWER = {'login': 'another-reviewer', 'databaseId': 2}
BOT = {'login': 'LizardByte-bot', 'databaseId': 3}


def response(payload, next_url=None):
    result = Mock(spec=requests.Response)
    result.json.return_value = payload
    result.links = {'next': {'url': next_url}} if next_url else {}
    return result


def page(nodes, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor}}


def issue(number, *, closed_at=CLOSED, reason='COMPLETED'):
    return {
        'id': f'issue-{number}', 'number': number, 'stateReason': reason,
        'closedAt': closed_at, 'updatedAt': closed_at,
    }


def issue_page(issues, cursor=None):
    return response({'data': {'repository': {'issues': page(issues, cursor)}}})


def event(kind, created_at=OPENED, **details):
    return {'__typename': kind, 'createdAt': created_at, **details}


def label(name, actor, *, kind='LabeledEvent'):
    return event(kind, label={'name': name}, actor=actor)


def command(comment_id, author=REVIEWER, body='@LizardByte-bot approve'):
    return event('IssueComment', id=comment_id, body=body, author=author)


def test_backfills_both_approval_paths_and_reuses_unchanged_history(tmp_path, monkeypatch):
    session = requests.Session()
    legacy_time = '2023-01-02T00:00:00Z'
    direct, queued, automated, legacy, exception, cached = (
        issue(1), issue(2), issue(3), issue(4, closed_at=legacy_time), issue(5, reason='NOT_PLANNED'), issue(6)
    )
    history_file = tmp_path / 'approval_history.json'
    previous = {
        '6': {'updated_at': CLOSED, 'approver': {'id': 2, 'login': 'another-reviewer'}},
        '7': {'updated_at': CLOSED, 'approver': {'id': 9, 'login': 'reopened-since-last-refresh'}},
    }
    history_file.write_text(json.dumps(previous), encoding='utf-8')
    post = Mock(side_effect=[
        issue_page([direct, queued, exception], 'issues-cursor'),
        issue_page([queued, automated, legacy, cached]),
        response({'data': {'nodes': [
            {'id': direct['id'], 'timelineItems': page([label('approve-theme', REVIEWER)])},
            {'id': queued['id'], 'timelineItems': page([
                command('accepted', body='@LizardByte-bot  APPROVE  '),
                command('unconfirmed', author=OTHER_REVIEWER),
                command('check', body='@LizardByte-bot check'),
            ], 'timeline-cursor')},
            {'id': automated['id'], 'timelineItems': page([
                label('approve-queue', BOT), label('approve-theme', BOT),
                label('approve-queue', BOT, kind='UnlabeledEvent'),
            ])},
        ]}}),
        response({'data': {'node': {'timelineItems': page([
            label('approve-queue', BOT), label('approve-theme', BOT), event('ClosedEvent', CLOSED),
        ])}}}),
        response({'data': {'nodes': [
            {'id': 'accepted', 'reactions': page([{'user': None}], 'reactions-cursor')},
            {'id': 'unconfirmed', 'reactions': page([{'user': {'login': 'a-reader'}}])},
        ]}}),
        response({'data': {'node': {'reactions': page([{'user': {'login': 'LizardByte-bot'}}])}}}),
    ])
    rest = Mock(side_effect=[
        response([
            {'event': 'cross-referenced'},
            {'event': 'labeled', 'created_at': '2023-01-01T00:00:00Z', 'label': {'name': 'add-movie'},
             'actor': {'login': 'old-reviewer', 'id': 1, 'type': 'User'}},
        ], next_url='https://api.github.com/test?page=2'),
        response([
            {'event': 'closed', 'created_at': legacy_time},
            {'event': 'labeled', 'created_at': '2023-08-07T00:08:41Z', 'label': {'name': 'approve-theme'},
             'actor': {'login': 'migration-operator', 'id': 10, 'type': 'User'}},
        ]),
    ])
    monkeypatch.setenv('GITHUB_REPOSITORY', 'example/ThemerrDB')
    monkeypatch.setattr(session, 'post', post)
    monkeypatch.setattr(session, 'get', rest)
    monkeypatch.setattr(leaderboard, 'create_github_session', lambda: session)
    monkeypatch.setattr(leaderboard, 'fetch_avatar_data_uri', lambda profile, session: 'data:image/png;base64,YQ==')

    leaderboard.write_approval_leaderboard(tmp_path)

    variables = [call.kwargs['json']['variables'] for call in post.call_args_list]
    assert variables == [
        {'owner': 'example', 'name': 'ThemerrDB', 'label': 'approve-theme', 'cursor': None},
        {'owner': 'example', 'name': 'ThemerrDB', 'label': 'approve-theme', 'cursor': 'issues-cursor'},
        {'ids': ('issue-1', 'issue-2', 'issue-3')},
        {'id': 'issue-2', 'cursor': 'timeline-cursor'},
        {'ids': ('accepted', 'unconfirmed')},
        {'id': 'accepted', 'cursor': 'reactions-cursor'},
    ]
    assert [call.args[0] for call in rest.call_args_list] == [
        'https://api.github.com/repos/example/ThemerrDB/issues/4/timeline',
        'https://api.github.com/test?page=2',
    ]
    history = json.loads(history_file.read_text(encoding='utf-8'))
    assert set(history) == {'1', '2', '3', '4', '6'}
    assert history['2']['approver'] == {'id': 1, 'login': 'reviewer'}
    assert history['3']['approver'] is None
    assert history['4']['approver'] == {'id': 1, 'login': 'old-reviewer'}
    page_text = (tmp_path / 'approval_leaderboard.md').read_text(encoding='utf-8')
    summary = '5 total approvals · 4 attributed · 1 unattributed'
    assert summary in page_text
    assert 'https://github.com/reviewer">@reviewer</a> | 3 |' in page_text
    assert 'https://github.com/another-reviewer">@another-reviewer</a> | 1 |' in page_text
    assert 'old-reviewer' not in page_text
    chart = ElementTree.parse(tmp_path / 'approval_leaderboard.svg')
    svg = {'svg': 'http://www.w3.org/2000/svg'}
    assert chart.find('svg:title', svg).text == 'Approval Leaderboard'
    assert summary in chart.find('svg:text[@class="subtitle"]', svg).text
    assert chart.find('svg:text[@class="section-title"]', svg) is None
    for css_class, expected in [('rank', ['1', '2']), ('login', ['@reviewer', '@another-reviewer']),
                                ('count', ['3', '1'])]:
        assert [node.text for node in chart.findall(f'svg:text[@class="{css_class}"]', svg)] == expected
    assert len(chart.findall('svg:image', svg)) == 2


def test_approval_chart_shows_every_ranked_approver():
    approvers = []
    for rank in range(1, 13):
        login = f'reviewer-{rank}'
        profile = ContributorProfile(str(rank), login, f'Reviewer {rank}', '')
        approvers.append(TopContributor(profile.user_id, 13 - rank, profile))
    chart = ElementTree.fromstring(leaderboard.render_approval_svg(approvers, '78 total approvals'))
    svg = {'svg': 'http://www.w3.org/2000/svg'}

    assert [node.text for node in chart.findall('svg:text[@class="login"]', svg)] == [
        f'@reviewer-{rank}' for rank in range(1, 13)
    ]


def test_attribution_follows_the_final_approval_after_reopening():
    timeline = [
        label('approve-theme', OTHER_REVIEWER),
        event('ClosedEvent', '2026-01-02T00:00:00Z'),
        event('ReopenedEvent', '2026-01-02T00:01:00Z'),
        label('request-theme', None),
        label('approve-queue', REVIEWER),
        label('approve-theme', BOT),
        label('approve-queue', None, kind='UnlabeledEvent'),
        command('late-retry', OTHER_REVIEWER),
        label('approve-theme', BOT, kind='UnlabeledEvent'),
        label('approve-queue', REVIEWER),
        label('approve-theme', BOT),
        event('LabeledEvent', '2026-01-05T00:00:00Z', label={'name': 'approve-theme'}, actor=OTHER_REVIEWER),
    ]
    for retry_event in timeline[3:-1]:
        retry_event['createdAt'] = '2026-01-03T00:00:00Z'

    assert leaderboard._find_approver(timeline, CLOSED, {'late-retry'}) == {'id': 1, 'login': 'reviewer'}
    assert leaderboard._find_approver(timeline[:-3], CLOSED, {'late-retry'}) is None


def test_automated_or_missing_people_do_not_get_credit():
    for actor in (None, BOT, {'login': 'github-actions'}):
        timeline = [label('approve-theme', actor), event('ClosedEvent', CLOSED)]
        assert leaderboard._find_approver(timeline, CLOSED, set()) is None


def test_empty_leaderboard_is_published(tmp_path, monkeypatch):
    session = requests.Session()
    monkeypatch.setattr(session, 'post', Mock(return_value=issue_page([])))
    monkeypatch.setattr(leaderboard, 'create_github_session', lambda: session)

    leaderboard.write_approval_leaderboard(tmp_path)

    assert 'No attributed theme approvals.' in (tmp_path / 'approval_leaderboard.md').read_text(encoding='utf-8')
    assert 'No attributed theme approvals' in (tmp_path / 'approval_leaderboard.svg').read_text(encoding='utf-8')
    assert json.loads((tmp_path / 'approval_history.json').read_text(encoding='utf-8')) == {}


def test_refresh_failure_preserves_the_previous_leaderboard(tmp_path, monkeypatch, capsys):
    history_file = tmp_path / 'approval_history.json'
    chart_file = tmp_path / 'approval_leaderboard.svg'
    history_file.write_text('{}', encoding='utf-8')
    chart_file.write_text('previous chart', encoding='utf-8')
    session = requests.Session()
    monkeypatch.setattr(leaderboard, 'create_github_session', lambda: session)
    post = Mock(side_effect=[
        response({'data': {}, 'errors': [{'message': 'query limit'}]}),
        issue_page([issue(1)]),
        response({'data': {'nodes': [None]}}),
    ])
    monkeypatch.setattr(session, 'post', post)

    leaderboard.write_approval_leaderboard(tmp_path)
    leaderboard.write_approval_leaderboard(tmp_path)

    assert history_file.read_text(encoding='utf-8') == '{}'
    assert chart_file.read_text(encoding='utf-8') == 'previous chart'
    output = capsys.readouterr().out
    assert 'Unable to refresh approval leaderboard: GitHub GraphQL: query limit' in output
    assert 'GitHub returned no timeline for issue #1' in output
