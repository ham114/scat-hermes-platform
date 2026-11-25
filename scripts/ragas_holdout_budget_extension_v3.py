"""Preserve approved caps and stop admission on the first provider transport error."""
import json,sqlite3,time
from ragas_holdout_budget_extension_v2 import ExtendedBudget as AuthorizedBudget

class ExtendedBudget(AuthorizedBudget):
 def pause_on_provider_error(self,error):
  kind=type(error).__name__;status=getattr(error,'status_code',None)
  if kind not in ['APIConnectionError','APITimeoutError','APIStatusError','AuthenticationError','PermissionDeniedError','RateLimitError','InternalServerError']and not isinstance(status,int):return False
  detail={'at':time.time(),'error_type':kind,'status_code':status,'request_bodies_logged':False,'counters_reset':False}
  with sqlite3.connect(self.path,timeout=20)as db:
   db.execute('begin immediate')
   db.execute("insert or ignore into meta(key,value)values('exhausted','provider_transport_pause')")
   db.execute("insert or replace into meta(key,value)values('last_provider_pause',?)",(json.dumps(detail),))
  return True
