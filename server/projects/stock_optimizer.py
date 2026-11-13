"""Joint, integer allocation of warehouse pieces. Reading a plan never reserves stock."""
import json
from collections import Counter,defaultdict
from decimal import Decimal
from functools import lru_cache
from django.core.exceptions import ValidationError

ERROR='Совместный подбор муфт не завершён. Повторите проверку; до подтверждения расчёта потребность отправлять нельзя.'
STANDARD=(1000,1500,2000,2500)


def wall_key(value):return format(Decimal(value).normalize(),'f')


class Model:
    def __init__(self):
        self.upper=[];self.rows=[];self.costs=[{} for _ in range(8)]
    def var(self,upper,**costs):
        col=len(self.upper)
        if col>=50000:raise ValidationError(ERROR)
        self.upper.append(int(upper))
        for key,value in costs.items():self.costs[int(key[1:])][col]=value
        return col
    def row(self,terms,lower=float('-inf'),upper=float('inf')):
        self.rows.append((dict(terms),lower,upper))
    def solve(self):
        import time
        import highspy
        import numpy as np
        n=len(self.upper);empty=np.array([],dtype=np.int32);deadline=time.monotonic()+15
        solution=None
        # Solve each priority separately and constrain its verified integer value.
        # Do not use blended objectives: no secondary preference may sacrifice a defect.
        for costs in self.costs:
            if not any(costs.values()):continue
            seconds=deadline-time.monotonic()
            if seconds<=0:raise ValidationError(ERROR)
            h=highspy.Highs()
            for key,value in [('output_flag',False),('threads',1),('parallel','off'),('random_seed',0),
                              ('mip_rel_gap',0.0),('mip_abs_gap',0.0),('time_limit',seconds)]:
                if h.setOptionValue(key,value)!=highspy.HighsStatus.kOk:raise ValidationError(ERROR)
            h.addCols(n,np.array([costs.get(i,0) for i in range(n)],dtype=float),np.zeros(n),np.array(self.upper,dtype=float),0,empty,empty,np.array([],dtype=float))
            h.changeColsIntegrality(n,np.arange(n,dtype=np.int32),np.ones(n,dtype=np.uint8))
            starts=[0];indices=[];values=[];lower=[];upper=[]
            for terms,lo,hi in self.rows:
                for col,value in sorted(terms.items()):
                    if value:indices.append(col);values.append(float(value))
                starts.append(len(indices));lower.append(lo);upper.append(hi)
            h.addRows(len(lower),np.array(lower),np.array(upper),len(indices),np.array(starts,dtype=np.int32),np.array(indices,dtype=np.int32),np.array(values))
            h.run()
            if h.getModelStatus()!=highspy.HighsModelStatus.kOptimal:raise ValidationError(ERROR)
            raw=h.getSolution().col_value;solution=[int(round(value)) for value in raw]
            if any(abs(value-integer)>1e-5 or not 0<=integer<=bound for value,integer,bound in zip(raw,solution,self.upper)):
                raise ValidationError(ERROR)
            for terms,lo,hi in self.rows:
                value=sum(coefficient*solution[col] for col,coefficient in terms.items())
                if not lo-1e-5<=value<=hi+1e-5:raise ValidationError(ERROR)
            optimum=sum(coefficient*solution[col] for col,coefficient in costs.items())
            if abs(h.getObjectiveValue()-optimum)>1e-4:raise ValidationError(ERROR)
            self.row(costs,upper=optimum+1e-7)
        if solution is None:raise ValidationError(ERROR)
        return solution


