"""对外发布用的纯差分隐私机制（ε-DP，δ=0）。

邻接关系
--------
两个数据集由同一网点、同一日期集合构成，仅 **一天** 的日量不同；日量先按
预设上限 ``clip_upper``（默认 200）裁剪，所以单日替换在一个输入坐标上的 L1 影响不超过
200。发布只依赖提交时冻结的日量快照，不依赖之后补录。

灵敏度
------
未来 7 步预测是输入的线性函数（周季节朴素直接复制最近 7 日，Holt 是线性递推），
因此单日替换引起的 **输出向量** L1 变化为 ``|δ| · Σ_h |c_j(h)|``。逐日取最大：

    S_model = max_j Σ_{h=1..7} |c_model,j(h)|,   Δ_model = clip_upper · S_model

Holt 的系数由闭式递推精确给出（见 :func:`holt_coefficients`），不靠数值近似。

噪声
----
对每个模型的 7 维输出整体加一次独立 Laplace(0, Δ_model/ε_model) 噪声——向量值
Laplace 机制，对 7 个坐标同时成立 ε_model-DP。两个模型串行组合，ε1+ε2=ε，故整组
14 个整数是 ε-DP。取整与截零都是后处理，不改变界。

可复现性
--------
噪声来自“种子 + 坐标标签”的 SHA-256 计数器，种子只存服务端、从不外传。同一条
发布无论重试多少次、是否被中断后续算，逐坐标结果完全一致。
"""
import hashlib
import math

from src.constants.privacy import DEFAULT_CLIP_UPPER

HORIZON = 7
MODELS = ('seasonal_naive', 'holt')


def clip(volume, upper=DEFAULT_CLIP_UPPER):
    """把日量裁剪到 [0, upper]，机制在此裁剪后的数据集上运行。"""
    return max(0, min(int(upper), int(volume)))


def holt_coefficients(n, alpha, beta):
    """未来 1..7 步对每个输入位置 j 的 (水平系数, 趋势系数)。

    无新观测时 Holt 状态按 ``l'=(1-α)(l+b)``、``b'=β(l'-l)+(1-β)b`` 线性演化；
    新观测只注入到当天的水平上。因此输入 j 的响应 = 初值项的演化 + 注入项的演化。
    """
    def step(l, b):
        nl = (1.0 - alpha) * (l + b)
        return nl, beta * (nl - l) + (1.0 - beta) * b

    out = [None] * n
    out[0] = (0.0, -1.0)                  # l_0=y_0, b_0=y_1-y_0
    if n > 1:
        out[1] = (1.0, 1.0)               # b_0 对 y_1 的贡献
    v = (alpha, alpha * beta)             # t=n-1 注入项
    for j in range(n - 1, 1, -1):
        out[j] = v
        v = step(*v)
    for j in (0, 1):                      # 初值项再演化 n-2 步
        v = out[j]
        for _ in range(2, n):
            v = step(*v)
        out[j] = v
    return out


def sensitivities(n, alpha, beta, clip_upper=DEFAULT_CLIP_UPPER):
    """返回 {模型: (S, Δ)}，S 为逐日 L1 系数上界，Δ 为裁剪单位的输出灵敏度。"""
    holt = [sum(abs(l + h * b) for h in range(1, HORIZON + 1))
            for l, b in holt_coefficients(n, alpha, beta)]
    return {
        'holt': (max(holt), max(holt) * clip_upper),
        'seasonal_naive': (1.0, float(clip_upper)),
    }


def split_epsilon(epsilon):
    """两模型平均分配总预算。"""
    return {'holt': epsilon / 2.0, 'seasonal_naive': epsilon / 2.0}


def _uniform(key, label):
    digest = hashlib.sha256(key + b'\x00' + label).digest()
    return (int.from_bytes(digest[:8], 'big') + 1) / (float(1 << 64) + 2.0)


def noise(key, label, scale):
    """由种子确定地抽一个 Laplace(0, scale) 样本（逆变换 + SHA-256 均匀源）。"""
    u = _uniform(key, label)
    r = abs(u - 0.5)
    magnitude = -scale * math.log(1.0 - 2.0 * r) if r else 0.0
    return magnitude if u >= 0.5 else -magnitude


def coordinate(seed, model, index, raw, scale):
    """第 model 个模型第 index 个未来值的扰动后整数（可单独计算、可续算）。"""
    z = noise(seed, model.encode() + b'|' + str(index).encode(), scale)
    return max(0, int(math.floor(raw + z + 0.5)))


def mechanism_description(n, alpha, beta, epsilon, branch, first_day, last_day, dimensions,
                          clip_upper=DEFAULT_CLIP_UPPER):
    """可核验的机制说明：只含参数，不含任何日量、回测真值或种子。"""
    sens = sensitivities(n, alpha, beta, clip_upper)
    eps = split_epsilon(epsilon)
    return {
        'name': 'vector_laplace_pure_dp',
        'guarantee': 'epsilon-differential_privacy',
        'delta': 0.0,
        'neighboring': '同网点同日期集合，单日日量任意替换；日量先按 clip_upper 裁剪',
        'clip_upper': clip_upper,
        'horizon': HORIZON,
        'branch': branch,
        'training_window': {'first_day': first_day, 'last_day': last_day, 'days': n},
        'holt_alpha': alpha,
        'holt_beta': beta,
        'epsilon_total': epsilon,
        'epsilon_per_model': eps,
        'l1_sensitivity': {m: sens[m][0] for m in MODELS},
        'output_sensitivity': {m: sens[m][1] for m in MODELS},
        'noise_scale': {m: sens[m][1] / eps[m] for m in MODELS},
        'noise': 'independent Laplace(0, noise_scale) per coordinate; vector mechanism',
        'composition': '两个模型串行组合 ε1+ε2=ε；逐日台账把 ε 记到每个训练日期',
        'rounding': 'round-half-up 后截负为 0（后处理，不改变界）',
        'dims': dimensions,
    }
