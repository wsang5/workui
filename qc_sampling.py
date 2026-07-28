"""质检抽检核心逻辑：三阶段递进抽检 + 既有 SPRT 兼容函数。

纯函数模块，不做任何 IO。`/api/qc-stats` 与 `/api/qc` 写回响应共用此模块，保证同源判定。
对应设计文档：`.trae/documents/质检作业模式与智能抽检.md`。
"""

import hashlib
import math
from dataclasses import dataclass


PASS = "通过"
FAIL = "不通过"

# 状态枚举
ACCEPT = "accept"        # 验收通过：整批放行
REJECT = "reject"        # 转全量质检
CONTINUE = "continue"    # 抽样中，可继续抽
FULL_CHECK = "full-check"  # 小批量全检兜底

# 三阶段责任池状态枚举
STAGE1 = "S1"
STAGE2 = "S2"
DONE = "DONE"
REWORK = "REWORK"
PASS_STATE = "PASS"
SUSPECT = "SUSPECT"
PENDING = "PENDING"

DEFAULT_POOL_CONFIG = {"T": 0.80, "R1": 0.30, "R2": 0.50}


@dataclass(frozen=True)
class SprtParams:
    p1: float = 0.90      # AQL 可接受正确率
    p0: float = 0.80      # RQL 不可接受正确率
    alpha: float = 0.05   # 生产者风险（误拒好人）
    beta: float = 0.05    # 消费者风险（漏放差人）
    nmax_cap: int = 100   # n_max 截断上限
    n_min: int = 50       # 小批量全检阈值


@dataclass(frozen=True)
class SprtBoundaries:
    a: float    # 接收截距（负）
    b: float    # 拒收截距（正）
    s: float    # 中性斜率
    q_mid: float  # 到顶收口分界（缺陷率中点）


def sprt_boundaries(params: SprtParams) -> SprtBoundaries:
    """由四参数唯一确定 Wald 两线斜率/截距与到顶收口分界。"""
    q0 = 1.0 - params.p1   # H0 缺陷率（好），默认 0.10
    q1 = 1.0 - params.p0   # H1 缺陷率（差），默认 0.20
    if not (0.0 < q0 < q1 < 1.0):
        raise ValueError("要求 0 < 1-p1 < 1-p0 < 1，即 p0 < p1 且均在 (0,1)")
    denom = math.log(q1 / q0) + math.log((1.0 - q0) / (1.0 - q1))
    s = math.log((1.0 - q0) / (1.0 - q1)) / denom
    a = math.log(params.beta / (1.0 - params.alpha)) / denom   # 接收线含 α 在分母
    b = math.log((1.0 - params.beta) / params.alpha) / denom   # 拒收线含 α 在分母
    return SprtBoundaries(a=a, b=b, s=s, q_mid=(q0 + q1) / 2.0)


def _stable_hash_key(prompt_id: str) -> str:
    """promptId 的稳定哈希，用于可复现的随机抽样顺序。"""
    return hashlib.md5(str(prompt_id or "").encode("utf-8")).hexdigest()


def _replay_order(checked_items):
    """判定/终态重放顺序：优先 qcTime 升序；任一已检条目缺 qcTime 则整组退化为 promptId 哈希序。"""
    if all((item.get("qcTime") or "").strip() for item in checked_items):
        return sorted(checked_items, key=lambda it: (it.get("qcTime") or "", _stable_hash_key(it.get("promptId"))))
    return sorted(checked_items, key=lambda it: _stable_hash_key(it.get("promptId")))


def _hash_sorted_ids(prompt_ids):
    return sorted(prompt_ids, key=_stable_hash_key)


def _item_prompt_id(item):
    if isinstance(item, dict):
        return item.get("promptId") or item.get("id") or item.get("rowKey") or ""
    return item


def _hash_sorted_items(items):
    return sorted(items, key=lambda it: _stable_hash_key(_item_prompt_id(it)))


