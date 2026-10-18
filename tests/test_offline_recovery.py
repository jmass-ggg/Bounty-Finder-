"""Downtime coverage, claim evidence and fair priority under a large old backlog."""

import json
import re
import time
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from test_all_prices import options
from test_discovery import NOW, issue, no_sleep, payload
from test_verification import FakeInspector, candidate

from bountyradar import cli
from bountyradar.delivery import github_revalidator
from bountyradar.discovery import API_URL, GitHubDiscovery
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.models import SearchProgress, SearchWindow
from bountyradar.run_state import RunState
from bountyradar.verification import Verifier
from bountyradar.verification_models import IssueInspection


@pytest.mark.asyncio
@pytest.mark.parametrize("downtime", [5, 8, 12, 24])
async def test_offline_period_full_source_claim_outbox_pipeline(tmp_path, respx_mock,
                                                              monkeypatch, downtime):
    start = NOW - timedelta(hours=downtime)
    amounts = [5, 10, 50, 100, 200]
    items = [issue(n, body=f"Bounty reward ${amount}", comments=int(n == 3),
                   created_at=(start+timedelta(minutes=n)).isoformat(),
                   updated_at=(start+timedelta(minutes=n)).isoformat(),
                   assignees=[{"login":"builder"}] if n == 2 else [])
             for n, amount in enumerate(amounts, 1)]
    queries = []

    def search(request):
        queries.append(request.url.params['q'])
        return httpx.Response(200, json=payload(items))

    def inspect(request):
        if request.url.path.endswith('/timeline'):
            return httpx.Response(200, json=[])
        if request.url.path.endswith('/comments'):
            return httpx.Response(200, json=[dict(
                body="Assigned to @builder for implementation.", author_association="OWNER",
                created_at=start.isoformat(), html_url="https://github.com/acme/tool/issues/3#c1")])
        return httpx.Response(200, json=items[int(request.url.path.rsplit('/', 1)[1])-1])

    respx_mock.get(API_URL).mock(side_effect=search)
    respx_mock.get(url__regex=r"https://api\.github\.com/repos/acme/tool/issues/").mock(
        side_effect=inspect)
    monkeypatch.setattr(cli, 'GitHubDiscovery', lambda client, settings, **kw:
                        GitHubDiscovery(client, settings, **kw, sleep=no_sleep))
    settings = options(database_path=tmp_path/'offline.db', search_terms=['bounty'],
                       search_labels=[], monetary_terms=[], notification_mode='include_unverified',
                       email_enabled=True, smtp_username='sender@example.com',
                       smtp_password='fixture', email_to='sender@example.com')
    state = RunState(settings.database_path)
    state.save([], [], completed_at=start)
    state.close()
    messages = []

    class Transport:
        async def send(self, message):
            messages.append(message)

    summary = await cli._run(settings, False, transport=Transport(), clock=lambda: NOW)
    assert summary[2:4] == (3, 3)
    assert summary[4:] == (0, 0)
    expected = (start-timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert all(expected in q for q in queries)
    assert all('POTENTIAL_UNVERIFIED' in str(m['Subject']) for m in messages)
    assert all('APPARENTLY_UNCLAIMED' in m.as_string() for m in messages)
    # A normal five-minute rescan uses the actual interval plus overlap, not 8/24/72h.
    queries.clear()
    assert (await cli._run(settings, False, transport=Transport(),
                           clock=lambda: NOW+timedelta(minutes=5)))[3] == 0
    assert len(messages) == 3
    expected = (NOW-timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert all(expected in q for q in queries)
    state = RunState(settings.database_path)
    try:
        statuses = {int(r[0].split(':')[1]):json.loads(r[1])['claim_status'] for r in
                    state.db.execute(
                        'select source_issue_id,payload_json from verification_results')}
        assert statuses == {1:'APPARENTLY_UNCLAIMED', 2:'CLAIMED', 3:'CLAIMED',
                            4:'APPARENTLY_UNCLAIMED', 5:'APPARENTLY_UNCLAIMED'}
        assert state.db.execute('select count(*) from discovered_issues').fetchone()[0] == 5
        assert state.db.execute('pragma integrity_check').fetchone()[0] == 'ok'
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('checkpoint', [None, 'invalid', '2026-10-20T00:00:00+00:00'])
async def test_only_missing_or_invalid_checkpoint_uses_eight_hours(tmp_path, checkpoint):
    from bountyradar.models import ScanResult
    s = options(database_path=tmp_path/'cold.db')
    state = RunState(s.database_path)
    if checkpoint:
        state.db.execute("insert into scan_state values ('last_complete_at:github',?)",
                         (checkpoint,))
        state.db.commit()
    state.close()
    lookbacks = []

    class Source:
        name = 'github'

        async def scan(self, hours):
            lookbacks.append(hours)
            return ScanResult(candidates=[], unprocessed_windows=[])

    await cli._run(s, False, sources=[Source()], clock=lambda: NOW)
    assert lookbacks == [8]


@pytest.mark.asyncio
async def test_twelve_hour_split_work_resumes_without_advancing_early(tmp_path, respx_mock):
    state = RunState(tmp_path/'split.db')
    s = options(search_terms=['bounty'], search_labels=[], monetary_terms=[],
                github_max_search_requests_per_scan=2)
    items = [issue(n, created_at=(NOW-timedelta(hours=n)).isoformat(),
                   updated_at=(NOW-timedelta(hours=n)).isoformat()) for n in range(1, 6)]

    def respond(request):
        from datetime import datetime
        match = re.search(r'(created|updated):([^ ]+)\.\.([^ ]+)', request.url.params['q'])
        field, lo, hi = match.groups()
        lo, hi = datetime.fromisoformat(lo), datetime.fromisoformat(hi)
        selected = [i for i in items if lo <= datetime.fromisoformat(i[field+'_at']) <= hi]
        if hi-lo > timedelta(hours=6):
            return httpx.Response(200, json=payload([], total=1001))
        return httpx.Response(200, json=payload(selected))

    respx_mock.get(API_URL).mock(side_effect=respond)
    for _attempt in range(8):
        async with httpx.AsyncClient() as client:
            scan = await GitHubDiscovery(client, s, progress=state, clock=lambda: NOW,
                                         sleep=no_sleep).scan(12)
        assert scan.requests <= 2
        if scan.complete:
            state.save([], [], completed_at=scan.covered_until)
            break
        assert state.last_complete_at() is None
        assert state.search_progress().windows
        state.close()
        state = RunState(tmp_path/'split.db')
    else:
        pytest.fail('finite catch-up windows did not drain')
    assert _attempt > 0
    assert state.last_complete_at() == NOW
    assert state.db.execute('select count(*) from discovered_issues').fetchone()[0] == 5
    assert state.db.execute('pragma integrity_check').fetchone()[0] == 'ok'
    state.close()


@pytest.mark.asyncio
async def test_fresh_lane_does_not_lose_historical_cursor_or_exceed_budget(tmp_path, respx_mock):
    state = RunState(tmp_path/'lanes.db')
    old_time = NOW-timedelta(hours=12)
    window = SearchWindow(category='created', term='bounty', start=old_time-timedelta(hours=1),
                          end=old_time, field='created', page=2, total=101)
    state.save([], [], completed_at=None, progress=SearchProgress(
        covered_until=old_time, windows=[window]))
    requests = []

    def respond(request):
        requests.append(request.url)
        is_old = request.url.params['page'] == '2'
        return httpx.Response(200, json=payload([issue(1 if is_old else 99)],
                                                total=101 if is_old else 1))

    respx_mock.get(API_URL).mock(side_effect=respond)
    s = options(search_terms=['bounty'], search_labels=[], monetary_terms=[],
                github_max_search_requests_per_scan=4)
    async with httpx.AsyncClient() as client:
        scan = await GitHubDiscovery(client, s, progress=state, clock=lambda: NOW,
                                     sleep=no_sleep).scan(12)
    assert requests[0].params['page'] == '1'
    assert requests[1].params['q'] == window.query
    assert requests[1].params['page'] == '2'
    assert scan.requests == 2 <= 4
    assert scan.complete and scan.covered_until == old_time
    assert scan.fresh_unprocessed_windows
    state.save([], [], completed_at=scan.covered_until)
    assert state.search_progress() is None
    assert state.search_progress('github_fresh').windows
    assert state.db.execute('select count(*) from discovered_issues').fetchone()[0] == 2
    state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('labels,comments,status', [
    (['claimed'], [], 'CLAIMED'), (['reserved'], [], 'CLAIMED'),
    (['in-progress'], [], 'CLAIMED'), (['unclaimed'], [], 'APPARENTLY_UNCLAIMED'),
    ([], [{'body':'Can I work on this?', 'author_association':'NONE'}], 'UNKNOWN'),
    ([], [{'body':'Assigned to @builder.', 'author_association':'OWNER'}], 'CLAIMED'),
    ([], [{'body':'Not assigned to @builder.', 'author_association':'OWNER'}], 'UNKNOWN'),
    ([], [{'body':'Can I work on this?', 'author_association':'NONE'},
          {'body':'Go ahead @builder.', 'author_association':'MEMBER'}], 'CLAIMED'),
])
async def test_claim_evidence_and_ambiguity(respx_mock, labels, comments, status):
    data = issue(8, labels=[{'name':label} for label in labels], comments=len(comments))
    respx_mock.get('https://api.github.com/repos/acme/tool/issues/8').respond(200, json=data)
    respx_mock.get('https://api.github.com/repos/acme/tool/issues/8/comments').respond(
        200, json=comments)
    respx_mock.get('https://api.github.com/repos/acme/tool/issues/8/timeline').respond(200, json=[])
    s = options(notification_mode='include_unverified')
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, s, clock=lambda: NOW, sleep=no_sleep)
        r = await Verifier(inspector, s, clock=lambda: NOW).verify(candidate())
    assert r.claim_status.value == status
    assert r.notification_eligible is (status == 'APPARENTLY_UNCLAIMED')


@pytest.mark.asyncio
async def test_claimed_before_delivery_and_announcement_dates(respx_mock):
    data = issue(8, comments=1, assignees=[{'login':'builder'}])
    respx_mock.get('https://api.github.com/repos/acme/tool/issues/8').respond(200, json=data)
    respx_mock.get('https://api.github.com/repos/acme/tool/issues/8/comments').respond(200, json=[{
        'body':'Bounty reward $5', 'created_at':(NOW-timedelta(hours=2)).isoformat()}])
    respx_mock.get('https://api.github.com/repos/acme/tool/issues/8/timeline').respond(200, json=[])
    from test_email import result
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, options(), clock=lambda: NOW)
        assert await github_revalidator(inspector, result(), now=NOW) is False
        r = await Verifier(inspector, options(), clock=lambda: NOW).verify(candidate())
    assert r.claim_status.value == 'CLAIMED'
    assert r.reward_announced_at == NOW-timedelta(hours=2)


