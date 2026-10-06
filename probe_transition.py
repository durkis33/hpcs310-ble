import asyncio,time
from bleak import BleakClient
A='11:89:9A:A3:1C:29';C='0000ffe1-0000-1000-8000-00805f9b34fb'
async def main():
 q=asyncio.Queue()
 async with BleakClient(A,timeout=20) as c:
  await c.start_notify(C,lambda _,x:q.put_nowait(bytes(x)))
  async def req(b,pre,t=1):
   while not q.empty():q.get_nowait()
   await c.write_gatt_char(C,b,response=False);end=time.monotonic()+t
   while time.monotonic()<end:
    try:r=await asyncio.wait_for(q.get(),end-time.monotonic())
    except:return None
    if r.startswith(pre):return r
  await req(bytes.fromhex('8c4100'),bytes.fromhex('8c41'))
  for idx in [5,6,7,8,9,10]:
   await req(bytes([0x8c,0x3e,idx]),bytes.fromhex('8c3e'));rb=await req(bytes.fromhex('8c3d'),bytes.fromhex('8c3d'))
   t=time.monotonic();await req(bytes.fromhex('8c0e03'),bytes.fromhex('8c0e'));seen0=False;done=None;states=[]
   for _ in range(250):
    s=await req(bytes.fromhex('8c3b'),bytes.fromhex('8c3b'));v=s[2] if s and len(s)>2 else None
    if not states or states[-1]!=v:states.append(v)
    if v==0:seen0=True
    if seen0 and v==1:done=time.monotonic()-t;break
    await asyncio.sleep(.02)
   print(idx,'rb',rb[2] if rb else None,'states',states,'transition_s',round(done,3) if done else None)
  await req(bytes.fromhex('8c25'),bytes.fromhex('8c25'),.3);await req(bytes.fromhex('8c4101'),bytes.fromhex('8c41'))
asyncio.run(main())