def _normalize_pool_cfg(cfg=None):
    merged = dict(DEFAULT_POOL_CONFIG)
    if cfg:
        if hasattr(cfg, "__dict__") and not isinstance(cfg, dict):
            cfg = cfg.__dict__
        for key in ("T", "R1", "R2"):
            if key in cfg and cfg[key] is not None:
                merged[key] = float(cfg[key])
            lower = key.lower()
            if lower in cfg and cfg[lower] is not None:
                merged[key] = float(cfg[lower])
    if not (0 < merged["T"] < 1):
        raise ValueError("质检参数 T 必须满足 0 < T < 1")
    if not (0 < merged["R1"] <= merged["R2"] <= 1):
        raise ValueError("质检参数必须满足 0 < R1 <= R2 <= 1")
    return merged


def stage1_quota(total_n, R1=DEFAULT_POOL_CONFIG["R1"]):
    return max(1, math.ceil(int(total_n or 0) * float(R1))) if int(total_n or 0) > 0 else 0


def stage2_quota(total_n, R2=DEFAULT_POOL_CONFIG["R2"]):
    return max(1, math.ceil(int(total_n or 0) * float(R2))) if int(total_n or 0) > 0 else 0


def person_accuracy(items):
    items = list(items or [])
    n = len(items)
    return None if n == 0 else sum(1 for it in items if it.get("verdict") == PASS) / n


def overall_accuracy(all_checked):
    return person_accuracy(all_checked)


def _pass_rate_from_counts(checked, defects):
    return None if checked <= 0 else (checked - defects) / checked


def _annotator_dicts(annotators):
    if isinstance(annotators, dict):
        for reviewer, bucket in annotators.items():
            out = dict(bucket)
            out.setdefault("reviewer", reviewer)
            yield out
    else:
        for item in annotators or []:
            yield dict(item)


def _first_stage_decision(total, checked, cfg):
    """按 qcTime 重放：到达 S1 配额时首次达标即 PASS 锁定，否则 SUSPECT。"""
    q1 = stage1_quota(total, cfg["R1"])
    ordered = _replay_order(checked)
    if len(ordered) < q1:
        return PENDING, ordered, None
    first = ordered[:q1]
    acc = person_accuracy(first)
    return (PASS_STATE if acc is not None and acc >= cfg["T"] else SUSPECT), first, acc


def _suggest_from_unchecked(unchecked, need):
    if need <= 0:
        return []
    return _hash_sorted_items(list(unchecked or []))[:need]