@pytest.mark.asyncio
async def test_fifty_thousand_old_candidates_do_not_block_fresh_and_remain_fair(tmp_path):
    started = time.monotonic()
    state = RunState(tmp_path/'large.db')
    old = NOW-timedelta(days=30)
    rows = [candidate(source_issue_id=n, issue_number=n,
                      url=f'https://github.com/acme/tool/issues/{n}',
                      created_at=old, updated_at=old, discovered_at=old)
            for n in range(1, 50001)]
    state.save(rows, [], completed_at=None)
    for n in (60001, 60002):
        state.save([candidate(source_issue_id=n, issue_number=n,
                              url=f'https://github.com/acme/tool/issues/{n}',
                              body='Bounty $5', potential_reward_amount=Decimal(5))], [],
                   completed_at=None)
    queued_at = time.monotonic()
    work = state.verification_queue(8, now=NOW)
    queue_seconds = time.monotonic()-queued_at
    ids = [c.source_issue_id for c in work]
    assert ids[:2] == [60001, 60002]
    assert len(ids) == 8 and any(n <= 50000 for n in ids)
    verifier = Verifier(FakeInspector(), options(), clock=lambda: NOW)
    results = [await verifier.verify(c) for c in work]
    verified_seconds = time.monotonic()-queued_at
    state.save([], results, completed_at=None)
    next_ids = {c.source_issue_id for c in
                state.verification_queue(8, now=NOW+timedelta(minutes=5))}
    assert not next_ids.intersection(ids)
    assert state.db.execute('pragma integrity_check').fetchone()[0] == 'ok'
    state.close()
    print(f'50000-backlog queue selection: {queue_seconds:.3f}s; '
          f'8 offline verifications including fresh work: {verified_seconds:.3f}s')
    print(f'50000-backlog fixture including creation/integrity: {time.monotonic()-started:.3f}s')


