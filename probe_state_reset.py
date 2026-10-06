import asyncio,time
from bleak import BleakClient
A='11:89:9A:A3:1C:29';C='0000ffe1-0000-1000-8000-00805f9b34fb'
async def main():
 q=asyncio.Queue()
 async with BleakClient(A,timeout=20) as c:
  await c.start_notify(C,lambda _,x:q.put_nowait(bytes(x)))
  async def req(b,pre,t=2):
   while not q.empty():q.get_nowait()
   await c.write_gatt_char(C,b,response=False);end=time.monotonic()+t
   while time.monotonic()<end:
    try:r=await asyncio.wait_for(q.get(),end-time.monotonic())
    except:return None
    if r.startswith(pre):return r
  await req(bytes.fromhex('8c4100'),bytes.fromhex('8c41'))
  for idx in [3,4,5]:
   await req(bytes.fromhex('8c25'),bytes.fromhex('8c25'),.5);await asyncio.sleep(.5)
   await req(bytes([0x8c,0x3e,idx]),bytes.fromhex('8c3e'))
   rb=await req(bytes.fromhex('8c3d'),bytes.fromhex('8c3d')); pre=await req(bytes.fromhex('8c3b'),bytes.fromhex('8c3b'))
   t=time.monotonic();ack=await req(bytes.fromhex('8c0e03'),bytes.fromhex('8c0e')); imm=await req(bytes.fromhex('8c3b'),bytes.fromhex('8c3b'))
   ready=None
   for _ in range(150):
    if imm and len(imm)>2 and imm[2]==1: ready=time.monotonic()-t;break
    await asyncio.sleep(.1);imm=await req(bytes.fromhex('8c3b'),bytes.fromhex('8c3b'))
    if imm and len(imm)>2 and imm[2]==1:ready=time.monotonic()-t;break
   print(idx,'rb',rb.hex(' ') if rb else None,'pre',pre.hex(' ') if pre else None,'ack',ack.hex(' ') if ack else None,'ready_s',round(ready,2) if ready else None)
  await req(bytes.fromhex('8c25'),bytes.fromhex('8c25'),.5);await req(bytes.fromhex('8c4101'),bytes.fromhex('8c41'))
asyncio.run(main())
