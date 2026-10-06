"""纯业务判定：替代范围、临时豁免、分阶段生效、未来业务过滤、替代链解析。

发布后的提案**不回写**历史单据；某条业务是否受影响在查询时按以下要素即时计算：

1. 单据状态须落在提案范围允许的状态内（默认仅“草拟/待确认”等未锁定业务）；
2. 单据类型须在替代范围内；
3. 业务锚定日期不得早于生效门槛（发布日与所属阶段生效日的较晚者）；
4. 当日不在覆盖该单据（或部件）的有效豁免窗口内；
5. 替代链对该单据类型可达（PARTIAL 范围按单据类型裁剪）。
"""
from __future__ import annotations

from datetime import date

from . import models
from .errors import ValidationFailed
from .repository import Repository


# ---- 提案配置校验 ----


def validate_scope(scope: models.ProposalScope) -> None:
    if scope.ref_types is not None:
        unknown = set(scope.ref_types) - set(models.REF_TYPES)
        if unknown:
            raise ValidationFailed("未知单据类型：" + "、".join(sorted(unknown)))
    bad_states = set(scope.affect_states) - set(models.DOC_STATES)
    if bad_states:
        raise ValidationFailed("未知单据状态：" + "、".join(sorted(bad_states)))
    if models.STATE_CLOSED in scope.affect_states:
        raise ValidationFailed("已关闭单据不得纳入替代范围")


def validate_phases(phases: list[models.Phase]) -> dict[str, models.Phase]:
    """校验阶段配置并返回 ref_type -> 生效阶段 的映射。

    - 同一阶段内单据类型不重复；
    - 任一单据类型最多被一个阶段覆盖；
    - 覆盖全部类型的阶段（ref_types=None）至多一个，且不能与其它阶段并存。
    """
    mapping: dict[str, models.Phase] = {}
    for phase in phases:
        models.parse_date(phase.effective_date)
        targets = models.REF_TYPES if phase.ref_types is None else phase.ref_types
        unknown = set(targets) - set(models.REF_TYPES)
        if unknown:
            raise ValidationFailed(
                f"阶段 {phase.name} 含未知单据类型：" + "、".join(sorted(unknown))
            )
        for ref_type in targets:
            if ref_type in mapping:
                raise ValidationFailed(
                    f"单据类型 {ref_type} 同时被阶段 {mapping[ref_type].name} 与 {phase.name} 覆盖"
                )
            mapping[ref_type] = phase
    return mapping


def validate_exemption_window(valid_from: str, valid_to: str) -> None:
    start = models.parse_date(valid_from)
    end = models.parse_date(valid_to)
    if start > end:
        raise ValidationFailed("豁免生效起始日不得晚于到期日")


# ---- 替代链解析 ----


def resolve_replacement(
    repo: Repository, part_id: str, ref_type: str
) -> tuple[str | None, list[str]]:
    """沿替代链解析某类单据的最终替代料。

    PARTIAL 范围的替代边只对其声明的单据类型生效。
    返回 (最终替代料 id, 解析路径)；无可用替代时首元素为 None。
    """
    chain = [part_id]
    current = part_id
    for _ in range(len(repo.parts) + 1):
        chosen = None
        for link in repo.sub_targets(current):
            if link.scope == models.SCOPE_PARTIAL:
                if link.ref_types and ref_type in link.ref_types:
                    chosen = link
                    break
            else:
                chosen = link
                break
        if chosen is None:
            break
        if chosen.new_part_id in chain:
            # 数据层已拒绝成环写入，此处防御性截断
            break
        current = chosen.new_part_id
        chain.append(current)
    replacement = chain[-1] if len(chain) > 1 else None
    return replacement, chain


# ---- 影响项判定 ----


def _phase_for(
    phase_map: dict[str, models.Phase], ref_type: str
) -> models.Phase | None:
    """配置了阶段但该类型没有任何阶段覆盖时，返回 None（永不生效）。"""
    return phase_map.get(ref_type)