def evaluate_pool(annotators, cfg=None):
    """三阶段责任池抽检判定。

    annotators 可为 dict 或 list。每个作业人 bucket 支持：
      reviewer/name, total, checked=[{promptId, verdict, qcTime, ...}],
      uncheckedIds/unchecked=[promptId 或 {promptId, excelRow, ...}]

    返回：当前阶段、每人状态、总体正确率、下一步队列、待返工条目清单。
    """
    cfg = _normalize_pool_cfg(cfg)
    people = []
    all_checked = []
    total_items = 0
    total_checked = 0
    total_defects = 0
    stage1_open = False
    stage2_open = False
    suspects = []
    next_queue = []

    for raw in _annotator_dicts(annotators):
        reviewer = (raw.get("reviewer") or raw.get("name") or "").strip() or "未知"
        checked = list(raw.get("checked") or [])
        unchecked = list(raw.get("unchecked") or raw.get("uncheckedIds") or [])
        total = int(raw.get("total") or (len(checked) + len(unchecked)))
        q1 = stage1_quota(total, cfg["R1"])
        q2 = stage2_quota(total, cfg["R2"])
        checked_ordered = _replay_order(checked)
        checked_count = len(checked_ordered)
        defects = sum(1 for it in checked_ordered if it.get("verdict") == FAIL)
        acc = person_accuracy(checked_ordered)
        total_items += total
        total_checked += checked_count
        total_defects += defects
        all_checked.extend(checked_ordered)

        s1_state, s1_items, s1_acc = _first_stage_decision(total, checked_ordered, cfg)
        person = {
            "reviewer": reviewer,
            "total": total,
            "checked": checked_count,
            "defects": defects,
            "passRate": acc,
            "stage1Quota": q1,
            "stage2Quota": q2,
            "state": PENDING,
            "personState": PENDING,
            "currentStage": STAGE1,
            "nextSuggestedId": None,
            "suggestQueue": [],
            "unchecked": unchecked,
        }

        if total <= 0:
            person.update({"state": PASS_STATE, "personState": PASS_STATE, "currentStage": DONE})
        elif s1_state == PENDING:
            need = q1 - checked_count
            suggest = _suggest_from_unchecked(unchecked, need)
            person.update({
                "state": PENDING,
                "personState": PENDING,
                "currentStage": STAGE1,
                "nextSuggestedId": _item_prompt_id(suggest[0]) if suggest else None,
                "suggestQueue": suggest,
                "distanceToStageDone": need,
            })
            stage1_open = True
            next_queue.extend({"reviewer": reviewer, **it} if isinstance(it, dict) else {"reviewer": reviewer, "promptId": it} for it in suggest)
        elif s1_state == PASS_STATE:
            person.update({
                "state": PASS_STATE,
                "personState": PASS_STATE,
                "currentStage": DONE,
                "locked": True,
                "lockedAtChecked": q1,
                "stage1PassRate": s1_acc,
            })
        else:
            person.update({"state": SUSPECT, "personState": SUSPECT, "currentStage": STAGE2, "stage1PassRate": s1_acc})
            suspects.append(person)
            if checked_count < q2:
                need = q2 - checked_count
                suggest = _suggest_from_unchecked(unchecked, need)
                person.update({
                    "nextSuggestedId": _item_prompt_id(suggest[0]) if suggest else None,
                    "suggestQueue": suggest,
                    "distanceToStageDone": need,
                })
                stage2_open = True
                next_queue.extend({"reviewer": reviewer, **it} if isinstance(it, dict) else {"reviewer": reviewer, "promptId": it} for it in suggest)
        people.append(person)

    overall = overall_accuracy(all_checked)
    if stage1_open:
        pool_stage = STAGE1
        pool_state = CONTINUE
        rework_items = []
    elif not suspects:
        pool_stage = DONE
        pool_state = DONE
        rework_items = []
    elif stage2_open:
        pool_stage = STAGE2
        pool_state = CONTINUE
        rework_items = []
    elif overall is not None and overall >= cfg["T"]:
        pool_stage = DONE
        pool_state = DONE
        rework_items = []
    else:
        pool_stage = REWORK
        pool_state = REWORK
        rework_items = []
        suspect_names = {p["reviewer"] for p in suspects}
        for p in people:
            if p["reviewer"] in suspect_names:
                p["state"] = REWORK
                p["personState"] = REWORK
                p["currentStage"] = REWORK
                for item in p.get("unchecked") or []:
                    entry = dict(item) if isinstance(item, dict) else {"promptId": item}
                    entry.setdefault("reviewer", p["reviewer"])
                    rework_items.append(entry)

    return {
        "stage": pool_stage,
        "state": pool_state,
        "annotators": people,
        "overallAccuracy": overall,
        "nextQueue": next_queue,
        "reworkItems": rework_items,
        "summary": {
            "annotatorCount": len(people),
            "totalItems": total_items,
            "totalChecked": total_checked,
            "totalDefects": total_defects,
            "overallPassRate": overall,
            "overallAccuracy": overall,
            "fullCheckBaseline": total_items,
            "savedItems": max(0, total_items - total_checked),
            "savedRate": (total_items - total_checked) / total_items if total_items else None,
            "passCount": sum(1 for p in people if p.get("personState") == PASS_STATE),
            "suspectCount": sum(1 for p in people if p.get("personState") == SUSPECT),
            "reworkCount": sum(1 for p in people if p.get("personState") == REWORK),
            "pendingCount": sum(1 for p in people if p.get("personState") == PENDING),
        },
        "config": cfg,
    }


def _freeze_at_cap(d, n, bounds: SprtBoundaries):
    """到顶/检完收口：用经验缺陷率与中点比较。"""
    return ACCEPT if (d / n) <= bounds.q_mid else REJECT


