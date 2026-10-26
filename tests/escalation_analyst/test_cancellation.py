"""Stopping a master cancels its stream/children without discarding saved work."""
import asyncio
import threading
import time
from dataclasses import replace
from .test_core import client, ingest, message


def wait_until(test, timeout=5):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=test()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('Timed out waiting for worker')


def test_stop_master_closes_stream_keeps_completed_groups_and_releases_claim(client):
    ingest(client)
    row=message();row.update(group_id='second@g.us',wa_id='second',group_name='Second')
    ingest(client,row)
    r=client.app.state.runtime
    r.provider.settings=replace(r.settings,api_key='test-key')
    entered=threading.Event();closed=threading.Event()
    calls=[]
    original=r.provider.generate
    async def stream(*args):
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()
    r.provider._stream=stream
    def generate(system,user,meta):
        calls.append(meta['scope'])
        if len(calls)==1:
            return 'Completed first group'
        return original(system,user,meta)
    r.provider.generate=generate
    run=client.post('/master/analyse',json={'hours':24}).json()['runId']
    assert entered.wait(3)
    # Groups run concurrently, so the first group's save can land just after the second stream opens.
    wait_until(lambda:len(r.reports.history(scope='group'))==1)
    response=client.post(f'/master/runs/{run}/stop')
    assert response.status_code==202
    wait_until(lambda:r.jobs.get(run)['status']=='cancelled')
    assert closed.wait(3)
    wait_until(lambda:not any(j['status']=='running' for j in r.jobs.list('group')))
    assert calls==['group','group']
    progress=client.get('/master/progress',params={'runId':run}).json()['run']
    assert progress['status']=='cancelled'
    assert progress['completed_groups']==1
    assert not progress['stopRequested']
    assert len(r.reports.history(scope='group'))==1
    assert not r.reports.history(scope='master')
    assert r.jobs.update(run,{},'complete')==0
    assert client.post(f'/master/runs/{run}/stop').json()['status']=='cancelled'
    newer,created=r.jobs.claim('master','master',{})
    assert created
    r.jobs.cancelled(newer['id'])


def test_stop_does_not_cancel_an_independent_group_job(client):
    ingest(client)
    r=client.app.state.runtime
    r.provider.settings=replace(r.settings,api_key='test-key')
    entered=threading.Event();release=threading.Event()
    async def stream(*args):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        return 'Independent report',None
    r.provider._stream=stream
    independent,_=r.start('group','group:test@g.us',{'hours':24},lambda _:r.analysis.group('test@g.us',{'hours':24}))
    try:
        assert entered.wait(3)
        run=client.post('/master/analyse',json={'hours':24}).json()['runId']
        wait_until(lambda:r.jobs.get(run)['result'])
        client.post(f'/master/runs/{run}/stop')
        wait_until(lambda:r.jobs.get(run)['status']=='cancelled')
        assert r.jobs.get(independent['id'])['status']=='running'
    finally:
        release.set()
    wait_until(lambda:r.jobs.get(independent['id'])['status']=='complete')
    assert len(r.reports.history(scope='group'))==1


def test_stop_requires_identity_and_master_run(client):
    assert client.post('/master/runs/1/stop',headers={'X-User-Id':''}).status_code==401
    assert client.post('/master/runs/99999/stop').status_code==404
    job,_=client.app.state.runtime.jobs.claim('performance','performance',{})
    assert client.post(f"/master/runs/{job['id']}/stop").status_code==404
