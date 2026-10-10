"""Recompute the declared fixture in an isolated Python process and compare outputs."""
import csv
import math
import subprocess
from . import core, new_task_flow as flow


def main():
    out = flow.root()
    target = out / 'solver-independent.json'
    command = [core.PY, '-I', '-X', 'utf8', str(out / 'calibration_solver.py'),
               '--input', str(out / 'input.csv'), '--output', str(target)]
    process = subprocess.run(command, cwd=str(out), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=120)
    if process.returncode:
        raise RuntimeError(process.stderr.decode('utf-8', 'replace'))
    expected = core.load_json(out / 'solver-results.json')
    actual = core.load_json(target)
    compared = []

    def compare(left, right, path):
        if isinstance(left, dict):
            if not isinstance(right, dict) or set(left) != set(right):
                raise ValueError('result fields differ: ' + path)
            for key in left:
                compare(left[key], right[key], path + '.' + key)
        elif isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                raise ValueError('result lengths differ: ' + path)
            for index, value in enumerate(left):
                compare(value, right[index], path + '[' + str(index) + ']')
        elif isinstance(left, (int, float)):
            if not math.isfinite(right) or not math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError('numeric output differs: ' + path)
            compared.append(path)
        elif left != right:
            raise ValueError('result value differs: ' + path)

    compare(expected, actual, 'solver')
    with open(str(out / 'computed.csv'), encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    # Reproduction covers displayed per-row predictions and residuals as well as JSON.
    fields = list(rows[0])
    prediction = next(k for k in fields if k in ('prediction', 'pred', 'y_pred', 'predicted', 'predicted_degC'))
    residual = next(k for k in fields if k in ('residual', 'error', 'residual_degC'))
    compare(actual['pred'], [float(r[prediction]) for r in rows], 'computed.pred')
    compare(actual['residual'], [float(r[residual]) for r in rows], 'computed.residual')
    compare(actual['loo'], [float(r['loo_prediction_degC']) for r in rows], 'computed.loo')
    compare([23.,25.,27.], [actual['slope']*x+actual['intercept'] for x in actual['thresholds']], 'inverse_forward_identity')
    report = {'passed': True, 'executed_at': core.now_utc_iso(), 'command': command,
              'interpreter': core.PY, 'isolated_process': True,
              'input_sha256': core.sha256_of(out / 'input.csv'),
              'solver_sha256': core.sha256_of(out / 'calibration_solver.py'),
              'independent_output_sha256': core.sha256_of(target),
              'compared_numeric_values': len(compared), 'absolute_tolerance': 1e-12,
              'relative_tolerance': 1e-12,
              'scope': 'Independent execution of delivered solver; not an independent mathematical proof.'}
    core.write_json(out / 'reproduction_verification.json', report)
    print(report, flush=True)


if __name__ == '__main__':
    main()