def evaluate_annotator(total_n, checked_items, unchecked_ids, params: SprtParams = None):
    """对单个标注员逐条重放 SPRT，返回终态/统计/建议待检。

    入参契约（上游必须保证，否则终态不可复现）：
      total_n        该员总条数 N（含未检）
      checked_items  已检条目列表，元素 {promptId, verdict(通过/不通过), qcTime}
                     - 每个 promptId 在列表中唯一（同一条只出现一次）。
                     - `verdict` 取该条「最后一次质检结论」（与表格当前单元格一致）。
                     - `qcTime` 取该条「首次质检时间」，重检不得刷新它——否则重放
                       顺序漂移、可能改变已锁定的终态。计数用最后结论、排序用首次时间，
                       两者口径分离。
      unchecked_ids  未检条目的 promptId 列表
      params         SprtParams（默认值见 dataclass）

    返回 dict：见文档「输出给前端」。
    `distanceToAccept` 的语义由 `state` 决定（None 不自含义，必须配合 state 读）：
      - state=accept    → None 表示「已通过/已放行」
      - state=reject    → None 表示「转全量，无接收距离」
      - state=full-check→ None 表示「小批量全检，不跑抽检」
      - state=continue  → 数字为乐观追加条数；None 表示「不可达」（濒临 reject）
    """
    params = params or SprtParams()
    bounds = sprt_boundaries(params)
    total_n = int(total_n)

    # 小批量全检兜底，优先于 SPRT
    if total_n < params.n_min:
        suggest = _hash_sorted_ids(unchecked_ids)
        n = len(checked_items)
        d = sum(1 for it in checked_items if it.get("verdict") == FAIL)
        return _result(FULL_CHECK, total_n, n, d, None, suggest, bounds, params)

    n_max = min(total_n, params.nmax_cap)
    ordered = _replay_order(checked_items)

    state = None
    n = 0
    d = 0
    for item in ordered:
        n += 1
        if item.get("verdict") == FAIL:
            d += 1
        if d <= bounds.a + bounds.s * n:
            state = ACCEPT
            break
        if d >= bounds.b + bounds.s * n:
            state = REJECT
            break
        if n == n_max:
            state = _freeze_at_cap(d, n, bounds)
            break

    if state is None:
        # 未冻结：要么还能继续抽，要么（理论边界）已检完全部
        if n >= total_n and n > 0:
            state = _freeze_at_cap(d, n, bounds)
        else:
            state = CONTINUE

    if state == CONTINUE:
        suggest = _hash_sorted_ids(unchecked_ids)
        distance = _distance_to_accept(d, n, bounds)
    elif state == REJECT:
        suggest = _hash_sorted_ids(unchecked_ids)  # 转全量：剩余全部要检
        distance = None
    else:  # ACCEPT
        suggest = []  # 已放行，无需再检
        distance = None

    return _result(state, total_n, n, d, distance, suggest, bounds, params)


def _distance_to_accept(d, n, bounds: SprtBoundaries):
    """乐观估计：假设后续全过，触接收线 d ≤ a+s·n' 所需的最少追加条数。

    不可达返回 None：斜率非正，或「濒临 reject」（再错约 1 条即触拒收线，
    此时给接收距离会误导）——对应文档「濒临 reject 显示 —」。
    """
    if bounds.s <= 0:
        return None
    # 濒临 reject：下一条若不通过 (n+1, d+1) 即触拒收线
    if (d + 1) >= bounds.b + bounds.s * (n + 1):
        return None
    need_total = math.ceil((d - bounds.a) / bounds.s)
    remaining = need_total - n
    return remaining if remaining > 0 else None


def _result(state, total_n, n, d, distance, suggest, bounds: SprtBoundaries, params: SprtParams):
    pass_rate = (n - d) / n if n > 0 else None
    return {
        "state": state,
        "total": total_n,
        "checked": n,
        "defects": d,
        "passRate": pass_rate,
        "distanceToAccept": distance,
        "nextSuggestedId": suggest[0] if suggest else None,
        "suggestQueue": suggest,
        "boundaries": {"a": bounds.a, "b": bounds.b, "s": bounds.s, "qMid": bounds.q_mid},
        "params": {
            "p1": params.p1, "p0": params.p0, "alpha": params.alpha,
            "beta": params.beta, "nmaxCap": params.nmax_cap, "nMin": params.n_min,
        },
    }
