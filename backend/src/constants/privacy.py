"""对外发布的纯差分隐私常量。

预算一律以百万分之一 ε（epsilon micro-unit）的整数记账，避免浮点累加在
逐日上限的“末一笔”判断上产生漂移。
"""
DEFAULT_CLIP_UPPER = 200  # 日量预设裁剪上限；替换一天的邻近数据集在此区间内
MAX_CLIP_UPPER = 1000     # 日量裁剪上限允许范围；越大越贴近原值，灵敏度与噪声也随之上升
EPSILON_UNIT = 1_000_000  # 1 ε = 1_000_000 micro-ε
MIN_EPSILON = 0.01        # 每次发布预算下限（保证两模型各分到正预算）
MAX_EPSILON = 10.0
MAX_DAILY_CAP = 50.0
PUBLICATION_STATES = ('pending', 'claimable', 'withdrawn', 'failed')
PUBLICATION_STATE_LABELS = {
    'pending': '待处理',
    'claimable': '可领取',
    'withdrawn': '已撤回',
    'failed': '失败',
}
DEFAULT_PER_RELEASE_EPSILON = 1.0
DEFAULT_DAILY_CAP = 3.0
MAX_ATTEMPTS = 3