@pytest.mark.asyncio
@pytest.mark.parametrize('body,amount', [('No reward offered', None), ('Bounty $0', Decimal(0)),
                                      ('Bounty $1', Decimal(1))])
async def test_current_issue_amount_replaces_stale_search_amount(body, amount):
    inspector = FakeInspector(IssueInspection(state='open', title='Repair parser', body=body))
    s = options(min_reward_usd=5, notification_mode='include_unverified')
    r = await Verifier(inspector, s, clock=lambda: NOW).verify(candidate())
    assert r.amount == amount
    assert not r.notification_eligible


@pytest.mark.asyncio
async def test_reward_announced_only_in_comment_is_retained_and_revalidated(respx_mock):
    base = 'https://api.github.com/repos/acme/tool/issues/8'
    respx_mock.get(base).respond(200, json=issue(8, title='Repair parser', body='Please fix',
                                               comments=1))
    respx_mock.get(base+'/comments').respond(200, json=[{
        'body':'Bounty reward $5', 'author_association':'OWNER',
        'created_at':(NOW-timedelta(hours=1)).isoformat()}])
    respx_mock.get(base+'/timeline').respond(200, json=[])
    s = options(notification_mode='include_unverified')
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, s, clock=lambda: NOW)
        r = await Verifier(inspector, s, clock=lambda: NOW).verify(
            candidate(potential_reward_amount=None, currency=None))
        assert r.amount == Decimal(5) and r.currency == 'USD'
        assert r.notification_eligible
        assert r.reward_announced_at == NOW-timedelta(hours=1)
        assert await github_revalidator(inspector, r, now=NOW) is True


