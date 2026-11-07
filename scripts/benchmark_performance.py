"""Offline synthetic baseline; never reads live MES or sends model requests."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import perf_logger
from agent.tool_routing import select_tools, routing_instructions, ROUTING_INSTRUCTION
from core.tool_catalog import TOOL_POLICIES


def benchmark(groups=5000):
    with tempfile.TemporaryDirectory(prefix='camstar-index-bench-') as directory:
        path = Path(directory)/'performance.jsonl'
        with path.open('w', encoding='utf-8') as stream:
            for i in range(groups):
                for step in range(2):
                    record = {'timestamp': (datetime(2026, 1, 1)+timedelta(seconds=i, milliseconds=step)).isoformat(),
                              'session_id':'synthetic', 'username':'benchmark', 'action':'Tool_Execute',
                              'duration_ms':1, 'details':{'run_id':str(i),'prompt':f'query {i}'}}
                    stream.write(json.dumps(record)+'\n')
        original = perf_logger.PERF_LOG_FILE
        perf_logger.PERF_LOG_FILE = str(path)
        try:
            start = time.perf_counter()
            first = perf_logger.get_perf_log_groups()
            build = (time.perf_counter()-start)*1000
            timings = []
            for _ in range(5):
                start = time.perf_counter()
                page = perf_logger.get_perf_log_groups(cursor=first['next_cursor'])
                timings.append((time.perf_counter()-start)*1000)
            assert len(page['groups']) == 20
            result = {'groups':groups, 'records':groups*2, 'jsonl_bytes':path.stat().st_size,
                      'index_build_and_first_page_ms':round(build,2),
                      'indexed_page_median_ms':round(statistics.median(timings),2)}
        finally:
            perf_logger.PERF_LOG_FILE = original
    tools = [{'function':{'name':name}} for name in TOOL_POLICIES]
    rules = routing_instructions(select_tools(tools,[{'role':'user','content':'查询工艺路线'}]))
    result['workflow_rules'] = {'full_chars':len(ROUTING_INSTRUCTION), 'selected_chars':len(rules),
                                'reduction_percent':round(100*(1-len(rules)/len(ROUTING_INSTRUCTION)),2)}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--groups', type=int, default=5000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.groups < 40:
        parser.error('--groups must be at least 40')
    result = benchmark(args.groups)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered+'\n', encoding='utf-8')
    print(rendered)
