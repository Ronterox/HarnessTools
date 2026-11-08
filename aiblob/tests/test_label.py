"""label.py's pure parts: title validation and the sibling list (no server)."""
import json
import os

import pytest

import label
import timeline

TIMELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web', 'timeline.json')


@pytest.mark.parametrize('title', ['User login', 'Note sharing', 'Create new note', 'API key check', "User's notes", 'Notes via email'])
def test_finished_titles_pass(title):
    assert label.title_problem(title) is None


@pytest.mark.parametrize('title, problem', [
    ('', 'empty'),
    ('   ', 'empty'),
    ('List of your saved notes today', 'long'),   # over 4 words: rejected, never clipped
    ('Remove note from the database', 'long'),
    ('List of your', 'fragment'),
    ('View a single', 'fragment'),
    ('Checks if user', 'fragment'),
    ('Login for', 'fragment'),
    ('Handle login using', 'fragment'),
    ('Redirect user after', 'fragment'),
    ('Notes shared via', 'fragment'),
    ('Delete note without', 'fragment'),
    ('Show notes per', 'fragment'),
    ('Notes listing including', 'fragment'),
    ('Save note then', 'fragment'),
    ('Edit note so', 'fragment'),
    ('Login form \u2014', 'fragment'),
    ('User login ...', 'fragment'),
    ("Displays user's", 'fragment'),
    ('Displays user\u2019s', 'fragment'),
])
def test_bad_titles_are_rejected(title, problem):
    assert label.title_problem(title) == problem


def test_parse_label_keeps_the_title_whole():
    reply = '{"emoji": "x", "title": "List of your saved notes", "summary": "s", "risk": "low: display"}'
    assert label.parse_label(reply)['title'] == 'List of your saved notes'


def test_siblings_same_module_first_and_not_itself():
    titles = {'a.x': 'Login', 'a.y': 'Logout', 'b.z': 'Notes list', 'a.w': 'Signup'}
    module_of = {'a.x': 'a', 'a.y': 'a', 'b.z': 'b', 'a.w': 'a'}
    assert label.siblings('a.x', 'a', titles, module_of) == ['Logout', 'Signup', 'Notes list']
    assert label.siblings('b.z', 'b', titles, module_of) == ['Login', 'Logout', 'Signup']
    assert label.siblings('new', 'b', {}, {}) == []


def test_written_timeline_round_trips_through_expand_and_compact():
    with open(TIMELINE) as f:
        tl = json.load(f)
    steps = timeline.expand(tl)
    again = timeline.compact(steps)
    assert json.dumps(timeline.expand(again), sort_keys=True) == json.dumps(steps, sort_keys=True)


def test_blob_the_cache_does_not_back_loses_its_label():
    old = {'title': 'Old', 'emoji': 'x', 'summary': '', 'risk': 'low'}
    new = {'title': 'New', 'emoji': 'y', 'summary': '', 'risk': 'low'}
    backed = {'id': 'a', 'kind': 'route', 'funcs': [{'hash': 'h1'}], 'label': old}
    unbacked = {'id': 'b', 'kind': 'route', 'funcs': [{'hash': 'h2'}], 'label': old}
    file_blob = {'id': 'f', 'kind': 'file', 'path': 'x.html', 'label': old}   # no _head: no key
    plain = {'id': 'c', 'kind': 'route', 'funcs': [{'hash': 'h3'}]}
    steps = [{'blobs': [backed, unbacked, file_blob]}, {'blobs': [plain, backed]}]
    label.apply_labels(steps, {'h1': new})
    assert backed['label'] == new
    assert 'label' not in unbacked and 'label' not in file_blob and 'label' not in plain