def _find_exemption(
    exemptions: list[models.Exemption], item: models.ImpactItem, today: date
) -> models.Exemption | None:
    for exemption in exemptions:
        if not (models.parse_date(exemption.valid_from) <= today <= models.parse_date(exemption.valid_to)):
            continue
        if exemption.ref_id is not None:
            if exemption.ref_id == item.ref_id:
                return exemption
        elif exemption.part_id is not None:
            if exemption.part_id in (item.part_id, _target_part_on_path(item)):
                return exemption
    return None


def _target_part_on_path(item: models.ImpactItem) -> str | None:
    for hop in reversed(item.path):
        if hop.kind == "target":
            return hop.detail.get("part_id")
    return None


def evaluate_item(
    repo: Repository,
    proposal: models.ChangeProposal,
    item: models.ImpactItem,
    today: date,
    published: bool,
) -> None:
    """就地填充 ImpactItem 的 applicable / phase / exempted / reasons。

    “未来业务”以单据自身的业务锚定日期（交货/到货/开工日）为准：规则（发布
    门槛与阶段生效日）必须在业务执行当日已经生效，才会影响该单据；查询时刻
    只用于判断临时豁免是否仍在有效期。
    """
    reasons: list[str] = []
    exempted = False
    phase_name: str | None = None
    applicable = True
    business_day = models.parse_date(item.business_date)

    # 1. 终态 / 状态范围
    if item.state in models.TERMINAL_STATES:
        applicable = False
        reasons.append(f"单据状态为{item.state}，属于终态业务，不受影响")
    elif item.state not in proposal.scope.affect_states:
        applicable = False
        reasons.append(
            f"单据状态 {item.state} 不在替代范围允许状态"
            f"（{'、'.join(proposal.scope.affect_states)}）内"
        )

    # 2. 单据类型范围
    allowed_types = proposal.scope.ref_types
    if applicable and allowed_types is not None and item.ref_type not in allowed_types:
        applicable = False
        reasons.append("单据类型不在限定替代范围内")

    # 3. 分阶段生效：以业务日期对照阶段生效日
    gate: date | None = None
    phase_map = validate_phases(proposal.phases)
    if proposal.phases:
        phase = _phase_for(phase_map, item.ref_type)
        if phase is None:
            applicable = False
            reasons.append("该单据类型未配置任何生效阶段，永不自动适用")
        else:
            phase_name = phase.name
            eff = models.parse_date(phase.effective_date)
            gate = eff
            if applicable and business_day < eff:
                applicable = False
                reasons.append(
                    f"业务日期 {item.business_date} 早于阶段 {phase.name}"
                    f"生效日 {phase.effective_date}，该批次按旧规则执行"
                )

    # 4. 发布门槛：只影响发布日之后的未来业务
    if published:
        assert proposal.published_at is not None
        publish_day = date.fromisoformat(proposal.published_at[:10])
        gate = max(gate, publish_day) if gate else publish_day
        if applicable and business_day < publish_day:
            applicable = False
            reasons.append(
                f"业务日期 {item.business_date} 早于发布日 {publish_day.isoformat()}，"
                "属于历史/在行业务，不予追溯"
            )
    else:
        reasons.append("提案尚未发布，当前为预演结果")

    # 5. 临时豁免（按查询时刻判断窗口是否有效）
    exemption = _find_exemption(repo.exemptions_for(proposal.id), item, today)
    if exemption is not None:
        applicable = False
        exempted = True
        reasons.append(
            f"豁免单 {exemption.id} 生效至 {exemption.valid_to}：{exemption.reason}"
        )

    # 6. 替代料可达性提示（不改变停用造成的“受影响”事实）
    if applicable and proposal.action == models.ACTION_SUBSTITUTE:
        if proposal.replacement_part_id is not None:
            reasons.append(
                f"替代路径：{proposal.target_part_id} → {proposal.replacement_part_id}"
            )

    item.applicable = applicable
    item.exempted = exempted
    item.phase = phase_name
    item.reasons = reasons


def evaluate_report(
    repo: Repository,
    proposal: models.ChangeProposal,
    report: models.ImpactReport,
    today: date | None = None,
) -> models.ImpactReport:
    today = today or repo.now().date()
    report.replacement_part_id = proposal.replacement_part_id
    published = proposal.status == models.PROP_PUBLISHED
    for item in (*report.direct, *report.indirect):
        evaluate_item(repo, proposal, item, today, published)
    return report
