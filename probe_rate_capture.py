import asyncio,struct,time,statistics
from bleak import BleakClient
A='11:89:9A:A3:1C:29'; C='0000ffe1-0000-1000-8000-00805f9b34fb'
RATES={6:10000,7:20000,8:50000,9:100000,10:200000}
async def main():
 q=asyncio.Queue()
 async with BleakClient(A,timeout=20) as c:
  await c.start_notify(C,lambda _,x:q.put_nowait(bytes(x)))
  async def send(b): await c.write_gatt_char(C,b,response=False)
  async def one(prefix,t=3):
   end=time.monotonic()+t
   while time.monotonic()<end:
    try:r=await asyncio.wait_for(q.get(),end-time.monotonic())
    except asyncio.TimeoutError:return None
    if r.startswith(prefix):return r
  async def clear():
   while not q.empty():q.get_nowait()
  await clear();await send(bytes.fromhex('8c4100'));await one(bytes.fromhex('8c41'))
  for idx,hz in RATES.items():
   await clear();await send(bytes([0x8c,0x3e,idx]));ack=await one(bytes.fromhex('8c3e'))
   await clear();await send(bytes.fromhex('8c3d'));rb=await one(bytes.fromhex('8c3d'))
   await clear();t0=time.monotonic();await send(bytes.fromhex('8c0e03'));await one(bytes.fromhex('8c0e'))
   ready=None
   for n in range(300):
    await asyncio.sleep(.02);await clear();await send(bytes.fromhex('8c3b'));s=await one(bytes.fromhex('8c3b'),1)
    if s and len(s)>=3 and s[2]==1: ready=time.monotonic()-t0;break
   await clear();await send(bytes.fromhex('8c3c'));p=await one(bytes.fromhex('8c3c'))
   vals=struct.unpack_from('<4f',p,2) if p and len(p)>=18 else None
   await clear();await send(bytes.fromhex('8c3a'));buf=b'';end=time.monotonic()+5
   while len(buf)<802 and time.monotonic()<end:
    try:r=await asyncio.wait_for(q.get(),end-time.monotonic())
    except asyncio.TimeoutError:break
    if not buf and not r.startswith(bytes.fromhex('8c3a')):continue
    buf+=r
   if len(buf)>=802:
    x=struct.unpack_from('<400H',buf,2); stats=(len(x),min(x),max(x),round(statistics.mean(x),2),round(statistics.pstdev(x),2))
   else:stats=('bytes',len(buf))
   print(idx,hz,'readback',rb.hex(' ') if rb else None,'ready_s',round(ready,3) if ready else None,'3C',vals,'3A',stats)
   await clear();await send(bytes.fromhex('8c25'));await asyncio.sleep(.15)
  await clear();await send(bytes.fromhex('8c4101'));print('restore',((await one(bytes.fromhex('8c41'))) or b'').hex(' '))
asyncio.run(main())
