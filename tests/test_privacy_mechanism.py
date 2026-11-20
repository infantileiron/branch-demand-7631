"""机制正确性与可核验界的测试：系数、灵敏度、噪声尺度、一致重跑。"""
import hashlib
import math
import random

import pytest

from src.services import privacy
from src.services.analysis import holt, seasonal


def brute_coeff(n, alpha, beta, j):
    """用独立基向量直接模拟，作为闭式系数的对照。"""
    out = []
    for h in range(1, privacy.HORIZON + 1):
        base = [0.0] * n
        alt = [0.0] * n
        alt[j] = 1.0
        out.append(holt(alt, h, alpha, beta)[h - 1] - holt(base, h, alpha, beta)[h - 1])
    return out


@pytest.mark.parametrize('n', [2, 3, 7, 35, 70])
@pytest.mark.parametrize('alpha,beta', [(0.6, 0.2), (0.3, 0.1), (0.9, 0.9), (0.01, 0.99)])
def test_holt_coefficients_match_simulation(n, alpha, beta):
    co = privacy.holt_coefficients(n, alpha, beta)
    for j in range(n):
        want = brute_coeff(n, alpha, beta, j)
        got = [co[j][0] + h * co[j][1] for h in range(1, privacy.HORIZON + 1)]
        assert got == pytest.approx(want, abs=1e-10)


def test_clipped_replace_obeys_claimed_sensitivity():
    """一天日量在裁剪区间内任意替换，输出组 L1 变化不超过声明的 Δ。"""
    rng = random.Random(11)
    n = 70
    alpha, beta = 0.6, 0.2
    sens = privacy.sensitivities(n, alpha, beta)
    base = [rng.randint(0, 200) for _ in range(n)]
    base_out = (holt(base, 7, alpha, beta), seasonal(base, 7))
    joint_bound = sens['holt'][1] + sens['seasonal_naive'][1]
    for j in rng.sample(range(n), 12):
        for value in (0, 200):
            alt = base[:]
            alt[j] = value
            alt_out = (holt(alt, 7, alpha, beta), seasonal(alt, 7))
            for model, idx in (('holt', 0), ('seasonal_naive', 1)):
                changed = sum(abs(a - b) for a, b in zip(base_out[idx], alt_out[idx]))
                assert changed <= sens[model][1] * (1 + 1e-9)
            # 两模型串行组合的联合灵敏度同样成立
            joint = sum(abs(a - b) for a, b in zip(*[base_out[0], alt_out[0]])) + \
                    sum(abs(a - b) for a, b in zip(*[base_out[1], alt_out[1]]))
            assert joint <= joint_bound * (1 + 1e-9)


def test_sensitivity_uses_clip_bound():
    sens = privacy.sensitivities(70, 0.6, 0.2)
    assert sens['holt'][1] == pytest.approx(sens['holt'][0] * privacy.DEFAULT_CLIP_UPPER)
    assert sens['seasonal_naive'][1] == privacy.DEFAULT_CLIP_UPPER
    assert sens['holt'][0] == pytest.approx(7.56)
    # 裁剪上限可显式指定，灵敏度随之缩放，界仍可核验
    tight = privacy.sensitivities(70, 0.6, 0.2, 100)
    assert tight['holt'][1] == pytest.approx(sens['holt'][1] / 2)
    assert tight['seasonal_naive'][1] == 100.0


def test_epsilon_split_is_exact():
    parts = privacy.split_epsilon(1.0)
    assert parts['holt'] + parts['seasonal_naive'] == pytest.approx(1.0)
    assert parts['holt'] == parts['seasonal_naive']


def test_coordinate_is_a_pure_function_of_seed():
    seed = hashlib.sha256(b'unit-test').digest()
    draws = {privacy.coordinate(seed, 'holt', 3, 41.2, 1521.0) for _ in range(5)}
    assert draws == {privacy.coordinate(seed, 'holt', 3, 41.2, 1521.0)}


def test_noise_draws_stay_near_zero_and_trade_off_with_epsilon():
    seed = hashlib.sha256(b'scale').digest()
    coarse = [privacy.noise(seed, b'x|%d' % i, 1000.0) for i in range(400)]
    fine = [privacy.noise(seed, b'y|%d' % i, 10.0) for i in range(400)]
    assert max(abs(v) for v in fine) < 200
    assert max(abs(v) for v in coarse) > 200
    assert abs(sum(coarse)) / len(coarse) < 200


def test_mechanism_description_carries_no_secret():
    desc = privacy.mechanism_description(70, 0.6, 0.2, 1.0, '川北', '2026-09-06', '2026-11-14',
                                         {'seasonal_naive': 7, 'holt': 7})
    blob = repr(desc).lower()
    for banned in ('seed', 'snapshot', 'actual', 'volume'):
        assert banned not in blob
    assert desc['delta'] == 0.0
    assert desc['guarantee'] == 'epsilon-differential_privacy'
    assert desc['epsilon_total'] == 1.0
    assert desc['noise_scale']['holt'] == pytest.approx(desc['output_sensitivity']['holt'] / 0.5)
