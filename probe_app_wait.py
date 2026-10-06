import asyncio,time,struct
from bleak import BleakClient
A='11:89:9A:A3:1C:29';C='0000ffe1-0000-1000-8000-00805f9b34fb'; waits={5:2.0,6:1.0,7:.5,8:.2,9:.1,10:.05}
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
  await req(bytes.fromhex('8c25'),bytes.fromhex('8c25'),.3);await req(bytes.fromhex('8c4100'),bytes.fromhex('8c41'))
  for idx,w in waits.items():
   await req(bytes([0x8c,0x3e,idx]),bytes.fromhex('8c3e'));rb=await req(bytes.fromhex('8c3d'),bytes.fromhex('8c3d'))
   t=time.monotonic();await req(bytes.fromhex('8c0e03'),bytes.fromhex('8c0e'));await asyncio.sleep(w+.15)
   st=await req(bytes.fromhex('8c3b'),bytes.fromhex('8c3b'));p=await req(bytes.fromhex('8c3c'),bytes.fromhex('8c3c'));vals=struct.unpack_from('<4f',p,2) if p and len(p)>=18 else None
   print(idx,'wait',w,'elapsed',round(time.monotonic()-t,3),'rb',rb.hex(' ') if rb else None,'state',st.hex(' ') if st else None,'3C',vals,flush=True)
   await req(bytes.fromhex('8c25'),bytes.fromhex('8c25'),.3);await asyncio.sleep(.15)
  await req(bytes.fromhex('8c4101'),bytes.fromhex('8c41'))
asyncio.run(main())
