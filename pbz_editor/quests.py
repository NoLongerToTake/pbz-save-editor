"""Quest-record projections. This module never changes actors or world events."""
from __future__ import annotations
import copy
from datetime import datetime

PREFIX='EQuestCompletion::'
BRANCH='EBranchType::'
SIDE_FOLDERS=('/Simple/','/Collect/','/Branch/')
STATUS={'QC_Started':('active','进行中'),'QC_Succeded':('completed','已完成'),'QC_Failed':('failed','已失败')}


def _records(document):
    records=document.get('Narrative',{}).get('NarrativeSavedQuests',[])
    if not isinstance(records,list):raise ValueError('任务列表不是受支持的数组结构')
    ids=[record.get('QuestClass') for record in records if isinstance(record,dict)]
    if len(ids)!=len(records) or len(ids)!=len(set(ids)):raise ValueError('任务记录包含重复或无效任务ID')
    return records


def _status(record):
    if record is None:return 'unaccepted','未接取（无记录，前置未核实）'
    return STATUS.get(str(record.get('QuestCompletion','')).split('::')[-1],('unknown','状态不明'))


def list_quests(document,catalog):
    records={q['QuestClass']:q for q in _records(document)}
    specs={q['id']:q for q in catalog.quests}
    for quest_id in records:
        if quest_id not in specs and any(folder in quest_id for folder in SIDE_FOLDERS):
            specs[quest_id]=dict(id=quest_id,name=quest_id.rsplit('/',1)[-1],branches=[],routes=[],notes=['任务目录未识别；可查看记录、标记失败或重置'])
    rows=[]
    for quest_id,spec in specs.items():
        record=records.get(quest_id);status,label=_status(record)
        branches=(record or {}).get('QuestBranches',[])
        active=[b.get('BranchID','') for b in branches if str(b.get('BranchType','')).endswith('::BranchActivating')]
        rows.append(dict(spec,id=quest_id,status=status,status_label=label,record_present=record is not None,
                         current_stage='、'.join(active) or ('已结束' if status in ('completed','failed') else '—'),
                         region=spec.get('region',''),routes=spec.get('routes',[]),notes=spec.get('notes',[])))
    return rows


def _new_record(spec,quest_id):
    record=copy.deepcopy(spec.get('template') or {})
    record.update(QuestClass=quest_id,QuestCompletion=PREFIX+'QC_Started',CompletedBranches=[],ReachedStateNames=[])
    existing={b['BranchID']:b for b in record.get('QuestBranches',[]) if 'BranchID' in b}
    branches=[]
    for branch in spec.get('branches',[]):
        branch_id=str(branch['id']);value=copy.deepcopy(existing.get(branch_id,{}))
        value.update(BranchID=branch_id,BranchType=BRANCH+'None',DirtyTime='0001.01.01-00.00.00')
        if isinstance(value.get('TasksProgress'),list):value['TasksProgress']=[0 for _ in value['TasksProgress']]
        branches.append(value)
    record['QuestBranches']=branches
    return record


def _stop_tracking(narrative,quest_id):
    if narrative.get('TrackedQuestPath')!=quest_id:return
    for key in ('TrackedQuestPath','TrackedBranchID','TrackedTaskPointID'):
        if key in narrative:narrative[key]='None'


def _branch_label(value):
    return {'BranchCompleted':'已完成','BranchActivating':'进行中','BranchLocked':'已锁定','None':'未到达'}.get(str(value).split('::')[-1],'不明')


