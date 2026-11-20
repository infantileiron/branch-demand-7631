import os,time,json,logging
from pathlib import Path
import redis
from src.config.database import Session
from src.models.entities import ForecastRun,Publication
from src.services.analysis import forecast
from src.constants.privacy import MAX_ATTEMPTS
from src.services.publications import QUEUE as PUBLICATION_QUEUE,process as process_publication
r=redis.Redis.from_url(os.environ['REDIS_URL'])

def recover():
 """启动时把中断的预测与发布放回队列；发布重算逐位一致，不重复扣账。"""
 with Session() as recovery:
  for pending in recovery.query(ForecastRun).filter(ForecastRun.state.in_(['queued','running'])).all():
   pending.state='queued';r.rpush('demand:runs',pending.id)
  recovery.commit()
  # 只恢复预算已扣、仍在待处理的发布；批量取号避免长事务
  ids=[]
  for p in recovery.query(Publication).filter(Publication.state=='pending').order_by(Publication.id).all():
   if (p.attempts or 0)>=MAX_ATTEMPTS:
    p.state='failed';p.error='处理尝试已达上限，预算不退还；请换编号重新提交'
   else:ids.append(p.id)
  recovery.commit()
 for pid in ids:
  r.rpush(PUBLICATION_QUEUE,pid)
 return len(ids)

recovered=recover()
if recovered:logging.info('恢复待处理发布 %s 条',recovered)
while True:
 Path('/tmp/heartbeat').touch()
 try:
  item=r.blpop(['demand:runs',PUBLICATION_QUEUE],timeout=3)
  if not item:continue
  queue,raw=item;ident=int(raw)
  if queue.decode()==PUBLICATION_QUEUE:
   with Session() as s:
    try:process_publication(s,ident)
    except Exception:logging.exception('publication %s',ident)
   continue
  with Session() as s:
   row=s.get(ForecastRun,ident)
   if not row or row.state!='queued':continue
   snapshot=row.result['snapshot'];row.state='running';s.commit()
   try:
    row.result={**forecast(snapshot,row.backtest_days,row.alpha,row.beta),'snapshot':snapshot};row.state='done'
    Path('/artifacts/'+str(row.id)+'.json').write_text(json.dumps(row.result,ensure_ascii=False))
   except Exception as e:row.state='failed';row.error=str(e)
   s.commit()
 except Exception:logging.exception('worker');time.sleep(2)
