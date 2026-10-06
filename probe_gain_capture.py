import asyncio,struct,time,statistics
from bleak import BleakClient
A='11:89:9A:A3:1C:29';C='0000ffe1-0000-1000-8000-00805f9b34fb'
async def main():
 q=asyncio.Queue()
 async with BleakClient(A,timeout=20) as c:
  await c.start_notify(C,lambda _,x:q.put_nowait(bytes(x)))
  async def clear():
   while not q.empty():q.get_nowait()
  async def ask(b,p,t=2):
   await clear();await c.write_gatt_char(C,b,response=False);end=time.monotonic()+t
   while time.monotonic()<end:
    try:r=await asyncio.wait_for(q.get(),end-time.monotonic())
    except: return None
    if r.startswith(p):return r
  await ask(bytes.fromhex('8c3700'),bytes.fromhex('8c37'));await ask(bytes.fromhex('8c4100'),bytes.fromhex('8c41'));await ask(bytes([0x8c,0x3e,7]),bytes.fromhex('8c3e'))
  for g in range(4):
   a=await ask(bytes([0x8c,0x35,g]),bytes.fromhex('8c35'));rb=await ask(bytes.fromhex('8c36'),bytes.fromhex('8c36'))
   await ask(bytes.fromhex('8c0e03'),bytes.fromhex('8c0e'))
   for _ in range(100):
    await asyncio.sleep(.03);s=await ask(bytes.fromhex('8c3b'),bytes.fromhex('8c3b'),1)
    if s and len(s)>2 and s[2]==1:break
   p=await ask(bytes.fromhex('8c3c'),bytes.fromhex('8c3c'));vals=struct.unpack_from('<4f',p,2) if p and len(p)>=18 else None
   await clear();await c.write_gatt_char(C,bytes.fromhex('8c3a'),response=False);buf=b'';end=time.monotonic()+5
   while len(buf)<802 and time.monotonic()<end:
    try:r=await asyncio.wait_for(q.get(),end-time.monotonic())
    except:break
    if not buf and not r.startswith(bytes.fromhex('8c3a')):continue
    buf+=r
   x=struct.unpack_from('<400H',buf,2) if len(buf)>=802 else []
   print('gain',g,'ack',(a or b'').hex(' '),'readback',(rb or b'').hex(' '),'3C',vals,'3A', (len(x),min(x),max(x),round(statistics.mean(x),2),round(statistics.pstdev(x),2)) if x else len(buf))
   await ask(bytes.fromhex('8c25'),bytes.fromhex('8c25'),.2)
  await ask(bytes.fromhex('8c3701'),bytes.fromhex('8c37'));await ask(bytes.fromhex('8c4101'),bytes.fromhex('8c41'))
asyncio.run(main())