@lru_cache(maxsize=32)
def solve_component(serialized):
    """Lexicographic objectives, not a weighted guess: closed defects come first."""
    data=json.loads(serialized);jobs=data['jobs'];groups=data['groups']
    if not groups:
        return tuple((job['wall'],(),tuple((job['wall'],length,count) for length,count in job['required']),False) for job in jobs)
    model=Model()
    capacity=defaultdict(dict);records=[];covering=defaultdict(list)
    for j,job in enumerate(jobs):
        y=model.var(1);free=[];buys=[];walls=[]
        for defect in set(job['defects']):covering[defect].append(y)
        if job['method']=='p1':
            possible=[job['wall']] if job['fixed'] else sorted({job['wall'],*(g['wall'] for g in groups if j in g['jobs'])},key=Decimal)
            for wall in possible:
                color=model.var(1,c7=float(Decimal(wall)) if len(possible)>1 else 0);walls.append((wall,color))
                for g,group in enumerate(groups):
                    if j not in group['jobs'] or group['wall']!=wall:continue
                    bound=min(group['quantity'],job['max']//group['length'],33)
                    if not bound:continue
                    x=model.var(bound,c3=group['length'],c4=1)
                    free.append((g,x));capacity[g][x]=1;model.row({x:1,color:-bound},upper=0)
                for length in STANDARD:
                    bound=min(job['max']//length,33)
                    if not bound:continue
                    b=model.var(bound,c1=length,c2=1,c3=length,c4=1,c5=int(length==2500),c6=(length//500)**2)
                    buys.append((wall,length,b));model.row({b:1,color:-bound},upper=0)
            model.row({col:1 for _,col in walls},lower=1,upper=1)
            length_terms={col:groups[g]['length'] for g,col in free}
            length_terms.update({col:length for _,length,col in buys})
            model.row(length_terms,lower=job['gap'],upper=job['max'])
            model.row({**{col:groups[g]['length'] for g,col in free},y:-job['gap']},lower=0)
            model.row({**{col:1 for _,col in free},**{col:1 for _,_,col in buys}},upper=33)
            max_buys=33
        else:
            for required,count in job['required']:
                terms={}
                for g,group in enumerate(groups):
                    if j not in group['jobs'] or not (group['length']>=required if job['method']=='p2' else group['length']==required):continue
                    x=model.var(min(count,group['quantity']),c3=group['length']-required,c4=1)
                    free.append((g,x));capacity[g][x]=1;terms[x]=1
                b=model.var(count,c1=required,c2=1,c4=1);buys.append((job['wall'],required,b));terms[b]=1
                model.row(terms,lower=count,upper=count)
                model.row({**{col:value for col,value in terms.items() if col!=b},y:-count},lower=0)
            max_buys=sum(count for _,count in job['required'])
        model.row({**{col:1 for _,_,col in buys},y:max_buys},upper=max_buys)
        model.row({**{col:1 for _,_,col in buys},y:1},lower=1)
        records.append((y,free,buys,walls))
    # Interchangeable jobs with disjoint defects may be ordered without excluding
    # any physical plan. This removes permutations of identical warehouse kits.
    twins=defaultdict(list)
    for j,job in enumerate(jobs):
        if any(len(covering[d])!=1 for d in set(job['defects'])):continue
        key=json.dumps({k:v for k,v in job.items() if k not in ('id','defects')},sort_keys=True)
        eligible=tuple(g for g,group in enumerate(groups) if j in group['jobs'])
        twins[(key,eligible,len(set(job['defects'])))].append(j)
    for indices in twins.values():
        for a,b in zip(indices,indices[1:]):
            ay,afree,_,_=records[a];by,bfree,_,_=records[b]
            model.row({ay:1,by:-1},lower=0)
            terms={col:groups[g]['length'] for g,col in afree}
            terms.update({col:-groups[g]['length'] for g,col in bfree})
            terms[ay]=10500;terms[by]=-10500
            model.row(terms,lower=0)
    for defect in sorted(covering):
        z=model.var(1,c0=-1);model.row({z:1,**{col:-1 for col in covering[defect]}},upper=0)
    for g,terms in capacity.items():model.row(terms,upper=groups[g]['quantity'])
    values=model.solve();result=[]
    for j,(y,free,buys,walls) in enumerate(records):
        stock=Counter()
        for g,col in free:stock[g]+=values[col]
        purchases=Counter()
        for wall,length,col in buys:purchases[(wall,length)]+=values[col]
        chosen=next((wall for wall,col in walls if values[col]),jobs[j]['wall'])
        result.append((chosen,tuple(sorted((g,count) for g,count in stock.items() if count)),
                       tuple(sorted((wall,length,count) for (wall,length),count in purchases.items() if count)),bool(values[y])))
    return tuple(result)


@lru_cache(maxsize=32)
def solve(serialized):
    """Independent stock/defect components are exactly separable, never greedy."""
    import time
    data=json.loads(serialized);jobs=data['jobs'];groups=data['groups']
    parents=list(range(len(jobs)))
    def root(i):
        while parents[i]!=i:parents[i]=parents[parents[i]];i=parents[i]
        return i
    def join(indices):
        if not indices:return
        first=root(indices[0])
        for i in indices[1:]:parents[root(i)]=first
    for group in groups:join(group['jobs'])
    defects=defaultdict(list)
    for i,job in enumerate(jobs):
        for defect in set(job['defects']):defects[defect].append(i)
    for indices in defects.values():join(indices)
    components=defaultdict(list)
    for i in range(len(jobs)):components[root(i)].append(i)
    if len(components)<=1:return solve_component(serialized)
    result=[None]*len(jobs);deadline=time.monotonic()+15
    for indices in components.values():
        if time.monotonic()>deadline:raise ValidationError(ERROR)
        remap={old:new for new,old in enumerate(indices)}
        subset={'jobs':[jobs[i] for i in indices], 'groups':[
            {**group,'jobs':[remap[i] for i in group['jobs']]} for group in groups if group['jobs'] and group['jobs'][0] in remap]}
        answer=solve_component(json.dumps(subset,sort_keys=True,separators=(',',':')))
        for i,plan in zip(indices,answer):
            # Restore original group indices for physical stock materialisation.
            original=[g for g,group in enumerate(groups) if group['jobs'] and group['jobs'][0] in remap]
            wall,stock,purchases,complete=plan
            result[i]=(wall,tuple((original[g],count) for g,count in stock),purchases,complete)
    return tuple(result)


def allocate(records,supply,eligible):
    """Keep warehouse eligibility while merging interchangeable supply into counted groups."""
    specs=[];active=[];buckets={}
    baseline={str(d.pk) for record in records if not record['issues'] and not sum(record.get('_remaining',{}).values()) for d in record['job'].active_defects}
    for record in records:
        if record['issues']:continue
        job=record['job'];remaining=record['_remaining']
        if not sum(remaining.values()):continue
        spec={'id':str(job.pk),'method':job.method,'wall':wall_key(record['_wall']),
              'defects':sorted(str(d.pk) for d in job.active_defects if str(d.pk) not in baseline),'required':sorted(remaining.items()),'fixed':bool(record['_walls'])}
        if job.method=='p1':
            spec['gap']=max(0,int(Decimal(job.length_m)*1000)-record['_covered'])
            spec['max']=10500-record['_covered']
        specs.append(spec);active.append(record)
    if not active:return
    physical=Counter()
    for source in supply:
        item=source['item']
        if item.kind!='coupling' or item.reserved_job_id:continue
        if source['left']!=int(source['left']):raise ValidationError('Уточните дробный остаток муфты на складе.')
        if item.individual or item.coupling_id:
            physical[str(item.coupling_id or item.pk)]+=int(source['left'])
    if any(count!=1 for count in physical.values()):raise ValidationError('Одна физическая муфта числится в нескольких остатках. Уточните складской учёт перед подбором.')
    for index,source in sorted(enumerate(supply),key=lambda row:(str(row[1]['warehouse'].pk),row[1]['item'].sku,str(row[1]['item'].pk))):
        item=source['item']
        if item.kind!='coupling' or item.reserved_job_id or not item.length_mm or not item.wall_mm:continue
        count=int(source['left'])
        if count<1:continue
        targets=tuple(j for j,record in enumerate(active) if eligible(source['warehouse'],record['job'])
                      and item.method==record['job'].method and item.diameter_mm==record['job'].section.diameter_mm
                      and (item.wall_mm>=record['job'].wall_mm if item.method in ('p1','p2') else item.wall_mm==record['job'].wall_mm)
                      and (not record['_walls'] or Decimal(item.wall_mm) in record['_walls']))
        if not targets:continue
        key=(item.method,item.diameter_mm,item.length_mm,wall_key(item.wall_mm),targets)
        bucket=buckets.setdefault(key,{'method':item.method,'length':item.length_mm,'wall':wall_key(item.wall_mm),'jobs':targets,'quantity':0,'sources':[]})
        bucket['quantity']+=count;bucket['sources'].append([index,count])
    groups=[buckets[key] for key in sorted(buckets)]
    payload={'jobs':specs,'groups':[{k:v for k,v in group.items() if k!='sources'} for group in groups]}
    try:plans=solve(json.dumps(payload,sort_keys=True,separators=(',',':')))
    except (ImportError,RuntimeError,ValueError,OverflowError) as exc:raise ValidationError(ERROR) from exc
    for record,(wall,stock,purchases,complete) in zip(active,plans):
        job=record['job'];selected=[]
        for group_index,count in stock:
            group=groups[group_index]
            for member in group['sources']:
                take=min(count,member[1])
                if not take:continue
                index=member[0];member[1]-=take;count-=take
                selected.append((index,take));
                if not count:break
            if count:raise ValidationError(ERROR)
        record['allocation']=[{'item':supply[index]['item'],'warehouse':supply[index]['warehouse'],'quantity':count,'available':supply[index]['left']} for index,count in selected]
        record['stock_complete']=complete
        # Fixed reservations stay visible internally; only the actual purchasing remainder is published.
        lines=Counter()
        for length,count in record['_initial'].items():
            reserved=count-record['_remaining'][length]
            if reserved:lines[(wall_key(record['_wall']),length,'reserved')]+=reserved
        for index,count in selected:lines[(wall_key(supply[index]['item'].wall_mm),supply[index]['item'].length_mm,'free')]+=count
        for purchase_wall,length,count in purchases:lines[(purchase_wall,length,'missing')]+=count
        dimensions=sorted({(wall,length) for wall,length,_ in lines},key=lambda key:(Decimal(key[0]),key[1]))
        from projects.diameters import label
        record['couplings']=[]
        for wall,length in dimensions:
            reserved=lines[(wall,length,'reserved')];free=lines[(wall,length,'free')];missing=lines[(wall,length,'missing')]
            record['couplings'].append({'method':job.method,'diameter':job.section.diameter_mm,'length':length,'wall':Decimal(wall),
                'name':f'{job.get_method_display()} {label(job.section.diameter_mm)} · {length} × {Decimal(wall)} мм',
                'need':reserved+free+missing,'reserved':reserved,'missing':missing,'unit':'шт.'})
