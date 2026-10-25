"""Password verifier only; no plaintext password persistence or exchange credentials."""
import base64,hashlib,hmac,time
from threading import Lock
_attempts={};_lock=Lock()

def check_admin(header,verifier,client,at=None):
 at=time.monotonic() if at is None else at
 with _lock:
  recent=[t for t in _attempts.get(client,[]) if at-t<60]
  _attempts[client]=recent
  if len(recent)>=5:return False
 ok=False
 try:
  scheme,encoded=header.split(' ',1)
  if scheme!='Basic':raise ValueError()
  user,password=base64.b64decode(encoded,validate=True).decode('utf-8').split(':',1)
  algorithm,iterations,salt,digest=verifier.split('$')
  if algorithm!='pbkdf2_sha256' or not 100000<=int(iterations)<=1000000:raise ValueError()
  derived=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(iterations)).hex()
  ok=hmac.compare_digest(user,'admin') and hmac.compare_digest(derived,digest)
 except (ValueError,TypeError,UnicodeError):pass
 with _lock:
  if ok:_attempts.pop(client,None)
  else:_attempts[client].append(at)
 return ok