@pytest.mark.asyncio
@pytest.mark.parametrize('body,expected', [('Bounty $0', False), ('Bounty $10', None)])
async def test_unverified_delivery_checks_for_removed_or_changed_price(respx_mock, body, expected):
    from test_email import result

    from bountyradar.verification_models import Category
    base = 'https://api.github.com/repos/acme/tool/issues/8'
    respx_mock.get(base).respond(200, json=issue(8, body=body, comments=0))
    respx_mock.get(base+'/timeline').respond(200, json=[])
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, options(), clock=lambda: NOW)
        previous = result(amount='5', category=Category.REWARD_CLAIMED)
        assert await github_revalidator(inspector, previous, now=NOW) is expected


@pytest.mark.asyncio
async def test_one_slot_queue_preserves_old_work_under_continual_arrivals(tmp_path):
    path = tmp_path/'fair.db'
    old = candidate(source_issue_id=1, issue_number=1, url='https://github.com/acme/tool/issues/1',
                    created_at=NOW-timedelta(days=30), updated_at=NOW-timedelta(days=30),
                    discovered_at=NOW-timedelta(days=30))
    state = RunState(path)
    state.save([old], [], completed_at=None)
    picked = []
    for turn in range(4):
        now = NOW+timedelta(minutes=5*turn)
        fresh = candidate(source_issue_id=100+turn, issue_number=100+turn,
                          url=f'https://github.com/acme/tool/issues/{100+turn}',
                          created_at=now, updated_at=now, discovered_at=now)
        state.save([fresh], [], completed_at=None)
        work = state.verification_queue(1, now=now)
        picked.append(work[0].source_issue_id)
        r = await Verifier(FakeInspector(), options(), clock=lambda now=now: now).verify(work[0])
        state.save([], [r], completed_at=None)
        state.close()
        state = RunState(path)
    assert picked[:3] == [100, 101, 102]
    assert picked[3] == 1
    state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['claimed', 'reserved', 'in_progress'])
async def test_trusted_provider_claim_prevents_alert(status):
    from test_verification import FakeFunding, funded_record
    s = options(notification_mode='include_unverified')
    r = await Verifier(FakeInspector(), s, FakeFunding([funded_record(status=status)]),
                       clock=lambda: NOW).verify(candidate())
    assert r.claim_status.value == 'CLAIMED'
    assert not r.notification_eligible


