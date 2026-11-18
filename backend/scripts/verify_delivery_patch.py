"""差异交付回归：不调用真实模型。"""
import asyncio
from copy import deepcopy
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.agents import hub
from app.tools.mock import MockSource
from verify_peer_success import ScriptedTeam, brief, say


async def main():
    result = await hub.run_session(brief=brief(), source=MockSource(), client=ScriptedTeam(),
                                   emit=lambda *a, **k: None, max_turns=20, max_rounds=2)
    assert result.converged
    tasks = deepcopy(result.session['tasks'])
    original = deepcopy(tasks['T2']['delivery'])
    hub._request_rework(tasks, [{'task_id': 'T2', 'reason': '局部复核', 'propagate': False}])
    assert tasks['T2']['delivery_base'] == original
    assert tasks['T1']['status'] == 'delivered'
    patch = {'base_version': original['version'], 'rationale': '复核当日安排',
             'days': [deepcopy(original['days'][1])]}
    merged = hub._merge_delivery(tasks['T2'], {'delivery_patch': patch})
    assert hub._delivery_valid('local_expert', merged, result.ctx, brief())
    assert len(merged['days']) == brief().days
    assert 'evidence_ids' not in merged and 'version' not in merged
    assert tasks['T2']['delivery_base'] == original
    print('PASS baseline preserved; partial merge validated against full itinerary')
    bad_patches = [
        {**patch, 'base_version': original['version'] - 1},
        {**patch, 'days': patch['days'] * 2},
        {**patch, 'days': [{'date': '2099-01-01', 'poi_ids': []}]},
        {**patch, 'approved_version': 999},
    ]
    for bad in bad_patches:
        try:
            hub._merge_delivery(tasks['T2'], {'delivery_patch': bad})
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    forged = hub._merge_delivery(tasks['T2'], {'delivery_patch': {
        **patch, 'days': [{'date': original['days'][1]['date'], 'poi_ids': ['fake']}]}})
    assert not hub._delivery_valid('local_expert', forged, result.ctx, brief())
    print('PASS stale, duplicate, unknown and fabricated changes rejected')

    # A real tool write must precede a changed delivery; patch text alone is insufficient.
    from app.agents.tools_spec import execute
    changed_day = original['days'][1]['date']
    changed_ids = ['p02', 'p03', 'p13']
    patch['days'] = [{'date': changed_day, 'poi_ids': changed_ids}]
    assert not hub._delivery_valid('local_expert', hub._merge_delivery(
        tasks['T2'], {'delivery_patch': patch}), result.ctx, brief())
    assert execute(result.ctx, 'assign_pois', {'date': changed_day, 'poi_ids': changed_ids})['ok']
    assert execute(result.ctx, 'materialize_plan', {})['ok']
    assert hub._delivery_valid('local_expert', hub._merge_delivery(
        tasks['T2'], {'delivery_patch': patch}), result.ctx, brief())
    hotel_task = deepcopy(tasks['T1'])
    hotel_task['delivery_base'] = deepcopy(hotel_task['delivery'])
    hotel_patch = {'base_version': hotel_task['delivery_base']['version'],
                   'hotel_id': result.ctx.hotel_id, 'rationale': '复核住宿'}
    hotel_merged = hub._merge_delivery(hotel_task, {'delivery_patch': hotel_patch})
    assert hub._delivery_valid('logistics', hotel_merged, result.ctx, brief())
    assert hotel_merged['outbound_flight_no'] == hotel_task['delivery_base']['outbound_flight_no']
    print('PASS real tool change required; logistics retains unchanged flights')

    class PatchTeam(ScriptedTeam):
        async def chat_tools(self, messages, tools):
            if '本地体验与每日活动专员' in messages[0]['content']:
                return say('proposal', original['evidence_ids'][0], delivery_patch=patch)
            return await super().chat_tools(messages, tools)

    session = deepcopy(result.session)
    session['context'] = hub.dump_context(result.ctx)
    session.update(tasks=tasks, pending_agent='local_expert', pending_task='T2', reviews=0,
                   pending_rejections=[], pending_issue_codes=[])
    resumed = await hub.run_session(brief=brief(), source=MockSource(), client=PatchTeam(),
                                   emit=lambda *a, **k: None, max_turns=10, max_rounds=2,
                                   resume=json.loads(json.dumps(session)))
    assert resumed.converged, resumed.escalation
    expected = deepcopy(original['days'])
    expected[1] = patch['days'][0]
    assert resumed.session['tasks']['T2']['delivery']['days'] == expected
    assert 'delivery_base' not in resumed.session['tasks']['T2']
    print('PASS persisted baseline resumes through patch delivery, review and writer')


if __name__ == '__main__':
    asyncio.run(main())