def preview_quest(document,catalog,quest_id,action,route_id=None,stage_id=None,receipts=()):
    if action not in ('complete','stage','fail','reset','rewards'):raise ValueError('不支持该任务操作')
    records=_records(document);old=next((q for q in records if q['QuestClass']==quest_id),None)
    spec=catalog.find(quest_id)
    if spec is None:
        if old is None or not any(folder in quest_id for folder in SIDE_FOLDERS):raise ValueError('不是目录或存档中的可编辑支线')
        spec=dict(id=quest_id,name=quest_id.rsplit('/',1)[-1],branches=[],routes=[],rewards=[])
    updated=copy.deepcopy(document);narrative=updated.get('Narrative',{})
    if action!='rewards':
        narrative=updated.setdefault('Narrative',{})
        narrative.setdefault('NarrativeSavedQuests',[])
    current=next((q for q in narrative.get('NarrativeSavedQuests',[]) if q['QuestClass']==quest_id),None)
    warnings=['仅修改任务记录与奖励；NPC、剧情和地图状态不处理。']
    can_apply=True;route=None;completed=[]
    routes=spec.get('routes',[])
    if route_id:
        route=next((r for r in routes if r['id']==route_id),None)
        if route is None:raise ValueError('所选成功路线不存在')
    elif len(routes)==1:route=routes[0];route_id=route['id']
    stage_scope=action=='stage' or (action=='rewards' and stage_id is not None)
    if stage_scope:
        if route is None:
            can_apply=False;warnings.append('请选择已核实的成功路线；此任务可能尚无可用路线。')
        elif not route.get('stage_order_verified',False):
            can_apply=False;warnings.append('阶段先后尚未核实；完成分支集合不能代表流程顺序，暂只支持终点完成。')
        else:
            route_branches=list(route.get('branches',[]))
            if len(route_branches)!=len(set(route_branches)):raise ValueError('路线中有重复阶段，尚不支持此循环流程')
            ordered=route.get('stage_order',[])
            if len(ordered)!=len(set(ordered)) or set(ordered)!=set(route_branches):raise ValueError('阶段顺序与成功分支不一致')
            if stage_id not in ordered:raise ValueError('所选阶段不属于这条成功路线')
            completed=ordered[:ordered.index(stage_id)+1]
    if action in ('complete','stage'):
        if not can_apply:pass
        elif route is None:
            can_apply=False;warnings.append('请选择已核实的成功路线；此任务可能尚无可用路线。')
        else:
            route_branches=list(route.get('branches',[]))
            if len(route_branches)!=len(set(route_branches)):raise ValueError('路线中有重复阶段，尚不支持此循环流程')
            if action=='complete':completed=route_branches
            known={str(b['id']) for b in spec.get('branches',[])}|{str(b['BranchID']) for b in (old or {}).get('QuestBranches',[])}
            if any(x not in known for x in route_branches):raise ValueError('成功路线引用了未知分支')
            if current is None:
                current=_new_record(spec,quest_id);narrative['NarrativeSavedQuests'].append(current)
            branches=current.setdefault('QuestBranches',[])
            if not isinstance(branches,list):raise ValueError('任务分支结构不支持')
            ids=[b.get('BranchID') for b in branches]
            if len(ids)!=len(set(ids)):raise ValueError('任务包含重复分支ID')
            for branch in spec.get('branches',[]):
                if branch['id'] not in ids:
                    branches.append(dict(BranchID=branch['id'],BranchType=BRANCH+'None',DirtyTime='0001.01.01-00.00.00'))
            final=set(completed)==set(route_branches)
            active=None if final else ordered[len(completed)]
            now=datetime.now().strftime('%Y.%m.%d-%H.%M.%S')
            for branch in branches:
                state='BranchCompleted' if branch['BranchID'] in completed else 'BranchActivating' if branch['BranchID']==active else 'None'
                if branch.get('BranchType')!=BRANCH+state:branch['BranchType']=BRANCH+state;branch['DirtyTime']=now
            if set(current.get('CompletedBranches',[]))!=set(completed):current['CompletedBranches']=list(completed)
            current['QuestCompletion']=PREFIX+('QC_Succeded' if final else 'QC_Started')
            current['ReachedStateNames']=list(route.get('reached_states',[])) if final else []
            if final:_stop_tracking(narrative,quest_id)
            elif narrative.get('TrackedQuestPath')==quest_id:
                narrative['TrackedBranchID']=active
                if 'TrackedTaskPointID' in narrative:narrative['TrackedTaskPointID']='None'
    elif action=='fail':
        if current is None:
            current=_new_record(spec,quest_id);narrative['NarrativeSavedQuests'].append(current)
        current['QuestCompletion']=PREFIX+'QC_Failed'
        for branch in current.get('QuestBranches',[]):
            if branch.get('BranchType')==BRANCH+'BranchActivating':branch['BranchType']=BRANCH+'BranchLocked'
        _stop_tracking(narrative,quest_id)
        warnings.append('只标记任务失败，不执行该任务的失败剧情；已有奖励保留。')
    elif action=='reset':
        narrative['NarrativeSavedQuests']=[q for q in narrative['NarrativeSavedQuests'] if q['QuestClass']!=quest_id]
        if 'QuestGameTimes' in narrative:
            if not isinstance(narrative['QuestGameTimes'],list):raise ValueError('任务计时结构未知，无法精确重置')
            narrative['QuestGameTimes']=[q for q in narrative['QuestGameTimes'] if q.get('QuestClass')!=quest_id]
        _stop_tracking(narrative,quest_id);current=None
        warnings.append('仅重置任务记录；保留已有奖励，不保证能重新接取。')
    rewards=[]
    if action in ('complete','stage','rewards') and can_apply:
        for reward in spec.get('rewards',[]):
            if reward.get('route_ids') and route_id not in reward['route_ids']:continue
            if (action=='complete' or stage_scope) and reward.get('stage_id') and reward['stage_id'] not in completed:continue
            if stage_scope:
                if reward.get('category')=='quest_final':
                    if set(completed)!=set(route.get('branches',[])):continue
                elif reward.get('stage_id') not in completed:continue
            rewards.append(reward)
        from .rewards import build_reward_preview
        rewards=build_reward_preview(document,rewards,receipts)
        if not rewards:warnings.append('此操作没有可核实的适用奖励；未查明不代表没有奖励。')
    old_branches={b['BranchID']:b for b in (old or {}).get('QuestBranches',[])}
    new_branches={b['BranchID']:b for b in (current or {}).get('QuestBranches',[])}
    specs={b['id']:b for b in spec.get('branches',[])}
    flow=[]
    for branch_id in dict.fromkeys(list(specs)+list(old_branches)+list(new_branches)):
        info=specs.get(branch_id,{})
        flow.append(dict(info,id=branch_id,name=info.get('name') or branch_id,
                         before=_branch_label(old_branches.get(branch_id,{}).get('BranchType',BRANCH+'None')),
                         after=_branch_label(new_branches.get(branch_id,{}).get('BranchType',BRANCH+'None'))))
    from .format import json_differences
    return dict(quest_id=quest_id,name=spec.get('name',quest_id),action=action,route_id=route_id,stage_id=stage_id,
                changes=json_differences(document,updated),rewards=rewards,warnings=warnings,flow=flow,
                can_apply=can_apply,_document=updated)
