import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .forecast import run_forecast
from .io import load_records
from .structure import ROOT, load_structure
from .summary import summarize
from .validation import rolling_validation


def save_json(path, result):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.analysis-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(args):
    records = load_records(args.input)
    structure = load_structure(args.structure, args.closures, args.mappings)
    return records, structure


def _scheduling_payload(result):
    """排班系统可直接读取的精简结构：按周、按节点给协调后的非负工时。"""
    payload = {'product': result['product'], 'unit': result['unit'],
               'origin': result['origin'], 'horizon': result['horizon'],
               'root': ROOT, 'weeks': []}
    for week in result['weeks']:
        payload['weeks'].append({
            'week': week['week'],
            'forecast_hours': week['reconciled'],
            'leaf_hours': week['leaf_solution'],
            'fixed_zero': week['constrained']['fixed_zero'],
            'active_nonneg': week['constrained']['active_nonneg'],
            'conservation': week['conservation']})
    return payload


def _print_conservation(result):
    print('== 层级守恒与非负检查（协调后）==')
    for week in result['weeks']:
        check = week['conservation']
        bound = '，'.join(week['constrained']['active_nonneg']) or '无'
        fixed = '，'.join(week['constrained']['fixed_zero']) or '无'
        print(f"week={week['week']} 最大父子偏差={check['max_parent_gap']:.2e} "
              f"最小值={check['min_value']:.6f} 全部非负={check['all_nonneg']} "
              f"非负约束生效节点=[{bound}] 停用零值节点=[{fixed}]")
    first = result['weeks'][0]
    print('\n== 首周协调前后对比 ==')
    print(f"{'节点':<6}{'层级':<4}{'基准预测':>12}{'无约束协调':>12}{'最终协调':>12}")
    levels = result['structure']['levels']
    for node in result['structure']['nodes']:
        print(f"{node:<6}{levels[node]:<4}"
              f"{first['base'][node]:>12.4f}"
              f"{first['reconciled_unconstrained'][node]:>12.4f}"
              f"{first['reconciled'][node]:>12.4f}")
    print('\n== 损失变化（首周）==')
    for key, value in first['loss'].items():
        print(f'{key:<26}{value:.6f}')
    print('\n== 结构变更追溯 ==')
    for event in result['audit']['mapping_events']:
        print(f"week={event['week']} {event['node']}: {event['from_parent']} -> "
              f"{event['to_parent']} 策略={event['strategy']}")
    print(result['audit']['rule'])


def main():
    parser = argparse.ArgumentParser(description='本地观测数据与层级预测协调')
    commands = parser.add_subparsers(dest='command', required=True)
    summary = commands.add_parser('summary', help='按业务维度核对观测口径')
    summary.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    summary.add_argument('--output', type=Path)

    def add_structure_args(command):
        command.add_argument('--input', type=Path, default=Path('data/observations.csv'))
        command.add_argument('--structure', type=Path, default=Path('data/structure.csv'))
        command.add_argument('--closures', type=Path, default=Path('data/closures.csv'))
        command.add_argument('--mappings', type=Path, default=Path('data/node_mappings.csv'))
        command.add_argument('--output', type=Path)

    forecast = commands.add_parser('forecast', help='分层基准预测与非负层级协调')
    add_structure_args(forecast)
    forecast.add_argument('--origin', type=int, default=8)
    forecast.add_argument('--horizon', type=int, default=4)

    demo = commands.add_parser('demo', help='短演示：守恒检查、滚动验证与排班 JSON')
    add_structure_args(demo)
    demo.add_argument('--origin', type=int, default=10)
    demo.add_argument('--horizon', type=int, default=3)
    demo.add_argument('--validate', action='store_true', help='附带滚动原点验证')

    args = parser.parse_args()
    try:
        if args.command == 'summary':
            result = summarize(load_records(args.input))
            if args.output:
                save_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0
        records, structure = _load(args)
        result = run_forecast(records, structure, args.origin, args.horizon)
        if args.command == 'forecast':
            if args.output:
                save_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0
        _print_conservation(result)
        payload = _scheduling_payload(result)
        if args.output:
            save_json(args.output, payload)
            print(f"\n排班 JSON 已写入 {args.output}")
        validation = None
        if args.validate:
            validation = rolling_validation(records, structure, origins=[5, 6, 7, 8])
            print('\n== 滚动验证（站点误差 vs 区域资源偏差）==')
            for name, block in validation['summary'].items():
                print(name)
                for variant, metrics in block.items():
                    print(f"  {variant:<26}" + ' '.join(f'{k}={v:.4f}' if isinstance(v, float)
                                                        else f'{k}={v}' for k, v in metrics.items()))
            print('取舍：', json.dumps(validation['tradeoff'], ensure_ascii=False, sort_keys=True))
        output = {'scheduling': payload}
        if validation:
            output['rolling_validation'] = validation
        print('\n== 排班可读 JSON ==')
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
