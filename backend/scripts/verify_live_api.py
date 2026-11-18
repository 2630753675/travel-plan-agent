"""Use the actual FastAPI application, configured LLM and datasource, with no patched dependencies."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from app.api.main import app
from app.settings import load


def main():
    cfg = load()
    assert cfg.llm.configured, 'Real model configuration required'
    print('REAL MODEL:', cfg.llm.model, 'DATA:', cfg.datasource.kind, flush=True)
    brief = dict(origin='上海', destination='成都', start_date='2026-11-20', end_date='2026-11-24', travelers=2, budget_total=20000, companions=['老人'], preferences=['人文'], pace='适中')
    events = []
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        with client.stream('POST', '/api/plan', json=brief) as response:
            assert response.status_code == 200, response.status_code
            for line in response.iter_lines():
                if not line.startswith('data: '):
                    continue
                event = json.loads(line[6:])
                events.append(event)
                print(json.dumps(event, ensure_ascii=False), flush=True)
        result = next((e['payload']['result'] for e in events if e['event_type'] == 'run_completed'), None)
        output = ROOT / 'data' / 'live-api-result.json'
        output.write_text(json.dumps({'brief': brief, 'events': events, 'result': result}, ensure_ascii=False, indent=2), encoding='utf-8')
        assert result, 'No completion; inspect run_failed or interruption events'
        assert result['critique']['passed'], result['critique']
        plan_id = result['plan_id']
        record = client.get('/api/plan/' + plan_id)
        assert record.status_code == 200
        stored = record.json()
        assert stored['itinerary'] == result['itinerary'], 'Persisted itinerary differs'
        assert stored['critique']['passed']
        print('PASS actual API + real LLM + SSE + persistence/readback', plan_id, 'cost=', result['itinerary']['total_cost'], flush=True)

if __name__ == '__main__':
    main()
