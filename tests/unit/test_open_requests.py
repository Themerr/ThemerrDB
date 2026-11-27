# standard imports
from unittest.mock import Mock, call
from xml.etree import ElementTree

# lib imports
import requests

# local imports
from src import updater


def make_issues_response(issues, next_url=None):
    response = Mock(spec=requests.Response)
    response.json.return_value = issues
    response.links = {'next': {'url': next_url}} if next_url else {}
    return response


def test_open_request_outputs_page_through_issues_and_link_all_authors(tmp_path, monkeypatch):
    repository = 'example/ThemerrDB'
    monkeypatch.setenv('GITHUB_REPOSITORY', repository)
    api_url = f'https://api.github.com/repos/{repository}/issues'
    next_url = f'{api_url}?page=2'
    first_issue = {'number': 1, 'user': {'login': 'octocat'}, 'assignee': {'login': 'reviewer'}}
    first_page = make_issues_response([
        first_issue,
        {'number': 2, 'user': {'login': 'octocat'}},
        {'number': 3, 'user': None},
        {'number': 4, 'user': {'login': 'pull-author'}, 'pull_request': {}},
    ], next_url=next_url)
    second_page = make_issues_response([
        first_issue,
        {'number': 5, 'user': {'login': 'octocat'}},
        *({'number': number, 'user': {'login': f'user-{number}'}} for number in range(10, 20)),
    ])
    session = requests.Session()
    get_issues = Mock(side_effect=[first_page, second_page])
    monkeypatch.setattr(session, 'get', get_issues)
    monkeypatch.setattr(updater, 'create_github_session', lambda: session)

    updater._write_open_request_outputs(database_root=str(tmp_path))

    assert get_issues.call_args_list == [
        call(api_url, params={'state': 'open', 'labels': 'request-theme', 'per_page': 100, 'direction': 'asc'},
             timeout=15),
        call(next_url, params=None, timeout=15),
    ]
    svg = ElementTree.parse(tmp_path / 'open_requests.svg')
    labels = [text.text for text in svg.findall('.//{http://www.w3.org/2000/svg}text')]
    assert '3' in labels
    assert '14 open' in labels[1]
    assert 'Unknown author' in labels
    assert [label for label in labels if label.startswith('@')] == [
        '@octocat', *(f'@user-{number}' for number in range(10, 20)),
    ]
    page = (tmp_path / 'open_requests.md').read_text(encoding='utf-8')
    assert labels[1] in page
    assert (
        'https://github.com/example/ThemerrDB/issues?'
        'q=is%3Aissue%20is%3Aopen%20label%3Arequest-theme%20author%3Aoctocat">@octocat</a> | 3 |'
    ) in page
    assert '@user-19</a> | 1 |' in page
    assert '| Unknown author | 1 |' in page
    assert '@reviewer' not in page
    assert '@pull-author' not in page


def test_open_request_refresh_failure_keeps_the_previous_snapshot(tmp_path, monkeypatch, capsys):
    svg = tmp_path / 'open_requests.svg'
    page = tmp_path / 'open_requests.md'
    svg.write_text('previous chart', encoding='utf-8')
    page.write_text('previous table', encoding='utf-8')
    session = requests.Session()
    first_page = make_issues_response([], next_url='https://api.github.com/issues?page=2')
    monkeypatch.setattr(session, 'get', Mock(side_effect=[first_page, requests.Timeout('second page failed')]))
    monkeypatch.setattr(updater, 'create_github_session', lambda: session)

    updater._write_open_request_outputs(database_root=str(tmp_path))

    assert svg.read_text(encoding='utf-8') == 'previous chart'
    assert page.read_text(encoding='utf-8') == 'previous table'
    assert 'Unable to refresh open theme request counts' in capsys.readouterr().out