def test_newly_recovered_twelve_hour_bounty_precedes_deferred_recent_work(tmp_path):
    state = RunState(tmp_path/'recovered.db')
    prior = candidate(discovered_at=NOW-timedelta(hours=2), updated_at=NOW-timedelta(hours=1))
    recovered = candidate(source_issue_id=9, issue_number=9,
                          url='https://github.com/acme/tool/issues/9',
                          discovered_at=NOW, updated_at=NOW-timedelta(hours=12))
    state.save([prior, recovered], [], completed_at=None)
    assert state.verification_queue(1, now=NOW)[0].source_issue_id == 9
    state.close()


@pytest.mark.parametrize('batch', [1, 2, 5, 20, 100])
def test_priority_batch_rounding_empty_queue_and_historical_share(tmp_path, batch):
    state = RunState(tmp_path/'rounding.db')
    old = NOW-timedelta(days=30)
    historical = [candidate(source_issue_id=n, issue_number=n,
                            url=f'https://github.com/acme/tool/issues/{n}',
                            created_at=old, updated_at=old, discovered_at=old)
                  for n in range(1, 201)]
    recent = [candidate(source_issue_id=1000+n, issue_number=1000+n,
                        url=f'https://github.com/acme/tool/issues/{1000+n}') for n in range(200)]
    state.save(historical+recent, [], completed_at=None)
    for turn in range(4 if batch == 1 else 1):
        work = state.verification_queue(batch, now=NOW)
        assert len(work) == batch
        old_count = sum(c.source_issue_id < 1000 for c in work)
        assert old_count == (int(turn == 3) if batch == 1 else max(1, batch//4))
    state.close()
    empty = RunState(tmp_path/'empty.db')
    assert empty.verification_queue(batch, now=NOW) == []
    empty.save([recent[0]], [], completed_at=None)
    assert len(empty.verification_queue(batch, now=NOW)) == 1
    empty.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['Bounty $100 or $50', 'Reward USD 5 or EUR 5',
                                  'Reward $1,00', 'Reward $1.23.45', 'Reward 10 DOGE',
                                  'Reward $10 DOGE'])
async def test_ambiguous_or_unsupported_current_rewards_never_email(body):
    inspector = FakeInspector(IssueInspection(state='open', title='Repair parser', body=body))
    s = options(notification_mode='include_unverified')
    r = await Verifier(inspector, s, clock=lambda: NOW).verify(candidate())
    assert r.amount is None
    assert not r.notification_eligible


@pytest.mark.asyncio
async def test_comment_edit_or_deletion_does_not_reuse_old_price(respx_mock):
    from bountyradar.notification import eligible_for_delivery
    base = 'https://api.github.com/repos/acme/tool/issues/8'
    respx_mock.get(base).respond(200, json=issue(
        8, title='Parser', body='Please repair', comments=1))
    comments = respx_mock.get(base+'/comments').respond(200, json=[{
        'body':'Bounty $100', 'author_association':'OWNER',
        'created_at':(NOW-timedelta(hours=1)).isoformat()}])
    respx_mock.get(base+'/timeline').respond(200, json=[])
    s = options(notification_mode='include_unverified')
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, s, clock=lambda: NOW)
        r = await Verifier(inspector, s, clock=lambda: NOW).verify(candidate())
        assert r.amount == 100 and eligible_for_delivery(r, s, NOW)
        comments.respond(200, json=[{'body':'Bounty $50',
                                     'author_association':'OWNER',
                                     'created_at':(NOW-timedelta(hours=1)).isoformat()}])
        assert await github_revalidator(inspector, r, now=NOW) is None
        new = await Verifier(inspector, s, clock=lambda: NOW).verify(candidate())
        assert new.amount == 50
        comments.respond(200, json=[])
        assert await github_revalidator(inspector, new, now=NOW) is False


def test_stale_historical_page_cannot_overwrite_newer_finding(tmp_path):
    state = RunState(tmp_path/'version.db')
    newer = candidate(body='Bounty $50', potential_reward_amount=Decimal(50))
    stale = candidate(body='Bounty $100', updated_at=NOW-timedelta(hours=1))
    state.save([newer], [], completed_at=None)
    state.save([stale], [], completed_at=None)
    assert state.verification_queue(1, now=NOW)[0].potential_reward_amount == 50
    state.close()


