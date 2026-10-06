import asyncio,time
from bleak import BleakClient
A='11:89:9A:A3:1C:29';C='0000ffe1-0000-1000-8000-00805f9b34fb'
async def main():
 q=asyncio.Queue()
 async with BleakClient(A,timeout=20) as c:
  await c.start_notify(C,lambda _,x:q.put_nowait(bytes(x)))
  async def tx(b):await c.write_gatt_char(C,b,response=False)
  async def get(pre,t=2):
   end=time.monotonic()+t
   while time.monotonic()<end:
    try:r=await asyncio.wait_for(q.get(),end-time.monotonic())
    except: return None
    if r.startswith(pre):return r
  async def clr():
   while not q.empty():q.get_nowait()
  await tx(bytes.fromhex('8c4100'));await get(bytes.fromhex('8c41'))
  for idx in [5,4,3]:
   await clr();await tx(bytes([0x8c,0x3e,idx]));await get(bytes.fromhex('8c3e'));await clr();await tx(bytes.fromhex('8c3d'));rb=await get(bytes.fromhex('8c3d'))
   await clr();t=time.monotonic();await tx(bytes.fromhex('8c0e03'));await get(bytes.fromhex('8c0e'))
   ready=False
   while time.monotonic()-t<15:
    await asyncio.sleep(.1);await clr();await tx(bytes.fromhex('8c3b'));s=await get(bytes.fromhex('8c3b'),1)
    if s and len(s)>2 and s[2]==1:ready=True;break
   print('index',idx,'readback',rb.hex(' ') if rb else None,'ready',ready,'latency_s',round(time.monotonic()-t,2))
   await clr();await tx(bytes.fromhex('8c25'));await asyncio.sleep(.2)
  await clr();await tx(bytes.fromhex('8c4101'));await get(bytes.fromhex('8c41'))
asyncio.run(main())
