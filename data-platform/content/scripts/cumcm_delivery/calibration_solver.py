"""Reproducible numeric solver for the declared local calibration fixture."""
import argparse
import json
import numpy as np


def solve(rows, seed=20251007, repeats=500):
    if rows.shape != (12, 2) or not np.isfinite(rows).all():
        raise ValueError('expected twelve finite x,y pairs')
    x, y = rows.T
    design = np.column_stack([x, np.ones(len(x))])
    if np.linalg.matrix_rank(design) != 2:
        raise ValueError('calibration design is rank deficient')
    slope, intercept = np.linalg.lstsq(design, y, rcond=None)[0]
    pred = design @ np.array([slope, intercept])
    residual = y - pred
    rmse = float(np.sqrt(np.mean(residual ** 2)))
    r2 = float(1 - np.sum(residual ** 2) / np.sum((y-y.mean()) ** 2))
    loo = []
    for idx in range(len(x)):
        keep = np.arange(len(x)) != idx
        params = np.linalg.lstsq(design[keep], y[keep], rcond=None)[0]
        loo.append(float(design[idx] @ params))
    loo_rmse = float(np.sqrt(np.mean((np.asarray(loo)-y) ** 2)))
    rng = np.random.default_rng(seed)
    samples = []
    rejected = 0
    while len(samples) < repeats:
        indices = rng.integers(0, len(x), len(x))
        if np.linalg.matrix_rank(design[indices]) != 2:
            rejected += 1
            if rejected > repeats:
                raise ValueError('too many degenerate bootstrap samples')
            continue
        params = np.linalg.lstsq(design[indices], y[indices], rcond=None)[0]
        samples.append(params)
    ci = np.quantile(np.asarray(samples), [.025, .975], axis=0)
    if abs(slope) < 1e-12:
        raise ValueError('inverse calibration is undefined')
    thresholds = (np.asarray([23., 25., 27.])-intercept) / slope
    normal_error = float(np.max(np.abs(design.T @ residual)))
    values = [slope, intercept, rmse, r2, loo_rmse, normal_error]
    if not np.isfinite(values).all() or normal_error > 1e-8:
        raise ValueError('numeric checks failed')
    return dict(slope=float(slope), intercept=float(intercept),
                pred=pred.tolist(), residual=residual.tolist(), loo=loo,
                rmse=rmse, r2=r2, loo_rmse=loo_rmse, ci=ci.tolist(),
                thresholds=thresholds.tolist(), normal_error=normal_error,
                bootstrap_repeats=repeats, rejected_bootstrap=rejected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default='input.csv')
    parser.add_argument('--output', default='solver-results.json')
    args = parser.parse_args()
    data = np.genfromtxt(args.input, delimiter=',', skip_header=1)
    with open(args.output, 'w', encoding='utf-8') as stream:
        json.dump(solve(data), stream, ensure_ascii=False, allow_nan=False,
                  indent=2)