@pytest.mark.asyncio
async def test_priority_order_survives_verifier_dispatch():
    inspected = []

    class Inspector(FakeInspector):
        async def inspect(self, c):
            inspected.append(c.source_issue_id)
            return await super().inspect(c)

    older = candidate(source_issue_id=9, updated_at=NOW-timedelta(hours=12))
    await Verifier(Inspector(), options(), clock=lambda: NOW).verify_many(
        [older, candidate()], preserve_order=True)
    assert inspected == [9, 8]


@pytest.mark.asyncio
async def test_delayed_indexing_in_overlap_and_committed_checkpoint_restart(tmp_path,
                                                                         respx_mock):
    state = RunState(tmp_path/'indexing.db')
    state.save([], [], completed_at=NOW)
    state.close()
    delayed = issue(created_at=(NOW-timedelta(minutes=2)).isoformat(),
                    updated_at=(NOW-timedelta(minutes=2)).isoformat())
    respx_mock.get(API_URL).respond(200, json=payload([delayed]))
    s = options(database_path=tmp_path/'indexing.db', search_terms=['bounty'],
                search_labels=[], monetary_terms=[])
    # Inspect via an offline client; checkpoint survives reopen and late indexing is in overlap.
    async with httpx.AsyncClient() as client:
        state = RunState(s.database_path)
        elapsed = (NOW+timedelta(minutes=5)-state.last_complete_at()).total_seconds()/3600
        scan = await GitHubDiscovery(client, s, progress=state,
                                     clock=lambda: NOW+timedelta(minutes=5),
                                     sleep=no_sleep).scan(elapsed+5/60)
        assert scan.complete and len(scan.candidates) == 1
        state.save([], [], completed_at=scan.covered_until)
        state.close()
        state = RunState(s.database_path)
        assert state.last_complete_at() == NOW+timedelta(minutes=5)
        assert state.db.execute('select count(*) from discovered_issues').fetchone()[0] == 1
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['title', 'state', 'labels', 'assignees', 'comments', 'body'])
async def test_missing_live_issue_fields_cannot_prove_unclaimed(respx_mock, missing):
    data = issue(8, comments=0)
    del data[missing]
    respx_mock.get('https://api.github.com/repos/acme/tool/issues/8').respond(200, json=data)
    s = options(notification_mode='include_unverified')
    async with httpx.AsyncClient() as client:
        r = await Verifier(GitHubIssueInspector(client, s), s).verify(candidate())
    assert r.claim_status.value == 'UNKNOWN'
    assert not r.notification_eligible


@pytest.mark.asyncio
async def test_historical_inspection_retains_http_capacity_when_recent_work_is_dense(
    tmp_path, respx_mock,
):
    from bountyradar.models import ScanResult
    old_time = NOW-timedelta(days=30)
    s = options(database_path=tmp_path/'capacity.db', max_verifications_per_scan=4,
                github_max_inspection_requests_per_scan=8)
    state = RunState(s.database_path)
    old = candidate(source_issue_id=1, issue_number=1, url='https://github.com/acme/tool/issues/1',
                    created_at=old_time, updated_at=old_time, discovered_at=old_time)
    recent = [candidate(source_issue_id=n, issue_number=n,
                        url=f'https://github.com/acme/tool/issues/{n}') for n in range(2, 5)]
    state.save([old]+recent, [], completed_at=None)
    state.close()

    class Source:
        name = 'github'

        async def scan(self, hours):
            return ScanResult(candidates=[], unprocessed_windows=[])

    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/timeline'):
            return httpx.Response(200, json=[])
        if request.url.path.endswith('/comments'):
            return httpx.Response(200, json=[])
        n = int(request.url.path.rsplit('/', 1)[1])
        return httpx.Response(200, json=issue(n, comments=1 if n != 1 else 0))

    respx_mock.get(url__regex=r'https://api\.github\.com/repos/acme/tool/issues/').mock(
        side_effect=respond)
    await cli._run(s, False, sources=[Source()], clock=lambda: NOW)
    assert len(calls) == 8
    assert calls[-2:] == ['/repos/acme/tool/issues/1', '/repos/acme/tool/issues/1/timeline']
    state = RunState(s.database_path)
    historical = json.loads(state.db.execute(
        "select payload_json from verification_results where source_issue_id='github:1'"
    ).fetchone()[0])
    assert historical['claim_status'] == 'APPARENTLY_UNCLAIMED'
    state.close()
