from sqlalchemy import Column,Integer,String,Float,JSON,Text,ForeignKey,UniqueConstraint,PrimaryKeyConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True);password=Column(String(200));role=Column(String(20))
class DailyVolume(Base):
 __tablename__='daily_volumes';__table_args__=(UniqueConstraint('branch','day',name='uq_branch_day'),)
 id=Column(Integer,primary_key=True);branch=Column(String(30),nullable=False);day=Column(String(10),nullable=False);volume=Column(Integer,nullable=False);note=Column(Text,default='')
class ForecastRun(Base):
 __tablename__='forecast_runs'
 id=Column(Integer,primary_key=True);name=Column(String(100));branch=Column(String(30));backtest_days=Column(Integer);alpha=Column(Float);beta=Column(Float);state=Column(String(20),default='queued');result=Column(JSON);error=Column(Text,default='')
class Staffing(Base):
 __tablename__='staffings'
 id=Column(Integer,primary_key=True);run_id=Column(Integer,ForeignKey('forecast_runs.id'));capacity=Column(Integer);model=Column(String(30));schedule=Column(JSON);note=Column(Text);status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
class Publication(Base):
 """对外发布记录。number 是幂等号；seed 仅存服务端，绝不进入响应或导出。"""
 __tablename__='publications'
 id=Column(Integer,primary_key=True);number=Column(String(64),nullable=False,unique=True);name=Column(String(100));branch=Column(String(30),nullable=False);run_id=Column(Integer,ForeignKey('forecast_runs.id'),nullable=False)
 epsilon_micro=Column(Integer,nullable=False);daily_cap_micro=Column(Integer,nullable=False);clip_upper=Column(Integer,nullable=False,default=200);request_hash=Column(String(64),nullable=False)
 state=Column(String(20),default='pending');result=Column(JSON);mechanism=Column(JSON);error=Column(Text,default='')
 attempts=Column(Integer,default=0);seed=Column(String(64),nullable=False);created=Column(String(40));updated=Column(String(40))
class BudgetDay(Base):
 """逐日预算台账：（网点, 训练日）已累计扣账的 ε（micro）。"""
 __tablename__='budget_days';__table_args__=(PrimaryKeyConstraint('branch','day',name='pk_budget_day'),)
 branch=Column(String(30),nullable=False);day=Column(String(10),nullable=False);used_micro=Column(Integer,nullable=False,default=0)
class BudgetEntry(Base):
 """每笔扣账的明细，用于核验与审计；撤回不删除。"""
 __tablename__='budget_entries';__table_args__=(UniqueConstraint('number','branch','day',name='uq_budget_entry'),)
 id=Column(Integer,primary_key=True);number=Column(String(64),nullable=False);branch=Column(String(30),nullable=False);day=Column(String(10),nullable=False);epsilon_micro=Column(Integer,nullable=False);created=Column(String(40))
